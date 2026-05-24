"""Regression tests for the streaming chat endpoint.

The streaming generator persists the assistant reply to SQLite *while* the
response is being streamed. A real WSGI server consumes that generator AFTER
Flask has torn down the request context, so any db call inside it explodes on
g.user_db_path unless the generator is wrapped with stream_with_context. The
symptom is "An error occurred. Please try again." on every chat message.

NOTE: the Flask test client preserves the request context while it consumes a
streaming response, which hides this bug. These tests therefore drive the raw
WSGI app and consume the response iterator themselves, exactly like gunicorn.
"""
import json

import pytest
from werkzeug.test import EnvironBuilder

import app as app_module
import auth as auth_module
from app import app as flask_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_DIR", str(tmp_path))
    flask_app.config["TESTING"] = True
    flask_app.config["SECRET_KEY"] = "test-secret-key"
    app_module._app_initialized = False
    app_module._initialized_users.clear()
    app_module._user_media_cache.clear()
    app_module._user_ai_card_cache.clear()
    app_module._user_memory_cache.clear()
    # The register route is rate-limited via a process-global bucket dict; clear
    # it so a full-suite run doesn't 429 our registration after 5 prior ones.
    auth_module._RATE_BUCKETS.clear()
    # NOTE: deliberately NOT used as a context manager. The `with` form turns on
    # FlaskClient.preserve_context, which leaves a request context on the stack
    # while a streaming response is consumed — that hides the very bug these
    # tests exist to catch (a real WSGI server has no such lingering context).
    return flask_app.test_client()


def _register(client, username="alice", password="secret123"):
    return client.post(
        "/auth/register", json={"username": username, "password": password}
    )


def _mock_gemini(monkeypatch, tokens=("Hello ", "there!")):
    monkeypatch.setattr(app_module, "_get_gemini_key", lambda: "fake-key")
    monkeypatch.setattr(app_module, "_make_gemini", lambda key: object())
    monkeypatch.setattr(
        app_module,
        "_iter_gemini_response_tokens",
        lambda contents, gemini_client: iter(tokens),
    )


def _wsgi_stream(client, path, body):
    """POST `body` to `path` through the raw WSGI app and consume the streamed
    response the way a real server does — after the request context is gone."""
    session = client.get_cookie("session")
    builder = EnvironBuilder(
        path=path, method="POST", json=body,
        headers={"Cookie": f"session={session.value}"} if session else {},
    )
    environ = builder.get_environ()
    captured = {}

    def start_response(status, headers, exc_info=None):
        captured["status"] = status

    app_iter = flask_app(environ, start_response)
    try:
        raw = b"".join(app_iter)
    finally:
        if hasattr(app_iter, "close"):
            app_iter.close()
    events = [json.loads(l[6:]) for l in raw.decode().splitlines() if l.startswith("data: ")]
    return captured.get("status"), events


def test_chat_message_streams_without_error(client, monkeypatch):
    _register(client)
    _mock_gemini(monkeypatch)
    chat_id = client.post("/api/chats/new", json={}).get_json()["id"]

    _, events = _wsgi_stream(client, f"/api/chats/{chat_id}/message", {"content": "hi"})

    assert not any("error" in e for e in events), f"stream errored: {events}"
    assert any(e.get("done") for e in events)


def test_chat_message_persists_assistant_reply(client, monkeypatch):
    _register(client)
    _mock_gemini(monkeypatch, tokens=("All ", "good"))
    chat_id = client.post("/api/chats/new", json={}).get_json()["id"]

    _wsgi_stream(client, f"/api/chats/{chat_id}/message", {"content": "hi"})

    msgs = client.get(f"/api/chats/{chat_id}").get_json()["messages"]
    roles = [m["role"] for m in msgs]
    assert "assistant" in roles, f"assistant reply not persisted: {msgs}"
    assert any(m["content"] == "All good" for m in msgs)
