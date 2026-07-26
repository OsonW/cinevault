import os
import sqlite3


def _get_db_dir() -> str:
    return os.environ.get("DB_DIR", ".")


def get_user_db_path(user_id: int) -> str:
    return os.path.join(_get_db_dir(), f"movie_tracker_{user_id}.db")


def get_conn(db_path: str | None = None):
    """Open a connection. Pass db_path explicitly (e.g. in init) or rely on
    flask.g.user_db_path which before_request sets for every authenticated request."""
    if db_path is None:
        from flask import g
        db_path = g.user_db_path
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_user_db(user_id: int) -> None:
    _create_tables(get_user_db_path(user_id))


def _create_tables(db_path: str) -> None:
    with get_conn(db_path) as conn:
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
                length          TEXT,
                notes           TEXT,
                cover_url       TEXT,
                author          TEXT,
                overview        TEXT,
                year            TEXT,
                ratings         TEXT,
                ratings_updated_at TEXT,
                tmdb_rating     REAL,
                date_added      TEXT DEFAULT (date('now')),
                date_watchlist  TEXT,
                date_watching   TEXT,
                date_finished   TEXT,
                UNIQUE(external_id, media_type)
            )
        """)
        # Small key/value store for per-user counters (e.g. the auto-refresh cap),
        # persisted so they survive restarts and are shared across worker processes.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS app_meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        _migrate_media_dates(conn)


def _migrate_media_dates(conn) -> None:
    """Add later columns to pre-existing DBs. For the per-status date columns,
    seed each row's current-status date from its legacy date_added so nothing is
    lost. The ratings columns just need to exist (no seeding)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(media)").fetchall()}
    dates_added = False
    for col in ("date_watchlist", "date_watching", "date_finished"):
        if col not in cols:
            conn.execute(f"ALTER TABLE media ADD COLUMN {col} TEXT")
            dates_added = True
    for col in ("ratings", "ratings_updated_at"):
        if col not in cols:
            conn.execute(f"ALTER TABLE media ADD COLUMN {col} TEXT")
    if "tmdb_rating" not in cols:
        conn.execute("ALTER TABLE media ADD COLUMN tmdb_rating REAL")
    # Display-ready length ("1h 43m" / "4 Seasons" / "91 Chapters" / "512 Pages").
    # Persisted rather than fetched per detail-panel open so it ships with /api/list
    # and paints with the rest of the row instead of popping in a beat later.
    if "length" not in cols:
        conn.execute("ALTER TABLE media ADD COLUMN length TEXT")
    if dates_added and "date_added" in cols:
        conn.execute("UPDATE media SET date_watchlist = date_added WHERE status='watchlist' AND date_watchlist IS NULL")
        conn.execute("UPDATE media SET date_watching  = date_added WHERE status='watching'  AND date_watching  IS NULL")
        conn.execute("UPDATE media SET date_finished  = date_added WHERE status='finished'  AND date_finished  IS NULL")


def row_to_dict(row):
    return dict(row) if row is not None else None


def _user_db_missing() -> bool:
    """True when the current user's media DB file doesn't exist yet (keyless/abandoned
    account). Read helpers check this so a SELECT never makes sqlite3.connect() create
    the file on disk — storage is only allocated once a key is provided."""
    from flask import g
    db_path = getattr(g, "user_db_path", None)
    return not db_path or not os.path.exists(db_path)


def get_all_media():
    if _user_db_missing():
        return []
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM media ORDER BY title").fetchall()
    return [row_to_dict(r) for r in rows]


def get_media_by_external_id(external_id: str, media_type: str):
    if _user_db_missing():
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM media WHERE external_id = ? AND media_type = ?",
            (external_id, media_type),
        ).fetchone()
    return row_to_dict(row)


def get_media_by_id(media_id: int):
    if _user_db_missing():
        return None
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
    tmdb_rating=None, length=None,
):
    ext = external_id or (str(tmdb_id) if tmdb_id else None)
    date_col = {
        "watchlist": "date_watchlist",
        "watching":  "date_watching",
        "finished":  "date_finished",
    }.get(status, "date_watchlist")
    with get_conn() as conn:
        conn.execute(f"""
            INSERT OR IGNORE INTO media
                (tmdb_id, external_id, title, media_type, status,
                 cover_url, author, total_pages, overview, year, tmdb_rating,
                 length, {date_col})
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, date('now'))
        """, (tmdb_id, ext, title, media_type, status,
              cover_url, author, total_pages, overview, year, tmdb_rating, length))
        row = conn.execute(
            "SELECT id FROM media WHERE external_id = ? AND media_type = ?",
            (ext, media_type),
        ).fetchone()
    return row["id"] if row else None


_UPDATABLE = frozenset({
    "status", "rating", "last_timestamp",
    "last_season", "last_episode", "last_chapter",
    "current_page", "total_pages",
    "notes", "cover_url", "author", "overview", "length",
})

# Changing any of these while an item is in "watching" counts as progress.
_PROGRESS_FIELDS = frozenset({
    "last_timestamp", "last_season", "last_episode",
    "last_chapter", "current_page",
})

_STATUS_DATE_COL = {
    "watchlist": "date_watchlist",
    "watching":  "date_watching",
    "finished":  "date_finished",
}


