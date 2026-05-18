import sqlite3
from datetime import datetime

DB_NAME = "movie_tracker.db"


def get_conn():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS media (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                tmdb_id         INTEGER,
                external_id     TEXT,
                title           TEXT NOT NULL,
                media_type      TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'watchlist',
                rating          REAL,
                last_timestamp  TEXT,
                last_season     INTEGER,
                last_episode    INTEGER,
                last_volume     INTEGER,
                last_chapter    REAL,
                current_page    INTEGER,
                total_pages     INTEGER,
                notes           TEXT,
                cover_url       TEXT,
                author          TEXT,
                overview        TEXT,
                year            TEXT,
                date_added      TEXT DEFAULT (date('now')),
                UNIQUE(external_id, media_type)
            )
        """)

        # migration: add year column to existing databases
        existing = [r[1] for r in conn.execute("PRAGMA table_info(media)").fetchall()]
        if "year" not in existing:
            conn.execute("ALTER TABLE media ADD COLUMN year TEXT")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS chats (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                media_id        INTEGER REFERENCES media(id) ON DELETE CASCADE,
                title           TEXT NOT NULL,
                context_tag     TEXT,
                spoiler_season  INTEGER,
                spoiler_episode INTEGER,
                spoiler_chapter REAL,
                created_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
                updated_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_messages (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id      INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
                role         TEXT NOT NULL,
                content      TEXT NOT NULL,
                snap_season  INTEGER,
                snap_episode INTEGER,
                snap_chapter REAL,
                created_at   TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_memory (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                key        TEXT UNIQUE NOT NULL,
                value      TEXT NOT NULL,
                updated_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
            )
        """)


def row_to_dict(row):
    return dict(row) if row is not None else None


def get_all_media():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM media ORDER BY title").fetchall()
    return [row_to_dict(r) for r in rows]


def get_media_by_external_id(external_id: str, media_type: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM media WHERE external_id = ? AND media_type = ?",
            (external_id, media_type),
        ).fetchone()
    return row_to_dict(row)


def get_media_by_id(media_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM media WHERE id = ?", (media_id,)
        ).fetchone()
    return row_to_dict(row)


def add_media_entry(
    title, media_type, status="watchlist",
    tmdb_id=None, external_id=None,
    cover_url=None, author=None,
    total_pages=None, overview=None, year=None,
):
    ext = external_id or (str(tmdb_id) if tmdb_id else None)
    with get_conn() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO media
                (tmdb_id, external_id, title, media_type, status,
                 cover_url, author, total_pages, overview, year)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (tmdb_id, ext, title, media_type, status,
              cover_url, author, total_pages, overview, year))
        row = conn.execute(
            "SELECT id FROM media WHERE external_id = ? AND media_type = ?",
            (ext, media_type),
        ).fetchone()
    return row["id"] if row else None


_UPDATABLE = frozenset({
    "status", "rating", "last_timestamp",
    "last_season", "last_episode",
    "last_volume", "last_chapter",
    "current_page", "total_pages",
    "notes", "cover_url", "author", "overview",
})


def update_media_entry(media_id: int, **fields):
    updates = {k: v for k, v in fields.items() if k in _UPDATABLE}
    if not updates:
        return
    clauses = ", ".join(f"{k} = ?" for k in updates)
    params  = list(updates.values()) + [media_id]
    with get_conn() as conn:
        conn.execute(f"UPDATE media SET {clauses} WHERE id = ?", params)


def get_items_missing_year():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, media_type, external_id, tmdb_id FROM media WHERE year IS NULL OR year = ''"
        ).fetchall()
    return [dict(r) for r in rows]


def set_year(media_id: int, year: str):
    with get_conn() as conn:
        conn.execute("UPDATE media SET year = ? WHERE id = ?", (year, media_id))


def delete_media_entry(media_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM chats WHERE media_id = ?", (media_id,))
        conn.execute("DELETE FROM media WHERE id = ?", (media_id,))


def get_chats_by_media_id(media_id: int):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM chats WHERE media_id = ? ORDER BY updated_at DESC",
            (media_id,),
        ).fetchall()
    return [row_to_dict(r) for r in rows]


def get_all_chats():
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT c.*,
                   m.title       AS media_title,
                   m.media_type  AS media_media_type,
                   m.cover_url   AS media_cover_url,
                   m.external_id AS media_external_id
            FROM chats c
            LEFT JOIN media m ON m.id = c.media_id
            ORDER BY c.updated_at DESC
        """).fetchall()
    return [row_to_dict(r) for r in rows]


def get_chat_by_id(chat_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM chats WHERE id = ?", (chat_id,)
        ).fetchone()
    return row_to_dict(row)


def get_chat_messages(chat_id: int):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM chat_messages WHERE chat_id = ? ORDER BY id",
            (chat_id,),
        ).fetchall()
    return [row_to_dict(r) for r in rows]


def create_chat(media_id, title, context_tag=None):
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO chats (media_id, title, context_tag, created_at, updated_at) VALUES (?,?,?,?,?)",
            (media_id, title, context_tag, now, now),
        )
        return cur.lastrowid


def append_message(chat_id: int, role: str, content: str):
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO chat_messages (chat_id, role, content, created_at) VALUES (?,?,?,?)",
            (chat_id, role, content, now),
        )
        conn.execute(
            "UPDATE chats SET updated_at = ? WHERE id = ?", (now, chat_id)
        )
        return cur.lastrowid


def update_message_snap(msg_id: int, snap_season=None, snap_episode=None, snap_chapter=None):
    with get_conn() as conn:
        conn.execute(
            "UPDATE chat_messages SET snap_season = ?, snap_episode = ?, snap_chapter = ? WHERE id = ?",
            (snap_season, snap_episode, snap_chapter, msg_id),
        )


def update_chat_spoiler_threshold(chat_id: int, season=None, episode=None, chapter=None):
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    with get_conn() as conn:
        conn.execute(
            "UPDATE chats SET spoiler_season = ?, spoiler_episode = ?, spoiler_chapter = ?, updated_at = ? WHERE id = ?",
            (season, episode, chapter, now, chat_id),
        )


def delete_chat(chat_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM chats WHERE id = ?", (chat_id,))


def clear_chat_messages(chat_id: int):
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    with get_conn() as conn:
        conn.execute("DELETE FROM chat_messages WHERE chat_id = ?", (chat_id,))
        conn.execute(
            "UPDATE chats SET updated_at = ? WHERE id = ?", (now, chat_id)
        )


def get_all_memory() -> dict:
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM user_memory").fetchall()
    return {r["key"]: r["value"] for r in rows}