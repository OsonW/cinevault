import os
import json
import re
import sqlite3
import time
from datetime import datetime, timezone
import pytest

import db
import app as app_module
from app import app as flask_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_DIR", str(tmp_path))
    flask_app.config["TESTING"] = True
    flask_app.config["SECRET_KEY"] = "test-secret-key"
    app_module._app_initialized = False
    app_module._initialized_users.clear()
    app_module._user_media_cache.clear()
    app_module._mdblist_status_cache.clear()
    app_module._ratings_cache.clear()
    app_module._tmdb_meta_cache.clear()
    from auth import _RATE_BUCKETS
    _RATE_BUCKETS.clear()
    with flask_app.test_client() as c:
        yield c


def _register(client, username="alice", password="secret123"):
    return client.post("/auth/register", json={"username": username, "password": password})


def _ensure_tmdb_key(username="alice"):
    """Give an account a TMDB key so storage-writing endpoints are unlocked.
    Keyless accounts are blocked from writing (no DB file is created), by design,
    so any test that adds media must provision a key first."""
    from users_db import get_user_by_username, get_user_keys, set_user_keys
    uid = get_user_by_username(username)["id"]
    keys = get_user_keys(uid)
    if not keys["tmdb_key"]:
        set_user_keys(uid, "tmdbkey", keys["mdblist_key"])


def _add_movie(client, title="Inception", external_id="27205"):
    _ensure_tmdb_key()
    return client.post("/api/add", json={
        "title": title, "media_type": "movie",
        "external_id": external_id, "tmdb_id": int(external_id), "status": "watchlist",
    })


def test_media_row_has_ratings_columns(client):
    _register(client)
    _add_movie(client)
    item = client.get("/api/list").get_json()[0]
    assert "ratings" in item
    assert "ratings_updated_at" in item
    assert item["ratings"] is None


def test_set_media_ratings_roundtrip(client):
    _register(client)
    _add_movie(client)
    item = client.get("/api/list").get_json()[0]
    from flask import g
    with flask_app.test_request_context():
        from db import get_user_db_path
        from users_db import get_user_by_username
        uid = get_user_by_username("alice")["id"]
        g.user_db_path = get_user_db_path(uid)
        db.set_media_ratings(item["id"], json.dumps({"imdb": 8.1}), "2026-06-20T00:00:00")
        row = db.get_media_by_id(item["id"])
    assert json.loads(row["ratings"]) == {"imdb": 8.1}
    assert row["ratings_updated_at"] == "2026-06-20T00:00:00"


