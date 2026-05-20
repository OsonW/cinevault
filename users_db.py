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
                gemini_key    TEXT,
                tmdb_key      TEXT,
                created_at    TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
            )
        """)
        for col in ("gemini_key", "tmdb_key"):
            try:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError:
                pass


def get_user_keys(user_id: int) -> dict:
    with _get_users_conn() as conn:
        row = conn.execute(
            "SELECT gemini_key, tmdb_key FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    if not row:
        return {"gemini_key": "", "tmdb_key": ""}
    return {
        "gemini_key": row["gemini_key"] or "",
        "tmdb_key":   row["tmdb_key"]   or "",
    }


def set_user_keys(user_id: int, gemini_key: str, tmdb_key: str) -> None:
    with _get_users_conn() as conn:
        conn.execute(
            "UPDATE users SET gemini_key = ?, tmdb_key = ? WHERE id = ?",
            (gemini_key, tmdb_key, user_id),
        )


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
