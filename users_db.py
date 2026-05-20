import os
import sqlite3
import bcrypt


def _get_db_dir() -> str:
    return os.environ.get("DB_DIR", ".")


def get_users_db_path() -> str:
    return os.path.join(_get_db_dir(), "users.db")


def _get_users_conn():
    conn = sqlite3.connect(get_users_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def init_users_db() -> None:
    with _get_users_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT UNIQUE NOT NULL COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                created_at    TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
            )
        """)


def create_user(username: str, password: str) -> int | None:
    """Returns new user_id on success, None if username is taken."""
    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    try:
        with _get_users_conn() as conn:
            cur = conn.execute(
                "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                (username, pw_hash),
            )
            return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


def get_user_by_id(user_id: int) -> dict | None:
    with _get_users_conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    return dict(row) if row else None


def get_user_by_username(username: str) -> dict | None:
    with _get_users_conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
    return dict(row) if row else None


def verify_password(username: str, password: str) -> dict | None:
    """Returns the user dict if credentials are valid, else None."""
    user = get_user_by_username(username)
    if not user:
        return None
    if bcrypt.checkpw(password.encode(), user["password_hash"].encode()):
        return user
    return None
