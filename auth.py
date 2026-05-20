from flask import Blueprint, request, jsonify, redirect, url_for, render_template
from flask_login import LoginManager, UserMixin, login_user, logout_user

from users_db import get_user_by_id, create_user, verify_password

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
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400
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
