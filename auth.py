import re
import functools
import threading
from collections import deque
from time import time as _now
import requests
from flask import Blueprint, request, jsonify, redirect, url_for, render_template
from flask_login import LoginManager, UserMixin, login_user, logout_user, current_user, login_required
from google import genai

from users_db import (
    get_user_by_id, create_user, verify_password,
    get_user_keys, set_user_keys,
)

auth_bp = Blueprint("auth", __name__)
login_manager = LoginManager()

# Any non-whitespace characters — letters, digits, symbols are fine; tabs/spaces/newlines are not.
_NO_WHITESPACE_RE = re.compile(r"^\S+$")


def _as_str(value) -> str:
    """Coerce JSON field to a stripped string. Anything that isn't a string
    (None, list, dict, int, bool, ...) becomes an empty string so downstream
    validation rejects it instead of crashing on `.strip()`."""
    return value.strip() if isinstance(value, str) else ""


# ─── Rate limiter ────────────────────────────────────────────────────────────
# Small in-process sliding-window limiter. Keyed by (client IP, route name).
# Fine for a single-process Flask deployment; swap for Flask-Limiter + Redis
# if you ever run multiple workers.
_RATE_LOCK = threading.Lock()
_RATE_BUCKETS: dict[str, deque] = {}


def _client_ip() -> str:
    # Honour the leftmost X-Forwarded-For entry if a proxy added one,
    # otherwise fall back to remote_addr.
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or "-"


def rate_limit(max_requests: int, window_seconds: int):
    """Decorator: limit a route to `max_requests` per `window_seconds` per IP."""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            key    = f"{_client_ip()}:{fn.__name__}"
            now    = _now()
            cutoff = now - window_seconds
            with _RATE_LOCK:
                bucket = _RATE_BUCKETS.setdefault(key, deque())
                while bucket and bucket[0] < cutoff:
                    bucket.popleft()
                if len(bucket) >= max_requests:
                    retry_after = max(1, int(bucket[0] + window_seconds - now) + 1)
                    return (
                        jsonify({"error": "Too many requests. Please try again later."}),
                        429,
                        {"Retry-After": str(retry_after)},
                    )
                bucket.append(now)
            return fn(*args, **kwargs)
        return wrapper
    return decorator


class User(UserMixin):
    def __init__(self, user_id: int, username: str):
        self.id = user_id
        self.username = username

    @staticmethod
    def from_db(row: dict) -> "User":
        return User(user_id=row["id"], username=row["username"])


@login_manager.user_loader
def load_user(user_id):
    # Cookie tampering can hand us a non-numeric id; treat it as logged-out
    # instead of 500-ing.
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return None
    row = get_user_by_id(uid)
    return User.from_db(row) if row else None


@auth_bp.route("/login")
def login_page():
    return render_template("login.html")


@auth_bp.route("/auth/register", methods=["POST"])
@rate_limit(max_requests=5, window_seconds=3600)   # 5/hour per IP
def register():
    data     = request.get_json(silent=True) or {}
    username = _as_str(data.get("username"))
    password = _as_str(data.get("password"))
    if not username or not password:
        return jsonify({"error": "Username and password required"}), 400
    if len(username) < 4 or len(username) > 32:
        return jsonify({"error": "Username must be 4–32 characters"}), 400
    if len(password) < 4 or len(password) > 32:
        return jsonify({"error": "Password must be 4–32 characters"}), 400
    if not _NO_WHITESPACE_RE.match(username):
        return jsonify({"error": "Username cannot contain spaces"}), 400
    if not _NO_WHITESPACE_RE.match(password):
        return jsonify({"error": "Password cannot contain spaces"}), 400
    user_id = create_user(username, password)
    if user_id is None:
        return jsonify({"error": "Username already taken"}), 409
    login_user(User.from_db(get_user_by_id(user_id)), remember=True)
    return jsonify({"status": "ok", "username": username}), 201


@auth_bp.route("/auth/login", methods=["POST"])
@rate_limit(max_requests=10, window_seconds=60)    # 10/minute per IP
def login():
    data     = request.get_json(silent=True) or {}
    username = _as_str(data.get("username"))
    password = _as_str(data.get("password"))
    if not username or not password:
        return jsonify({"error": "Invalid credentials"}), 401
    row = verify_password(username, password)
    if not row:
        return jsonify({"error": "Invalid credentials"}), 401
    login_user(User.from_db(row), remember=True)
    return jsonify({"status": "ok", "username": username})


@auth_bp.route("/auth/logout", methods=["POST"])
@rate_limit(max_requests=30, window_seconds=60)    # generous; just blocks runaway loops
def logout():
    # POST-only so a cross-origin <img src="/auth/logout"> can't kick a
    # logged-in user out without their consent.
    logout_user()
    return jsonify({"status": "ok"})


def _validate_tmdb_key(key: str) -> bool:
    try:
        resp = requests.get(
            "https://api.themoviedb.org/3/authentication",
            params={"api_key": key},
            timeout=6,
        )
        return resp.status_code == 200
    except Exception:
        return False


def _validate_gemini_key(key: str) -> bool:
    try:
        client = genai.Client(api_key=key)
        models_iter = client.models.list()
        next(iter(models_iter), None)
        return True
    except Exception:
        return False


@auth_bp.route("/auth/keys", methods=["GET"])
@login_required
def get_keys():
    keys = get_user_keys(int(current_user.id))
    return jsonify({
        "has_keys":   bool(keys["gemini_key"] and keys["tmdb_key"]),
        "gemini_key": keys["gemini_key"],
        "tmdb_key":   keys["tmdb_key"],
    })


@auth_bp.route("/auth/keys", methods=["POST"])
@login_required
@rate_limit(max_requests=10, window_seconds=60)    # 10/minute per IP
def save_keys():
    data       = request.get_json(silent=True) or {}
    gemini_key = _as_str(data.get("gemini_key"))
    tmdb_key   = _as_str(data.get("tmdb_key"))
    if not gemini_key or not tmdb_key:
        return jsonify({"error": "Both keys required"}), 400
    if not _validate_tmdb_key(tmdb_key):
        return jsonify({"error": "invalid_keys", "which": "tmdb"}), 400
    if not _validate_gemini_key(gemini_key):
        return jsonify({"error": "invalid_keys", "which": "gemini"}), 400
    set_user_keys(int(current_user.id), gemini_key, tmdb_key)
    return jsonify({"status": "ok"})
