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
    # The register/login routes are rate-limited via a process-global bucket
    # dict keyed by client IP. Every test client shares the same IP, so without
    # clearing it the 6th registration in a run gets 429'd and auth-dependent
    # tests fail with spurious 401s.
    from auth import _RATE_BUCKETS
    _RATE_BUCKETS.clear()
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
    _register(client, username="bobby", password="hunter2!!")
    resp = client.post(
        "/auth/login", json={"username": "bobby", "password": "hunter2!!"}
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
    _register(client, username="evelyn", password="pass123!")
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


# ── Change username ───────────────────────────────────────────────────────────

def test_change_username_success(client):
    _register(client, username="alice", password="secret123")
    resp = client.post("/auth/username", json={"username": "alice2"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "ok"
    assert body["username"] == "alice2"
    # New name works for login afterward.
    client.get("/auth/logout")
    assert client.post(
        "/auth/login", json={"username": "alice2", "password": "secret123"}
    ).status_code == 200


def test_change_username_duplicate_case_insensitive(client):
    _register(client, username="alice", password="secret123")
    client.get("/auth/logout")
    _register(client, username="bobby", password="secret123")
    resp = client.post("/auth/username", json={"username": "ALICE"})
    assert resp.status_code == 409
    assert "taken" in resp.get_json()["error"]


def test_change_username_own_case_change(client):
    _register(client, username="Bobby", password="secret123")
    resp = client.post("/auth/username", json={"username": "bobby"})
    assert resp.status_code == 200
    assert resp.get_json()["username"] == "bobby"


def test_change_username_same_name_is_noop(client):
    _register(client, username="alice", password="secret123")
    resp = client.post("/auth/username", json={"username": "alice"})
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "unchanged"


def test_change_username_invalid(client):
    _register(client, username="alice", password="secret123")
    resp = client.post("/auth/username", json={"username": "ab"})
    assert resp.status_code == 400


def test_change_username_unauthenticated_redirects(client):
    resp = client.post("/auth/username", json={"username": "alice2"})
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


# ── Change password ───────────────────────────────────────────────────────────

def test_change_password_success(client):
    _register(client, username="alice", password="oldpass1")
    resp = client.post(
        "/auth/password",
        json={"current_password": "oldpass1", "new_password": "newpass2"},
    )
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"
    # Old password rejected, new password accepted.
    client.get("/auth/logout")
    assert client.post(
        "/auth/login", json={"username": "alice", "password": "oldpass1"}
    ).status_code == 401
    assert client.post(
        "/auth/login", json={"username": "alice", "password": "newpass2"}
    ).status_code == 200


def test_change_password_wrong_current(client):
    _register(client, username="alice", password="oldpass1")
    resp = client.post(
        "/auth/password",
        json={"current_password": "WRONG", "new_password": "newpass2"},
    )
    assert resp.status_code == 401


def test_change_password_invalid_new(client):
    _register(client, username="alice", password="oldpass1")
    resp = client.post(
        "/auth/password",
        json={"current_password": "oldpass1", "new_password": "ab"},
    )
    assert resp.status_code == 400


def test_change_password_unauthenticated_redirects(client):
    resp = client.post(
        "/auth/password",
        json={"current_password": "x", "new_password": "yyyy"},
    )
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]
