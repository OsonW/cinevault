import sqlite3

DB_NAME = "movie_tracker.db"


def get_conn():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS media (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                tmdb_id        INTEGER NOT NULL,
                title          TEXT NOT NULL,
                media_type     TEXT NOT NULL,
                status         TEXT NOT NULL DEFAULT 'watchlist',
                rating         REAL,
                last_timestamp TEXT,
                last_season    INTEGER,
                last_episode   INTEGER,
                notes          TEXT,
                date_added     TEXT DEFAULT (date('now')),
                UNIQUE(tmdb_id, media_type)
            )
        """)
        # Non-destructive migrations for older databases that predate the
        # UNIQUE constraint or missing columns. The constraint itself can't
        # be added retroactively via ALTER TABLE in SQLite, but new inserts
        # will use INSERT OR IGNORE so duplicates are silently skipped.
        for col, defn in [
            ("date_added",   "TEXT DEFAULT (date('now'))"),
            ("last_season",  "INTEGER"),
            ("last_episode", "INTEGER"),
        ]:
            try:
                conn.execute(f"ALTER TABLE media ADD COLUMN {col} {defn}")
            except sqlite3.OperationalError:
                pass  # column already exists


def row_to_dict(row):
    return dict(row) if row is not None else None


# ── Read ──────────────────────────────────────────

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


# ── Write ─────────────────────────────────────────

def add_media_entry(tmdb_id, title, media_type, status="watchlist"):
    """
    Insert a new entry. Uses INSERT OR IGNORE so calling this with an already-
    tracked (tmdb_id, media_type) pair is a safe no-op.
    Returns the row id of the existing or newly-created row.
    """
    with get_conn() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO media (tmdb_id, title, media_type, status)
            VALUES (?, ?, ?, ?)
        """, (tmdb_id, title, media_type, status))
        row = conn.execute(
            "SELECT id FROM media WHERE tmdb_id = ? AND media_type = ?",
            (tmdb_id, media_type),
        ).fetchone()
    return row["id"] if row else None


# Columns that callers are permitted to update.
_UPDATABLE = frozenset({
    "status", "rating", "last_timestamp",
    "last_season", "last_episode", "notes",
})


def update_media_entry(media_id: int, **fields):
    """
    Update only the fields that were explicitly passed by the caller.
    Falsy values (0, "", 0.0) are written — only keys absent from `fields`
    are skipped.  Pass a key with value None to explicitly clear a field.
    """
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