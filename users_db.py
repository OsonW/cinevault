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
                tmdb_key      TEXT,
                mdblist_key   TEXT,
                created_at    TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
            )
        """)
        # Idempotent migrations for databases created before each column existed.
        for column in ("tmdb_key", "mdblist_key"):
            try:
                conn.execute(f"ALTER TABLE users ADD COLUMN {column} TEXT")
            except sqlite3.OperationalError:
                pass


def get_user_keys(user_id: int) -> dict:
    with _get_users_conn() as conn:
        row = conn.execute(
            "SELECT tmdb_key, mdblist_key FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    if not row:
        return {"tmdb_key": "", "mdblist_key": ""}
    return {
        "tmdb_key":    row["tmdb_key"]    or "",
        "mdblist_key": row["mdblist_key"] or "",
    }


def set_user_keys(user_id: int, tmdb_key: str, mdblist_key: str = "") -> None:
    with _get_users_conn() as conn:
        conn.execute(
            "UPDATE users SET tmdb_key = ?, mdblist_key = ? WHERE id = ?",
            (tmdb_key, mdblist_key, user_id),
        )


def delete_user(user_id: int) -> None:
    with _get_users_conn() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


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


# Precomputed once at import time. Used as a constant-time decoy when a
# login is attempted against a username that doesn't exist, so the response
# time doesn't leak whether the account exists.
_DUMMY_PASSWORD_HASH = bcrypt.hashpw(b"_no_such_user_", bcrypt.gensalt())


def verify_password(username: str, password: str) -> dict | None:
    """Returns the user dict if credentials are valid, else None.

    Runs a bcrypt comparison on both code paths (missing user vs wrong
    password) so attackers can't enumerate usernames by timing.
    """
    user = get_user_by_username(username)
    pw_bytes = password.encode() if isinstance(password, str) else b""
    if not user:
        # Decoy compare; result is intentionally discarded.
        bcrypt.checkpw(pw_bytes, _DUMMY_PASSWORD_HASH)
        return None
    if bcrypt.checkpw(pw_bytes, user["password_hash"].encode()):
        return user
    return None
