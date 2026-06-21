# Media Scores & Card Display Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add per-source rating pills (IMDb, Rotten Tomatoes tomatometer/popcornmeter, Metacritic, Letterboxd, MyAnimeList) from MDBList, a selector-driven pill row (year/type/scores) for the library grid and search results, and Text/Default/Poster-only card size modes.

**Architecture:** A single Flask endpoint `/api/ratings/<media_type>/<tmdb_id>` returns normalized ratings using a hybrid model — persisted in the per-user `media` table for library items, served from a 24h in-process TTL cache for everything else. The frontend (single `templates/index.html`) renders pills lazily per card, gated by a per-context multi-select pill selector persisted in `localStorage`.

**Tech Stack:** Python 3 / Flask, SQLite (per-user `movie_tracker_<uid>.db`), pytest for backend, vanilla inline JS/CSS for frontend. MDBList detail endpoint `GET https://api.mdblist.com/{provider}/{type}/{id}?apikey=KEY`.

**Spec:** `docs/superpowers/specs/2026-06-20-media-scores-and-card-display-design.md`

**Note on frontend tests:** The repo has a Python (pytest) suite but no JS test harness. Backend tasks are TDD with pytest. Frontend tasks (4–8) use explicit manual browser verification — adding a JS harness is out of scope (YAGNI).

---

## File Structure

- `db.py` — add `ratings`/`ratings_updated_at` columns + `set_media_ratings()` helper.
- `app.py` — add `_fetch_mdblist_ratings()` + `GET /api/ratings/<media_type>/<tmdb_id>`.
- `tests/test_ratings.py` — new backend test module (self-contained client fixture).
- `templates/index.html` — pill rendering, pill selector, size-mode refactor, grid/search/detail wiring (CSS + inline JS).

---

## Task 1: DB columns + ratings persistence helper

**Files:**
- Modify: `db.py` (table DDL ~`db.py:32-58`, migration ~`db.py:62-74`, add helper near other mutators)
- Test: `tests/test_ratings.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_ratings.py`:

```python
import os
import json
import pytest

import app as app_module
from app import app as flask_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_DIR", str(tmp_path))
    flask_app.config["TESTING"] = True
    flask_app.config["SECRET_KEY"] = "test-secret-key"
    app_module._app_initialized = False
    app_module._initialized_users.clear()
    app_module._user_media_cache.clear()
    from auth import _RATE_BUCKETS
    _RATE_BUCKETS.clear()
    with flask_app.test_client() as c:
        yield c


def _register(client, username="alice", password="secret123"):
    return client.post("/auth/register", json={"username": username, "password": password})


def _add_movie(client, title="Inception", external_id="27205"):
    return client.post("/api/add", json={
        "title": title, "media_type": "movie",
        "external_id": external_id, "tmdb_id": int(external_id), "status": "watchlist",
    })


def test_media_row_has_ratings_columns(client):
    _register(client)
    _add_movie(client)
    item = client.get("/api/list").get_json()[0]
    assert "ratings" in item
    assert "ratings_updated_at" in item
    assert item["ratings"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ratings.py::test_media_row_has_ratings_columns -v`
