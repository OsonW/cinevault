import re
import functools
import threading
from collections import deque
from time import time as _now
import requests
from flask import Blueprint, request, jsonify, redirect, url_for, render_template
from flask_login import LoginManager, UserMixin, login_user, logout_user, current_user, login_required
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
_RATE_BUCKETS_MAX = 10_000


def _client_ip() -> str:
    # ProxyFix (applied in app.py in production) already resolved the real
    # client IP into remote_addr, so we never need to read X-Forwarded-For
    # here. Reading it directly would let a client spoof their IP.
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
                if key not in _RATE_BUCKETS:
                    if len(_RATE_BUCKETS) >= _RATE_BUCKETS_MAX:
                        _RATE_BUCKETS.pop(next(iter(_RATE_BUCKETS)))
                    _RATE_BUCKETS[key] = deque()
                bucket = _RATE_BUCKETS[key]
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


@auth_bp.route("/auth/logout", methods=["GET", "POST"])
@rate_limit(max_requests=30, window_seconds=60)    # generous; just blocks runaway loops
def logout():
    # GET is a top-level browser navigation: clear the session and redirect to
    # /login in the SAME request, so the client never lands on a page while
    # still authenticated (which would bounce it back to the library). POST
    # remains for any programmatic JSON caller.
    # CSRF is not a concern: the session/remember cookies are SameSite=Strict,
    # so a cross-site request to this route carries no auth cookie and is a
    # no-op for the victim.
    logout_user()
    if request.method == "GET":
        return redirect(url_for("auth.login_page"))
    return jsonify({"status": "ok"})


def _validate_tmdb_key(key: str) -> tuple[bool, str]:
    """Returns (valid, error_message). error_message is empty string on success."""
    try:
        resp = requests.get(
            "https://api.themoviedb.org/3/authentication",
            params={"api_key": key},
            timeout=10,
        )
        if resp.status_code == 200:
            return True, ""
        if resp.status_code == 401:
            return False, "Invalid TMDB API key — please check and try again."
        return False, f"TMDB validation failed (HTTP {resp.status_code}) — please try again."
    except requests.exceptions.Timeout:
        return False, "TMDB validation timed out — please try again."
    except Exception:
        return False, "Could not reach TMDB to validate key — please try again."


def _validate_gemini_key(key: str) -> tuple[bool, str]:
    """Returns (valid, error_message). error_message is empty string on success."""
    try:
        resp = requests.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            params={"key": key},
            timeout=10,
        )
        if resp.status_code == 200:
            return True, ""
        if resp.status_code in (400, 403):
            return False, "Invalid Gemini key — please check and try again."
        return False, f"Gemini validation failed (HTTP {resp.status_code}) — please try again."
    except requests.exceptions.Timeout:
        return False, "Gemini validation timed out — please try again."
    except Exception:
        return False, "Could not reach Gemini to validate key — please try again."


def _mask_key(key: str | None) -> str:
    if not key:
        return ""
    if len(key) < 8:
        return "***"
    return key[:6] + "..." + key[-4:]


@auth_bp.route("/auth/keys", methods=["GET"])
@login_required
def get_keys():
    keys = get_user_keys(int(current_user.id))
    return jsonify({
        "has_keys":   bool(keys["gemini_key"] and keys["tmdb_key"]),
        "gemini_key": _mask_key(keys["gemini_key"]),
        "tmdb_key":   _mask_key(keys["tmdb_key"]),
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
    tmdb_ok, tmdb_err = _validate_tmdb_key(tmdb_key)
    if not tmdb_ok:
        return jsonify({"error": tmdb_err, "which": "tmdb"}), 400
    gemini_ok, gemini_err = _validate_gemini_key(gemini_key)
    if not gemini_ok:
        return jsonify({"error": gemini_err, "which": "gemini"}), 400
    set_user_keys(int(current_user.id), gemini_key, tmdb_key)
    return jsonify({"status": "ok"})
