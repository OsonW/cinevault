# Tab-Coloured Filter Bar + Length Metadata Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recolour the grid filter bar to match the active tab's colour, and show a length figure (runtime / seasons / chapters / pages) beside the media type in the detail panel and under the author on search cards.

**Architecture:** Part 1 is pure CSS — `document.body.dataset.tab` already tracks the active tab, so a `--f-accent` custom-property triple scoped to `.grid-panel` drives every filter control. Part 2 consolidates the existing TMDB director fetch into a `_fetch_tmdb_meta` helper that returns director *and* length from a single request (`append_to_response=credits` for movies; `/tv/{id}` already carries both), so search issues the same number of network calls it does today. Book page counts and manga chapter counts are already present in local data / the search payload and need no fetch at all.

**Tech Stack:** Flask (Python 3), vanilla JS in a single `templates/index.html`, pytest.

**Reference spec:** `docs/superpowers/specs/2026-07-25-tab-colours-and-length-metadata-design.md`

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `templates/index.html` | Entire frontend (CSS + markup + JS in one file — existing project convention) | Modify: CSS custom properties + ~10 rules; 3 JS render/hydrate sites; 1 new formatting helper |
| `app.py` | Flask routes + external API fetching | Modify: rename/extend director helper & endpoint, add runtime formatter, map manga chapter count |
| `tests/test_ratings.py` | pytest suite covering ratings/search/director endpoints | Modify: 4 existing references to renamed symbols; add 4 new tests |

`templates/index.html` is ~237 KB and holds all frontend code. This is the established
pattern in this project — do **not** split it as part of this work.

---

## Task 1: Tab-coloured filter bar (CSS only)

No tests — this is presentational CSS with no JS test runner in the project. Verified
manually in step 3.

**Files:**
- Modify: `templates/index.html` (CSS block, lines ~647, ~676-678, ~689, ~706, ~708, ~747, ~770, ~775, ~788, ~672-673)

- [ ] **Step 1: Add the per-tab accent triple**

Find this line (~647):

```css
.grid-panel { grid-area: grid; overflow-y: auto; padding: 1.25rem 1.5rem; background: var(--bg); }
```

Replace it with:

```css
/* The grid filter bar takes the active tab's colour so each tab's filters read as
   belonging to that tab. body[data-tab] is already maintained by switchTab(). The
   base rule defaults to purple, so "finished" needs no override and the bar still
   renders correctly if data-tab is ever absent. Scoped to .grid-panel so the
   top-bar search Filter button (.search-filter-wrap, outside this subtree) keeps
   the global purple accent. */
.grid-panel { grid-area: grid; overflow-y: auto; padding: 1.25rem 1.5rem; background: var(--bg);
  --f-accent: var(--purple); --f-dim: var(--purple-dim); --f-glow: var(--purple-glow); }
body[data-tab="watchlist"] .grid-panel { --f-accent: var(--blue);  --f-dim: var(--blue-dim);  --f-glow: var(--blue-glow); }
body[data-tab="watching"]  .grid-panel { --f-accent: var(--green); --f-dim: var(--green-dim); --f-glow: var(--green-glow); }
```

- [ ] **Step 2: Point every filter-bar rule at the new triple**

Make these ten edits in the CSS block. Each `old` string appears exactly once.

**2a.** The shared dropdown button — add `.grid-panel`-scoped overrides. Find (~689):

```css
.size-dd-btn:hover { border-color: var(--accent); background: var(--accent-dim); }
```

Replace with:

```css
.size-dd-btn:hover { border-color: var(--accent); background: var(--accent-dim); }
/* Grid-scoped overrides: .size-dd-btn / .size-dd-item / .pill-dd-check are shared
   with the top-bar search filter, which must stay purple. */
.grid-panel .size-dd-btn { border-color: var(--f-dim); background: var(--f-glow); color: var(--f-accent); }
.grid-panel .size-dd-btn:hover { border-color: var(--f-accent); background: var(--f-dim); }
.grid-panel .size-dd-item.active { color: var(--f-accent); background: var(--f-glow); }
.grid-panel .pill-dd-check { color: var(--f-accent); }
```

**2b.** Card-size dots. Find (~672-673):

```css
.csz-dot.active::before { background: var(--accent); border-color: var(--accent); }
.csz-dot:focus-visible { outline: 2px solid var(--accent-dim); outline-offset: 1px; }
```

Replace with:

```css
.csz-dot.active::before { background: var(--f-accent); border-color: var(--f-accent); }
.csz-dot:focus-visible { outline: 2px solid var(--f-dim); outline-offset: 1px; }
```