def test_migration_adds_ratings_to_legacy_db(tmp_path):
    """A pre-existing DB with the date columns but no ratings columns gains them
    on migration without disturbing existing rows."""
    db_path = str(tmp_path / "movie_tracker_legacy.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute("""CREATE TABLE media (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
            media_type TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'watchlist',
            external_id TEXT, date_added TEXT, date_watchlist TEXT,
            date_watching TEXT, date_finished TEXT,
            UNIQUE(external_id, media_type))""")
        conn.execute("INSERT INTO media (title, media_type, status, date_watchlist) "
                     "VALUES ('Dune', 'movie', 'watchlist', '2025-01-01')")
    db._create_tables(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(media)").fetchall()}
        assert "ratings" in cols
        assert "ratings_updated_at" in cols
        row = conn.execute("SELECT * FROM media").fetchone()
        assert row["date_watchlist"] == "2025-01-01"   # existing data untouched
        assert row["ratings"] is None


def test_fetch_mdblist_ratings_normalizes(monkeypatch):
    import app as a

    class FakeResp:
        status_code = 200
        def json(self):
            return {"ratings": [
                {"source": "imdb", "value": 8.1},
                {"source": "Tomatoes", "value": 94},       # capitalized -> tomatoes
                {"source": "popcorn", "value": 88},         # alias -> audience
                {"source": "metacritic", "value": 76},
                {"source": "letterboxd", "value": 4.2},
                {"source": "myanimelist", "value": 8.5},    # alias -> mal
                {"source": "metacriticuser", "value": 70},  # not surfaced
                {"source": "trakt", "value": 90},           # not surfaced
            ]}

    monkeypatch.setattr(a.requests, "get", lambda *args, **kw: FakeResp())
    out = a._fetch_mdblist_ratings("movie", 27205, "fake-key")
    assert out == {"imdb": 8.1, "tomatoes": 94, "audience": 88,
                   "metacritic": 76, "letterboxd": 4.2, "mal": 8.5}


def test_fetch_mdblist_ratings_attaches_imdb_id(monkeypatch):
    """When scores are present, the IMDb tconst rides along under `imdb_id` so the
    IMDb pill can deep-link. Tolerant of both flat `imdbid` and nested `ids.imdb`."""
    import app as a

    class FlatResp:
        status_code = 200
        def json(self):
            return {"imdbid": "tt1375666", "ratings": [{"source": "imdb", "value": 8.8}]}

    monkeypatch.setattr(a.requests, "get", lambda *args, **kw: FlatResp())
    assert a._fetch_mdblist_ratings("movie", 27205, "k") == {"imdb": 8.8, "imdb_id": "tt1375666"}

    class NestedResp:
        status_code = 200
        def json(self):
            return {"ids": {"imdb": "tt0111161"}, "ratings": [{"source": "imdb", "value": 9.3}]}

    monkeypatch.setattr(a.requests, "get", lambda *args, **kw: NestedResp())
    assert a._fetch_mdblist_ratings("movie", 278, "k") == {"imdb": 9.3, "imdb_id": "tt0111161"}


def test_fetch_mdblist_ratings_resolves_urls_and_mal(monkeypatch):
    """MDBList returns per-rating `url` as RELATIVE paths; they get resolved to
    absolute links (with the right domain, and the {movie|tv} segment re-inserted for
    Metacritic) so pills open the exact title page instead of a search. The MAL id
    rides along too. Mirrors a real /tmdb/movie response."""
    import app as a

    def resp(payload):
        class R:
            status_code = 200
            def json(self_):
                return payload
        return lambda *args, **kw: R()

    movie = {
        "ids": {"imdb": "tt1375666", "mal": 12345},
        "ratings": [
            {"source": "imdb", "value": 8.8, "url": 93},                 # number -> ignored
            {"source": "tomatoes", "value": 87, "url": "/m/inception"},
            {"source": "popcorn", "value": 91, "url": "/m/inception"},   # alias -> audience
            {"source": "metacritic", "value": 74, "url": "/inception"},  # no type segment
            {"source": "letterboxd", "value": 4.2, "url": "/film/inception/"},
            {"source": "rogerebert", "value": 4.0, "url": "inception-2010"},  # no leading slash
            {"source": "trakt", "value": 86, "url": None},               # not surfaced
        ],
    }
    monkeypatch.setattr(a.requests, "get", resp(movie))
    out = a._fetch_mdblist_ratings("movie", 27205, "k")
    assert out["imdb_id"] == "tt1375666"
    assert out["mal_id"] == "12345"
    assert out["src_urls"] == {
        "tomatoes":   "https://www.rottentomatoes.com/m/inception",
        "audience":   "https://www.rottentomatoes.com/m/inception",
        "metacritic": "https://www.metacritic.com/movie/inception",
        "letterboxd": "https://letterboxd.com/film/inception/",
    }
    assert "imdb" not in out["src_urls"]    # numeric url ignored (we use the tconst)
    assert "trakt" not in out["src_urls"]

    # Shows: RT path already carries /tv; Metacritic's does not, so /tv is inserted.
    show = {
        "ids": {},
        "ratings": [
            {"source": "metacritic", "value": 96, "url": "/breaking-bad"},
            {"source": "tomatoes", "value": 96, "url": "/tv/breaking_bad"},
        ],
    }
    monkeypatch.setattr(a.requests, "get", resp(show))
    out = a._fetch_mdblist_ratings("tv", 1396, "k")
    assert out["src_urls"]["metacritic"] == "https://www.metacritic.com/tv/breaking-bad"
    assert out["src_urls"]["tomatoes"]   == "https://www.rottentomatoes.com/tv/breaking_bad"


def test_fetch_mdblist_ratings_no_imdb_id_when_no_scores(monkeypatch):
    """An imdb id without any scores must NOT make the result non-empty — downstream
    caching/overwrite guards treat a non-empty dict as 'has ratings'."""
    import app as a

    class FakeResp:
        status_code = 200
        def json(self):
            return {"imdbid": "tt1375666", "ratings": []}

    monkeypatch.setattr(a.requests, "get", lambda *args, **kw: FakeResp())
    assert a._fetch_mdblist_ratings("movie", 27205, "k") == {}


def test_fetch_mdblist_ratings_returns_none_on_http_error(monkeypatch):
    """A non-200 upstream response is a FAILURE, signalled as None — distinct from a
    successful call that simply surfaced no ratings ({})."""
    import app as a

    class FakeResp:
        status_code = 429
        def json(self):
            return {}

    monkeypatch.setattr(a.requests, "get", lambda *args, **kw: FakeResp())
    assert a._fetch_mdblist_ratings("movie", 27205, "fake-key") is None


def test_fetch_mdblist_ratings_returns_none_on_exception(monkeypatch):
    import app as a

    def boom(*args, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(a.requests, "get", boom)
    assert a._fetch_mdblist_ratings("movie", 27205, "fake-key") is None


def test_fetch_mdblist_ratings_unsupported_type(monkeypatch):
    import app as a
    called = {"n": 0}
    def _spy(*a_, **k_):
        called["n"] += 1
    monkeypatch.setattr(a.requests, "get", _spy)
    assert a._fetch_mdblist_ratings("book", 5, "fake-key") == {}
    assert called["n"] == 0


def _set_mdblist_key(username="alice", key="fake-key"):
    from users_db import get_user_by_username, get_user_keys, set_user_keys
    uid = get_user_by_username(username)["id"]
    set_user_keys(uid, get_user_keys(uid)["tmdb_key"] or "tmdbkey", key)


def test_ratings_endpoint_no_key_returns_empty(client):
    _register(client)
    resp = client.get("/api/ratings/movie/27205")
    assert resp.status_code == 200
    assert resp.get_json() == {"ratings": {}}


def test_ratings_endpoint_caches_non_library(client, monkeypatch):
    _register(client)
    _set_mdblist_key()
    calls = {"n": 0}
    def fake(media_type, tmdb_id, key):
        calls["n"] += 1
        return {"imdb": 8.1}
    monkeypatch.setattr(app_module, "_fetch_mdblist_ratings", fake)
    first  = client.get("/api/ratings/movie/603").get_json()
    second = client.get("/api/ratings/movie/603").get_json()
    assert first == second == {"ratings": {"imdb": 8.1}}
    assert calls["n"] == 1  # second served from TTL cache


def test_ratings_endpoint_persists_library_item(client, monkeypatch):
    _register(client)
    _set_mdblist_key()
    _add_movie(client, external_id="27205")
    monkeypatch.setattr(app_module, "_fetch_mdblist_ratings",
                        lambda *a, **k: {"imdb": 7.5})
    client.get("/api/ratings/movie/27205")
    item = client.get("/api/list").get_json()[0]
    import json as _j
    assert _j.loads(item["ratings"]) == {"imdb": 7.5}
    assert item["ratings_updated_at"]


def test_mdblist_status_no_key(client):
    _register(client)
    resp = client.get("/api/mdblist-status")
    assert resp.status_code == 200
    assert resp.get_json() == {"has_key": False, "limit": None,
                               "used": None, "remaining": None}


def test_mdblist_status_reports_remaining(client, monkeypatch):
    _register(client)
    _set_mdblist_key()

    class FakeResp:
        status_code = 200
        def json(self):
            return {"api_requests": 1000, "api_requests_count": 753}

    monkeypatch.setattr(app_module.requests, "get", lambda *a, **k: FakeResp())
    data = client.get("/api/mdblist-status").get_json()
    assert data["has_key"] is True
    assert data["limit"] == 1000
    assert data["used"] == 753
    assert data["remaining"] == 247


def test_media_row_has_tmdb_rating_column(client):
    _register(client)
    _add_movie(client)
    item = client.get("/api/list").get_json()[0]
    assert "tmdb_rating" in item
    assert item["tmdb_rating"] is None


def test_add_movie_persists_tmdb_rating(client):
    _register(client)
    _ensure_tmdb_key()
    client.post("/api/add", json={
        "title": "Inception", "media_type": "movie",
        "external_id": "27205", "tmdb_id": 27205, "status": "watchlist",
        "tmdb_rating": 8.4,
    })
    item = client.get("/api/list").get_json()[0]
    assert item["tmdb_rating"] == 8.4


def test_set_media_tmdb_rating_roundtrip(client):
    _register(client)
    _add_movie(client)
    item = client.get("/api/list").get_json()[0]
    from flask import g
    with flask_app.test_request_context():
        from db import get_user_db_path
        from users_db import get_user_by_username
        uid = get_user_by_username("alice")["id"]
        g.user_db_path = get_user_db_path(uid)
        db.set_media_tmdb_rating(item["id"], 7.2)
        row = db.get_media_by_id(item["id"])
    assert row["tmdb_rating"] == 7.2


def test_migration_adds_tmdb_rating_to_legacy_db(tmp_path):
    db_path = str(tmp_path / "movie_tracker_legacy2.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute("""CREATE TABLE media (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
            media_type TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'watchlist',
            external_id TEXT, ratings TEXT, ratings_updated_at TEXT,
            UNIQUE(external_id, media_type))""")
    db._create_tables(db_path)
    with sqlite3.connect(db_path) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(media)").fetchall()}
    assert "tmdb_rating" in cols


def test_tmdb_rating_endpoint_persists_library_item(client, monkeypatch):
    _register(client)
    _set_mdblist_key()
    _add_movie(client, external_id="27205")
    monkeypatch.setattr(app_module, "_fetch_tmdb_rating", lambda *a, **k: 8.4)
    resp = client.get("/api/tmdb-rating/movie/27205").get_json()
    assert resp == {"tmdb": 8.4}
    item = client.get("/api/list").get_json()[0]
    assert item["tmdb_rating"] == 8.4


def test_tmdb_rating_endpoint_uses_stored_value(client, monkeypatch):
    _register(client)
    _set_mdblist_key()
    client.post("/api/add", json={
        "title": "Inception", "media_type": "movie", "external_id": "27205",
        "tmdb_id": 27205, "status": "watchlist", "tmdb_rating": 7.0})
    calls = {"n": 0}
    def spy(*a, **k):
        calls["n"] += 1
        return 9.9
    monkeypatch.setattr(app_module, "_fetch_tmdb_rating", spy)
    resp = client.get("/api/tmdb-rating/movie/27205").get_json()
    assert resp == {"tmdb": 7.0}
    assert calls["n"] == 0  # stored value served, no fetch


def test_tmdb_rating_endpoint_no_tmdb_key(client):
    _register(client)  # no keys set -> _get_tmdb_key() is None
    resp = client.get("/api/tmdb-rating/movie/27205")
    assert resp.status_code == 200
    assert resp.get_json() == {"tmdb": None}


def test_search_includes_tmdb_rating(client, monkeypatch):
    _register(client)
    _set_mdblist_key()  # ensures a tmdb key exists for the search route

    class FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"results": [
                {"id": 603, "title": "The Matrix", "release_date": "1999-03-30",
                 "poster_path": "/x.jpg", "overview": "o", "popularity": 9,
                 "vote_average": 8.2},
            ]}

    monkeypatch.setattr(app_module.requests, "get", lambda *a, **k: FakeResp())
    monkeypatch.setattr(app_module, "_fetch_tmdb_meta", lambda *a, **k: {"author": "", "length": ""})
    data = client.get("/api/search?q=matrix&type=movie").get_json()
    assert data[0]["tmdb_rating"] == 8.2


def _store_stale_ratings(client, external_id="27205"):
    """Add a movie and stamp it with old MDBList ratings (>7 days)."""
    _add_movie(client, external_id=external_id)
    item = client.get("/api/list").get_json()[0]
    from flask import g
    with flask_app.test_request_context():
        from db import get_user_db_path
        from users_db import get_user_by_username
        uid = get_user_by_username("alice")["id"]
        g.user_db_path = get_user_db_path(uid)
        db.set_media_ratings(item["id"], json.dumps({"imdb": 5.0}),
                             "2020-01-01T00:00:00+00:00")
    return item


def test_take_lazy_refresh_slot_caps(client):
    """The persisted per-user slot counter caps at the limit and is atomic."""
    _register(client)
    _add_movie(client)   # triggers init_user_db so app_meta exists
    from flask import g
    from db import get_user_db_path, take_lazy_refresh_slot
    from users_db import get_user_by_username
    with flask_app.test_request_context():
        uid = get_user_by_username("alice")["id"]
        g.user_db_path = get_user_db_path(uid)
        assert take_lazy_refresh_slot(2) is True
        assert take_lazy_refresh_slot(2) is True
        assert take_lazy_refresh_slot(2) is False      # cap hit


def test_lazy_cap_persists_across_connections(client):
    """The count lives in the DB, so a fresh connection still sees it (survives
    restarts / is shared across workers)."""
    _register(client)
    _add_movie(client)
    from flask import g
    from db import get_user_db_path, take_lazy_refresh_slot, get_lazy_refresh_count
    from users_db import get_user_by_username
    uid = get_user_by_username("alice")["id"]
    with flask_app.test_request_context():
        g.user_db_path = get_user_db_path(uid)
        take_lazy_refresh_slot(500)
        take_lazy_refresh_slot(500)
    with flask_app.test_request_context():       # new request/connection
        g.user_db_path = get_user_db_path(uid)
        assert get_lazy_refresh_count() == 2


def test_mdblist_status_resets_lazy_cap_on_quota_drop(client, monkeypatch):
    """When the MDBList used-count drops (quota refreshed), the persisted auto-refresh
    cap counter resets in lockstep."""
    _register(client)
    _set_mdblist_key()
    _add_movie(client)   # triggers init_user_db so app_meta exists
    from flask import g
    from db import get_user_db_path, take_lazy_refresh_slot, get_lazy_refresh_count
    from users_db import get_user_by_username
    uid = get_user_by_username("alice")["id"]

    used_box = {"v": 900}

    class FakeResp:
        status_code = 200
        def json(self):
            return {"api_requests": 1000, "api_requests_count": used_box["v"]}

    monkeypatch.setattr(app_module.requests, "get", lambda *a, **k: FakeResp())

    # Spend two slots.
    with flask_app.test_request_context():
        g.user_db_path = get_user_db_path(uid)
        take_lazy_refresh_slot(500)
        take_lazy_refresh_slot(500)
        assert get_lazy_refresh_count() == 2

    client.get("/api/mdblist-status")            # records used=900
    used_box["v"] = 5                            # quota resets
    app_module._mdblist_status_cache.clear()     # bust the 2-min status cache
    client.get("/api/mdblist-status")            # detects the drop -> resets count

    with flask_app.test_request_context():
        g.user_db_path = get_user_db_path(uid)
        assert get_lazy_refresh_count() == 0


def test_ratings_force_refreshes_and_returns_updated_at(client, monkeypatch):
    _register(client)
    _set_mdblist_key()
    _store_stale_ratings(client)
    monkeypatch.setattr(app_module, "_fetch_mdblist_ratings",
                        lambda *a, **k: {"imdb": 9.0})
    resp = client.get("/api/ratings/movie/27205?force=1").get_json()
    assert resp["ratings"] == {"imdb": 9.0}
    assert resp["updated_at"]   # present and truthy


def test_ratings_lazy_cap_serves_stale_without_fetch(client, monkeypatch):
    _register(client)
    _set_mdblist_key()
    _store_stale_ratings(client)
    calls = {"n": 0}
    def spy(*a, **k):
        calls["n"] += 1
        return {"imdb": 9.0}
    monkeypatch.setattr(app_module, "_fetch_mdblist_ratings", spy)
    monkeypatch.setattr(app_module, "_take_lazy_refresh_slot", lambda uid: False)
    resp = client.get("/api/ratings/movie/27205").get_json()
    assert resp["ratings"] == {"imdb": 5.0}   # old stored value
    assert calls["n"] == 0                     # cap blocked the fetch


def test_force_refresh_empty_does_not_wipe_stored(client, monkeypatch):
    """A force refresh that returns no ratings (MDBList occasionally 200s with an empty
    set under load) must NOT wipe good stored ratings, and must NOT restamp the row (so
    the user isn't locked out of retrying for 60s)."""
    _register(client)
    _set_mdblist_key()
    _store_stale_ratings(client)   # stored {"imdb": 5.0}, stamp in 2020
    monkeypatch.setattr(app_module, "_fetch_mdblist_ratings", lambda *a, **k: {})
    resp = client.get("/api/ratings/movie/27205?force=1").get_json()
    assert resp["ratings"] == {"imdb": 5.0}            # kept, not wiped
    assert resp["updated_at"].startswith("2020")       # not restamped -> retry allowed
    item = client.get("/api/list").get_json()[0]
    assert json.loads(item["ratings"]) == {"imdb": 5.0}


def test_force_refresh_with_scores_still_persists(client, monkeypatch):
    """Sanity: a force refresh that DOES return scores still overwrites + restamps."""
    _register(client)
    _set_mdblist_key()
    _store_stale_ratings(client)
    monkeypatch.setattr(app_module, "_fetch_mdblist_ratings", lambda *a, **k: {"imdb": 9.0})
    resp = client.get("/api/ratings/movie/27205?force=1").get_json()
    assert resp["ratings"] == {"imdb": 9.0}
    assert not resp["updated_at"].startswith("2020")   # restamped fresh


def test_ratings_norefresh_serves_stored_without_calling_mdblist(client, monkeypatch):
    """norefresh=1 (sent by the client when the MDBList quota is exhausted) must serve the
    stored/persisted ratings for FREE — never attempting a live MDBList call — so cached
    pills still render when quota is gone."""
    _register(client)
    _set_mdblist_key()
    _store_stale_ratings(client)   # stored {"imdb": 5.0}, stale (>7 days)
    calls = {"n": 0}

    def spy(*a, **k):
        calls["n"] += 1
        return {"imdb": 9.0}

    monkeypatch.setattr(app_module, "_fetch_mdblist_ratings", spy)
    resp = client.get("/api/ratings/movie/27205?norefresh=1").get_json()
    assert resp["ratings"] == {"imdb": 5.0}   # stored served, even though stale
    assert calls["n"] == 0                     # no MDBList call attempted


def test_ratings_norefresh_non_library_no_call(client, monkeypatch):
    """norefresh on an unknown (non-library) title returns empty without a live call."""
    _register(client)
    _set_mdblist_key()
    calls = {"n": 0}

    def spy(*a, **k):
        calls["n"] += 1
        return {"imdb": 9.0}

    monkeypatch.setattr(app_module, "_fetch_mdblist_ratings", spy)
    resp = client.get("/api/ratings/movie/603?norefresh=1").get_json()
    assert resp == {"ratings": {}}
    assert calls["n"] == 0


def test_ratings_cap_denied_never_fetched_returns_null_updated_at(client, monkeypatch):
    """Never-fetched library row + cap denied: serve empty ratings with a null
    updated_at. The client sweep relies on this null to detect the cap and stop."""
    _register(client)
    _set_mdblist_key()
    _add_movie(client, external_id="27205")   # added, but ratings never fetched
    calls = {"n": 0}
    def spy(*a, **k):
        calls["n"] += 1
        return {"imdb": 9.0}
    monkeypatch.setattr(app_module, "_fetch_mdblist_ratings", spy)
    monkeypatch.setattr(app_module, "_take_lazy_refresh_slot", lambda uid: False)
    resp = client.get("/api/ratings/movie/27205").get_json()
    assert resp["ratings"] == {}
    assert resp["updated_at"] is None
    assert calls["n"] == 0


def test_failed_upstream_does_not_poison_library_row(client, monkeypatch):
    """A failed MDBList call must NOT overwrite a stored row with an empty, freshly
    stamped result (which would blank its pills for 7 days). Stored data + stamp are
    preserved so the next access retries."""
    _register(client)
    _set_mdblist_key()
    _store_stale_ratings(client)   # stored {"imdb": 5.0}, stamp in 2020 (stale)

    class FakeResp:
        status_code = 429
        def json(self):
            return {}

    monkeypatch.setattr(app_module.requests, "get", lambda *a, **k: FakeResp())
    resp = client.get("/api/ratings/movie/27205").get_json()
    assert resp["ratings"] == {"imdb": 5.0}            # stored served, not blanked
    item = client.get("/api/list").get_json()[0]
    assert json.loads(item["ratings"]) == {"imdb": 5.0}
    assert item["ratings_updated_at"].startswith("2020")  # old stamp untouched -> retryable


def test_failed_upstream_never_fetched_stays_retryable(client, monkeypatch):
    """A never-fetched row whose first MDBList call fails stays unstamped (null
    updated_at) so the client treats it as retryable rather than 'authoritative empty'."""
    _register(client)
    _set_mdblist_key()
    _add_movie(client, external_id="27205")   # added, ratings never fetched

    class FakeResp:
        status_code = 500
        def json(self):
            return {}

    monkeypatch.setattr(app_module.requests, "get", lambda *a, **k: FakeResp())
    resp = client.get("/api/ratings/movie/27205").get_json()
    assert resp["ratings"] == {}
    assert resp["updated_at"] is None
    item = client.get("/api/list").get_json()[0]
    assert item["ratings"] is None            # not poisoned with an empty "fresh" value


def test_search_does_not_block_on_directors(client, monkeypatch):
    _register(client)
    _set_mdblist_key()  # also seeds a tmdb key

    class FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"results": [
                {"id": 603, "title": "The Matrix", "release_date": "1999-03-30",
                 "poster_path": "/x.jpg", "overview": "o", "popularity": 9, "vote_average": 8.2},
            ]}

    monkeypatch.setattr(app_module.requests, "get", lambda *a, **k: FakeResp())
    called = {"n": 0}
    def spy(*a, **k):
        called["n"] += 1
        return {"author": "Some Director", "length": "2h 16m"}
    monkeypatch.setattr(app_module, "_fetch_tmdb_meta", spy)

    data = client.get("/api/search?q=matrix&type=movie").get_json()
    assert data[0]["title"] == "The Matrix"
    assert called["n"] == 0                      # search must NOT fetch directors
    assert not data[0].get("author")             # author absent/empty on search


def test_tmdb_meta_endpoint(client, monkeypatch):
    _register(client)
    _set_mdblist_key()
    calls = {"n": 0}
    def spy(media_type, tmdb_id, key):
        calls["n"] += 1
        return {"author": "Lana Wachowski", "length": "2h 16m"}
    monkeypatch.setattr(app_module, "_fetch_tmdb_meta", spy)
    first  = client.get("/api/tmdb-meta/movie/603").get_json()
    second = client.get("/api/tmdb-meta/movie/603").get_json()
    assert first == second == {"author": "Lana Wachowski", "length": "2h 16m"}
    assert calls["n"] == 1                        # second served from cache


def test_tmdb_meta_and_fetch_director_share_one_upstream_call(client, monkeypatch):
    """Opening a detail panel fires both routes for the same title — the lazy length
    and the stored-author backfill. They must resolve to the same cache key so TMDB
    is hit once, not twice."""
    _register(client)
    _add_movie(client, title="The Matrix", external_id="603")
    item_id = client.get("/api/list").get_json()[0]["id"]
    calls = {"n": 0}

    def spy(media_type, tmdb_id, key):
        calls["n"] += 1
        return {"author": "Lana Wachowski", "length": "2h 16m"}

    monkeypatch.setattr(app_module, "_fetch_tmdb_meta", spy)
    meta = client.get("/api/tmdb-meta/movie/603").get_json()
    director = client.get(f"/api/item/{item_id}/fetch_director").get_json()
    assert meta["length"] == "2h 16m"
    assert director["author"] == "Lana Wachowski"
    assert calls["n"] == 1
    # Same key from both callers — the parsed int, never the raw path string.
    assert list(app_module._tmdb_meta_cache) == ["movie:603"]


def test_tmdb_meta_no_tmdb_key(client):
    _register(client)
    resp = client.get("/api/tmdb-meta/movie/603")
    assert resp.status_code == 200
    assert resp.get_json() == {"author": "", "length": ""}


def test_add_create_returns_id_and_date(client):
    _register(client)
    _ensure_tmdb_key()
    resp = client.post("/api/add", json={
        "title": "Inception", "media_type": "movie",
        "external_id": "27205", "tmdb_id": 27205, "status": "watchlist",
    }).get_json()
    assert isinstance(resp.get("id"), int)
    assert resp.get("date_added")
    item = client.get("/api/list").get_json()[0]
    assert item["id"] == resp["id"]


class _FakeTMDBResp:
    """Minimal stand-in for requests.Response with a canned JSON body."""
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_fetch_tmdb_meta_movie_one_call(monkeypatch):
    """Movie director + runtime must arrive from a SINGLE request.

    Guards the append_to_response consolidation: if this ever splits back into
    /movie/{id} + /movie/{id}/credits, search doubles its TMDB calls per card.
    """
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs.get("params", {})))
        return _FakeTMDBResp({
            "runtime": 103,
            "credits": {"crew": [
                {"name": "Lana Wachowski", "job": "Director"},
                {"name": "Someone Else", "job": "Editor"},
            ]},
        })

    monkeypatch.setattr(app_module.requests, "get", fake_get)
    meta = app_module._fetch_tmdb_meta("movie", 603, "k")
    assert meta == {"author": "Lana Wachowski", "length": "1h 43m"}
    assert len(calls) == 1
    assert calls[0][1].get("append_to_response") == "credits"


