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
    from auth import _RATE_BUCKETS
    _RATE_BUCKETS.clear()
    with flask_app.test_client() as c:
        yield c


def _register(client, username="alice", password="secret123"):
    return client.post("/auth/register", json={"username": username, "password": password})


def _add_movie(client, title="Inception", external_id="27205"):
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
                {"source": "tomatoes", "value": 94},
                {"source": "audience", "value": 88},
                {"source": "metacritic", "value": 76},
                {"source": "letterboxd", "value": 4.2},
                {"source": "mal", "value": None},
                {"source": "trakt", "value": 90},
            ]}

    monkeypatch.setattr(a.requests, "get", lambda *args, **kw: FakeResp())
    out = a._fetch_mdblist_ratings("movie", 27205, "fake-key")
    assert out == {"imdb": 8.1, "tomatoes": 94, "audience": 88,
                   "metacritic": 76, "letterboxd": 4.2}


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