**2c.** Clear-filters button. Find (~747):

```css
.grid-clear-btn:hover { color: var(--text); border-color: var(--accent); background: rgba(255,255,255,0.04); }
```

Replace with:

```css
.grid-clear-btn:hover { color: var(--text); border-color: var(--f-accent); background: rgba(255,255,255,0.04); }
```

**2d.** Title-search focus ring. Find (~770):

```css
.grid-filter-input:focus { outline: none; border-color: var(--accent); }
```

Replace with:

```css
.grid-filter-input:focus { outline: none; border-color: var(--f-accent); }
```

**2e.** Random button hover. Find (~788):

```css
.grid-random-btn:hover { color: var(--accent); border-color: var(--accent); background: var(--subtle); }
```

Replace with:

```css
.grid-random-btn:hover { color: var(--f-accent); border-color: var(--f-accent); background: var(--subtle); }
```

Leave `.show-unrated-toggle` untouched: it only renders on the finished tab, which is
purple, so its hardcoded `%23a78bfa` checkmark SVG is already correct.

- [ ] **Step 3: Verify manually**

Run: `python app.py`

Open the app and check, on each tab:

| Tab | Filter button fill/border/text | Search-input focus ring | Size dots (active) |
|---|---|---|---|
| Watchlist | blue | blue | blue |
| Watching | green | green | green |
| Finished | purple | purple | purple |

Also confirm: open the Filter dropdown on the watchlist tab — active rows and
checkmarks are blue. Open the **search modal** (magnifier in the top bar) and its
Filter button is still **purple** on every tab.

- [ ] **Step 4: Commit**

```bash
git add templates/index.html
git commit -m "feat: colour the grid filter bar to match the active tab"
```

---

## Task 2: Server — consolidated TMDB meta helper

**Files:**
- Modify: `app.py:516-530` (`_fetch_tmdb_director`)
- Test: `tests/test_ratings.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ratings.py`:

```python
class _FakeTMDBResp:
    """Minimal stand-in for requests.Response with a canned JSON body."""
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_fetch_tmdb_meta_movie_one_call(monkeypatch):
    """Movie director + runtime must arrive from a SINGLE request.

    Guards the append_to_response consolidation: if this ever splits back into
    /movie/{id} + /movie/{id}/credits, search doubles its TMDB calls per card.
    """
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs.get("params", {})))
        return _FakeTMDBResp({
            "runtime": 103,
            "credits": {"crew": [
                {"name": "Lana Wachowski", "job": "Director"},
                {"name": "Someone Else", "job": "Editor"},
            ]},
        })

    monkeypatch.setattr(app_module.requests, "get", fake_get)
    meta = app_module._fetch_tmdb_meta("movie", 603, "k")
    assert meta == {"author": "Lana Wachowski", "length": "1h 43m"}
    assert len(calls) == 1
    assert calls[0][1].get("append_to_response") == "credits"


def test_fetch_tmdb_meta_tv_seasons(monkeypatch):
    monkeypatch.setattr(
        app_module.requests, "get",
        lambda *a, **k: _FakeTMDBResp({
            "number_of_seasons": 4,
            "created_by": [{"name": "Vince Gilligan"}],
        }),
    )
    meta = app_module._fetch_tmdb_meta("tv", 1396, "k")
    assert meta == {"author": "Vince Gilligan", "length": "4 Seasons"}


def test_fetch_tmdb_meta_tv_single_season_singular(monkeypatch):
    monkeypatch.setattr(
        app_module.requests, "get",
        lambda *a, **k: _FakeTMDBResp({"number_of_seasons": 1, "created_by": []}),
    )
    assert app_module._fetch_tmdb_meta("tv", 1, "k")["length"] == "1 Season"


def test_fetch_tmdb_meta_request_failure(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(app_module.requests, "get", boom)
    assert app_module._fetch_tmdb_meta("movie", 603, "k") == {"author": "", "length": ""}


@pytest.mark.parametrize("minutes,expected", [
    (103, "1h 43m"),
    (44, "44m"),
    (120, "2h"),
    (0, ""),
    (None, ""),
    (-5, ""),
])
def test_fmt_runtime(minutes, expected):
    assert app_module._fmt_runtime(minutes) == expected
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_ratings.py -k "tmdb_meta or fmt_runtime" -v`
Expected: FAIL — `AttributeError: module 'app' has no attribute '_fetch_tmdb_meta'`
(and the same for `_fmt_runtime`).