def test_fetch_tmdb_meta_movie_caps_at_three_directors(monkeypatch):
    """names[:3] cap and ", ".join together: 4 directors in, first 3 joined out."""
    monkeypatch.setattr(
        app_module.requests, "get",
        lambda *a, **k: _FakeTMDBResp({
            "runtime": 90,
            "credits": {"crew": [
                {"name": "Director One", "job": "Director"},
                {"name": "Director Two", "job": "Director"},
                {"name": "Director Three", "job": "Director"},
                {"name": "Director Four", "job": "Director"},
            ]},
        }),
    )
    meta = app_module._fetch_tmdb_meta("movie", 603, "k")
    assert meta["author"] == "Director One, Director Two, Director Three"


def test_fetch_tmdb_meta_movie_no_director_in_crew(monkeypatch):
    """Crew present but nobody has job == 'Director': author empty, length independent."""
    monkeypatch.setattr(
        app_module.requests, "get",
        lambda *a, **k: _FakeTMDBResp({
            "runtime": 95,
            "credits": {"crew": [
                {"name": "Someone", "job": "Editor"},
                {"name": "Someone Else", "job": "Producer"},
            ]},
        }),
    )
    meta = app_module._fetch_tmdb_meta("movie", 603, "k")
    assert meta == {"author": "", "length": "1h 35m"}