Expected: FAIL — `KeyError: 'ratings'` (column doesn't exist yet).

- [ ] **Step 3: Add the columns and migration in `db.py`**

In the `CREATE TABLE IF NOT EXISTS media (...)` block, add two columns just before `date_added`:

```python
                overview        TEXT,
                year            TEXT,
                ratings         TEXT,
                ratings_updated_at TEXT,
                date_added      TEXT DEFAULT (date('now')),
```

In `_migrate_media_dates(conn)`, extend the column loop so legacy DBs gain the new columns. Replace:

```python
    for col in ("date_watchlist", "date_watching", "date_finished"):
        if col not in cols:
            conn.execute(f"ALTER TABLE media ADD COLUMN {col} TEXT")
            added = True
```

with:

```python
    for col in ("date_watchlist", "date_watching", "date_finished",
                "ratings", "ratings_updated_at"):
        if col not in cols:
            conn.execute(f"ALTER TABLE media ADD COLUMN {col} TEXT")
            added = True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_ratings.py::test_media_row_has_ratings_columns -v`
Expected: PASS.

- [ ] **Step 5: Add the `set_media_ratings` helper + test**

Add to `db.py` near the other write helpers (e.g. after `add_media_entry`):

```python
def set_media_ratings(media_id: int, ratings_json: str, updated_at: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE media SET ratings = ?, ratings_updated_at = ? WHERE id = ?",
            (ratings_json, updated_at, media_id),
        )
```

Append this test to `tests/test_ratings.py`:

```python
def test_set_media_ratings_roundtrip(client):
    _register(client)
    _add_movie(client)
    item = client.get("/api/list").get_json()[0]
    import db
    from flask import g
    with flask_app.test_request_context():
        from db import get_user_db_path
        from users_db import get_user_by_username
        uid = get_user_by_username("alice")["id"]
        g.user_db_path = get_user_db_path(uid)
        db.set_media_ratings(item["id"], json.dumps({"imdb": 8.1}), "2026-06-20T00:00:00")
        row = db.get_media_by_id(item["id"])
    assert json.loads(row["ratings"]) == {"imdb": 8.1}
    assert row["ratings_updated_at"] == "2026-06-20T00:00:00"
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/test_ratings.py -v`
Expected: both tests PASS.

- [ ] **Step 7: Commit**

```bash
git add db.py tests/test_ratings.py
git commit -m "feat(db): add ratings columns and set_media_ratings helper"
```

---

## Task 2: MDBList fetch + normalization

**Files:**
- Modify: `app.py` (add function near `_get_mdblist_key`, ~`app.py:104`)
- Test: `tests/test_ratings.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ratings.py`:

```python
def test_fetch_mdblist_ratings_normalizes(monkeypatch):
    import app as a

    class FakeResp:
        status_code = 200
        def json(self):
            return {"ratings": [
                {"source": "imdb", "value": 8.1},
                {"source": "tomatoes", "value": 94},
                {"source": "audience", "value": 88},
                {"source": "metacritic", "value": 76},
                {"source": "letterboxd", "value": 4.2},
                {"source": "mal", "value": None},
                {"source": "trakt", "value": 90},
            ]}

    monkeypatch.setattr(a.requests, "get", lambda *args, **kw: FakeResp())
    out = a._fetch_mdblist_ratings("movie", 27205, "fake-key")
    assert out == {"imdb": 8.1, "tomatoes": 94, "audience": 88,
                   "metacritic": 76, "letterboxd": 4.2}


def test_fetch_mdblist_ratings_unsupported_type(monkeypatch):
    import app as a
    called = {"n": 0}
    def _spy(*a_, **k_):
        called["n"] += 1
    monkeypatch.setattr(a.requests, "get", _spy)
    assert a._fetch_mdblist_ratings("book", 5, "fake-key") == {}
    assert called["n"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ratings.py::test_fetch_mdblist_ratings_normalizes -v`
Expected: FAIL — `AttributeError: module 'app' has no attribute '_fetch_mdblist_ratings'`.

- [ ] **Step 3: Implement `_fetch_mdblist_ratings` in `app.py`**

Add directly after `_get_mdblist_key()`:

```python
# MDBList rating sources we surface, mapped from the API's `source` field.
_MDBLIST_SOURCES = {"imdb", "tomatoes", "audience", "metacritic", "letterboxd", "mal"}

# App media_type -> MDBList path segment. Only movies/shows are supported.
_MDBLIST_TYPE = {"movie": "movie", "tv": "show"}


def _fetch_mdblist_ratings(media_type: str, tmdb_id, key: str) -> dict:
    """Return {source: value} for the sources we display, or {} on any problem.
    One detail call returns every rating, so cost is per-title, not per-source."""
    mtype = _MDBLIST_TYPE.get(media_type)
    if not mtype or not tmdb_id:
        return {}
    try:
        resp = requests.get(
            f"https://api.mdblist.com/tmdb/{mtype}/{tmdb_id}",
            params={"apikey": key},
            timeout=8,
        )
        if resp.status_code != 200:
            return {}
        ratings = resp.json().get("ratings") or []
    except Exception:
        return {}
    out = {}
    for r in ratings:
        if not isinstance(r, dict):
            continue
        src, val = r.get("source"), r.get("value")
        if src in _MDBLIST_SOURCES and val is not None:
            out[src] = val
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_ratings.py -k fetch_mdblist -v`
Expected: both PASS.

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_ratings.py
git commit -m "feat(ratings): add MDBList fetch + normalization"
```

---

## Task 3: `/api/ratings` endpoint (hybrid persist + TTL cache)

**Files:**
- Modify: `app.py` (add a module-level cache near `poster_cache` ~`app.py:117`; add route near other `/api` routes)
- Test: `tests/test_ratings.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ratings.py`:

```python
def _set_mdblist_key(username="alice", key="fake-key"):
    from users_db import get_user_by_username, get_user_keys, set_user_keys
    uid = get_user_by_username(username)["id"]
    set_user_keys(uid, get_user_keys(uid)["tmdb_key"] or "tmdbkey", key)


def test_ratings_endpoint_no_key_returns_empty(client):
    _register(client)
    resp = client.get("/api/ratings/movie/27205")
    assert resp.status_code == 200
    assert resp.get_json() == {"ratings": {}}


def test_ratings_endpoint_caches_non_library(client, monkeypatch):
    _register(client)
    _set_mdblist_key()
    calls = {"n": 0}
    def fake(media_type, tmdb_id, key):
        calls["n"] += 1
        return {"imdb": 8.1}
    monkeypatch.setattr(app_module, "_fetch_mdblist_ratings", fake)
    first  = client.get("/api/ratings/movie/603").get_json()
    second = client.get("/api/ratings/movie/603").get_json()
    assert first == second == {"ratings": {"imdb": 8.1}}
    assert calls["n"] == 1  # second served from TTL cache


def test_ratings_endpoint_persists_library_item(client, monkeypatch):
    _register(client)
    _set_mdblist_key()
    _add_movie(client, external_id="27205")
    monkeypatch.setattr(app_module, "_fetch_mdblist_ratings",
                        lambda *a, **k: {"imdb": 7.5})
    client.get("/api/ratings/movie/27205")
    item = client.get("/api/list").get_json()[0]
    import json as _j
    assert _j.loads(item["ratings"]) == {"imdb": 7.5}
    assert item["ratings_updated_at"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_ratings.py -k ratings_endpoint -v`
Expected: FAIL — 404 (route not defined).

- [ ] **Step 3: Add the TTL cache near the other module caches (~`app.py:117`)**

```python
# Ratings for non-library titles (search results). Keyed "media_type:tmdb_id".
_ratings_cache: dict[str, tuple[float, dict]] = {}
_RATINGS_TTL = 24 * 3600          # seconds
_RATINGS_MAX_AGE_DAYS = 7         # persisted library ratings refresh after this
```

`app.py` already imports `json` and `time` (lines 3–4). It does **not** import `datetime` — add this line to the top imports:

```python
from datetime import datetime
```

And add `set_media_ratings` to the existing `from db import (...)` block (line 14–18), alongside `get_media_by_external_id`.

- [ ] **Step 4: Add the route (near the other `/api` routes, e.g. after the poster route)**

```python
@app.route("/api/ratings/<media_type>/<tmdb_id>")
@login_required
def api_ratings(media_type, tmdb_id):
    key = _get_mdblist_key()
    if not key:
        return jsonify({"ratings": {}})

    # Library item? Serve/refresh persisted ratings.
    row = get_media_by_external_id(str(tmdb_id), media_type)
    if row:
        fresh = False
        if row.get("ratings") and row.get("ratings_updated_at"):
            try:
                age = datetime.utcnow() - datetime.fromisoformat(row["ratings_updated_at"])
                fresh = age.days < _RATINGS_MAX_AGE_DAYS
            except Exception:
                fresh = False
        if fresh:
            try:
                return jsonify({"ratings": json.loads(row["ratings"])})
            except Exception:
                pass
        data = _fetch_mdblist_ratings(media_type, tmdb_id, key)
        set_media_ratings(row["id"], json.dumps(data), datetime.utcnow().isoformat())
        return jsonify({"ratings": data})

    # Non-library (search result): TTL cache.
    ck = f"{media_type}:{tmdb_id}"
    hit = _ratings_cache.get(ck)
    if hit and (time.time() - hit[0]) < _RATINGS_TTL:
        return jsonify({"ratings": hit[1]})
    data = _fetch_mdblist_ratings(media_type, tmdb_id, key)
    if len(_ratings_cache) > 2000:
        _ratings_cache.pop(next(iter(_ratings_cache)))
    _ratings_cache[ck] = (time.time(), data)
    return jsonify({"ratings": data})
```

(Imports `json`, `time`, `datetime`, and `set_media_ratings` were added in Step 3.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_ratings.py -v`
Expected: all PASS.

- [ ] **Step 6: Run the full suite (no regressions)**

Run: `python -m pytest -q`
Expected: all tests PASS.

- [ ] **Step 7: Commit**

```bash
git add app.py tests/test_ratings.py
git commit -m "feat(ratings): add /api/ratings endpoint (hybrid persist + TTL cache)"
```

---

## Task 4: Card size-mode refactor (Text / Default / Poster only)

**Files:**
- Modify: `templates/index.html` — `_VALID_SIZES` + `tabSize` defaults (~`1658-1661`), `renderGrid()` size dropdown + card branches (~`1941-2042`).

- [ ] **Step 1: Replace the size vocabulary + migration**

Find `_VALID_SIZES` (search for it near `tabSize`). Replace its definition and the `tabSize` initializer with:

```javascript
const _VALID_SIZES = new Set(['text', 'poster', 'default']);
function _migrateSize(v) {
  if (v === 'small') return 'text';
  if (v === 'medium' || v === 'large') return 'default';
  return _VALID_SIZES.has(v) ? v : 'default';
}
const tabSize = {
  watchlist: _migrateSize(localStorage.getItem('tabSize_watchlist')),
  watching:  _migrateSize(localStorage.getItem('tabSize_watching')),
  finished:  _migrateSize(localStorage.getItem('tabSize_finished')),
};
```

- [ ] **Step 2: Replace the dropdown option lists in `renderGrid()`**

Replace:

```javascript
  const sizeOpts   = ['small','medium','large'];
  const sizeLabels = { small:'Compact', medium:'Default', large:'Large' };
  const sizeHints  = { small:'remove poster', medium:'', large:'add description' };
```

with:

```javascript
  const sizeOpts   = ['text','default','poster'];
  const sizeLabels = { text:'Text only', default:'Default', poster:'Poster only' };
  const sizeHints  = { text:'no poster', default:'', poster:'image only' };
```

- [ ] **Step 3: Replace the three card-render branches**

Replace the `if (size === 'small') {...}`, `if (size === 'large') {...}`, and the final default `return ...` blocks with the following (note `badgeRow` is replaced by a pills container; `renderPills` arrives in Task 6 — until then it is defined as a stub in Step 4):

```javascript
    const pillsHost = `<div class="poster-badge-row" id="pills-${item.id}">${renderPills(item, _ratingsCache[item.media_type+':'+(item.tmdb_id||item.external_id)] || {}, gridPills)}</div>`;

    if (size === 'poster') {
      return `<div class="poster-card poster-only" id="pc-${item.id}" style="${cardFontStyle}" onclick="selectItem(${item.id})">
        <img src="${posterUrl(item)}" referrerpolicy="no-referrer" onerror="this.src='${BROKEN_POSTER_FULL}'" loading="lazy">
      </div>`;
    }

    if (size === 'text') {
      return `<div class="poster-card" id="pc-${item.id}" style="${cardFontStyle}" onclick="selectItem(${item.id})">
        <div class="poster-info">
          ${pillsHost}
          <div class="poster-title" style="white-space:normal;overflow:visible;text-overflow:clip">${esc(item.title)}</div>
          ${author}
          ${dateHtml}
          ${stars}
        </div>
      </div>`;
    }

    return `<div class="poster-card" id="pc-${item.id}" style="${cardFontStyle}" onclick="selectItem(${item.id})">
      <img src="${posterUrl(item)}" referrerpolicy="no-referrer" onerror="this.src='${BROKEN_POSTER_FULL}'" loading="lazy">
      <div class="poster-info">
        ${pillsHost}
        <div class="poster-title">${esc(item.title)}</div>
        ${author}
        ${dateHtml}
        ${stars}
      </div>
    </div>`;
```

Also delete the now-unused `const badgeRow = ...` line (~`1998`) and the `yearBadge`/`typeBadge` lines it depended on (~`1996-1997`) — `renderPills` regenerates them.

- [ ] **Step 4: Add temporary stubs so the page renders before Task 6**

Near the top of the `<script>` block (just after `esc` is defined), add temporary stubs to be replaced in Tasks 5–6:

```javascript
// TEMP stubs (replaced in Task 5/6).
let gridPills = ['year','type'];
const _ratingsCache = {};
function renderPills(item, ratings, selected) {
  let h = '';
  if (selected.includes('year') && item.year) h += `<span class="poster-year-badge">${esc(String(item.year))}</span>`;
  if (selected.includes('type')) h += `<span class="poster-type-badge ptb-${item.media_type}">${TYPE_SVG[item.media_type]} ${TYPE_LABEL[item.media_type]}</span>`;
  return h;
}
```

- [ ] **Step 5: Add poster-only CSS**

In the stylesheet near `.poster-card`, add:

```css
.poster-card.poster-only { padding: 0; }
.poster-card.poster-only img { border-radius: var(--radius); }
```

- [ ] **Step 6: Manual verification**

Run: `python app.py` (or the project's run command), log in, open the library.
- The size dropdown now lists **Text only / Default / Poster only**.
- Switching to **Poster only** shows just posters (no title/badges).
- **Text only** shows no poster; **Default** shows poster + title + year/type badges.
- Reload the page: the previously stored size still works (legacy `small`/`medium`/`large` no longer error).

- [ ] **Step 7: Commit**

```bash
git add templates/index.html
git commit -m "feat(ui): replace card sizes with Text/Default/Poster-only modes"
```

---

## Task 5: Pill rendering core (logos, CSS, lazy ratings loader)

**Files:**
- Modify: `templates/index.html` — add CSS for `.spill*`; replace the Task-4 stub `renderPills` + add helpers and the lazy fetch loader.

- [ ] **Step 1: Add pill CSS**

Near `.poster-badge-row` in the stylesheet, add:

```css
.spill { display:inline-flex; align-items:center; gap:0.2em; padding:0.1em 0.42em;
  border-radius:0.32em; font-size:0.66em; font-weight:600; line-height:1.45;
  border:1px solid transparent; white-space:nowrap; }
.spill-ic { height:1em; width:auto; }
.spill-imdb { background:#f5c518; color:#000; }
.spill-imdb b, .spill-mal b { font-weight:800; letter-spacing:0.01em; }
.spill-mal { background:#2e51a2; color:#fff; }
.spill-tomatoes  { background:rgba(250,50,10,0.14);  color:#ff6a4d; border-color:rgba(250,50,10,0.32); }
.spill-audience  { background:rgba(250,110,30,0.14); color:#ffa05c; border-color:rgba(250,110,30,0.32); }
.spill-letterboxd{ background:rgba(0,224,84,0.12);   color:#5be08a; border-color:rgba(0,224,84,0.28); }
.spill-metacritic { color:#fff; }
.spill-metacritic.mc-good  { background:#00a85a; }
.spill-metacritic.mc-mixed { background:#caa300; color:#1a1a1a; }
.spill-metacritic.mc-bad   { background:#d33b3b; }
```

- [ ] **Step 2: Replace the Task-4 `renderPills` stub with the full implementation**

Replace the TEMP stub block from Task 4 Step 4 with:

```javascript
const PILL_ORDER  = ['year','type','imdb','tomatoes','audience','metacritic','letterboxd','mal'];
const SCORE_SOURCES = ['imdb','tomatoes','audience','metacritic','letterboxd','mal'];
const PILL_LABELS = { year:'Year', type:'Media type', imdb:'IMDb', tomatoes:'Tomatometer',
  audience:'Popcornmeter', metacritic:'Metacritic', letterboxd:'Letterboxd', mal:'MyAnimeList' };
const DEFAULT_PILLS = ['year','type','imdb','metacritic','tomatoes'];

const SCORE_SVG = {
  tomatoes:  '<svg class="spill-ic" viewBox="0 0 24 24" fill="#fa320a"><path d="M12 4c1-2 4-2 5-1-1 1-2 1-3 2 3 0 6 2 6 6 0 5-4 9-8 9s-8-4-8-9c0-4 3-6 6-6-1-1-2-1-3-2 1-1 4-1 5 1z"/></svg>',
  audience:  '<svg class="spill-ic" viewBox="0 0 24 24"><path d="M5 8h14l-1.6 12H6.6z" fill="#fa6e1e"/><path d="M5 8l2-3.5 3 1.8 2-2.6 2 2.6 3-1.8 2 3.5z" fill="#ffd24d"/></svg>',
  letterboxd:'<svg class="spill-ic" viewBox="0 0 36 12"><circle cx="6" cy="6" r="5" fill="#ff8000"/><circle cx="18" cy="6" r="5" fill="#00e054"/><circle cx="30" cy="6" r="5" fill="#40bcf4"/></svg>',
};

const _ratingsCache = {};   // "media_type:tmdb_id" -> ratings object

function _pillValue(src, ratings) {
  const v = ratings ? ratings[src] : undefined;
  if (v === undefined || v === null) return null;
  if (src === 'tomatoes' || src === 'audience') return Math.round(v) + '%';
  if (src === 'metacritic') return String(Math.round(v));
  return Number(v).toFixed(1);   // imdb, mal, letterboxd
}
function _mcBand(v) { return v >= 61 ? 'good' : v >= 40 ? 'mixed' : 'bad'; }

function renderPills(item, ratings, selected) {
  const out = [];
  for (const src of PILL_ORDER) {
    if (!selected.includes(src)) continue;
    if (src === 'year') { if (item.year) out.push(`<span class="poster-year-badge">${esc(String(item.year))}</span>`); continue; }
    if (src === 'type') { out.push(`<span class="poster-type-badge ptb-${item.media_type}">${TYPE_SVG[item.media_type]} ${TYPE_LABEL[item.media_type]}</span>`); continue; }
    const val = _pillValue(src, ratings);
    if (val === null) continue;
    if (src === 'imdb')       { out.push(`<span class="spill spill-imdb"><b>IMDb</b> ${val}</span>`); continue; }
    if (src === 'mal')        { out.push(`<span class="spill spill-mal"><b>MAL</b> ${val}</span>`); continue; }
    if (src === 'metacritic') { out.push(`<span class="spill spill-metacritic mc-${_mcBand(ratings.metacritic)}">${val}</span>`); continue; }
    out.push(`<span class="spill spill-${src}">${SCORE_SVG[src] || ''}${val}</span>`);
  }
  return out.join('');
}

async function fetchRatings(mediaType, tmdbId) {
  if (!tmdbId || (mediaType !== 'movie' && mediaType !== 'tv')) return {};
  const k = `${mediaType}:${tmdbId}`;
  if (k in _ratingsCache) return _ratingsCache[k];
  try {
    const res = await fetch(`/api/ratings/${mediaType}/${encodeURIComponent(tmdbId)}`);
    const data = res.ok ? ((await res.json()).ratings || {}) : {};
    _ratingsCache[k] = data;
    return data;
  } catch { _ratingsCache[k] = {}; return {}; }
}

// Lazily fill a pills container once ratings arrive.
function hydratePills(hostId, item, selected) {
  if (!selected.some(s => SCORE_SOURCES.includes(s))) return;
  const tmdbId = item.tmdb_id || item.external_id;
  if (!tmdbId || (item.media_type !== 'movie' && item.media_type !== 'tv')) return;
  fetchRatings(item.media_type, tmdbId).then(r => {
    const host = document.getElementById(hostId);
    if (host) host.innerHTML = renderPills(item, r, selected);
  });
}
```

(Also remove the temporary `let gridPills = ['year','type'];` line from the Task-4 stub — `gridPills` is defined in Task 6.)

- [ ] **Step 3: Manual verification (logos render)**

Temporarily, in the browser console after the app loads, run:
```javascript
renderPills({media_type:'movie',year:2010}, {imdb:8.1,tomatoes:94,metacritic:76,audience:88,letterboxd:4.2,mal:8.5}, PILL_ORDER)
```
Expected: an HTML string containing a yellow `IMDb 8.1` chip, a 🍅 `94%` pill, a color-banded Metacritic `76`, a popcorn `88%`, Letterboxd tri-dot `4.2`, and blue `MAL 8.5`.

- [ ] **Step 4: Commit**

```bash
git add templates/index.html
git commit -m "feat(ui): pill rendering core with brand logos + lazy ratings loader"
```

---

## Task 6: Pill selector + grid wiring

**Files:**
- Modify: `templates/index.html` — pill-selector state/persistence + component, grid top-bar wiring in `renderGrid()`, lazy hydrate after grid render, selector CSS.

- [ ] **Step 1: Add selector state + persistence (near `PILL_ORDER`)**

```javascript
const _VALID_PILLS = new Set(PILL_ORDER);
function _loadPills(storeKey) {
  try {
    const a = JSON.parse(localStorage.getItem(storeKey));
    if (Array.isArray(a)) return a.filter(x => _VALID_PILLS.has(x));
  } catch {}
  return DEFAULT_PILLS.slice();
}
let gridPills   = _loadPills('gridPills');
let searchPills = _loadPills('searchPills');

function renderPillSelector(which) {
  const sel = which === 'grid' ? gridPills : searchPills;
  const rows = PILL_ORDER.map(src =>
    `<div class="size-dd-item pill-dd-item${sel.includes(src) ? ' active' : ''}" onclick="togglePill('${which}','${src}',event)">
       <span>${PILL_LABELS[src]}</span><span class="pill-dd-check">${sel.includes(src) ? '✓' : ''}</span>
     </div>`).join('');
  return `<div class="size-dd-wrap pill-dd-wrap">
    <button class="size-dd-btn" onclick="togglePillDropdown('${which}',event)">Pills (${sel.length}) <span style="font-size:0.5625rem;opacity:0.7">▾</span></button>
    <div class="size-dd-menu" id="pillMenu_${which}">${rows}</div>
  </div>`;
}

function togglePillDropdown(which, ev) {
  ev.stopPropagation();
  const menu = document.getElementById('pillMenu_' + which);
  const open = menu.classList.contains('open');
  document.querySelectorAll('.size-dd-menu').forEach(m => m.classList.remove('open'));
  if (!open) menu.classList.add('open');
}

function togglePill(which, src, ev) {
  ev.stopPropagation();
  const arr = which === 'grid' ? gridPills : searchPills;
  const i = arr.indexOf(src);
  if (i >= 0) arr.splice(i, 1); else arr.push(src);
  localStorage.setItem(which === 'grid' ? 'gridPills' : 'searchPills', JSON.stringify(arr));
  if (which === 'grid') renderGrid(); else rerenderSearch();
}
```

- [ ] **Step 2: Extend the outside-click closer**

Find the document click handler that closes the size dropdown (search `closeSizeDropdown`). In that same listener add a line so pill menus also close on outside click:

```javascript
  if (!e.target.closest('.pill-dd-wrap')) document.querySelectorAll('.size-dd-menu').forEach(m => m.classList.remove('open'));
```

- [ ] **Step 3: Place the grid selector in the top bar**

In `renderGrid()`, in the `.grid-right-group`, add the selector immediately before the `<div class="size-dd-wrap">` size dropdown:

```javascript
      ${renderPillSelector('grid')}
      <div class="size-dd-wrap">
```

- [ ] **Step 4: Hydrate pills after the grid renders**

In `renderGrid()`, immediately after `panel.innerHTML = statsHtml + topBar + ...` (the line that sets the grid HTML, ~`2042`), add:

```javascript
  if (size !== 'poster') {
    items.forEach(item => hydratePills(`pills-${item.id}`, item, gridPills));
  }
```

- [ ] **Step 5: Add selector CSS**

```css
.pill-dd-item { display:flex; align-items:center; justify-content:space-between; gap:1rem; }
.pill-dd-check { width:0.9em; text-align:center; color:var(--accent); }
.pill-dd-wrap .size-dd-menu { min-width: 11rem; }
```

- [ ] **Step 6: Add a temporary `rerenderSearch` stub (replaced in Task 7)**

Near `togglePill`, add:

```javascript
function rerenderSearch() { /* implemented in Task 7 */ }
```

- [ ] **Step 7: Manual verification**

Run the app, open the library (Default mode):
- A **Pills (n) ▾** button sits left of the size dropdown.
- Opening it lists all 8 pills with checkmarks on the selected ones; default = Year, Media type, IMDb, Metacritic, Tomatometer.
- Toggling a source re-renders the grid; score pills appear shortly after (lazy fetch). With no MDBList key set, only Year/Type appear and score toggles add nothing.
- With an MDBList key set (Settings), IMDb/🍅/Metacritic pills populate on cards.
- Reload: selection persists.

- [ ] **Step 8: Commit**

```bash
git add templates/index.html
git commit -m "feat(ui): pill selector + grid wiring with lazy hydration"
```

---

## Task 7: Search results — selector + pills

**Files:**
- Modify: `templates/index.html` — caption + selector beside `#searchTypeBtns` (~`1171`), `renderSmCard()` pills (~`2861`), re-render plumbing in `renderModalResults()` (~`2820`).

- [ ] **Step 1: Add the caption + selector beside the search type filters**

Locate the `<div class="search-type-btns" id="searchTypeBtns" ...>` block (~`1171`). Wrap/extend its container so a captioned selector sits beside it. Immediately after the closing `</div>` of `searchTypeBtns`, add:

```html
      <div class="search-pills-wrap">
        <div class="search-pills-cap">FILTER SEARCH RATINGS</div>
        <span id="searchPillSelectorHost"></span>
      </div>
```

- [ ] **Step 2: Inject the search selector after the search UI mounts**

Find where the search modal/panel is shown (search for `searchTypeBtns` usage in JS, or the function that opens search). After that UI is rendered/opened, set the host once:

```javascript
  const host = document.getElementById('searchPillSelectorHost');
  if (host) host.innerHTML = renderPillSelector('search');
```

If the search bar is always present in the DOM, instead add this line at the end of the main init (after first paint). Verify the `#searchPillSelectorHost` exists when this runs.

- [ ] **Step 3: Add caption CSS**

```css
.search-pills-wrap { display:flex; flex-direction:column; gap:0.25rem; }
.search-pills-cap { font-size:0.625rem; letter-spacing:0.07em; text-transform:uppercase;
  color:var(--muted); font-weight:600; }
```

- [ ] **Step 4: Render pills inside search cards**

In `renderSmCard(r)`, replace the chips block:

```javascript
  const chips = [];
  if (r.year) chips.push(`<span class="sm-chip year">${esc(r.year)}</span>`);
  chips.push(`<span class="sm-chip ${r.media_type}">${TYPE_SVG[r.media_type]} ${TYPE_LABEL[r.media_type]}</span>`);
```

with a single selector-driven pills host (year/type come from `searchPills` now):

```javascript
  const smItem = { media_type: r.media_type, year: r.year, tmdb_id: r.tmdb_id, external_id: r.external_id };
  const pillsId = `spills-${r.media_type}-${(r.external_id || r.tmdb_id || '').toString().replace(/[^a-z0-9]/gi,'_')}`;
  const chipsHtml = `<div class="sm-chips" id="${pillsId}">${renderPills(smItem, _ratingsCache[r.media_type+':'+(r.tmdb_id||r.external_id)] || {}, searchPills)}</div>`;
```

Then in the returned template replace `<div class="sm-chips">${chips.join('')}</div>` with `${chipsHtml}`.

- [ ] **Step 5: Hydrate + enable re-render in `renderModalResults`**

In `renderModalResults(libMatches, tmdbResults, q)`, cache the args for re-render and hydrate after paint. Add at the top of the function:

```javascript
  _lastModalArgs = { libMatches, tmdbResults, q };
```

and after the results are written to the DOM (end of the function), add:

```javascript
  if (searchPills.some(s => SCORE_SOURCES.includes(s))) {
    tmdbResults.forEach(r => {
      const item = { media_type: r.media_type, year: r.year, tmdb_id: r.tmdb_id, external_id: r.external_id };
      const pillsId = `spills-${r.media_type}-${(r.external_id || r.tmdb_id || '').toString().replace(/[^a-z0-9]/gi,'_')}`;
      hydratePills(pillsId, item, searchPills);
    });
  }
```

Declare the cache var near other module globals: `let _lastModalArgs = null;`

- [ ] **Step 6: Replace the Task-6 `rerenderSearch` stub**

```javascript
function rerenderSearch() {
  const host = document.getElementById('searchPillSelectorHost');
  if (host) host.innerHTML = renderPillSelector('search');
  if (_lastModalArgs) renderModalResults(_lastModalArgs.libMatches, _lastModalArgs.tmdbResults, _lastModalArgs.q);
}
```

- [ ] **Step 7: Manual verification**

Run the app, open search, run a movie/TV query:
- A **FILTER SEARCH RATINGS** caption with a **Pills (n) ▾** selector sits beside the type filters.
- Search cards show Year/Type by default; with an MDBList key, IMDb/🍅/Metacritic populate shortly after results appear.
- Toggling the search selector re-renders the current results without a new text search, and the selection persists on reload — independent of the grid selection.

- [ ] **Step 8: Commit**

```bash
git add templates/index.html
git commit -m "feat(ui): search pill selector + pills on search cards"
```

---

## Task 8: Details-panel pills

**Files:**
- Modify: `templates/index.html` — `renderDetail()` insert a pills host above the "Load description" button (~`2244`) + hydrate.

- [ ] **Step 1: Insert a pills host above "Load description"**

In `renderDetail(item)`, immediately before the line containing `<button id="descToggleBtn" ...>Load description</button>`, add a host div:

```javascript
      <div class="detail-scores" id="detailScores">${renderPills(item, _ratingsCache[item.media_type+':'+(item.tmdb_id||item.external_id)] || {}, SCORE_SOURCES)}</div>
```

(The details panel always shows *all available* score pills, so it passes `SCORE_SOURCES` rather than a user selection.)

- [ ] **Step 2: Hydrate after the detail panel renders**

At the end of `renderDetail(item)` (after `panel.innerHTML = ...`), add:

```javascript
  hydratePills('detailScores', item, SCORE_SOURCES);
```

- [ ] **Step 3: Add detail-scores spacing CSS**

```css
.detail-scores { display:flex; flex-wrap:wrap; gap:0.35rem; margin-bottom:0.75rem; }
.detail-scores:empty { display:none; margin:0; }
```

- [ ] **Step 4: Manual verification**

Run the app, select a movie/TV item in the library with an MDBList key configured:
- Score pills appear above the **Load description** button (all available sources, side-by-side).
- For a book/manga item, or with no MDBList key, no pills and no empty gap appear.

- [ ] **Step 5: Commit**

```bash
git add templates/index.html
git commit -m "feat(ui): show score pills in the details panel"
```

---

## Final verification

- [ ] Run the full backend suite: `python -m pytest -q` → all PASS.
- [ ] Manual smoke (with an MDBList key in Settings): grid pills, poster-only mode, search pills, detail pills all render; toggling selectors works and persists; grid and search selections are independent.
- [ ] Manual smoke (no MDBList key): only Year/Type pills appear; no errors in console; no empty pill rows.