- [ ] **Step 3: Replace `_fetch_tmdb_director` with `_fetch_tmdb_meta`**

In `app.py`, find this function (~line 516):

```python
def _fetch_tmdb_director(media_type: str, tmdb_id: int, api_key: str) -> str:
    params = _tmdb_params(api_key)
    try:
        if media_type == "movie":
            url = f"https://api.themoviedb.org/3/movie/{tmdb_id}/credits"
            data = requests.get(url, params=params, timeout=3).json()
            names = [c["name"] for c in data.get("crew", []) if c.get("job") == "Director"]
        else:
            url = f"https://api.themoviedb.org/3/tv/{tmdb_id}"
            data = requests.get(url, params=params, timeout=3).json()
            names = [c["name"] for c in data.get("created_by", [])]
        return ", ".join(names[:3])
    except Exception:
        return ""
```

Replace it entirely with:

```python
def _fmt_runtime(minutes) -> str:
    """'1h 43m' / '44m' / '2h'. Empty string for missing or non-positive values."""
    try:
        mins = int(minutes)
    except (TypeError, ValueError):
        return ""
    if mins <= 0:
        return ""
    hours, rem = divmod(mins, 60)
    if not hours:
        return f"{rem}m"
    return f"{hours}h {rem}m" if rem else f"{hours}h"


def _fetch_tmdb_meta(media_type: str, tmdb_id: int, api_key: str) -> dict:
    """Director/creator + length for a TMDB title, from a SINGLE request.

    Movies use append_to_response so credits and runtime arrive together — keeping
    search hydration at one call per card, exactly as it was when this only fetched
    the director. /tv/{id} already carries both created_by and number_of_seasons.

    Returns {"author": str, "length": str}; either may be "" when unavailable.
    """
    params = _tmdb_params(api_key)
    try:
        if media_type == "movie":
            url = f"https://api.themoviedb.org/3/movie/{tmdb_id}"
            data = requests.get(
                url, params={**params, "append_to_response": "credits"}, timeout=3
            ).json()
            crew = data.get("credits", {}).get("crew", [])
            names = [c["name"] for c in crew if c.get("job") == "Director"]
            length = _fmt_runtime(data.get("runtime"))
        else:
            url = f"https://api.themoviedb.org/3/tv/{tmdb_id}"
            data = requests.get(url, params=params, timeout=3).json()
            names = [c["name"] for c in data.get("created_by", [])]
            seasons = data.get("number_of_seasons") or 0
            length = f"{seasons} Season{'' if seasons == 1 else 's'}" if seasons else ""
        return {"author": ", ".join(names[:3]), "length": length}
    except Exception:
        return {"author": "", "length": ""}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_ratings.py -k "tmdb_meta or fmt_runtime" -v`
Expected: PASS (9 tests — 4 meta + 6 parametrized runtime cases, minus none).

The rest of the suite is still red at this point (three call sites still reference
`_fetch_tmdb_director`); Task 3 fixes them.

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_ratings.py
git commit -m "feat: fetch TMDB director and length in one request"
```

---

## Task 3: Server — `/api/tmdb-meta` endpoint + call sites

**Files:**
- Modify: `app.py:532-551` (`fetch_item_director`), `app.py:231-233` (cache decl), `app.py:993-1014` (endpoint)
- Test: `tests/test_ratings.py:408`, `:662`, `:670-687`

- [ ] **Step 1: Update the existing tests to the new names**

In `tests/test_ratings.py`, make these four edits.

**1a.** Line ~408, inside the TMDB-rating search test:

```python
    monkeypatch.setattr(app_module, "_fetch_tmdb_director", lambda *a, **k: "")
```

becomes:

```python
    monkeypatch.setattr(app_module, "_fetch_tmdb_meta", lambda *a, **k: {"author": "", "length": ""})
```

**1b.** In `test_search_does_not_block_on_directors` (~line 655-666):

```python
    called = {"n": 0}
    def spy(*a, **k):
        called["n"] += 1
        return "Some Director"
    monkeypatch.setattr(app_module, "_fetch_tmdb_director", spy)
```

becomes:

```python
    called = {"n": 0}
    def spy(*a, **k):
        called["n"] += 1
        return {"author": "Some Director", "length": "2h 16m"}
    monkeypatch.setattr(app_module, "_fetch_tmdb_meta", spy)
