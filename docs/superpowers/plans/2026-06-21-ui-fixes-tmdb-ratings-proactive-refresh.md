# UI Fixes, TMDB Ratings & Proactive Refresh — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship six independent improvements to CineVault: fix the iOS refresh
resize flash, harden auth autofill, polish rating display, add a free TMDB rating
pill, enlarge dropdown carets, and add an in-browser MDBList refresh model
(manual button + "updated" label + 7-day auto with a 500/day lazy cap).

**Architecture:** Flask backend (`app.py`, `db.py`) with per-user SQLite; a single
large template (`templates/index.html`) holding all app JS/CSS, and a separate
`templates/login.html`. Ratings come from MDBList (BYOK, quota-limited) and now
also TMDB `vote_average` (free). Backend changes are test-driven; the
template/JS/CSS changes are verified manually with exact code provided.

**Tech Stack:** Python 3 / Flask, SQLite, pytest, vanilla JS + CSS.

**Spec:** `docs/superpowers/specs/2026-06-21-ui-fixes-tmdb-ratings-proactive-refresh-design.md`

**Run tests with:** `python -m pytest tests/test_ratings.py -v`

---

## Task 1: DB — `tmdb_rating` column, migration, setter, add path

**Files:**
- Modify: `db.py` (table DDL, `_migrate_media_dates`, `add_media_entry`, new `set_media_tmdb_rating`)
- Test: `tests/test_ratings.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_ratings.py`:

```python
def test_media_row_has_tmdb_rating_column(client):
    _register(client)
    _add_movie(client)
    item = client.get("/api/list").get_json()[0]
    assert "tmdb_rating" in item
    assert item["tmdb_rating"] is None


def test_add_movie_persists_tmdb_rating(client):
    _register(client)
    client.post("/api/add", json={
        "title": "Inception", "media_type": "movie",
        "external_id": "27205", "tmdb_id": 27205, "status": "watchlist",
        "tmdb_rating": 8.4,
    })
    item = client.get("/api/list").get_json()[0]
    assert item["tmdb_rating"] == 8.4


def test_set_media_tmdb_rating_roundtrip(client):
    _register(client)
    _add_movie(client)
    item = client.get("/api/list").get_json()[0]
    from flask import g
    with flask_app.test_request_context():
        from db import get_user_db_path
        from users_db import get_user_by_username
        uid = get_user_by_username("alice")["id"]
        g.user_db_path = get_user_db_path(uid)
        db.set_media_tmdb_rating(item["id"], 7.2)
        row = db.get_media_by_id(item["id"])
    assert row["tmdb_rating"] == 7.2


def test_migration_adds_tmdb_rating_to_legacy_db(tmp_path):
    db_path = str(tmp_path / "movie_tracker_legacy2.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute("""CREATE TABLE media (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
            media_type TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'watchlist',
            external_id TEXT, ratings TEXT, ratings_updated_at TEXT,
            UNIQUE(external_id, media_type))""")
    db._create_tables(db_path)
    with sqlite3.connect(db_path) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(media)").fetchall()}
    assert "tmdb_rating" in cols
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `python -m pytest tests/test_ratings.py -k "tmdb_rating" -v`
Expected: FAIL (column/function missing).

- [ ] **Step 3: Add the column to the table DDL**

In `db.py` `_create_tables`, inside the `CREATE TABLE` column list, add `tmdb_rating` after the `ratings_updated_at` line:

```python
                ratings         TEXT,
                ratings_updated_at TEXT,
                tmdb_rating     REAL,
                date_added      TEXT DEFAULT (date('now')),
```

- [ ] **Step 4: Extend the migration**

In `db.py` `_migrate_media_dates`, change the ratings-columns loop to also add `tmdb_rating`:

```python
    for col in ("ratings", "ratings_updated_at"):
        if col not in cols:
            conn.execute(f"ALTER TABLE media ADD COLUMN {col} TEXT")
    if "tmdb_rating" not in cols:
        conn.execute("ALTER TABLE media ADD COLUMN tmdb_rating REAL")
```

- [ ] **Step 5: Thread `tmdb_rating` through `add_media_entry`**

In `db.py` `add_media_entry`, add the parameter and include it in the INSERT:

```python
def add_media_entry(
    title, media_type, status="watchlist",
    tmdb_id=None, external_id=None,
    cover_url=None, author=None,
    total_pages=None, overview=None, year=None,
    tmdb_rating=None,
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
                 cover_url, author, total_pages, overview, year, tmdb_rating, {date_col})
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, date('now'))
        """, (tmdb_id, ext, title, media_type, status,
              cover_url, author, total_pages, overview, year, tmdb_rating))
        row = conn.execute(
            "SELECT id FROM media WHERE external_id = ? AND media_type = ?",
            (ext, media_type),
        ).fetchone()
    return row["id"] if row else None
```

- [ ] **Step 6: Add `set_media_tmdb_rating`**

In `db.py`, after `set_media_ratings`:

```python
def set_media_tmdb_rating(media_id: int, value) -> None:
    """Persist the TMDB vote_average for a library row (free; no MDBList quota)."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE media SET tmdb_rating = ? WHERE id = ?",
            (value, media_id),
        )