def test_fetch_tmdb_meta_tv_seasons(monkeypatch):
    monkeypatch.setattr(
        app_module.requests, "get",
        lambda *a, **k: _FakeTMDBResp({
            "number_of_seasons": 4,
            "created_by": [{"name": "Vince Gilligan"}],
        }),
    )
    meta = app_module._fetch_tmdb_meta("tv", 1396, "k")
    assert meta == {"author": "Vince Gilligan", "length": "4 Seasons"}


def test_fetch_tmdb_meta_tv_single_season_singular(monkeypatch):
    monkeypatch.setattr(
        app_module.requests, "get",
        lambda *a, **k: _FakeTMDBResp({"number_of_seasons": 1, "created_by": []}),
    )
    meta = app_module._fetch_tmdb_meta("tv", 1, "k")
    assert meta["length"] == "1 Season"
    assert meta["author"] == ""  # no created_by entries


def test_fetch_tmdb_meta_request_failure(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(app_module.requests, "get", boom)
    assert app_module._fetch_tmdb_meta("movie", 603, "k") == {"author": "", "length": ""}


def test_fetch_tmdb_meta_unsupported_media_type_no_request(monkeypatch):
    """Anything other than movie/tv must short-circuit with no HTTP call.

    Guards against a future caller forgetting the movie/tv guard and having a
    book/manga id silently hit /tv/{id}, producing plausible-looking garbage.
    """
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs.get("params", {})))
        return _FakeTMDBResp({})

    monkeypatch.setattr(app_module.requests, "get", fake_get)
    assert app_module._fetch_tmdb_meta("book", 1, "k") == {"author": "", "length": ""}
    assert len(calls) == 0