```

**1c.** Replace the whole of `test_tmdb_director_endpoint`:

```python
def test_tmdb_director_endpoint(client, monkeypatch):
    _register(client)
    _set_mdblist_key()
    calls = {"n": 0}
    def spy(media_type, tmdb_id, key):
        calls["n"] += 1
        return "Lana Wachowski"
    monkeypatch.setattr(app_module, "_fetch_tmdb_director", spy)
    first  = client.get("/api/tmdb-director/movie/603").get_json()
    second = client.get("/api/tmdb-director/movie/603").get_json()
    assert first == second == {"author": "Lana Wachowski"}
    assert calls["n"] == 1                        # second served from cache
```

with:

```python
def test_tmdb_meta_endpoint(client, monkeypatch):
    _register(client)
    _set_mdblist_key()
    calls = {"n": 0}
    def spy(media_type, tmdb_id, key):
        calls["n"] += 1
        return {"author": "Lana Wachowski", "length": "2h 16m"}
    monkeypatch.setattr(app_module, "_fetch_tmdb_meta", spy)
    first  = client.get("/api/tmdb-meta/movie/603").get_json()
    second = client.get("/api/tmdb-meta/movie/603").get_json()
    assert first == second == {"author": "Lana Wachowski", "length": "2h 16m"}
    assert calls["n"] == 1                        # second served from cache
```

**1d.** Replace the whole of `test_tmdb_director_no_tmdb_key`:

```python
def test_tmdb_director_no_tmdb_key(client):
    _register(client)
    resp = client.get("/api/tmdb-director/movie/603")
    assert resp.status_code == 200
    assert resp.get_json() == {"author": ""}
```

with:

```python
def test_tmdb_meta_no_tmdb_key(client):
    _register(client)
    resp = client.get("/api/tmdb-meta/movie/603")
    assert resp.status_code == 200
    assert resp.get_json() == {"author": "", "length": ""}
```

**1e.** The `client` fixture clears per-test caches by name. Find this line in the
fixture (~line 19):

```python
    app_module._ratings_cache.clear()
```

and add the meta cache beside it:

```python
    app_module._ratings_cache.clear()
    app_module._tmdb_meta_cache.clear()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_ratings.py -k "tmdb_meta or does_not_block" -v`
Expected: FAIL — `AttributeError: module 'app' has no attribute '_tmdb_meta_cache'`.

- [ ] **Step 3: Rename the cache**

In `app.py`, find (~line 231):

```python
# TMDB director/creator for search results. Keyed "media_type:tmdb_id".
_tmdb_director_cache: dict[str, tuple[float, str]] = {}
_TMDB_DIRECTOR_TTL = 24 * 3600  # seconds
```

Replace with:

```python
# TMDB director/creator + length for search results. Keyed "media_type:tmdb_id".
_tmdb_meta_cache: dict[str, tuple[float, dict]] = {}
_TMDB_META_TTL = 24 * 3600  # seconds
```

- [ ] **Step 4: Update the library-item director route**

In `app.py`, inside `fetch_item_director` (~line 548), find:

```python
    author = _fetch_tmdb_director(item["media_type"], int(tmdb_id), tmdb_key)
```

Replace with:

```python
    author = _fetch_tmdb_meta(item["media_type"], int(tmdb_id), tmdb_key)["author"]
```

The route's path and `{"author": ...}` response shape are unchanged — the detail
panel's author hydration keeps working as-is.

- [ ] **Step 5: Replace the director endpoint with the meta endpoint**

In `app.py`, find the whole route (~line 993):

```python
@app.route("/api/tmdb-director/<media_type>/<tmdb_id>")
@login_required
def api_tmdb_director(media_type, tmdb_id):
    """Free TMDB director/creator name, fetched lazily so search stays fast."""
    if media_type not in ("movie", "tv"):
        return jsonify({"author": ""})
    key = _get_tmdb_key()
    if not key:
        return jsonify({"author": ""})
    try:
        tid = int(tmdb_id)
    except (TypeError, ValueError):
        return jsonify({"author": ""})
    ck = f"{media_type}:{tmdb_id}"
    hit = _tmdb_director_cache.get(ck)
    if hit and (time.time() - hit[0]) < _TMDB_DIRECTOR_TTL:
        return jsonify({"author": hit[1]})
    author = _fetch_tmdb_director(media_type, tid, key) or ""
    if len(_tmdb_director_cache) > 2000:
        _tmdb_director_cache.pop(next(iter(_tmdb_director_cache)))
    _tmdb_director_cache[ck] = (time.time(), author)
    return jsonify({"author": author})
