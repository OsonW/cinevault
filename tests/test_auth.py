import os
import pytest

import app as app_module
from app import app as flask_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Isolated test client with a fresh temp DB_DIR per test."""
    monkeypatch.setenv("DB_DIR", str(tmp_path))
    flask_app.config["TESTING"] = True
    flask_app.config["SECRET_KEY"] = "test-secret-key"
    # Reset module-level state so tests don't bleed into each other
    app_module._app_initialized = False
    app_module._initialized_users.clear()
    app_module._user_media_cache.clear()
    app_module._user_ai_card_cache.clear()
    app_module._user_memory_cache.clear()
    with flask_app.test_client() as c:
        yield c


def _register(client, username="alice", password="secret123"):
    return client.post(
        "/auth/register",
        json={"username": username, "password": password},
    )


# ── Registration ─────────────────────────────────────────────────────────────

def test_register_creates_user(client):
    resp = _register(client)
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["status"] == "ok"
    assert data["username"] == "alice"


def test_register_duplicate_username(client):
    _register(client)
    resp = _register(client, password="different_pass")
    assert resp.status_code == 409
    assert "taken" in resp.get_json()["error"]


def test_register_missing_password(client):
    resp = client.post("/auth/register", json={"username": "alice"})
    assert resp.status_code == 400


def test_register_short_password(client):
    resp = client.post(
        "/auth/register", json={"username": "alice", "password": "abc"}
    )
    assert resp.status_code == 400


# ── Login ─────────────────────────────────────────────────────────────────────

def test_login_success(client):
    _register(client, username="bob", password="hunter2!!")
    resp = client.post(
        "/auth/login", json={"username": "bob", "password": "hunter2!!"}
    )
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"


def test_login_wrong_password(client):
    _register(client, username="carol", password="correct1")
    resp = client.post(
        "/auth/login", json={"username": "carol", "password": "wrong"}
    )
    assert resp.status_code == 401


def test_login_unknown_user(client):
    resp = client.post(
        "/auth/login", json={"username": "nobody", "password": "pass"}
    )
    assert resp.status_code == 401


# ── Logout ────────────────────────────────────────────────────────────────────

def test_logout_redirects_to_login(client):
    _register(client, username="dave", password="pass123!")
    resp = client.get("/auth/logout", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


# ── Route protection ──────────────────────────────────────────────────────────

def test_index_unauthenticated_redirects_to_login(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_api_list_unauthenticated_returns_401(client):
    resp = client.get("/api/list")
    assert resp.status_code == 401
    assert resp.get_json()["error"] == "Unauthorized"


def test_api_search_unauthenticated_returns_401(client):
    resp = client.get("/api/search?q=inception&type=movie")
    assert resp.status_code == 401


def test_api_list_authenticated_returns_200(client):
    _register(client, username="eve", password="pass123!")
    resp = client.get("/api/list")
    assert resp.status_code == 200
    assert isinstance(resp.get_json(), list)


def test_api_add_authenticated(client):
    _register(client, username="frank", password="pass123!")
    resp = client.post(
        "/api/add",
        json={
            "title": "Inception",
            "media_type": "movie",
            "external_id": "27205",
            "status": "watchlist",
        },
    )
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"


def test_users_have_isolated_libraries(client):
    """Media added by user1 must not appear in user2's library."""
    _register(client, username="user1", password="pass123!")
    client.post(
        "/api/add",
        json={
            "title": "User1 Only Movie",
            "media_type": "movie",
            "external_id": "99991",
            "status": "watchlist",
        },
    )
    client.get("/auth/logout")

    _register(client, username="user2", password="pass123!")
    titles = [m["title"] for m in client.get("/api/list").get_json()]
    assert "User1 Only Movie" not in titles
