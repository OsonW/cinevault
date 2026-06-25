import os
import json
import sqlite3
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
    monkeypatch.setattr(app_module, "_fetch_tmdb_director", lambda *a, **k: "")
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
        return "Some Director"
    monkeypatch.setattr(app_module, "_fetch_tmdb_director", spy)

    data = client.get("/api/search?q=matrix&type=movie").get_json()
    assert data[0]["title"] == "The Matrix"
    assert called["n"] == 0                      # search must NOT fetch directors
    assert not data[0].get("author")             # author absent/empty on search


def test_tmdb_director_endpoint(client, monkeypatch):
    _register(client)
    _set_mdblist_key()
    calls = {"n": 0}
    def spy(media_type, tmdb_id, key):
        calls["n"] += 1
        return "Lana Wachowski"
    monkeypatch.setattr(app_module, "_fetch_tmdb_director", spy)
    first  = client.get("/api/tmdb-director/movie/603").get_json()
    second = client.get("/api/tmdb-director/movie/603").get_json()
    assert first == second == {"author": "Lana Wachowski"}
    assert calls["n"] == 1                        # second served from cache


def test_tmdb_director_no_tmdb_key(client):
    _register(client)
    resp = client.get("/api/tmdb-director/movie/603")
    assert resp.status_code == 200
    assert resp.get_json() == {"author": ""}


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