```

Replace it entirely with:

```python
@app.route("/api/tmdb-meta/<media_type>/<tmdb_id>")
@login_required
def api_tmdb_meta(media_type, tmdb_id):
    """Free TMDB director/creator + length, fetched lazily so search stays fast."""
    empty = {"author": "", "length": ""}
    if media_type not in ("movie", "tv"):
        return jsonify(empty)
    key = _get_tmdb_key()
    if not key:
        return jsonify(empty)
    try:
        tid = int(tmdb_id)
    except (TypeError, ValueError):
        return jsonify(empty)
    ck = f"{media_type}:{tmdb_id}"
    hit = _tmdb_meta_cache.get(ck)
    if hit and (time.time() - hit[0]) < _TMDB_META_TTL:
        return jsonify(hit[1])
    meta = _fetch_tmdb_meta(media_type, tid, key)
    if len(_tmdb_meta_cache) > 2000:
        _tmdb_meta_cache.pop(next(iter(_tmdb_meta_cache)))
    _tmdb_meta_cache[ck] = (time.time(), meta)
    return jsonify(meta)
```

- [ ] **Step 6: Verify no stale references remain**

Run: `grep -rn "_fetch_tmdb_director\|_tmdb_director_cache\|_TMDB_DIRECTOR_TTL\|tmdb-director" app.py tests/ templates/`
Expected: only hits in `templates/index.html` (fixed in Task 5). If `app.py` or
`tests/` still match, fix those before continuing.

- [ ] **Step 7: Run the full suite**

Run: `python -m pytest tests/ -v`
Expected: PASS — all tests green.

- [ ] **Step 8: Commit**

```bash
git add app.py tests/test_ratings.py
git commit -m "feat: replace /api/tmdb-director with /api/tmdb-meta"
```

---

## Task 4: Server — manga chapter count in search results

**Files:**
- Modify: `app.py:764-775` (manga search result dict)
- Test: `tests/test_ratings.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ratings.py`:

```python
def _manga_search_payload(attributes):
    """One MangaDex search hit with the given attributes block."""
    return {"data": [{
        "id": "abc-123",
        "attributes": {"title": {"en": "Berserk"}, **attributes},
        "relationships": [],
    }]}


def test_manga_search_maps_last_chapter(client, monkeypatch):
    """Manga has its own route (/api/search/manga) — it does NOT go through
    /api/search, which is TMDB-only and key-gated."""
    _register(client)
    monkeypatch.setattr(
        app_module.requests, "get",
        lambda *a, **k: _FakeTMDBResp(_manga_search_payload({"lastChapter": "91"})),
    )
    app_module.search_cache.clear()
    data = client.get("/api/search/manga?q=berserk").get_json()
    assert data[0]["total_chapters"] == "91"


def test_manga_search_missing_last_chapter(client, monkeypatch):
    _register(client)
    monkeypatch.setattr(
        app_module.requests, "get",
        lambda *a, **k: _FakeTMDBResp(_manga_search_payload({})),
    )
    app_module.search_cache.clear()
    data = client.get("/api/search/manga?q=berserk").get_json()
    assert data[0]["total_chapters"] is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_ratings.py -k manga_search -v`
Expected: FAIL — `KeyError: 'total_chapters'`.

Note: `_FakeTMDBResp` and `_register` come from earlier in the file (Task 2 added
`_FakeTMDBResp`), so Task 2 must be complete before this task runs.

- [ ] **Step 3: Map the field**

In `app.py`, inside `search_manga`, find (~line 764):

```python
            items.append({
                "external_id": manga_id,
                "title": title,
                "author": author,
                "year": str(attrs.get("year") or ""),
                "media_type": "manga",
                "cover_url": cover_url,
                "overview": overview[:300] if overview else "",
                "status": attrs.get("status"),
                "popularity": 0,
            })
```

Replace with:

```python
            items.append({
                "external_id": manga_id,
                "title": title,
                "author": author,
                "year": str(attrs.get("year") or ""),
                "media_type": "manga",
                "cover_url": cover_url,
                "overview": overview[:300] if overview else "",
                "status": attrs.get("status"),
                # Already in the search response — lets search cards show the chapter
                # count with no extra request.
                "total_chapters": attrs.get("lastChapter"),
                "popularity": 0,
            })
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_ratings.py -k manga_search -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_ratings.py
git commit -m "feat: expose manga chapter count in search results"
```

---

## Task 5: Client — length formatter + detail panel

No automated tests (no JS test runner in this project). Verified manually in step 5.

**Files:**
- Modify: `templates/index.html` (~2195 helper, ~3414 detail markup, ~3444-3457 hydration)

- [ ] **Step 1: Add the shared formatter and cache**

Find this line (~2195):

```javascript
const TYPE_LABEL = { movie:'Movie', tv:'TV Show', book:'Book', manga:'Manga' };
```

Add directly below it:

```javascript
// Length shown beside the media type: runtime (movie), season count (tv), chapter
// count (manga), page count (book). Movie/TV strings are formatted server-side by
// /api/tmdb-meta; this handles the two types whose raw counts are already local.
function _fmtCount(n, noun) {
  const v = Number(n);
  if (!v || v <= 0) return '';
  return `${v} ${noun}${v === 1 ? '' : 's'}`;
}

