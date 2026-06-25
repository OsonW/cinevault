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


def _validate_username(username: str) -> str | None:
    """Returns an error message, or None if the username is valid."""
    if not username:
        return "Username required"
    if len(username) < 4 or len(username) > 32:
        return "Username must be 4–32 characters"
    if not _NO_WHITESPACE_RE.match(username):
        return "Username cannot contain spaces"
    return None


def _validate_password(password: str) -> str | None:
    """Returns an error message, or None if the password is valid."""
    if not password:
        return "Password required"
    if len(password) < 4 or len(password) > 32:
        return "Password must be 4–32 characters"
    if not _NO_WHITESPACE_RE.match(password):
        return "Password cannot contain spaces"
    return None


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


def check_rate_limit(key: str, max_requests: int, window_seconds: int) -> int | None:
    """Sliding-window check for one bucket key. Returns the Retry-After value
    (seconds) if the caller is over the limit, or None if the request is allowed
    (in which case it is recorded against the window)."""
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
            return max(1, int(bucket[0] + window_seconds - now) + 1)
        bucket.append(now)
    return None


def _too_many_requests(retry_after: int):
    return (
        jsonify({"error": "Too many requests. Please try again later."}),
        429,
        {"Retry-After": str(retry_after)},
    )


def rate_limit(max_requests: int, window_seconds: int):
    """Decorator: limit a route to `max_requests` per `window_seconds` per IP."""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            retry_after = check_rate_limit(
                f"{_client_ip()}:{fn.__name__}", max_requests, window_seconds
            )
            if retry_after is not None:
                return _too_many_requests(retry_after)
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
    err = _validate_username(username) or _validate_password(password)
    if err:
        return jsonify({"error": err}), 400
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


def _validate_mdblist_key(key: str) -> tuple[bool, str]:
    """Returns (valid, error_message). error_message is empty string on success.

    Hits /user, the cheapest authenticated endpoint. MDBList returns 403 for a
    missing or invalid key (there is no separate 401)."""
    try:
        resp = requests.get(
            "https://api.mdblist.com/user",
            params={"apikey": key},
            timeout=10,
        )
        if resp.status_code == 200:
            return True, ""
        if resp.status_code in (401, 403):
            return False, "Invalid MDBList API key — please check and try again."
        return False, f"MDBList validation failed (HTTP {resp.status_code}) — please try again."
    except requests.exceptions.Timeout:
        return False, "MDBList validation timed out — please try again."
    except Exception:
        return False, "Could not reach MDBList to validate key — please try again."


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
        "has_keys":    bool(keys["tmdb_key"]),
        "tmdb_key":    _mask_key(keys["tmdb_key"]),
        "mdblist_key": _mask_key(keys["mdblist_key"]),
    })


@auth_bp.route("/auth/keys", methods=["POST"])
@login_required
@rate_limit(max_requests=10, window_seconds=60)    # 10/minute per IP
def save_keys():
    data       = request.get_json(silent=True) or {}
    tmdb_input = _as_str(data.get("tmdb_key"))
    mdb_input  = _as_str(data.get("mdblist_key"))
    existing   = get_user_keys(int(current_user.id))

    # TMDB is required. An empty field means "keep the existing key", so we only
    # validate (and pay the network round-trip) when the user supplied a new one.
    if tmdb_input:
        tmdb_ok, tmdb_err = _validate_tmdb_key(tmdb_input)
        if not tmdb_ok:
            return jsonify({"error": tmdb_err, "which": "tmdb"}), 400
        tmdb_final = tmdb_input
    else:
        tmdb_final = existing["tmdb_key"]
    if not tmdb_final:
        return jsonify({"error": "TMDB API key required", "which": "tmdb"}), 400

    # MDBList is optional. Same keep-on-empty semantics as TMDB; validate only
    # when a new value is supplied.
    if mdb_input:
        mdb_ok, mdb_err = _validate_mdblist_key(mdb_input)
        if not mdb_ok:
            return jsonify({"error": mdb_err, "which": "mdblist"}), 400
        mdblist_final = mdb_input
    else:
        mdblist_final = existing["mdblist_key"]

    set_user_keys(int(current_user.id), tmdb_final, mdblist_final)
    return jsonify({"status": "ok"})
