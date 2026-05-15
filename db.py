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
        # ── Core media table ──────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS media (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                tmdb_id         INTEGER,
                external_id     TEXT,
                title           TEXT NOT NULL,
                media_type      TEXT NOT NULL,   -- movie|tv|book|manga
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
                date_added      TEXT DEFAULT (date('now')),
                UNIQUE(external_id, media_type)
            )
        """)

        # ── Chat sessions ─────────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chats (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                media_id    INTEGER REFERENCES media(id) ON DELETE SET NULL,
                title       TEXT NOT NULL,
                context_tag TEXT,              -- watchlist|watching|finished (snapshot)
                created_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
                updated_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
            )
        """)

        # ── Chat messages ─────────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id    INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
                role       TEXT NOT NULL,   -- user|assistant
                content    TEXT NOT NULL,
                created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
            )
        """)

        # ── User memory (persistent facts Gemini can reference) ───────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_memory (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                key        TEXT UNIQUE NOT NULL,
                value      TEXT NOT NULL,
                updated_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
            )
        """)

        # ── Non-destructive migrations for old databases ──────────────────
        migration_cols = [
            ("date_added",     "TEXT DEFAULT (date('now'))"),
            ("last_season",    "INTEGER"),
            ("last_episode",   "INTEGER"),
            ("last_volume",    "INTEGER"),
            ("last_chapter",   "REAL"),
            ("current_page",   "INTEGER"),
            ("total_pages",    "INTEGER"),
            ("cover_url",      "TEXT"),
            ("author",         "TEXT"),
            ("external_id",    "TEXT"),
        ]
        for col, defn in migration_cols:
            try:
                conn.execute(f"ALTER TABLE media ADD COLUMN {col} {defn}")
            except sqlite3.OperationalError:
                pass  # column already exists

        # Backfill external_id from tmdb_id for existing rows
        conn.execute("""
            UPDATE media SET external_id = CAST(tmdb_id AS TEXT)
            WHERE external_id IS NULL AND tmdb_id IS NOT NULL
        """)


def row_to_dict(row):
    return dict(row) if row is not None else None


# ══════════════════════════════════════════════════
# MEDIA
# ══════════════════════════════════════════════════

def get_all_media():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM media ORDER BY title").fetchall()
    return [row_to_dict(r) for r in rows]


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
    total_pages=None,
):
    """
    Insert a new media entry. Uses INSERT OR IGNORE for idempotency.
    external_id is the canonical unique identifier (tmdb_id cast to str for
    movies/TV; ISBN / MangaDex UUID for books/manga).
    Returns the row id.
    """
    ext = external_id or (str(tmdb_id) if tmdb_id else None)
    with get_conn() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO media
                (tmdb_id, external_id, title, media_type, status,
                 cover_url, author, total_pages)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (tmdb_id, ext, title, media_type, status,
              cover_url, author, total_pages))
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
    "notes", "cover_url", "author",
})


def update_media_entry(media_id: int, **fields):
    updates = {k: v for k, v in fields.items() if k in _UPDATABLE}
    if not updates:
        return
    clauses = ", ".join(f"{k} = ?" for k in updates)
    params  = list(updates.values()) + [media_id]
    with get_conn() as conn:
        conn.execute(f"UPDATE media SET {clauses} WHERE id = ?", params)


def delete_media_entry(media_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM media WHERE id = ?", (media_id,))


# ══════════════════════════════════════════════════
# CHATS
# ══════════════════════════════════════════════════

def get_all_chats():
    """Return all chats with basic media info joined in."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT c.*,
                   m.title      AS media_title,
                   m.media_type AS media_media_type,
                   m.cover_url  AS media_cover_url,
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
        conn.execute(
            "INSERT INTO chat_messages (chat_id, role, content, created_at) VALUES (?,?,?,?)",
            (chat_id, role, content, now),
        )
        conn.execute(
            "UPDATE chats SET updated_at = ? WHERE id = ?", (now, chat_id)
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


# ══════════════════════════════════════════════════
# USER MEMORY
# ══════════════════════════════════════════════════

def upsert_memory(key: str, value: str):
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO user_memory (key, value, updated_at) VALUES (?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """, (key, value, now))


def get_all_memory() -> dict:
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM user_memory").fetchall()
    return {r["key"]: r["value"] for r in rows}