// "<media_type>:<id>" -> length string ('' means "looked up, nothing available").
const _lengthCache = {};
```

- [ ] **Step 2: Render the length span in the detail panel**

Find this line inside `renderDetail` (~3414):

```javascript
      <p class="detail-meta">${TYPE_SVG[item.media_type]} ${TYPE_LABEL[item.media_type]}</p>
```

Replace with:

```javascript
      <p class="detail-meta">${TYPE_SVG[item.media_type]} ${TYPE_LABEL[item.media_type]}<span id="detailLength">${_detailLengthSeed(item)}</span></p>
```

- [ ] **Step 3: Add the seed + hydration helpers**

Add these two functions immediately **above** `function renderDetail(` (search for
`function renderDetail` to locate it):

```javascript
// Length we can show without any network call: book page counts live on the row, and
// anything already resolved this session is in _lengthCache. Returns the ", 512 Pages"
// suffix (comma included) or '' so the type line reads plainly when nothing is known.
function _detailLengthSeed(item) {
  const id = item.tmdb_id || item.external_id;
  const cached = _lengthCache[`${item.media_type}:${id}`];
  if (cached) return `, ${cached}`;
  if (item.media_type === 'book') {
    const s = _fmtCount(item.total_pages, 'Page');
    return s ? `, ${s}` : '';
  }
  return '';
}

// Fill the detail-panel length for types that need a lookup. Movie/TV share the
// /api/tmdb-meta response with the director fetch (server-cached 24h); manga reuses
// the existing /api/manga-info endpoint and its client-side cache. Books never get
// here — their page count is already on the row.
function hydrateDetailLength(item) {
  const id = item.tmdb_id || item.external_id;
  if (!id) return;
  const ck = `${item.media_type}:${id}`;
  const paint = (len) => {
    _lengthCache[ck] = len || '';
    if (!len || selectedId !== item.id) return;
    const el = document.getElementById('detailLength');
    if (el) el.textContent = `, ${len}`;
  };
  if (ck in _lengthCache) { paint(_lengthCache[ck]); return; }

  if (item.media_type === 'movie' || item.media_type === 'tv') {
    fetch(`/api/tmdb-meta/${item.media_type}/${encodeURIComponent(id)}`)
      .then(r => r.json()).then(d => paint(d.length)).catch(() => {});
  } else if (item.media_type === 'manga') {
    const cachedInfo = mangaInfoCache[id];
    if (cachedInfo) { paint(_fmtCount(cachedInfo.last_chapter, 'Chapter')); return; }
    fetch(`/api/manga-info/${encodeURIComponent(id)}`)
      .then(r => r.json())
      .then(d => {
        if (d && !d.error) mangaInfoCache[id] = d;
        paint(_fmtCount(d && d.last_chapter, 'Chapter'));
      })
      .catch(() => {});
  }
}
```

- [ ] **Step 4: Call the hydrator after the panel paints**

Find this block near the end of `renderDetail` (~3444):

```javascript
  loadDescriptionPreview(item);

  if (!item.author && (item.media_type === 'movie' || item.media_type === 'tv')) {
```

Replace with:

```javascript
  loadDescriptionPreview(item);
  hydrateDetailLength(item);

  if (!item.author && (item.media_type === 'movie' || item.media_type === 'tv')) {
```

- [ ] **Step 5: Verify manually**

Run: `python app.py`

Select one library item of each type and confirm the media-type line reads:

| Type | Expected |
|---|---|
| Movie | `Movie, 1h 43m` |
| TV | `TV Show, 4 Seasons` |
| Manga | `Manga, 91 Chapters` |
| Book | `Book, 512 Pages` (instantly, no flicker) |

Then confirm graceful degradation: find a book added before OpenLibrary supplied a
page count (or temporarily edit `_detailLengthSeed` to receive `total_pages: null`)
and check it renders a bare `Book` with **no trailing comma**.

Reselect a movie you already viewed — the length should appear immediately from
`_lengthCache`, with no second request in the Network tab.

- [ ] **Step 6: Commit**

```bash
git add templates/index.html
git commit -m "feat: show length beside media type in the detail panel"
```

---

## Task 6: Client — length on search cards

No automated tests (no JS test runner in this project). Verified manually in step 5.

**Files:**
- Modify: `templates/index.html` (~423 CSS, ~4119-4159 hydration, ~4204-4213 card markup, ~4100 dispatch)

- [ ] **Step 1: Add the card style**

Find this line (~423):

```css
.sm-card-author { font-size: 0.6875rem; color: var(--muted); }
```

Add directly below it:

```css
.sm-card-length { font-size: 0.6875rem; color: var(--muted); }
```

- [ ] **Step 2: Render the length line on the card**

Find this block inside `renderSmCard` (~4204):

```javascript
  // Movie/TV cards arrive without a director; show an animated "…" until it lands.
  const authorLoading = !r.author && (r.media_type === 'movie' || r.media_type === 'tv') && (r.tmdb_id || r.external_id);
  const authorHtml = r.author
    ? `<div class="sm-card-author">${esc(r.author)}</div>`
    : (authorLoading ? `<div class="sm-card-author sm-author-loading"><span class="author-dots"><span></span><span></span><span></span></span></div>` : '');
```

Replace with:

```javascript
  // Movie/TV cards arrive without a director; show an animated "…" until it lands.
  const authorLoading = !r.author && (r.media_type === 'movie' || r.media_type === 'tv') && (r.tmdb_id || r.external_id);
  const authorHtml = r.author
    ? `<div class="sm-card-author">${esc(r.author)}</div>`
    : (authorLoading ? `<div class="sm-card-author sm-author-loading"><span class="author-dots"><span></span><span></span><span></span></span></div>` : '');
  // Length sits under the author. Book/manga counts are already in the search payload
  // so they paint immediately; movie/TV backfill via hydrateSmLength. Deliberately no
  // "…" placeholder — a second row of dots on every card is noise.
  const lengthId = `smlen-${r.media_type}-${(r.external_id || r.tmdb_id || '').toString().replace(/[^a-z0-9]/gi,'_')}`;
  const lengthHtml = `<div class="sm-card-length" id="${lengthId}">${esc(_smLengthSeed(r))}</div>`;
```

Then find, a few lines below, the card body markup:

```javascript
      <div class="sm-card-title">${esc(r.title)}</div>
      ${authorHtml}
      ${chipsHtml}
```

Replace with:

```javascript
      <div class="sm-card-title">${esc(r.title)}</div>
      ${authorHtml}
      ${lengthHtml}
      ${chipsHtml}
```

- [ ] **Step 3: Add the seed + hydration helpers**

Find this line (~4119):

```javascript
const _smAuthorCache = {};   // "media_type:tmdb_id" -> author string
```

Add directly above it:

```javascript
// Length already available without a request: manga chapter counts and book page
// counts both ride along in the search payload. Movie/TV return '' and backfill.
function _smLengthSeed(r) {
  const cached = _lengthCache[`${r.media_type}:${r.tmdb_id || r.external_id}`];
  if (cached) return cached;
  if (r.media_type === 'manga') return _fmtCount(r.total_chapters, 'Chapter');
  if (r.media_type === 'book')  return _fmtCount(r.total_pages, 'Page');
  return '';
}

const _smLengthPending = new Set();  // in-flight length fetches

// Movie/TV search cards backfill their length from /api/tmdb-meta — the same endpoint
// and the same response that hydrateSmAuthor uses, so the pair costs one request.
// Deduped per session via _lengthCache.
function hydrateSmLength(r) {
  if (r.media_type !== 'movie' && r.media_type !== 'tv') return;   // book/manga seeded
  const tid = r.tmdb_id || r.external_id;
  if (!tid) return;
  const ck = `${r.media_type}:${tid}`;
  const fill = (len) => {
    if (!len) return;
    const id = `smlen-${r.media_type}-${(r.external_id || r.tmdb_id || '').toString().replace(/[^a-z0-9]/gi,'_')}`;
    const el = document.getElementById(id);
    if (el) el.textContent = len;
  };
  if (ck in _lengthCache) { fill(_lengthCache[ck]); return; }
  if (_smLengthPending.has(ck)) return;
  _smLengthPending.add(ck);
  fetch(`/api/tmdb-meta/${r.media_type}/${encodeURIComponent(tid)}`)
    .then(res => res.json())
    .then(d => {
      _lengthCache[ck] = d.length || '';
      // Same response carries the director — seed it so hydrateSmAuthor's own fetch
      // hits cache instead of repeating the round trip.
      if (!(ck in _smAuthorCache)) _smAuthorCache[ck] = d.author || '';
      fill(d.length);
    })
    .catch(() => {})
    .finally(() => _smLengthPending.delete(ck));
}
```

- [ ] **Step 4: Point the author hydrator at the new endpoint and dispatch the length hydrator**

**4a.** Inside `hydrateSmAuthor` (~4155), find:

```javascript
  fetch(`/api/tmdb-director/${r.media_type}/${encodeURIComponent(tid)}`)
    .then(res => res.json()).then(d => { _smAuthorCache[ck] = d.author || ''; fill(d.author); })
```

Replace with:

```javascript
  fetch(`/api/tmdb-meta/${r.media_type}/${encodeURIComponent(tid)}`)
    .then(res => res.json()).then(d => {
      _smAuthorCache[ck] = d.author || '';
      if (!(ck in _lengthCache)) _lengthCache[ck] = d.length || '';
      fill(d.author);
    })
```

**4b.** In `renderModalResults` (~4100), find:

```javascript
    shownResults.forEach(r => hydrateSmAuthor(r));
```

Replace with:

```javascript
    shownResults.forEach(r => { hydrateSmAuthor(r); hydrateSmLength(r); });
```

Both hydrators fire for the same card, but `hydrateSmAuthor` runs first and populates
`_lengthCache` from its own response, so `hydrateSmLength` normally hits the cache and
issues no request. Where it does fire (author already known from a previous session
render), the server's 24h `_tmdb_meta_cache` serves it without touching TMDB.

- [ ] **Step 5: Verify manually**

Run: `python app.py`

Open the search modal and search for each of the following, watching the Network tab:

| Query | Expected card | Requests for the length |
|---|---|---|
| `blade runner 2049` (movie) | title → `Denis Villeneuve` → `2h 44m` | shares the `tmdb-meta` call with the director |
| `breaking bad` (tv) | title → `Vince Gilligan` → `5 Seasons` | shares the `tmdb-meta` call |
| `berserk` (manga) | title → author → `91 Chapters` immediately | **zero** |
| `dune` (book) | title → author → `412 Pages` immediately | **zero** |

Confirm the director still loads with its `…` placeholder, and that the length line has
**no** placeholder — it simply appears. Confirm a movie with no runtime in TMDB shows an
empty length line rather than a stray comma or `0m`.

- [ ] **Step 6: Verify no stale endpoint references remain**

Run: `grep -rn "tmdb-director" templates/ app.py tests/`
Expected: no output. (`/api/item/<id>/fetch_director` is a different route and is
intentionally kept — it will not match this pattern.)

- [ ] **Step 7: Run the full suite**

Run: `python -m pytest tests/ -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add templates/index.html
git commit -m "feat: show length under the author on search cards"
```

---

## Self-Review Notes

Spec coverage check — every spec requirement maps to a task:

| Spec section | Task |
|---|---|
| Part 1, `--f-*` triple + 10 recoloured rules | Task 1 |
| Part 1, `.grid-panel` scoping keeps search modal purple | Task 1 steps 2a, 3 |
| Part 2, `_fetch_tmdb_meta` single-call consolidation | Task 2 |
| Part 2, `/api/tmdb-meta` + cache rename + `fetch_director` call site | Task 3 |
| Part 2, manga `total_chapters` in search | Task 4 |
| Part 2, detail panel (all four types + graceful omission) | Task 5 |
| Part 2, search cards (all four types, no placeholder, shared request) | Task 6 |
| Existing tests referencing renamed symbols (4 sites) | Task 3 step 1 |
| New server tests | Tasks 2, 4 |

Naming is consistent across tasks: `_fmt_runtime` / `_fetch_tmdb_meta` /
`_tmdb_meta_cache` / `_TMDB_META_TTL` (server); `_fmtCount` / `_lengthCache` /
`_detailLengthSeed` / `hydrateDetailLength` / `_smLengthSeed` / `hydrateSmLength` /
`_smLengthPending` (client). The `smlen-<type>-<safeId>` element id is built with the
identical `replace(/[^a-z0-9]/gi,'_')` sanitiser in both the renderer and the hydrator.
