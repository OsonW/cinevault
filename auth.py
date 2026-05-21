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


class User(UserMixin):
    def __init__(self, user_id: int, username: str):
        self.id = user_id
        self.username = username

    @staticmethod
    def from_db(row: dict) -> "User":
        return User(user_id=row["id"], username=row["username"])


@login_manager.user_loader
def load_user(user_id: str):
    row = get_user_by_id(int(user_id))
    return User.from_db(row) if row else None


@auth_bp.route("/login")
def login_page():
    return render_template("login.html")


@auth_bp.route("/auth/register", methods=["POST"])
def register():
    data     = request.json or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    if not username or not password:
        return jsonify({"error": "Username and password required"}), 400
    if len(username) < 4 or len(username) > 32:
        return jsonify({"error": "Username must be 4–32 characters"}), 400
    if len(password) < 4 or len(password) > 32:
        return jsonify({"error": "Password must be 4–32 characters"}), 400
    user_id = create_user(username, password)
    if user_id is None:
        return jsonify({"error": "Username already taken"}), 409
    login_user(User.from_db(get_user_by_id(user_id)), remember=True)
    return jsonify({"status": "ok", "username": username}), 201


@auth_bp.route("/auth/login", methods=["POST"])
def login():
    data     = request.json or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    row = verify_password(username, password)
    if not row:
        return jsonify({"error": "Invalid credentials"}), 401
    login_user(User.from_db(row), remember=True)
    return jsonify({"status": "ok", "username": username})


@auth_bp.route("/auth/logout")
def logout():
    logout_user()
    return redirect(url_for("auth.login_page"))


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
def save_keys():
    data       = request.json or {}
    gemini_key = (data.get("gemini_key") or "").strip()
    tmdb_key   = (data.get("tmdb_key")   or "").strip()
    if not gemini_key or not tmdb_key:
        return jsonify({"error": "Both keys required"}), 400
    if not _validate_tmdb_key(tmdb_key):
        return jsonify({"error": "invalid_keys", "which": "tmdb"}), 400
    if not _validate_gemini_key(gemini_key):
        return jsonify({"error": "invalid_keys", "which": "gemini"}), 400
    set_user_keys(int(current_user.id), gemini_key, tmdb_key)
    return jsonify({"status": "ok"})