def update_media_entry(media_id: int, **fields):
    """Update whitelisted fields, and maintain the per-status "date added":
      • watchlist — set once on first entry, then preserved forever
      • watching  — set on first entry, refreshed only when progress is made
      • finished  — refreshed to today every time the item (re-)enters finished
    `date_added` always mirrors the date of the item's current status (what the UI shows).
    """
    updates = {k: v for k, v in fields.items() if k in _UPDATABLE}
    if not updates:
        return
    with get_conn() as conn:
        row = conn.execute(
            "SELECT status, date_watchlist, date_watching, date_finished FROM media WHERE id = ?",
            (media_id,),
        ).fetchone()
        if row is None:
            return
        prev_status = row["status"]
        new_status  = updates.get("status", prev_status)

        # Which per-status date columns should refresh to today.
        touch_today = set()
        if new_status != prev_status:
            if new_status == "watchlist" and not row["date_watchlist"]:
                touch_today.add("date_watchlist")
            elif new_status == "watching" and not row["date_watching"]:
                touch_today.add("date_watching")
            elif new_status == "finished":
                touch_today.add("date_finished")          # finished always refreshes
        if new_status == "watching" and any(k in updates for k in _PROGRESS_FIELDS):
            touch_today.add("date_watching")              # progress bumps the watching date

        set_parts = [f"{k} = ?" for k in updates]
        params    = list(updates.values())
        for col in touch_today:
            set_parts.append(f"{col} = date('now')")

        # Keep date_added in sync with the current status's date.
        status_col = _STATUS_DATE_COL[new_status]
        if status_col in touch_today:
            set_parts.append("date_added = date('now')")
        else:
            set_parts.append(f"date_added = COALESCE({status_col}, date_added)")

        conn.execute(
            f"UPDATE media SET {', '.join(set_parts)} WHERE id = ?",
            params + [media_id],
        )


def set_media_lengths(updates) -> None:
    """Bulk-write backfilled lengths (and, for books, the page count they came from).

    Deliberately not routed through update_media_entry: this is a derived-metadata
    write, not a user edit, so it must never touch the per-status date columns —
    a backfill sweep would otherwise look like activity on every row it touched.
    `updates` is an iterable of (media_id, length, total_pages); total_pages may be
    None for non-books, leaving that column alone.
    """
    rows = list(updates)
    if not rows:
        return
    with get_conn() as conn:
        for media_id, length, total_pages in rows:
            if total_pages is None:
                conn.execute(
                    "UPDATE media SET length = ? WHERE id = ?", (length, media_id)
                )
            else:
                conn.execute(
                    "UPDATE media SET length = ?, total_pages = ? WHERE id = ?",
                    (length, total_pages, media_id),
                )


_DATE_COLS = frozenset({"date_added", "date_watchlist", "date_watching", "date_finished"})


def set_media_dates(media_id: int, **dates):
    """Directly set the (already-validated) date columns — used by undo/restore to
    bring back an item's original per-status dates instead of today's."""
    cols = {k: v for k, v in dates.items() if k in _DATE_COLS and v is not None}
    if not cols:
        return
    clauses = ", ".join(f"{k} = ?" for k in cols)
    with get_conn() as conn:
        conn.execute(
            f"UPDATE media SET {clauses} WHERE id = ?",
            list(cols.values()) + [media_id],
        )


def set_media_ratings(media_id: int, ratings_json: str, updated_at: str) -> None:
    """Persist a pre-serialised JSON ratings blob and its fetch timestamp."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE media SET ratings = ?, ratings_updated_at = ? WHERE id = ?",
            (ratings_json, updated_at, media_id),
        )


def set_media_tmdb_rating(media_id: int, value: float | None) -> None:
    """Persist the TMDB vote_average for a library row (free; no MDBList quota)."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE media SET tmdb_rating = ? WHERE id = ?",
            (value, media_id),
        )


def delete_media_entry(media_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM media WHERE id = ?", (media_id,))


# ── Per-user auto-refresh cap (persisted in the user's DB) ──────────────────────
# Stored in app_meta so the cap survives restarts and is shared across worker
# processes. The conditional UPDATE is atomic under SQLite's write lock, so two
# concurrent requests can't both grab the last slot.

def take_lazy_refresh_slot(cap: int) -> bool:
    """Atomically consume one automatic-refresh slot for the current user. Returns
    True (and increments) if under `cap`, else False once the cap is reached."""
    with get_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO app_meta (key, value) VALUES ('lazy_refresh_count', '0')")
        cur = conn.execute(
            "UPDATE app_meta SET value = CAST(value AS INTEGER) + 1 "
            "WHERE key = 'lazy_refresh_count' AND CAST(value AS INTEGER) < ?",
            (cap,),
        )
        return cur.rowcount == 1


def get_lazy_refresh_count() -> int:
    """Current per-user auto-refresh slot count (0 if never set)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM app_meta WHERE key = 'lazy_refresh_count'"
        ).fetchone()
    try:
        return int(row["value"]) if row and row["value"] is not None else 0
    except (TypeError, ValueError):
        return 0


def sync_lazy_cap_to_quota(used: int) -> None:
    """Track the MDBList used-count; when it drops below the last seen value (i.e.
    the daily quota reset), reset the per-user auto-refresh slot counter so the cap
    resets in lockstep with the quota."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM app_meta WHERE key = 'mdblist_last_used'"
        ).fetchone()
        prev = None
        if row and row["value"] is not None:
            try:
                prev = int(row["value"])
            except (TypeError, ValueError):
                prev = None
        if prev is not None and used < prev:
            conn.execute("INSERT OR IGNORE INTO app_meta (key, value) VALUES ('lazy_refresh_count', '0')")
            conn.execute("UPDATE app_meta SET value = '0' WHERE key = 'lazy_refresh_count'")
        conn.execute("INSERT OR IGNORE INTO app_meta (key, value) VALUES ('mdblist_last_used', ?)", (str(used),))
        conn.execute("UPDATE app_meta SET value = ? WHERE key = 'mdblist_last_used'", (str(used),))
