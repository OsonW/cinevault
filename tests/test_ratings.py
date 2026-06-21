import os
import json
import pytest

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
    import db
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