```

- [ ] **Step 7: Run tests, verify pass**

Run: `python -m pytest tests/test_ratings.py -k "tmdb_rating" -v`
Expected: PASS (4 tests). Also run the full file to confirm no regressions:
`python -m pytest tests/test_ratings.py -v`

- [ ] **Step 8: Commit**

```bash
git add db.py tests/test_ratings.py
git commit -m "feat(db): add tmdb_rating column, migration, setter and add path"
```

---

## Task 2: Backend — search payload carries TMDB rating; `/api/add` accepts it

**Files:**
- Modify: `app.py` (`search()` movie + tv item dicts, `add_media()` create branch)
- Test: `tests/test_ratings.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_ratings.py`:

```python
def test_search_includes_tmdb_rating(client, monkeypatch):
    _register(client)
    _set_mdblist_key()  # ensures a tmdb key exists for the search route

    class FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"results": [
                {"id": 603, "title": "The Matrix", "release_date": "1999-03-30",
                 "poster_path": "/x.jpg", "overview": "o", "popularity": 9,
                 "vote_average": 8.2},
            ]}

    monkeypatch.setattr(app_module.requests, "get", lambda *a, **k: FakeResp())
    monkeypatch.setattr(app_module, "_fetch_tmdb_director", lambda *a, **k: "")
    data = client.get("/api/search?q=matrix&type=movie").get_json()
    assert data[0]["tmdb_rating"] == 8.2
```

Note: `_set_mdblist_key` seeds a tmdb key (`"tmdbkey"`) via its existing
implementation, satisfying `_get_tmdb_key()`.

- [ ] **Step 2: Run test, verify fail**

Run: `python -m pytest tests/test_ratings.py::test_search_includes_tmdb_rating -v`
Expected: FAIL (KeyError `tmdb_rating`).

- [ ] **Step 3: Add `tmdb_rating` to both search item dicts**

In `app.py` `search()`, add `"tmdb_rating": r.get("vote_average"),` to the movie
item dict (after `"popularity"`) and the identical line to the tv item dict:

```python
                    "overview":    r.get("overview"),
                    "popularity":  r.get("popularity", 0) or 0,
                    "tmdb_rating": r.get("vote_average"),
                }