@pytest.mark.parametrize("minutes,expected", [
    (103, "1h 43m"),
    (44, "44m"),
    (120, "2h"),
    (0, ""),
    (None, ""),
    (-5, ""),
])
def test_fmt_runtime(minutes, expected):
    assert app_module._fmt_runtime(minutes) == expected


def _manga_search_payload(attributes):
    """One MangaDex search hit with the given attributes block."""
    return {"data": [{
        "id": "abc-123",
        "attributes": {"title": {"en": "Berserk"}, **attributes},
        "relationships": [],
    }]}


def _manga_search_payload_multi(attributes_list):
    """Several MangaDex search hits, one per attributes block, distinct ids."""
    return {"data": [
        {
            "id": f"manga-{i}",
            "attributes": {"title": {"en": f"Title {i}"}, **attrs},
            "relationships": [],
        }
        for i, attrs in enumerate(attributes_list)
    ]}


def test_manga_search_maps_last_chapter(client, monkeypatch):
    """Manga has its own route (/api/search/manga) — it does NOT go through
    /api/search, which is TMDB-only and key-gated."""
    _register(client)
    monkeypatch.setattr(
        app_module.requests, "get",
        lambda *a, **k: _FakeTMDBResp(_manga_search_payload({"lastChapter": "91"})),
    )
    app_module.search_cache.clear()
    data = client.get("/api/search/manga?q=berserk").get_json()
    assert data[0]["total_chapters"] == 91.0


