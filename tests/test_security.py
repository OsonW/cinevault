"""Security hardening regression tests: global rate limiting, input
validation/allowlists, and oversized/malformed payload rejection."""
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
    return client.post(
        "/auth/register", json={"username": username, "password": password}
    )


def _ensure_tmdb_key(username="alice"):
    """Provision a TMDB key so storage-writing endpoints are unlocked. Keyless
    accounts are blocked from writing (no DB file is created), by design."""
    from users_db import get_user_by_username, get_user_keys, set_user_keys
    uid = get_user_by_username(username)["id"]
    keys = get_user_keys(uid)
    if not keys["tmdb_key"]:
        set_user_keys(uid, "tmdbkey", keys["mdblist_key"])


def test_keyless_account_cannot_write(client):
    """A freshly-registered account with no API keys is blocked (403) from any
    storage-writing API call, so empty accounts never create a DB file on disk."""
    _register(client)
    resp = client.post(
        "/api/add",
        json={"title": "X", "media_type": "movie", "external_id": "1",
              "status": "watchlist"},
    )
    assert resp.status_code == 403
    # A read stays allowed and returns empty without provisioning storage.
    assert client.get("/api/list").get_json() == []


# ── Global rate limiting (applies to every endpoint) ───────────────────────────

def test_global_rate_limit_returns_429(client, monkeypatch):
    _register(client)
    monkeypatch.setattr(app_module, "GLOBAL_RATE_MAX", 2)
    assert client.get("/api/list").status_code == 200
    assert client.get("/api/list").status_code == 200
    resp = client.get("/api/list")
    assert resp.status_code == 429
    assert resp.headers.get("Retry-After")


def test_global_rate_limit_is_per_endpoint(client, monkeypatch):
    """Exhausting one endpoint must not 429 a different endpoint."""
    _register(client)
    monkeypatch.setattr(app_module, "GLOBAL_RATE_MAX", 2)
    client.get("/api/list")
    client.get("/api/list")
    assert client.get("/api/list").status_code == 429
    # Different endpoint has its own bucket and is still available.
    assert client.get("/api/item/999/fetch_director").status_code == 200


# ── Input validation / allowlists ──────────────────────────────────────────────

def test_add_rejects_invalid_media_type(client):
    _register(client)
    _ensure_tmdb_key()
    resp = client.post(
        "/api/add",
        json={"title": "X", "media_type": "exe", "status": "watchlist"},
    )
    assert resp.status_code == 400


def test_add_rejects_invalid_status(client):
    _register(client)
    _ensure_tmdb_key()
    resp = client.post(
        "/api/add",
        json={"title": "X", "media_type": "movie", "status": "pwned"},
    )
    assert resp.status_code == 400


def test_add_rejects_missing_title(client):
    _register(client)
    _ensure_tmdb_key()
    resp = client.post("/api/add", json={"media_type": "movie"})
    assert resp.status_code == 400


def test_add_rejects_out_of_range_rating(client):
    _register(client)
    _ensure_tmdb_key()
    client.post(
        "/api/add",
        json={"title": "X", "media_type": "movie", "external_id": "1",
              "status": "finished"},
    )
    item_id = client.get("/api/list").get_json()[0]["id"]
    resp = client.post("/api/add", json={"id": item_id, "rating": 99})
    assert resp.status_code == 400


def test_add_valid_payload_still_works(client):
    _register(client)
    _ensure_tmdb_key()
    resp = client.post(
        "/api/add",
        json={"title": "Inception", "media_type": "movie",
              "external_id": "27205", "status": "watchlist"},
    )
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"


# ── Oversized / malformed payloads ─────────────────────────────────────────────

def test_oversized_body_rejected(client):
    _register(client)
    _ensure_tmdb_key()
    big = b'{"x":"' + b"a" * (300 * 1024) + b'"}'
    resp = client.post("/api/add", data=big, content_type="application/json")
    assert resp.status_code == 413


def test_malformed_json_does_not_500(client):
    _register(client)
    _ensure_tmdb_key()
    resp = client.post(
        "/api/add", data="{not valid json", content_type="application/json"
    )
    # silent=True -> {} -> missing-title validation -> 400, never a 500.
    assert resp.status_code == 400