```

(Apply to both the `media_type == "movie"` block and the `else` tv block.)

- [ ] **Step 4: Accept `tmdb_rating` in `/api/add` create branch**

In `app.py` `add_media()`, in the `else:` (create) branch, parse and pass it.
Add this just before the `add_media_entry(` call:

```python
        tmdb_rating = data.get("tmdb_rating")
        if tmdb_rating is not None:
            try:
                tmdb_rating = float(tmdb_rating)
            except (TypeError, ValueError):
                tmdb_rating = None
```

Then add `tmdb_rating = tmdb_rating,` to the `add_media_entry(...)` call args:

```python
        add_media_entry(
            title       = title,
            media_type  = media_type,
            status      = status,
            tmdb_id     = data.get("tmdb_id"),
            external_id = data.get("external_id"),
            cover_url   = cover_url,
            author      = data.get("author"),
            total_pages = data.get("total_pages"),
            overview    = data.get("overview"),
            year        = data.get("year"),
            tmdb_rating = tmdb_rating,
        )
```

- [ ] **Step 5: Run test, verify pass**

Run: `python -m pytest tests/test_ratings.py::test_search_includes_tmdb_rating -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app.py tests/test_ratings.py
git commit -m "feat(search): expose TMDB vote_average and persist on add"
```

---

## Task 3: Backend — `/api/tmdb-rating` endpoint (free backfill)

**Files:**
- Modify: `app.py` (new `_fetch_tmdb_rating`, new route, import `set_media_tmdb_rating`)
- Test: `tests/test_ratings.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_ratings.py`:

```python
def test_tmdb_rating_endpoint_persists_library_item(client, monkeypatch):
    _register(client)
    _set_mdblist_key()
    _add_movie(client, external_id="27205")
    monkeypatch.setattr(app_module, "_fetch_tmdb_rating", lambda *a, **k: 8.4)
    resp = client.get("/api/tmdb-rating/movie/27205").get_json()
    assert resp == {"tmdb": 8.4}
    item = client.get("/api/list").get_json()[0]
    assert item["tmdb_rating"] == 8.4


def test_tmdb_rating_endpoint_uses_stored_value(client, monkeypatch):
    _register(client)
    _set_mdblist_key()
    client.post("/api/add", json={
        "title": "Inception", "media_type": "movie", "external_id": "27205",
        "tmdb_id": 27205, "status": "watchlist", "tmdb_rating": 7.0})
    calls = {"n": 0}
    def spy(*a, **k):
        calls["n"] += 1
        return 9.9
    monkeypatch.setattr(app_module, "_fetch_tmdb_rating", spy)
    resp = client.get("/api/tmdb-rating/movie/27205").get_json()
    assert resp == {"tmdb": 7.0}
    assert calls["n"] == 0  # stored value served, no fetch


def test_tmdb_rating_endpoint_no_tmdb_key(client):
    _register(client)  # no keys set -> _get_tmdb_key() is None
    resp = client.get("/api/tmdb-rating/movie/27205")
    assert resp.status_code == 200
    assert resp.get_json() == {"tmdb": None}
```

- [ ] **Step 2: Run tests, verify fail**

Run: `python -m pytest tests/test_ratings.py -k tmdb_rating_endpoint -v`
Expected: FAIL (404 / route missing).

- [ ] **Step 3: Import the setter**

In `app.py`, find the `from db import (...)` block that includes
`set_media_ratings,` and add `set_media_tmdb_rating,` next to it.

- [ ] **Step 4: Add the fetch helper**

In `app.py`, after `_fetch_mdblist_ratings` (near line 156), add:

```python
# TMDB rating (vote_average) for non-library titles. Keyed "media_type:tmdb_id".
_tmdb_rating_cache: dict[str, tuple[float, object]] = {}
_TMDB_RATING_TTL = 24 * 3600  # seconds


def _fetch_tmdb_rating(media_type: str, tmdb_id, api_key: str):
    """Return the TMDB vote_average (float) or None. Free; no MDBList quota."""
    if media_type not in ("movie", "tv") or not tmdb_id:
        return None
    try:
        endpoint = "movie" if media_type == "movie" else "tv"
        url = f"https://api.themoviedb.org/3/{endpoint}/{tmdb_id}"
        data = requests.get(url, params=_tmdb_params(api_key), timeout=5).json()
        val = data.get("vote_average")
        return float(val) if isinstance(val, (int, float)) and val else None
    except Exception:
        return None
```

- [ ] **Step 5: Add the route**

In `app.py`, immediately after the `api_mdblist_status` function (near line 814),
add:

```python
@app.route("/api/tmdb-rating/<media_type>/<tmdb_id>")
@login_required
def api_tmdb_rating(media_type, tmdb_id):
    """Free TMDB vote_average. Persists for library rows; TTL-caches others."""
    if media_type not in ("movie", "tv"):
        return jsonify({"tmdb": None})
    key = _get_tmdb_key()
    if not key:
        return jsonify({"tmdb": None})

    row = get_media_by_external_id(str(tmdb_id), media_type)
    if row:
        if row.get("tmdb_rating") is not None:
            return jsonify({"tmdb": row["tmdb_rating"]})
        val = _fetch_tmdb_rating(media_type, tmdb_id, key)
        if val is not None:
            set_media_tmdb_rating(row["id"], val)
        return jsonify({"tmdb": val})

    ck = f"{media_type}:{tmdb_id}"
    hit = _tmdb_rating_cache.get(ck)
    if hit and (time.time() - hit[0]) < _TMDB_RATING_TTL:
        return jsonify({"tmdb": hit[1]})
    val = _fetch_tmdb_rating(media_type, tmdb_id, key)
    if len(_tmdb_rating_cache) > 2000:
        _tmdb_rating_cache.pop(next(iter(_tmdb_rating_cache)))
    _tmdb_rating_cache[ck] = (time.time(), val)
    return jsonify({"tmdb": val})
```

- [ ] **Step 6: Run tests, verify pass**

Run: `python -m pytest tests/test_ratings.py -k tmdb_rating_endpoint -v`
Expected: PASS (3 tests).

- [ ] **Step 7: Commit**

```bash
git add app.py tests/test_ratings.py
git commit -m "feat(api): add free /api/tmdb-rating backfill endpoint"
```

---

## Task 4: Backend — `/api/ratings` force mode, `updated_at`, daily lazy cap

**Files:**
- Modify: `app.py` (`api_ratings`, new cap helper + globals)
- Test: `tests/test_ratings.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_ratings.py`:

```python
def _store_stale_ratings(client, external_id="27205"):
    """Add a movie and stamp it with old MDBList ratings (>7 days)."""
    _add_movie(client, external_id=external_id)
    item = client.get("/api/list").get_json()[0]
    from flask import g
    with flask_app.test_request_context():
        from db import get_user_db_path
        from users_db import get_user_by_username
        uid = get_user_by_username("alice")["id"]
        g.user_db_path = get_user_db_path(uid)
        db.set_media_ratings(item["id"], json.dumps({"imdb": 5.0}),
                             "2020-01-01T00:00:00+00:00")
    return item


def test_take_lazy_refresh_slot_caps_and_resets(monkeypatch):
    import app as a
    monkeypatch.setattr(a, "_LAZY_REFRESH_DAILY_CAP", 2)
    a._lazy_refresh_counts.clear()
    assert a._take_lazy_refresh_slot(1) is True
    assert a._take_lazy_refresh_slot(1) is True
    assert a._take_lazy_refresh_slot(1) is False      # cap hit
    a._lazy_refresh_counts[1] = ("1999-01-01", 2)     # simulate old day
    assert a._take_lazy_refresh_slot(1) is True        # rolls over, resets


def test_ratings_force_refreshes_and_returns_updated_at(client, monkeypatch):
    _register(client)
    _set_mdblist_key()
    _store_stale_ratings(client)
    monkeypatch.setattr(app_module, "_fetch_mdblist_ratings",
                        lambda *a, **k: {"imdb": 9.0})
    resp = client.get("/api/ratings/movie/27205?force=1").get_json()
    assert resp["ratings"] == {"imdb": 9.0}
    assert resp["updated_at"]   # present and truthy


def test_ratings_lazy_cap_serves_stale_without_fetch(client, monkeypatch):
    _register(client)
    _set_mdblist_key()
    _store_stale_ratings(client)
    calls = {"n": 0}
    def spy(*a, **k):
        calls["n"] += 1
        return {"imdb": 9.0}
    monkeypatch.setattr(app_module, "_fetch_mdblist_ratings", spy)
    monkeypatch.setattr(app_module, "_take_lazy_refresh_slot", lambda uid: False)
    resp = client.get("/api/ratings/movie/27205").get_json()
    assert resp["ratings"] == {"imdb": 5.0}   # old stored value
    assert calls["n"] == 0                     # cap blocked the fetch
```

- [ ] **Step 2: Run tests, verify fail**

Run: `python -m pytest tests/test_ratings.py -k "lazy or force or updated_at" -v`
Expected: FAIL (missing helper / keys).

- [ ] **Step 3: Add cap globals + helper**

In `app.py`, near the other ratings globals (after `_MDBLIST_STATUS_TTL`, ~line 171), add:

```python
# Per-user daily cap on AUTOMATIC (7-day-on-access) ratings refreshes. Manual
# force refreshes are exempt. In-memory; resets when the UTC date rolls over.
_LAZY_REFRESH_DAILY_CAP = 500
_lazy_refresh_counts: dict[int, tuple[str, int]] = {}


def _take_lazy_refresh_slot(uid: int) -> bool:
    """True (and consume a slot) if the user is under today's lazy-refresh cap."""
    today = datetime.now(timezone.utc).date().isoformat()
    day, count = _lazy_refresh_counts.get(uid, (today, 0))
    if day != today:
        day, count = today, 0
    if count >= _LAZY_REFRESH_DAILY_CAP:
        _lazy_refresh_counts[uid] = (day, count)
        return False
    _lazy_refresh_counts[uid] = (day, count + 1)
    return True
```

- [ ] **Step 4: Rewrite the library branch of `api_ratings`**

In `app.py` `api_ratings`, replace the library branch (the `if row:` block) with:

```python
    force = request.args.get("force") == "1"
    uid = int(current_user.id)

    # Library item? Serve/refresh persisted ratings.
    row = get_media_by_external_id(str(tmdb_id), media_type)
    if row:
        stored = {}
        if row.get("ratings"):
            try:
                stored = json.loads(row["ratings"])
            except Exception:
                stored = {}
        fresh = False
        if row.get("ratings") and row.get("ratings_updated_at"):
            try:
                age = datetime.now(timezone.utc) - datetime.fromisoformat(row["ratings_updated_at"])
                fresh = age.days < _RATINGS_MAX_AGE_DAYS
            except Exception:
                fresh = False
        # Refresh when forced (manual button, exempt from cap) or stale-and-under-cap.
        if force or (not fresh and _take_lazy_refresh_slot(uid)):
            now = datetime.now(timezone.utc).isoformat()
            data = _fetch_mdblist_ratings(media_type, tmdb_id, key)
            set_media_ratings(row["id"], json.dumps(data), now)
            return jsonify({"ratings": data, "updated_at": now})
        # Fresh, or cap hit: serve what we have.
        return jsonify({"ratings": stored, "updated_at": row.get("ratings_updated_at")})
```

Leave the non-library (search result) branch below unchanged.

- [ ] **Step 5: Run tests, verify pass**

Run: `python -m pytest tests/test_ratings.py -k "lazy or force or updated_at" -v`
Expected: PASS. Then run the whole file:
`python -m pytest tests/test_ratings.py -v` — all green.

- [ ] **Step 6: Commit**

```bash
git add app.py tests/test_ratings.py
git commit -m "feat(api): ratings force mode, updated_at, 500/day lazy cap"
```

---

## Task 5: Frontend — rating display (`Unrated` / no-decimal-10)

**Files:**
- Modify: `templates/index.html` (`renderDetail` rating block; add `fmtRating`)

- [ ] **Step 1: Add the helper**

In `templates/index.html`, just before `function renderDetail(item) {`, add:

```javascript
// 0 -> "Unrated"; otherwise drop a trailing .0 (10.0 -> "10", 7.5 -> "7.5").
function fmtRating(r) {
  const n = Number(r) || 0;
  if (n <= 0) return 'Unrated';
  return (Math.round(n * 10) % 10 === 0) ? String(Math.round(n)) : n.toFixed(1);
}
function ratingLabel(r) {
  const n = Number(r) || 0;
  return n <= 0 ? 'Unrated' : fmtRating(n) + ' / 10';
}
```

- [ ] **Step 2: Use it in the rating block**

In `renderDetail`, replace the `ratingHtml` definition:

```javascript
  const ratingHtml = item.status === 'finished' ? `
    <div class="rating-wrap">
      <div class="rating-label">Your rating
        <span class="rating-val" id="rv">${ratingLabel(item.rating)}</span>
      </div>
      <input type="range" min="0" max="10" step="0.1" value="${item.rating||0}"
             oninput="document.getElementById('rv').textContent=ratingLabel(this.value)"
             onchange="saveField(${item.id},'rating',parseFloat(this.value))">
    </div>` : '';
```

- [ ] **Step 3: Manual verification**

Run the app: `python app.py` (or the project's run command). Open a finished
title. Verify:
- rating `0` shows **"Unrated"** (slider at far left).
- drag to `10` → shows **"10 / 10"** (no `.0`).
- drag to `7.5` → shows **"7.5 / 10"**.
Stop the app.

- [ ] **Step 4: Commit**

```bash
git add templates/index.html
git commit -m "feat(ui): show Unrated at 0 and drop trailing .0 on rating"
```

---

## Task 6: Frontend — TMDB rating pill

**Files:**
- Modify: `templates/index.html` (pill constants, `renderPills`, `_pillValue`,
  `hydratePills`, `.spill-tmdb` CSS, `addFromModal` body)

- [ ] **Step 1: Register the pill in the constants**

In `templates/index.html`, update the pill constant block:

```javascript
const PILL_ORDER  = ['year','type','tmdb','imdb','tomatoes','audience','metacritic','letterboxd','mal'];
const SCORE_SOURCES = ['imdb','tomatoes','audience','metacritic','letterboxd','mal'];
const PILL_LABELS = { year:'Year', type:'Media type', tmdb:'TMDB', imdb:'IMDb', tomatoes:'Tomatometer',
  audience:'Popcornmeter', metacritic:'Metacritic', letterboxd:'Letterboxd', mal:'MyAnimeList' };
const DEFAULT_PILLS = ['year','type','tmdb','imdb','metacritic','tomatoes'];
```

(Keep `SCORE_SOURCES` without `tmdb` — TMDB is local data, not an MDBList score.)

- [ ] **Step 2: Add `tmdb` to the TMDB selector section**

Update `PILL_SECTIONS` so the TMDB section lists `tmdb` under year/type:

```javascript
const PILL_SECTIONS = [
  { label: 'TMDB',    note: '',              sources: ['year','type','tmdb'],                                         limited: false },
  { label: 'MDBList', note: '(resets daily)', sources: ['imdb','tomatoes','audience','metacritic','letterboxd','mal'], limited: true  },
];
```

- [ ] **Step 3: Render the pill from local item data**

In `renderPills`, add a `tmdb` case alongside `year`/`type` (it reads
`item.tmdb_rating`, not the MDBList `ratings` dict). Insert after the `type` case:

```javascript
    if (src === 'tmdb') {
      const tv = item.tmdb_rating;
      if (tv !== undefined && tv !== null && Number(tv) > 0)
        out.push(`<span class="spill spill-tmdb"><b>TMDB</b> ${Number(tv).toFixed(1)}</span>`);
      continue;
    }
```

- [ ] **Step 4: Add the pill style**

In the CSS (after the `.spill-mal` rule near line 757), add:

```css
.spill-tmdb { background:rgba(1,180,134,0.16); color:#3fd0a6; border-color:rgba(1,180,134,0.4); }
```

- [ ] **Step 5: Lazily backfill TMDB rating for library cards**

Replace `hydratePills` with a version that also backfills `tmdb_rating` via the
free endpoint when the pill is selected and the value is missing:

```javascript
// Lazily fill a pills container once ratings arrive.
function hydratePills(hostId, item, selected) {
  // Backfill TMDB rating (free) when selected and not already known.
  if (selected.includes('tmdb') && (item.tmdb_rating === undefined || item.tmdb_rating === null)) {
    const tid = item.tmdb_id || item.external_id;
    if (tid && (item.media_type === 'movie' || item.media_type === 'tv')) {
      fetch(`/api/tmdb-rating/${item.media_type}/${encodeURIComponent(tid)}`)
        .then(r => r.json()).then(d => {
          item.tmdb_rating = d.tmdb;
          const host = document.getElementById(hostId);
          if (host) host.innerHTML = renderPills(item, _ratingsCache[item.media_type+':'+tid] || {}, selected);
        }).catch(() => {});
    }
  }
  if (!selected.some(s => SCORE_SOURCES.includes(s))) return;
  const tmdbId = item.tmdb_id || item.external_id;
  if (!tmdbId || (item.media_type !== 'movie' && item.media_type !== 'tv')) return;
  fetchRatings(item.media_type, tmdbId).then(r => {
    const host = document.getElementById(hostId);
    if (host) host.innerHTML = renderPills(item, r, selected);
  });
}
```

- [ ] **Step 6: Send `tmdb_rating` when adding from search**

In `addFromModal`, add `tmdb_rating` to the `/api/add` body:

```javascript
      overview:    r.overview   || null,
      year:        r.year       || null,
      tmdb_rating: r.tmdb_rating ?? null,
```

- [ ] **Step 7: Pass search items' `tmdb_rating` to the pill renderer**

In `renderSmCard`, include `tmdb_rating` in `smItem`; and in `renderModalResults`'s
hydrate loop, include it in the constructed `item`. Update both object literals:

`renderSmCard`:
```javascript
  const smItem = { media_type: r.media_type, year: r.year, tmdb_id: r.tmdb_id, external_id: r.external_id, tmdb_rating: r.tmdb_rating };
```

`renderModalResults` hydrate loop:
```javascript
      const item = { media_type: r.media_type, year: r.year, tmdb_id: r.tmdb_id, external_id: r.external_id, tmdb_rating: r.tmdb_rating };
```

- [ ] **Step 8: Manual verification**

Run the app. With a TMDB key set:
- Search a movie → a green **`TMDB 8.2`** pill appears on result cards (on by default).
- Add it → the grid card and details panel show the same TMDB pill.
- Open the Filter dropdown (grid + search) → **TMDB** appears under Year/Media type
  in the TMDB section and toggles the pill on/off.
- An older library item with no stored value backfills its TMDB pill shortly after
  the grid renders.
Stop the app.

- [ ] **Step 9: Commit**

```bash
git add templates/index.html
git commit -m "feat(ui): add free TMDB rating pill (grid + search + selector)"
```

---

## Task 7: Frontend — iOS refresh resize flash fix

**Files:**
- Modify: `templates/index.html` (inline script in `<head>`)

- [ ] **Step 1: Add the synchronous rem script**

In `templates/index.html`, add this `<script>` in `<head>` **immediately after the
opening `<style>`'s preceding `<link>` tags and before `<style>`** (so it runs as
early as possible — placing it right after the viewport `<meta>` is ideal):

```html
<script>
// Set root font-size from clientWidth (not vw) to avoid the iOS first-paint
// flash where 100vw is briefly miscomputed. Mirrors the CSS clamp(12,100vw/120,22).
(function () {
  function setRem() {
    var w = document.documentElement.clientWidth || window.innerWidth || 0;
    var px = Math.max(12, Math.min(22, w / 120));
    document.documentElement.style.fontSize = px + 'px';
  }
  setRem();
  window.addEventListener('resize', setRem);
  window.addEventListener('orientationchange', setRem);
})();
</script>
```

The existing `html { font-size: clamp(12px, 100vw / 120, 22px); }` stays as the
no-JS fallback.

- [ ] **Step 2: Manual verification**

- Desktop: resize the browser window; the UI scales smoothly (font-size attr on
  `<html>` updates). No console errors.
- iOS (if available): refresh the page repeatedly; the UI should paint at the
  correct size immediately with no oversized bottom-right flash.

- [ ] **Step 3: Commit**

```bash
git add templates/index.html
git commit -m "fix(ios): set root rem from clientWidth to kill refresh resize flash"
```

---

## Task 8: Frontend — larger dropdown caret

**Files:**
- Modify: `templates/index.html` (add `.dd-caret` CSS; swap the three inline carets)

- [ ] **Step 1: Add the caret class**

In the CSS, after the `.size-dd-btn` rule (near line 629), add:

```css
.dd-caret { font-size: 0.8rem; opacity: 0.7; margin-left: 0.15em; }
```

- [ ] **Step 2: Replace the three inline carets**

Replace each occurrence of
`<span style="font-size:0.5625rem;opacity:0.7">▾</span>`
with
`<span class="dd-caret">▾</span>`

There are three: in `renderSearchFilter` ("Filter"), `renderGridControls`
("Filter"), and `renderPillSelector` (the legacy pills button). Use find/replace
to update all three.

- [ ] **Step 3: Manual verification**

Run the app. The `▾` on both grid and search **Filter** buttons is visibly larger.
Stop the app.

- [ ] **Step 4: Commit**

```bash
git add templates/index.html
git commit -m "style(ui): enlarge dropdown caret via shared .dd-caret class"
```

---

## Task 9: Frontend — details-panel manual refresh + "updated" label

**Files:**
- Modify: `templates/index.html` (`renderDetail` detail-scores block; new helpers
  `_updatedAgo`, `refreshRatingsNow`; small CSS)

- [ ] **Step 1: Add the relative-time + refresh helpers**

In `templates/index.html`, near `renderPills` (after `fetchRatings`), add:

```javascript
// "just now" (<60s) / "Xm ago" / "Xh ago" / "Xd ago" from an ISO timestamp.
function _updatedAgo(iso) {
  if (!iso) return '';
  let then;
  try { then = new Date(iso).getTime(); } catch { return ''; }
  if (!then) return '';
  const s = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (s < 60)      return 'just now';
  if (s < 3600)    return Math.floor(s / 60) + 'm ago';
  if (s < 86400)   return Math.floor(s / 3600) + 'h ago';
  return Math.floor(s / 86400) + 'd ago';
}

// Manual force-refresh of one title's MDBList ratings. Debounced: while the label
// still reads "just now" (<60s since last refresh) a click is a no-op.
async function refreshRatingsNow(itemId) {
  const item = allItems.find(i => i.id === itemId);
  if (!item) return;
  if (!_mdblist.has_key || _mdblist.exhausted) return;
  const tid = item.tmdb_id || item.external_id;
  if (!tid || (item.media_type !== 'movie' && item.media_type !== 'tv')) return;
  if (_updatedAgo(item.ratings_updated_at) === 'just now') return;  // 60s debounce
  const btn = document.getElementById('ratingsRefreshBtn');
  if (btn) btn.classList.add('spinning');
  try {
    const d = await fetch(`/api/ratings/${item.media_type}/${encodeURIComponent(tid)}?force=1`).then(r => r.json());
    const ratings = d.ratings || {};
    item.ratings = JSON.stringify(ratings);
    item.ratings_updated_at = d.updated_at || new Date().toISOString();
    _ratingsCache[item.media_type + ':' + tid] = ratings;
    refreshMdblistStatus();
    if (selectedId === itemId) renderDetail(item);
  } catch {}
}
```

- [ ] **Step 2: Render the refresh control + label in the detail panel**

In `renderDetail`, replace the `detail-scores` line with a scores block that
includes the refresh button + label (movie/tv with a key only):

```javascript
      <div class="detail-scores" id="detailScores">${renderPills(item, _seedRatings(item), SCORE_SOURCES)}</div>
      ${_ratingsMetaHtml(item)}
```

Then add these two helpers next to `renderDetail`:

```javascript
function _seedRatings(item) {
  const tid = item.tmdb_id || item.external_id;
  const k = item.media_type + ':' + tid;
  if (_ratingsCache[k]) return _ratingsCache[k];
  if (item.ratings) { try { return JSON.parse(item.ratings); } catch {} }
  return {};
}

function _ratingsMetaHtml(item) {
  if (!_mdblist.has_key) return '';
  if (item.media_type !== 'movie' && item.media_type !== 'tv') return '';
  const ago = _updatedAgo(item.ratings_updated_at);
  const label = ago ? `<span class="ratings-updated">Updated ${ago}</span>` : '';
  const dis = _mdblist.exhausted ? ' disabled' : '';
  return `<div class="ratings-meta">
    <button class="ratings-refresh${dis}" id="ratingsRefreshBtn" title="Refresh ratings"
            onclick="refreshRatingsNow(${item.id})"${dis}>
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>
    </button>${label}
  </div>`;
}
```

Note: `_seedRatings` replaces the previous inline
`_ratingsCache[item.media_type+':'+(item.tmdb_id||item.external_id)] || {}` so the
panel also seeds from the persisted `item.ratings` on first open.

- [ ] **Step 3: Add CSS for the refresh control**

In the CSS, after the `.detail-scores:empty` rule (near line 569), add:

```css
.ratings-meta { display:flex; align-items:center; gap:0.5rem; margin:-0.25rem 0 0.85rem; }
.ratings-refresh {
  display:inline-flex; align-items:center; justify-content:center;
  background:none; border:1px solid var(--border-md); border-radius:var(--radius);
  color:var(--muted); padding:0.25rem 0.4rem; cursor:pointer; transition:color 0.15s, border-color 0.15s;
}
.ratings-refresh:hover:not([disabled]) { color:var(--text); border-color:var(--accent); }
.ratings-refresh[disabled] { opacity:0.4; cursor:default; }
.ratings-refresh.spinning svg { animation:ratings-spin 0.8s linear infinite; }
@keyframes ratings-spin { to { transform:rotate(360deg); } }
.ratings-updated { font-size:0.95rem; color:var(--muted); }
```

- [ ] **Step 4: Manual verification**

Run the app with an MDBList key. Open a movie/tv title:
- A refresh ⟳ button and an **"Updated …"** label show under the score pills.
- Click ⟳ → pills refresh, label reads **"just now"**, icon spins briefly.
- Click ⟳ again immediately → nothing happens (debounced).
- Books/manga titles, or no-key state → no refresh control.
Stop the app.

- [ ] **Step 5: Commit**

```bash
git add templates/index.html
git commit -m "feat(ui): details-panel manual ratings refresh + updated-ago label"
```

---

## Task 10: Frontend — proactive launch refresh sweep

**Files:**
- Modify: `templates/index.html` (new `proactiveRefreshStale`; one-time hook in `loadList`)

Relies on Task 4's `/api/ratings` returning `updated_at` and enforcing the
500/day cap. The sweep detects cap exhaustion when a stale item's `updated_at`
fails to advance, and stops.

- [ ] **Step 1: Add the sweep function**

In `templates/index.html`, near `loadList` (after `rebuildLibrarySet`), add:

```javascript
let _didProactiveSweep = false;

// Once per browser launch: refresh MDBList ratings for movie/tv items that are
// missing or >=7 days old, oldest-first, until the server's 500/day cap is hit.
// Non-blocking; paced sequentially to respect the per-endpoint rate limit.
async function proactiveRefreshStale() {
  await refreshMdblistStatus();
  if (!_mdblist.has_key || _mdblist.exhausted) return;
  const WEEK = 7 * 86400 * 1000;
  const now  = Date.now();
  const ts   = i => (i.ratings_updated_at ? new Date(i.ratings_updated_at).getTime() || 0 : 0);
  const stale = allItems
    .filter(i => (i.media_type === 'movie' || i.media_type === 'tv')
              && (i.tmdb_id || i.external_id)
              && (!i.ratings_updated_at || (now - ts(i)) >= WEEK))
    .sort((a, b) => ts(a) - ts(b));   // oldest (and never-fetched = 0) first

  for (const item of stale) {
    const tid  = item.tmdb_id || item.external_id;
    const prev = item.ratings_updated_at || '';
    let d;
    try {
      d = await fetch(`/api/ratings/${item.media_type}/${encodeURIComponent(tid)}`).then(r => r.json());
    } catch { continue; }
    if (!d) continue;
    const ratings = d.ratings || {};
    item.ratings = JSON.stringify(ratings);
    item.ratings_updated_at = d.updated_at || prev;
    _ratingsCache[item.media_type + ':' + tid] = ratings;
    // updated_at didn't advance -> server served stale (cap reached) -> stop.
    if (!d.updated_at || d.updated_at === prev) break;
    // Repaint this card's pills + the detail panel if it's the open one.
    const host = document.getElementById('pills-' + item.id);
    if (host) host.innerHTML = renderPills(item, ratings, gridPills);
    if (selectedId === item.id) renderDetail(item);
  }
  refreshMdblistStatus();
}
```

- [ ] **Step 2: Trigger it once from `loadList`**

Replace `loadList` so it fires the sweep exactly once per launch:

```javascript
async function loadList() {
  const res = await fetch('/api/list');
  allItems = await res.json();
  rebuildLibrarySet();
  renderGrid();
  if (!_didProactiveSweep) {
    _didProactiveSweep = true;
    proactiveRefreshStale();   // fire-and-forget; runs once per page load
  }
}
```

- [ ] **Step 3: Manual verification**

Run the app with an MDBList key and at least one library movie/tv item.
- Force an item stale: temporarily lower the server `_RATINGS_MAX_AGE_DAYS` (or set
  an item's `ratings_updated_at` to an old date in its DB), reload the page.
- In DevTools Network, confirm a background burst of `/api/ratings` calls on load
  (no `?force`), and that the affected card's pills update without interaction.
- Confirm it does **not** re-fire after add/remove actions (only once per load).
- With no MDBList key, confirm **no** sweep requests fire.
Restore any temporary changes. Stop the app.

- [ ] **Step 4: Commit**

```bash
git add templates/index.html
git commit -m "feat(ui): proactive launch sweep to refresh stale ratings (<=500/day)"
```

---

## Task 11: Frontend — auth autofill hardening

**Files:**
- Modify: `templates/login.html` (verify/tighten the two-mode logic + comments)

- [ ] **Step 1: Confirm the initial-paint state is clean login**

In `templates/login.html`, after the script defines `setMode`, ensure the page
initializes login mode on load. At the end of the `<script>` (before the closing
`</script>`), add (if not already present):

```javascript
// Initialize a clean Sign-In form on load so saved-password autofill works and
// register-mode credential suppression is applied only after an explicit switch.
setMode('login');
```

- [ ] **Step 2: Harden register-mode suppression**

In `setMode`, in the `if (m === 'register')` branch, set the confirm field's
autocomplete explicitly and ensure the username hint is fully cleared:

```javascript
  if (m === 'register') {
    user.setAttribute('autocomplete', 'off');
    user.setAttribute('name', 'reg_user_' + Math.random().toString(36).slice(2));
    setMasked(pw, true);
    setMasked(conf, true);
    conf.setAttribute('autocomplete', 'off');
    maskReal['authConfirm'] = ''; renderMasked(conf);
  } else {
    user.setAttribute('autocomplete', 'username');
    user.removeAttribute('name');
    setMasked(pw, false);
    setMasked(conf, false);
    pw.autocomplete = 'current-password';
    conf.value = '';
  }
```

The randomized `name` on the username field in register mode further discourages
iOS/managers from matching it to a saved username; login mode restores a clean
field for saved-password autofill.

- [ ] **Step 3: Manual verification (iOS device/simulator if available)**

- **Sign In tab:** with a saved CineVault credential, iOS offers it on the
  username field; tapping fills username + password.
- **Create Account tab:** no "Use Strong Password" prompt, no suggested usernames,
  no saved-credential autofill on any field; typed characters mask to bullets and
  the eye toggle reveals them.
- Switching tabs back and forth keeps each mode's behavior correct.

Document in the commit that iOS autofill is heuristic and not 100% guaranteed.

- [ ] **Step 4: Commit**

```bash
git add templates/login.html
git commit -m "fix(auth): harden create-account autofill suppression; clean sign-in"
```

---

## Final verification

- [ ] **Run the full backend test suite**

Run: `python -m pytest tests/ -v`
Expected: all tests pass (existing + new ratings tests).

- [ ] **Smoke-test the app end to end**

Run the app, then verify each item once more: iOS-style resize (desktop resize
proxy), Sign-In vs Create-Account behavior, Unrated/10 rating display, TMDB pill
in search + grid + selector, larger Filter caret, and the details-panel manual
refresh + "Updated" label. Stop the app.

---

## Self-review notes (spec coverage)

- #1 iOS flash → Task 7. #2 autofill → Task 11. #3 rating display → Task 5.
- #4 TMDB pill → Tasks 1–3 (data) + Task 6 (UI). #5 caret → Task 8.
- #6 refresh: manual button + label + debounce → Task 9; force mode + updated_at +
  500/day lazy cap (server) → Task 4; proactive launch sweep (7-day ceiling,
  ≤500/day, oldest-first, cap-aware stop) → Task 10.