def test_manga_search_missing_last_chapter(client, monkeypatch):
    _register(client)
    monkeypatch.setattr(
        app_module.requests, "get",
        lambda *a, **k: _FakeTMDBResp(_manga_search_payload({})),
    )
    app_module.search_cache.clear()
    data = client.get("/api/search/manga?q=berserk").get_json()
    assert data[0]["total_chapters"] is None


def test_manga_search_decimal_last_chapter(client, monkeypatch):
    """Decimal chapters are real MangaDex data (e.g. filler/side chapters)."""
    _register(client)
    monkeypatch.setattr(
        app_module.requests, "get",
        lambda *a, **k: _FakeTMDBResp(_manga_search_payload({"lastChapter": "91.5"})),
    )
    app_module.search_cache.clear()
    data = client.get("/api/search/manga?q=one-piece").get_json()
    assert data[0]["total_chapters"] == 91.5


def test_manga_search_empty_last_chapter(client, monkeypatch):
    _register(client)
    monkeypatch.setattr(
        app_module.requests, "get",
        lambda *a, **k: _FakeTMDBResp(_manga_search_payload({"lastChapter": ""})),
    )
    app_module.search_cache.clear()
    data = client.get("/api/search/manga?q=chainsaw-man").get_json()
    assert data[0]["total_chapters"] is None


def test_manga_search_non_numeric_last_chapter(client, monkeypatch):
    """The non-numeric case that motivated normalization: free-text values like
    "Oneshot" must not survive as a truthy string the frontend would render."""
    _register(client)
    monkeypatch.setattr(
        app_module.requests, "get",
        lambda *a, **k: _FakeTMDBResp(_manga_search_payload({"lastChapter": "Oneshot"})),
    )
    app_module.search_cache.clear()
    data = client.get("/api/search/manga?q=some-oneshot").get_json()
    assert data[0]["total_chapters"] is None


def test_manga_search_one_bad_value_does_not_wipe_batch(client, monkeypatch):
    """A single garbage lastChapter must not take down the whole search response —
    the coercion is per-item, guarded, and must not blow up the outer try/except."""
    _register(client)
    payload = _manga_search_payload_multi([
        {"lastChapter": "Oneshot"},
        {"lastChapter": "91"},
    ])
    monkeypatch.setattr(
        app_module.requests, "get",
        lambda *a, **k: _FakeTMDBResp(payload),
    )
    app_module.search_cache.clear()
    data = client.get("/api/search/manga?q=batch-safety").get_json()
    assert len(data) == 2
    assert data[0]["total_chapters"] is None
    assert data[1]["total_chapters"] == 91.0


@pytest.mark.parametrize("value,expected", [
    ("91", 91.0),
    ("91.5", 91.5),
    ("", None),
    (None, None),
    ("Oneshot", None),
    ("nan", None),
    ("inf", None),
    ("-inf", None),
    ("Infinity", None),
    ("NaN", None),
])
def test_safe_float(value, expected):
    assert app_module._safe_float(value) == expected


def test_manga_search_nan_last_chapter_is_valid_json(client, monkeypatch):
    """float("nan") parses without raising, so a naive _safe_float would let a
    bare NaN token into the JSON body — invalid per RFC 8259 and rejected by a
    browser's JSON.parse, breaking the whole response for one bad manga.

    resp.get_json() uses Python's tolerant parser and would pass even with the
    bug present, so this asserts on the raw bytes instead.
    """
    _register(client)
    monkeypatch.setattr(
        app_module.requests, "get",
        lambda *a, **k: _FakeTMDBResp(_manga_search_payload({"lastChapter": "nan"})),
    )
    app_module.search_cache.clear()
    resp = client.get("/api/search/manga?q=nan-chapter")
    assert b"NaN" not in resp.data
    data = resp.get_json()
    assert data[0]["total_chapters"] is None


# ── /api/manga-info ────────────────────────────────────────────────
# manga_info_cache is module-level and is NOT reset by the client fixture, so
# every test below clears it and uses a distinct manga id.

def _manga_info_payload(attrs):
    return {"data": {"id": "x", "attributes": attrs}}


def test_manga_info_non_numeric_last_chapter_is_200(client, monkeypatch):
    """"Oneshot" is truthy, so a bare float() raised and the handler returned 500 —
    an error log plus a fresh upstream request every time the title was opened."""
    _register(client)
    monkeypatch.setattr(
        app_module.requests, "get",
        lambda *a, **k: _FakeTMDBResp(_manga_info_payload({"lastChapter": "Oneshot"})),
    )
    app_module.manga_info_cache.clear()
    resp = client.get("/api/manga-info/oneshot-id")
    assert resp.status_code == 200
    assert resp.get_json() == {"last_chapter": None}


def test_manga_info_decimal_last_chapter(client, monkeypatch):
    _register(client)
    monkeypatch.setattr(
        app_module.requests, "get",
        lambda *a, **k: _FakeTMDBResp(_manga_info_payload({"lastChapter": "91.5"})),
    )
    app_module.manga_info_cache.clear()
    assert client.get("/api/manga-info/decimal-id").get_json()["last_chapter"] == 91.5


def test_manga_info_missing_last_chapter(client, monkeypatch):
    _register(client)
    monkeypatch.setattr(
        app_module.requests, "get",
        lambda *a, **k: _FakeTMDBResp(_manga_info_payload({})),
    )
    app_module.manga_info_cache.clear()
    assert client.get("/api/manga-info/missing-id").get_json()["last_chapter"] is None


def test_manga_info_non_numeric_result_is_cached(client, monkeypatch):
    """The regression that matters: the old code threw before reaching the cache
    write, so every open of a "Oneshot" title re-hit MangaDex."""
    _register(client)
    calls = {"n": 0}

    def fake_get(*a, **k):
        calls["n"] += 1
        return _FakeTMDBResp(_manga_info_payload({"lastChapter": "Oneshot"}))

    monkeypatch.setattr(app_module.requests, "get", fake_get)
    app_module.manga_info_cache.clear()
    first = client.get("/api/manga-info/cached-oneshot-id")
    second = client.get("/api/manga-info/cached-oneshot-id")
    assert first.status_code == second.status_code == 200
    assert first.get_json() == second.get_json() == {"last_chapter": None}
    assert calls["n"] == 1                        # second served from cache


# â”€â”€ Persisted `length` + the backfill sweep â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# The detail panel reads `length` straight off the row so it paints with the rest of
# the item instead of chasing a request after paint. These cover what keeps that
# column trustworthy: how it's formatted, how it's filled, and â€” crucially â€” the
# NULL ("never asked") vs "" ("asked, nothing published") distinction that stops the
# sweep from re-requesting the same rows forever.

@pytest.mark.parametrize("value,noun,expected", [
    (512, "Page", "512 Pages"),
    (1, "Chapter", "1 Chapter"),
    (91.0, "Chapter", "91 Chapters"),      # MangaDex floats must lose the ".0"
    (91.5, "Chapter", "91.5 Chapters"),
    (0, "Page", ""),
    (None, "Page", ""),
    (-3, "Page", ""),
    ("Oneshot", "Chapter", ""),
    (float("inf"), "Page", ""),
    (float("nan"), "Page", ""),
])
def test_fmt_count(value, noun, expected):
    assert app_module._fmt_count(value, noun) == expected


@pytest.mark.parametrize("value,expected", [
    ("1h 43m", "1h 43m"),
    ("  4 Seasons  ", "4 Seasons"),
    ("", None),
    ("   ", None),
    (None, None),
    (512, None),                             # non-strings are dropped, not rejected
    ("x" * 100, "x" * 40),                   # capped at MAX_LENGTH_LEN
])
def test_clean_length(value, expected):
    assert app_module._clean_length(value) == expected


def test_book_search_requests_the_page_count_field(client, monkeypatch):
    """OpenLibrary's default projection omits number_of_pages_median, so without an
    explicit fields= every book's page count came back None â€” which is exactly how
    book page counts were silently broken."""
    _register(client)
    seen = {}

    def fake_get(url, **kwargs):
        seen.update(kwargs.get("params", {}))
        return _FakeTMDBResp({"docs": [{
            "key": "/works/OL1W", "title": "Dune", "author_name": ["Frank Herbert"],
            "first_publish_year": 1965, "cover_i": 1,
            "number_of_pages_median": 412,
        }]})

    monkeypatch.setattr(app_module.requests, "get", fake_get)
    app_module.search_cache.clear()
    data = client.get("/api/search/books?q=dune").get_json()
    assert "number_of_pages_median" in seen.get("fields", "")
    assert data[0]["total_pages"] == 412


def test_resolve_length_movie_uses_the_cached_meta(monkeypatch):
    monkeypatch.setattr(
        app_module, "_cached_tmdb_meta",
        lambda *a: {"author": "Denis Villeneuve", "length": "2h 16m"},
    )
    item = {"media_type": "movie", "tmdb_id": 329865}
    assert app_module._resolve_length(item, "k") == ("2h 16m", None)


def test_resolve_length_without_tmdb_key_stays_retryable():
    """No key is not an answer â€” leave the column NULL so a later session retries."""
    item = {"media_type": "movie", "tmdb_id": 603}
    assert app_module._resolve_length(item, None) == (None, None)


def test_resolve_length_unsupported_type_settles():
    assert app_module._resolve_length({"media_type": "podcast", "tmdb_id": 1}, "k") == ("", None)


def test_resolve_length_no_external_id_settles():
    assert app_module._resolve_length({"media_type": "movie"}, "k") == ("", None)


def test_resolve_length_manga(monkeypatch):
    monkeypatch.setattr(app_module, "_fetch_manga_info", lambda mid: {"last_chapter": 91.0})
    item = {"media_type": "manga", "external_id": "abc-123"}
    assert app_module._resolve_length(item, "k") == ("91 Chapters", None)


def test_resolve_length_manga_failure_stays_retryable(monkeypatch):
    monkeypatch.setattr(app_module, "_fetch_manga_info", lambda mid: None)
    item = {"media_type": "manga", "external_id": "abc-123"}
    assert app_module._resolve_length(item, "k") == (None, None)


def test_resolve_length_manga_no_chapter_count_settles(monkeypatch):
    """MangaDex leaves lastChapter empty for many ongoing series. That is a real
    answer, so it must be stored as "" â€” otherwise the sweep asks again forever."""
    monkeypatch.setattr(app_module, "_fetch_manga_info", lambda mid: {"last_chapter": None})
    item = {"media_type": "manga", "external_id": "abc-123"}
    assert app_module._resolve_length(item, "k") == ("", None)


def test_resolve_length_book_uses_stored_pages_without_fetching(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not hit OpenLibrary when total_pages is known")

    monkeypatch.setattr(app_module, "_fetch_book_pages", boom)
    item = {"media_type": "book", "external_id": "OL1W", "total_pages": 412}
    assert app_module._resolve_length(item, None) == ("412 Pages", 412)


def test_resolve_length_book_fetches_a_missing_page_count(monkeypatch):
    monkeypatch.setattr(app_module, "_fetch_book_pages", lambda wid: 496)
    item = {"media_type": "book", "external_id": "OL1W", "total_pages": None}
    assert app_module._resolve_length(item, None) == ("496 Pages", 496)


def test_resolve_length_book_fetch_failure_stays_retryable(monkeypatch):
    def boom(wid):
        raise RuntimeError("openlibrary down")

    monkeypatch.setattr(app_module, "_fetch_book_pages", boom)
    item = {"media_type": "book", "external_id": "OL1W"}
    assert app_module._resolve_length(item, None) == (None, None)


def test_add_stores_and_returns_the_length(client):
    """The search card already resolved this, so the row is complete on creation and
    the detail panel never has to chase it."""
    _register(client)
    _ensure_tmdb_key()
    resp = client.post("/api/add", json={
        "title": "Arrival", "media_type": "movie", "external_id": "329865",
        "tmdb_id": 329865, "status": "watchlist", "length": "1h 56m",
    })
    assert resp.get_json()["length"] == "1h 56m"
    assert client.get("/api/list").get_json()[0]["length"] == "1h 56m"


def test_add_without_a_length_leaves_it_null_for_the_sweep(client):
    _register(client)
    _add_movie(client)
    assert client.get("/api/list").get_json()[0]["length"] is None


def test_backfill_lengths_persists_and_completes(client, monkeypatch):
    _register(client)
    _add_movie(client)
    monkeypatch.setattr(app_module, "_resolve_length", lambda item, key: ("2h 28m", None))
    item_id = client.get("/api/list").get_json()[0]["id"]

    first = client.post("/api/backfill-lengths").get_json()
    assert first["lengths"] == {str(item_id): "2h 28m"}
    assert first["remaining"] == 0
    assert client.get("/api/list").get_json()[0]["length"] == "2h 28m"
    # Nothing left pending -> a second sweep is a no-op.
    assert client.post("/api/backfill-lengths").get_json() == {
        "lengths": {}, "settled": 0, "remaining": 0,
    }


def test_backfill_lengths_settles_empty_so_it_stops_asking(client, monkeypatch):
    """A title with no published length must not stay pending forever â€” the sweep
    stores "" and never resolves that row again."""
    _register(client)
    _add_movie(client)
    calls = {"n": 0}

    def resolve(item, key):
        calls["n"] += 1
        return "", None

    monkeypatch.setattr(app_module, "_resolve_length", resolve)
    first = client.post("/api/backfill-lengths").get_json()
    assert first["lengths"] == {}
    assert first["settled"] == 1          # forward progress, even with nothing to show
    client.post("/api/backfill-lengths")
    assert calls["n"] == 1


def test_backfill_lengths_leaves_failures_retryable(client, monkeypatch):
    _register(client)
    _add_movie(client)
    monkeypatch.setattr(app_module, "_resolve_length", lambda item, key: (None, None))
    data = client.post("/api/backfill-lengths").get_json()
    assert data["settled"] == 0           # no progress -> the client stops looping
    assert client.get("/api/list").get_json()[0]["length"] is None


def test_backfill_lengths_writes_the_book_page_count_too(client, monkeypatch):
    """Books get their own column filled as well, so a backfilled book matches a
    freshly added one rather than only having the formatted string."""
    _register(client)
    _ensure_tmdb_key()
    client.post("/api/add", json={
        "title": "Dune", "media_type": "book",
        "external_id": "OL1W", "status": "watchlist",
    })
    monkeypatch.setattr(app_module, "_fetch_book_pages", lambda wid: 412)
    client.post("/api/backfill-lengths")
    row = client.get("/api/list").get_json()[0]
    assert row["length"] == "412 Pages"
    assert row["total_pages"] == 412


def test_backfill_lengths_does_not_touch_status_dates(client, monkeypatch):
    """A derived-metadata write must not look like user activity: a sweep that
    bumped date_added would silently reshuffle every "recently added" sort."""
    _register(client)
    _add_movie(client)
    before = client.get("/api/list").get_json()[0]
    monkeypatch.setattr(app_module, "_resolve_length", lambda item, key: ("2h 28m", None))
    client.post("/api/backfill-lengths")
    after = client.get("/api/list").get_json()[0]
    for col in ("date_added", "date_watchlist", "date_watching", "date_finished"):
        assert after[col] == before[col]


def test_backfill_lengths_movie_end_to_end(client, monkeypatch):
    """The whole chain with only the network mocked: sweep -> _resolve_length ->
    _cached_tmdb_meta -> TMDB -> persisted column. The other backfill tests stub
    _resolve_length, so this is what proves the pieces actually connect."""
    _register(client)
    _add_movie(client)
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return _FakeTMDBResp({
            "runtime": 148,
            "credits": {"crew": [{"name": "Christopher Nolan", "job": "Director"}]},
        })

    monkeypatch.setattr(app_module.requests, "get", fake_get)
    client.post("/api/backfill-lengths")
    assert client.get("/api/list").get_json()[0]["length"] == "2h 28m"
    assert len(calls) == 1                        # one request for runtime + director


def test_backfill_lengths_manga_end_to_end(client, monkeypatch):
    _register(client)
    _ensure_tmdb_key()
    client.post("/api/add", json={
        "title": "Berserk", "media_type": "manga",
        "external_id": "berserk-sweep-id", "status": "watchlist",
    })
    monkeypatch.setattr(
        app_module.requests, "get",
        lambda *a, **k: _FakeTMDBResp({"data": {"attributes": {"lastChapter": "374"}}}),
    )
    app_module.manga_info_cache.clear()
    client.post("/api/backfill-lengths")
    assert client.get("/api/list").get_json()[0]["length"] == "374 Chapters"


# ── "Recently added" ordering ────────────────────────────────────────────────
# The per-status stamps double as the sort key for the grid's "Recently Added" /
# "Last Progress" order. Stored as a bare date they tie for everything touched on
# the same day, and the client's tiebreak (row id) is creation order — unrelated
# to when an item entered the tab — so same-day items came out shuffled.

_STAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}")


def test_status_stamps_are_stored_in_utc(client):
    """The card converts the stamp into the viewer's zone for display, which only
    works if what's stored is UTC — one clock, no DST fold in the sort key."""
    _register(client)
    _add_movie(client)
    stamp = client.get("/api/list").get_json()[0]["date_added"]
    written = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S.%f").replace(tzinfo=timezone.utc)
    assert abs((datetime.now(timezone.utc) - written).total_seconds()) < 60


def test_status_stamps_carry_time_of_day(client):
    """Every stamp keeps its leading YYYY-MM-DD (what the card shows) and gains a
    time part (what the sort needs)."""
    _register(client)
    _add_movie(client)
    item = client.get("/api/list").get_json()[0]
    assert _STAMP_RE.fullmatch(item["date_added"])
    assert _STAMP_RE.fullmatch(item["date_watchlist"])


def test_same_day_finishes_are_ordered_by_time(client):
    """Two items finished on the same day must stay in the order they were
    finished. The newer row is finished FIRST here, so a row-id tiebreak would
    put it on top — only a real timestamp gets this right."""
    _register(client)
    _add_movie(client, "Inception", "27205")
    _add_movie(client, "Dune", "438631")
    ids = {i["title"]: i["id"] for i in client.get("/api/list").get_json()}

    client.post("/api/add", json={"id": ids["Dune"], "status": "finished"})
    time.sleep(0.01)
    client.post("/api/add", json={"id": ids["Inception"], "status": "finished"})

    rows = {i["title"]: i for i in client.get("/api/list").get_json()}
    assert rows["Inception"]["date_finished"] > rows["Dune"]["date_finished"]
    assert rows["Inception"]["date_added"] > rows["Dune"]["date_added"]


def test_same_day_progress_advances_the_watching_stamp(client):
    """Progress bumps the watching stamp every time, not just the first time that
    day — otherwise a second edit never moves the card to the front."""
    _register(client)
    _add_movie(client)
    media_id = client.get("/api/list").get_json()[0]["id"]
    client.post("/api/add", json={"id": media_id, "status": "watching"})

    client.post("/api/add", json={"id": media_id, "last_timestamp": "0:20:00"})
    first = client.get("/api/list").get_json()[0]["date_watching"]
    time.sleep(0.01)
    client.post("/api/add", json={"id": media_id, "last_timestamp": "0:40:00"})
    second = client.get("/api/list").get_json()[0]

    assert second["date_watching"] > first
    assert second["date_added"] == second["date_watching"]


def test_restore_media_preserves_timestamped_dates(client):
    """Undo restores the original stamps rather than re-adding as "today", so the
    restore path's format check has to accept the timestamped form."""
    _register(client)
    _add_movie(client)
    media_id = client.get("/api/list").get_json()[0]["id"]
    backup = client.get(f"/api/media-backup/{media_id}").get_json()["media"]
    client.post(f"/api/delete/{media_id}")

    client.post("/api/restore-media", json={"media": backup})
    restored = client.get("/api/list").get_json()[0]
    assert restored["date_added"] == backup["date_added"]
    assert restored["date_watchlist"] == backup["date_watchlist"]
