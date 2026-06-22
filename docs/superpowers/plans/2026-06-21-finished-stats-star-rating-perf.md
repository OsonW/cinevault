# Finished-Tab Stats, Star Rating, Per-Tab Filters & Perf — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship eight improvements to CineVault: rename the two Filter dropdowns, make library pills per-tab, redesign the finished-tab stats (avg + 10/10 + rating histogram + dual-range filter), show `X.Y⭐` on cards, a partial-star rating slider, per-tab date verbs, and two performance fixes (instant add/remove + non-blocking search).

**Architecture:** Flask backend (`app.py`, `db.py`) + one large template (`templates/index.html`) holding all app JS/CSS. Backend changes (search/add/director endpoint) are test-driven via pytest. Template JS/CSS changes ship with exact code and are verified manually (no JS test harness exists); a few pure helpers are unit-testable but live in the template, so they're verified by hand.

**Tech Stack:** Python 3 / Flask, SQLite, pytest, vanilla JS + CSS.

**Spec:** `docs/superpowers/specs/2026-06-21-finished-stats-star-rating-perf-design.md`

**Run tests with:** `python -m pytest tests/ -v`

---

## Task 1: Backend — fast search, add-returns-id, lazy director endpoint

**Files:**
- Modify: `app.py` (`search()`, `add_media()` create branch, new `api_tmdb_director`)
- Test: `tests/test_ratings.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_ratings.py`:

```python
def test_search_does_not_block_on_directors(client, monkeypatch):
    _register(client)
    _set_mdblist_key()  # also seeds a tmdb key

    class FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"results": [
                {"id": 603, "title": "The Matrix", "release_date": "1999-03-30",
                 "poster_path": "/x.jpg", "overview": "o", "popularity": 9, "vote_average": 8.2},
            ]}

    monkeypatch.setattr(app_module.requests, "get", lambda *a, **k: FakeResp())
    called = {"n": 0}
    def spy(*a, **k):
        called["n"] += 1
        return "Some Director"
    monkeypatch.setattr(app_module, "_fetch_tmdb_director", spy)

    data = client.get("/api/search?q=matrix&type=movie").get_json()
    assert data[0]["title"] == "The Matrix"
    assert called["n"] == 0                      # search must NOT fetch directors
    assert not data[0].get("author")             # author absent/empty on search


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


def test_tmdb_director_no_tmdb_key(client):
    _register(client)
    resp = client.get("/api/tmdb-director/movie/603")
    assert resp.status_code == 200
    assert resp.get_json() == {"author": ""}


def test_add_create_returns_id_and_date(client):
    _register(client)
    resp = client.post("/api/add", json={
        "title": "Inception", "media_type": "movie",
        "external_id": "27205", "tmdb_id": 27205, "status": "watchlist",
    }).get_json()
    assert isinstance(resp.get("id"), int)
    assert resp.get("date_added")
    item = client.get("/api/list").get_json()[0]
    assert item["id"] == resp["id"]
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `python -m pytest tests/test_ratings.py -k "director or does_not_block or returns_id" -v`
Expected: FAIL (route missing / directors still fetched / no id returned).

- [ ] **Step 3: Remove the blocking director fetch from `search()`**

In `app.py` `search()`, DELETE the post-processing block that fetches directors
for every result (the block that reads):

```python
    if items:
        with ThreadPoolExecutor(max_workers=len(items)) as pool:
            futs = {pool.submit(_fetch_tmdb_director, i["media_type"], i["tmdb_id"], tmdb_key): i for i in items}
            for fut in as_completed(futs):
                futs[fut]["author"] = fut.result()

    _cache_search(cache_key, items)
    return jsonify(items)
```

Replace it with just:

```python
    _cache_search(cache_key, items)
    return jsonify(items)
```

(Leave the `items = items[:10]` line above it untouched. `ThreadPoolExecutor` /
`as_completed` may now be unused in this function but stay imported for other code
— do not remove the imports.)

- [ ] **Step 4: Add the `/api/tmdb-director` endpoint + cache**

In `app.py`, near the other ratings caches (right after the `_tmdb_rating_cache`
definitions added earlier), add a director cache:

```python
# TMDB director/creator for search results. Keyed "media_type:tmdb_id".
_tmdb_director_cache: dict[str, tuple[float, str]] = {}
_TMDB_DIRECTOR_TTL = 24 * 3600  # seconds
```

Then add the route immediately AFTER the `api_tmdb_rating` function:

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
    ck = f"{media_type}:{tmdb_id}"
    hit = _tmdb_director_cache.get(ck)
    if hit and (time.time() - hit[0]) < _TMDB_DIRECTOR_TTL:
        return jsonify({"author": hit[1]})
    author = _fetch_tmdb_director(media_type, int(tmdb_id), key) or ""
    if len(_tmdb_director_cache) > 2000:
        _tmdb_director_cache.pop(next(iter(_tmdb_director_cache)))
    _tmdb_director_cache[ck] = (time.time(), author)
    return jsonify({"author": author})
```

- [ ] **Step 5: Make `/api/add` create branch return the new id + date**

In `app.py` `add_media()`, the create (`else:`) branch currently ends with
`add_media_entry(...)` then falls through to `return jsonify({"status": "ok"})`.
Capture the id and return it. Replace the `add_media_entry(...)` call's result
handling so the function ends like this (keep all the existing keyword args):

```python
        new_id = add_media_entry(
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
        created = get_media_by_id(new_id) if new_id else None
        return jsonify({
            "status": "ok",
            "id": new_id,
            "date_added": created["date_added"] if created else None,
        })
```

(Delete the old trailing `return jsonify({"status": "ok"})` that followed the
create branch, so the function returns from the branch above.)

- [ ] **Step 6: Run tests, verify pass**

Run: `python -m pytest tests/test_ratings.py -k "director or does_not_block or returns_id" -v`
Expected: PASS (4 tests). Then the whole suite: `python -m pytest tests/ -q` — all green.

- [ ] **Step 7: Commit**

```bash
git add app.py tests/test_ratings.py
git commit -m "perf(api): non-blocking search + lazy director endpoint; add returns id"
```

---

## Task 2: Dropdown labels — "Search" / "Library"

**Files:** Modify `templates/index.html` (`renderSearchFilter`, `renderGridControls`)

- [ ] **Step 1: Rename the search button label**

In `renderSearchFilter()`, find:

```javascript
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3"/></svg>
      Filter <span class="dd-caret">▾</span>
    </button>
    <div class="size-dd-menu search-filter-menu" id="pillMenu_search">
```

Replace `Filter <span class="dd-caret">▾</span>` on that line with
`Search <span class="dd-caret">▾</span>`.

- [ ] **Step 2: Rename the grid/library button label**

In `renderGridControls()`, find the analogous funnel button line:

```javascript
      Filter <span class="dd-caret">▾</span>
    </button>
    <div class="size-dd-menu grid-controls-menu" id="pillMenu_gridctl">
```

Replace `Filter <span class="dd-caret">▾</span>` there with
`Library <span class="dd-caret">▾</span>`.

- [ ] **Step 3: Manual verification**

Run `python app.py`, open the app. The grid top bar shows **Library ▾**; opening
the search modal shows **Search ▾**. Both keep the funnel icon. Stop the app.

- [ ] **Step 4: Commit**

```bash
git add templates/index.html
git commit -m "ui: rename Filter dropdowns to Search and Library"
```

---

## Task 3: Per-tab date verb (Added / Updated / Finished)

**Files:** Modify `templates/index.html` (`renderGrid` date line)

- [ ] **Step 1: Add a verb map and prefix the date**

In `renderGrid`, find:

```javascript
    const dateStr = fmtDate(item.date_added);

    const dateHtml = dateStr ? `<div class="poster-date">${dateStr}</div>` : '';
```

Replace with:

```javascript
    const dateStr = fmtDate(item.date_added);
    const dateVerb = { watchlist: 'Added', watching: 'Updated', finished: 'Finished' }[currentTab] || 'Added';
    const dateHtml = dateStr ? `<div class="poster-date">${dateVerb} ${dateStr}</div>` : '';
```

- [ ] **Step 2: Manual verification**

Run the app. Watchlist cards read **"Added <date>"**, Watching **"Updated <date>"**,
Finished **"Finished <date>"**. Stop the app.

- [ ] **Step 3: Commit**

```bash
git add templates/index.html
git commit -m "ui: per-tab date verb (Added/Updated/Finished)"
```

---

## Task 4: Poster rating "X.Y⭐"

**Files:** Modify `templates/index.html` (`renderGrid` stars; add `.poster-rating` CSS)

- [ ] **Step 1: Replace the star row with a rating string**

In `renderGrid`, find:

```javascript
    const stars = currentTab === 'finished'
      ? `<div class="poster-stars">${[1,2,3,4,5,6,7,8,9,10].map(s =>
          `<span class="pstar ${(item.rating||0)>=s?'lit':''}">★</span>`).join('')}</div>` : '';
```

Replace with:

```javascript
    const rRaw = item.rating || 0;
    const ratingTxt = rRaw > 0 ? fmtRating(rRaw) + '⭐' : 'Unrated';
    const stars = currentTab === 'finished'
      ? `<div class="poster-rating">${ratingTxt}</div>` : '';
```

(`fmtRating` already exists and drops a trailing `.0`, so `10⭐`, `7.5⭐`.)

- [ ] **Step 2: Add CSS**

In the CSS, just after the `.poster-stars` / `.pstar` rules (search for
`.pstar.lit`), add:

```css
.poster-rating { font-size: 0.9em; color: var(--accent); text-align: right; margin-top: 0.125rem; }
```

- [ ] **Step 3: Manual verification**

Run the app, go to Finished. Cards (text + default sizes) show a bottom-right
`7.5⭐` / `10⭐`, and `Unrated` for unrated items, instead of the star row.
Poster-only mode unchanged. Stop the app.

- [ ] **Step 4: Commit**

```bash
git add templates/index.html
git commit -m "ui: show X.Y⭐ on finished cards instead of star row"
```

---

## Task 5: Per-tab library pills

**Files:** Modify `templates/index.html` (pill state + 8 reference sites)

- [ ] **Step 1: Replace the global `gridPills` with a per-tab object + migration**

Find:

```javascript
let gridPills   = _loadPills('gridPills');
let searchPills = _loadPills('searchPills');
```

Replace with:

```javascript
const tabGridPills = {
  watchlist: _loadPills('gridPills_watchlist'),
  watching:  _loadPills('gridPills_watching'),
  finished:  _loadPills('gridPills_finished'),
};
// Migrate a legacy global gridPills into any per-tab key that's unset.
(function migrateGridPills() {
  let legacy;
  try { legacy = JSON.parse(localStorage.getItem('gridPills')); } catch {}
  if (!Array.isArray(legacy)) return;
  for (const tab of ['watchlist', 'watching', 'finished']) {
    if (localStorage.getItem('gridPills_' + tab) === null) {
      tabGridPills[tab] = legacy.filter(x => _VALID_PILLS.has(x));
      localStorage.setItem('gridPills_' + tab, JSON.stringify(tabGridPills[tab]));
    }
  }
})();
let searchPills = _loadPills('searchPills');
```

- [ ] **Step 2: Update the one-time TMDB-default migration to cover all tabs**

Find:

```javascript
(function ensureTmdbPillDefault() {
  if (localStorage.getItem('pills_tmdb_default_v1')) return;
  if (!gridPills.includes('tmdb'))   { gridPills.push('tmdb');   localStorage.setItem('gridPills', JSON.stringify(gridPills)); }
  if (!searchPills.includes('tmdb')) { searchPills.push('tmdb'); localStorage.setItem('searchPills', JSON.stringify(searchPills)); }
  localStorage.setItem('pills_tmdb_default_v1', '1');
})();
```

Replace with:

```javascript
(function ensureTmdbPillDefault() {
  if (localStorage.getItem('pills_tmdb_default_v1')) return;
  for (const tab of ['watchlist', 'watching', 'finished']) {
    if (!tabGridPills[tab].includes('tmdb')) {
      tabGridPills[tab].push('tmdb');
      localStorage.setItem('gridPills_' + tab, JSON.stringify(tabGridPills[tab]));
    }
  }
  if (!searchPills.includes('tmdb')) { searchPills.push('tmdb'); localStorage.setItem('searchPills', JSON.stringify(searchPills)); }
  localStorage.setItem('pills_tmdb_default_v1', '1');
})();
```

- [ ] **Step 3: Point every grid read-site at the current tab's array**

Replace `gridPills` with `tabGridPills[currentTab]` at each of these exact lines:

(3a) in `renderGrid` pill host:
```javascript
    const pillsHost = `<div class="poster-badge-row" id="pills-${item.id}">${renderPills(item, _ratingsCache[item.media_type+':'+(item.tmdb_id||item.external_id)] || {}, tabGridPills[currentTab])}</div>`;
```
(3b) in `renderGrid` hydrate loop:
```javascript
    items.forEach(item => hydratePills(`pills-${item.id}`, item, tabGridPills[currentTab]));
```
(3c) in `renderGridControls`:
```javascript
        <div class="sfm-col sfm-col-pills">${_pillSectionsHtml(tabGridPills[currentTab], 'grid')}</div>
```
(3d) in `proactiveRefreshStale` (the `if (host) host.innerHTML = renderPills(item, ratings, gridPills);` line):
```javascript
    if (host) host.innerHTML = renderPills(item, ratings, tabGridPills[currentTab]);
```
(3e) in `refreshRatingsNow` (the `if (ghost) ghost.innerHTML = renderPills(item, ratings, gridPills);` line):
```javascript
    if (ghost) ghost.innerHTML = renderPills(item, ratings, tabGridPills[currentTab]);
```
(3f) in `renderPillSelector` (the `const sel = which === 'grid' ? gridPills : searchPills;` line):
```javascript
  const sel = which === 'grid' ? tabGridPills[currentTab] : searchPills;
```

- [ ] **Step 4: Update `togglePill` to mutate + persist the per-tab array**

Find:

```javascript
function togglePill(which, src, ev) {
  ev.stopPropagation();
  const arr = which === 'grid' ? gridPills : searchPills;
  const i = arr.indexOf(src);
  if (i >= 0) arr.splice(i, 1); else arr.push(src);
  localStorage.setItem(which === 'grid' ? 'gridPills' : 'searchPills', JSON.stringify(arr));
```

Replace those lines with:

```javascript
function togglePill(which, src, ev) {
  ev.stopPropagation();
  const arr = which === 'grid' ? tabGridPills[currentTab] : searchPills;
  const i = arr.indexOf(src);
  if (i >= 0) arr.splice(i, 1); else arr.push(src);
  localStorage.setItem(which === 'grid' ? ('gridPills_' + currentTab) : 'searchPills', JSON.stringify(arr));
```

(Leave the rest of `togglePill` unchanged.)

- [ ] **Step 5: Verify no stragglers**

Run: `grep -n "gridPills\b" templates/index.html`
Expected: only `tabGridPills`, the `gridPills_<tab>` localStorage keys, and the
legacy-migration read of `'gridPills'` remain — no bare `gridPills` variable use.

- [ ] **Step 6: Manual verification**

Run the app. Open **Library ▾** on Watchlist, toggle off `Year`. Switch to
Finished — its pills are independent (Year still on there). Reload — selections
persist per tab. Stop the app.

- [ ] **Step 7: Commit**

```bash
git add templates/index.html
git commit -m "ui: per-tab library pill selection"
```

---

## Task 6: Detail-panel partial-star rating slider

**Files:** Modify `templates/index.html` (`renderDetail` rating block + post-render paint; new star helpers; CSS)

- [ ] **Step 1: Replace the range input with a star slider**

In `renderDetail`, find the `ratingHtml` block:

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

Replace with:

```javascript
  const ratingHtml = item.status === 'finished' ? `
    <div class="rating-wrap">
      <div class="rating-label">Your rating
        <span class="rating-val" id="rv">${ratingLabel(item.rating)}</span>
      </div>
      <div class="star-slider" id="starSlider" role="slider" tabindex="0"
           aria-valuemin="0" aria-valuemax="10" aria-valuenow="${item.rating||0}"
           onpointerdown="starPointerDown(event,${item.id})"
           onkeydown="starKey(event,${item.id})">
        ${[1,2,3,4,5,6,7,8,9,10].map(() =>
          `<span class="star-cell"><span class="star-base">★</span><span class="star-fill">★</span></span>`).join('')}
      </div>
    </div>` : '';
```

- [ ] **Step 2: Paint the initial fill after render**

In `renderDetail`, find the post-render line:

```javascript
  hydratePills('detailScores', item, DETAIL_SCORES);
```

Insert immediately BEFORE it:

```javascript
  const _ss = document.getElementById('starSlider');
  if (_ss) _paintStars(_ss, item.rating || 0);
```

- [ ] **Step 3: Add the star helpers**

In `templates/index.html`, just before `function renderDetail(item) {`, add:

```javascript
// Fill each of the 10 stars; the next star is partially filled for decimals.
function _paintStars(slider, R) {
  slider.querySelectorAll('.star-cell').forEach((cell, idx) => {
    const frac = Math.max(0, Math.min(1, R - idx));
    cell.querySelector('.star-fill').style.width = (frac * 100) + '%';
  });
}

// Rating from the pointer x, snapped to 0.1 (never an infinite-step slider).
function _starValueFromEvent(slider, e) {
  const rect = slider.getBoundingClientRect();
  const raw = (e.clientX - rect.left) / rect.width * 10;
  return Math.max(0, Math.min(10, Math.round(raw * 10) / 10));
}

function _starApply(slider, id, R, commit) {
  _paintStars(slider, R);
  slider.setAttribute('aria-valuenow', R);
  const rv = document.getElementById('rv');
  if (rv) rv.textContent = ratingLabel(R);
  if (commit) saveField(id, 'rating', R);
}

function starPointerDown(e, id) {
  e.preventDefault();
  const slider = document.getElementById('starSlider');
  if (!slider) return;
  try { slider.setPointerCapture(e.pointerId); } catch {}
  _starApply(slider, id, _starValueFromEvent(slider, e), false);
  slider.onpointermove = (ev) => _starApply(slider, id, _starValueFromEvent(slider, ev), false);
  slider.onpointerup = (ev) => {
    _starApply(slider, id, _starValueFromEvent(slider, ev), true);
    slider.onpointermove = null;
    slider.onpointerup = null;
  };
}

function starKey(e, id) {
  const slider = document.getElementById('starSlider');
  if (!slider) return;
  let R = Number(slider.getAttribute('aria-valuenow')) || 0;
  if (e.key === 'ArrowRight' || e.key === 'ArrowUp')      R = Math.min(10, Math.round((R + 0.1) * 10) / 10);
  else if (e.key === 'ArrowLeft' || e.key === 'ArrowDown') R = Math.max(0, Math.round((R - 0.1) * 10) / 10);
  else return;
  e.preventDefault();
  _starApply(slider, id, R, true);
}
```

- [ ] **Step 4: Add CSS**

In the CSS, just after the `input[type="range"]::-webkit-slider-thumb { … }` rule
(search for `::-webkit-slider-thumb`), add:

```css
.star-slider { display: inline-flex; gap: 0.15rem; cursor: pointer; touch-action: none; user-select: none; font-size: 1.9rem; line-height: 1; }
.star-slider:focus-visible { outline: 2px solid var(--accent-dim); outline-offset: 3px; border-radius: 4px; }
.star-cell { position: relative; display: inline-block; }
.star-base { color: var(--card); }
.star-fill { position: absolute; left: 0; top: 0; width: 0; overflow: hidden; white-space: nowrap; color: var(--accent); }
```

- [ ] **Step 5: Manual verification**

Run the app, open a finished title. The rating shows 10 stars with the current
rating filled (partial last star for decimals). Click/drag across the stars — the
fill and the `X.Y / 10` label update and snap to 0.1; releasing saves (reopen the
item to confirm it persisted). Arrow keys nudge by 0.1. Stop the app.

- [ ] **Step 6: Commit**

```bash
git add templates/index.html
git commit -m "ui: partial-star rating slider in the details panel"
```

---

## Task 7: Finished-tab stats — average, 10/10s, histogram, dual-range filter

**Files:** Modify `templates/index.html` (`renderGrid` stats block + `sortedItems` filter; new range state/helpers; CSS)

- [ ] **Step 1: Add the finished-range state + helpers**

In `templates/index.html`, just before `function renderGrid() {`, add:

```javascript
// Finished-tab rating range filter [lo, hi] (0–10, step 0.1). 0 == Unrated.
let finishedRange = (function () {
  try {
    const a = JSON.parse(localStorage.getItem('finishedRatingRange'));
    if (Array.isArray(a) && a.length === 2) return [Math.min(a[0], a[1]), Math.max(a[0], a[1])];
  } catch {}
  return [0, 10];
})();

function _starLbl(v) {
  v = Number(v) || 0;
  if (v <= 0) return 'Unrated';
  return (Math.round(v * 10) % 10 === 0 ? String(Math.round(v)) : v.toFixed(1)) + '⭐';
}

function onFinRange(which, val) {
  let [lo, hi] = finishedRange;
  val = Math.max(0, Math.min(10, Math.round(Number(val) * 10) / 10));
  if (which === 'lo') lo = Math.min(val, hi);
  else                hi = Math.max(val, lo);
  finishedRange = [lo, hi];
  localStorage.setItem('finishedRatingRange', JSON.stringify(finishedRange));
  renderGrid();
}

function showHistTip(el) {
  const tip = document.getElementById('histTip');
  if (!tip) return;
  const k = +el.dataset.k, c = +el.dataset.count;
  tip.textContent = `${c} title${c !== 1 ? 's' : ''} ${k - 1}.1–${k}.0`;
  tip.style.left = (el.offsetLeft + el.offsetWidth / 2) + 'px';
  tip.classList.add('show');
}
function hideHistTip() {
  const tip = document.getElementById('histTip');
  if (tip) tip.classList.remove('show');
}
```

- [ ] **Step 2: Replace the finished stats HTML**

In `renderGrid`, find:

```javascript
  let statsHtml = '';
  if (currentTab === 'finished') {
    const finished = allItems.filter(i => i.status === 'finished');
    const rated    = finished.filter(i => i.rating > 0);
    const avg      = rated.length
      ? (rated.reduce((s, i) => s + i.rating, 0) / rated.length).toFixed(1) : '—';
    statsHtml = `<div class="stats-bar">
      <div class="stat-box"><div class="stat-num">${finished.length}</div><div class="stat-lbl">Finished</div></div>
      <div class="stat-box"><div class="stat-num">${allItems.filter(i=>i.status==='watching').length}</div><div class="stat-lbl">Watching</div></div>
      <div class="stat-box"><div class="stat-num">${avg}</div><div class="stat-lbl">Avg rating</div></div>
    </div>`;
  }
```

Replace with:

```javascript
  let statsHtml = '';
  if (currentTab === 'finished') {
    const finished = allItems.filter(i => i.status === 'finished');
    const rated    = finished.filter(i => i.rating > 0);
    const avg      = rated.length
      ? (rated.reduce((s, i) => s + i.rating, 0) / rated.length).toFixed(1) : '—';
    const tens     = finished.filter(i => i.rating === 10).length;
    const buckets  = Array(10).fill(0);
    rated.forEach(i => { const k = Math.min(10, Math.max(1, Math.ceil(i.rating))); buckets[k - 1]++; });
    const maxB = Math.max(1, ...buckets);
    const [lo, hi] = finishedRange;
    const bars = buckets.map((c, idx) => {
      const k = idx + 1;
      const on = (lo <= k) && (hi > k - 1);
      const h = c ? Math.max(8, Math.round(c / maxB * 100)) : 0;
      return `<div class="fh-bar-wrap" data-k="${k}" data-count="${c}"
                onmouseenter="showHistTip(this)" onmouseleave="hideHistTip()">
                <div class="fh-bar${on ? ' on' : ''}" style="height:${h}%"></div>
                <div class="fh-lbl">${k}⭐</div>
              </div>`;
    }).join('');
    statsHtml = `<div class="fin-stats">
      <div class="fin-left">
        <div class="fin-avg">★ ${avg}</div>
        <div class="fin-tens">${tens} × 10/10</div>
      </div>
      <div class="fin-right">
        <div class="fh-tip" id="histTip"></div>
        <div class="fh-bars">${bars}</div>
        <div class="fh-slider">
          <input type="range" class="fh-range" id="finLo" min="0" max="10" step="0.1" value="${lo}" oninput="onFinRange('lo', this.value)">
          <input type="range" class="fh-range" id="finHi" min="0" max="10" step="0.1" value="${hi}" oninput="onFinRange('hi', this.value)">
        </div>
        <div class="fh-range-lbls"><span>${_starLbl(lo)}</span><span>${_starLbl(hi)}</span></div>
      </div>
    </div>`;
  }
```

- [ ] **Step 3: Apply the range filter in `sortedItems`**

In `sortedItems` (the function that builds `items` from the current tab), find the
filter block:

```javascript
  if (f.types.size < _ALL_TYPES.length)
    items = items.filter(i => f.types.has(i.media_type));
  return items;
```

Replace with:

```javascript
  if (f.types.size < _ALL_TYPES.length)
    items = items.filter(i => f.types.has(i.media_type));
  if (currentTab === 'finished') {
    const [lo, hi] = finishedRange;
    if (!(lo === 0 && hi === 10))
      items = items.filter(i => { const r = i.rating || 0; return r >= lo && r <= hi; });
  }
  return items;
```

- [ ] **Step 4: Add CSS**

In the CSS, just after the existing `.stats-bar` / `.stat-box` rules (search for
`.stat-lbl`), add:

```css
.fin-stats { display: flex; gap: 1.25rem; align-items: stretch; margin-bottom: 1rem; padding: 0.9rem 1.1rem; background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg); }
.fin-left { display: flex; flex-direction: column; justify-content: center; gap: 0.25rem; padding-right: 1.1rem; border-right: 1px solid var(--border); min-width: 7rem; }
.fin-avg { font-size: 1.9rem; font-weight: 500; color: var(--accent); }
.fin-tens { font-size: 0.95rem; color: var(--muted); }
.fin-right { position: relative; flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 0.35rem; }
.fh-bars { display: flex; align-items: flex-end; gap: 0.4rem; height: 4.5rem; }
.fh-bar-wrap { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: flex-end; height: 100%; cursor: default; }
.fh-bar { width: 100%; min-height: 0; background: var(--card); border-radius: 3px 3px 0 0; transition: background 0.12s; }
.fh-bar.on { background: var(--accent); }
.fh-lbl { font-size: 0.7rem; color: var(--muted); margin-top: 0.2rem; white-space: nowrap; }
.fh-bar-wrap:hover .fh-bar { background: var(--accent-hover); }
.fh-tip { position: absolute; top: -1.6rem; transform: translateX(-50%); background: var(--card); border: 1px solid var(--border-md); border-radius: var(--radius); padding: 0.15rem 0.5rem; font-size: 0.78rem; color: var(--text); white-space: nowrap; pointer-events: none; opacity: 0; transition: opacity 0.12s; z-index: 5; }
.fh-tip.show { opacity: 1; }
.fh-slider { position: relative; height: 1.4rem; }
.fh-range { position: absolute; left: 0; top: 0; width: 100%; margin: 0; background: none; pointer-events: none; -webkit-appearance: none; appearance: none; height: 1.4rem; }
.fh-range::-webkit-slider-thumb { pointer-events: auto; }
.fh-range::-moz-range-thumb { pointer-events: auto; }
.fh-range-lbls { display: flex; justify-content: space-between; font-size: 0.8rem; color: var(--muted); }
```

- [ ] **Step 5: Manual verification**

Run the app, go to Finished with several rated titles. Confirm:
- Left shows `★ <avg>` and `N × 10/10`.
- Ten bars sized by count, `k⭐` labels; hovering a bar shows `N titles X.1–Y.0`.
- Dragging the two slider handles filters the grid to `[X,Y]`; labels read
  `X⭐`/`Y⭐` (or `Unrated` at 0); bars in range turn purple. Low handle at 0
  includes unrated titles. Range persists across reload.
Stop the app.

- [ ] **Step 6: Commit**

```bash
git add templates/index.html
git commit -m "feat(ui): finished-tab histogram + dual-range rating filter"
```

---

## Task 8: Perf — instant add/remove from search modal

**Files:** Modify `templates/index.html` (`addFromModal`, `removeFromModal`)

- [ ] **Step 1: Make add optimistic (no full reload)**

Replace the body of `addFromModal` between the `try {` and its `} catch {` with a
version that appends locally using the id returned by `/api/add`:

```javascript
  try {
    const ext = r.external_id || String(r.tmdb_id || '');
    const data = await apiPost('/api/add', {
      tmdb_id:     r.tmdb_id    || null,
      external_id: ext,
      title:       r.title,
      media_type:  r.media_type,
      status:      'watchlist',
      cover_url:   r.cover_url  || null,
      author:      r.author     || null,
      total_pages: r.total_pages || null,
      overview:    r.overview   || null,
      year:        r.year       || null,
      tmdb_rating: r.tmdb_rating ?? null,
    });
    const newItem = {
      id: data && data.id, tmdb_id: r.tmdb_id || null, external_id: ext,
      title: r.title, media_type: r.media_type, status: 'watchlist',
      rating: 0, author: r.author || null, overview: r.overview || null,
      year: r.year || null, cover_url: r.cover_url || null,
      tmdb_rating: r.tmdb_rating ?? null,
      date_added: data && data.date_added, ratings: null, ratings_updated_at: null,
    };
    if (newItem.id != null && !allItems.some(i => i.id === newItem.id)) allItems.push(newItem);
    rebuildLibrarySet();
    showToast(`"${r.title}" added to Watchlist`);
    switchTab('watchlist', newItem.id != null ? newItem.id : null);
  } catch {
```

(Leave the existing `} catch { … rollback … }` block as-is — it already deletes
`key` from `inLibrary` and resets the button.)

NOTE: `apiPost` returns already-parsed JSON (it ends with `return res.json()`),
so `data` is the response object directly — `data.id` / `data.date_added`.

- [ ] **Step 2: Make remove optimistic (no full reload)**

In `removeFromModal`, find:

```javascript
  try {
    await fetch(`/api/delete/${item.id}`, { method: 'DELETE' });
    await loadList();
    showToast(`"${r.title}" removed from Watchlist`);
  } catch {
```

Replace with:

```javascript
  try {
    await fetch(`/api/delete/${item.id}`, { method: 'DELETE' });
    allItems = allItems.filter(i => i.id !== item.id);
    rebuildLibrarySet();
    if (selectedId === item.id) selectedId = null;
    renderGrid();
    showToast(`"${r.title}" removed from Watchlist`);
  } catch {
```

- [ ] **Step 3: Manual verification**

Run the app, open search. Add a title — it appears in Watchlist immediately with
no full-library reload flicker. Remove it from the modal — the button flips and
the library updates instantly. Both still work across a manual page reload
(server persisted). Stop the app.

- [ ] **Step 4: Commit**

```bash
git add templates/index.html
git commit -m "perf(ui): optimistic add/remove from search modal (no full reload)"
```

---

## Task 9: Perf — lazy director hydration on search cards

**Files:** Modify `templates/index.html` (`renderSmCard` + a hydrate pass in `renderModalResults`)

- [ ] **Step 1: Add a session cache + lazy author fetch helper**

In `templates/index.html`, just before `function renderSmCard(r) {`, add:

```javascript
const _smAuthorCache = {};   // "media_type:tmdb_id" -> author string

// Movie/TV search cards arrive without a director (search is now fast); fetch it
// lazily and fill the card when it lands. Deduped per session.
function hydrateSmAuthor(r) {
  if (r.author) return;
  if (r.media_type !== 'movie' && r.media_type !== 'tv') return;
  const tid = r.tmdb_id || r.external_id;
  if (!tid) return;
  const ck = `${r.media_type}:${tid}`;
  const fill = (a) => {
    if (!a) return;
    r.author = a;
    const safeId = (r.external_id || r.tmdb_id || '').toString().replace(/[^a-z0-9]/gi, '_');
    const card = document.getElementById(`smbtn-${r.media_type}-${safeId}`)?.closest('.sm-card');
    const body = card && card.querySelector('.sm-card-title');
    if (body && !card.querySelector('.sm-card-author')) {
      const div = document.createElement('div');
      div.className = 'sm-card-author';
      div.textContent = a;
      body.insertAdjacentElement('afterend', div);
    }
  };
  if (ck in _smAuthorCache) { fill(_smAuthorCache[ck]); return; }
  fetch(`/api/tmdb-director/${r.media_type}/${encodeURIComponent(tid)}`)
    .then(res => res.json()).then(d => { _smAuthorCache[ck] = d.author || ''; fill(d.author); })
    .catch(() => {});
}
```

- [ ] **Step 2: Kick off hydration after the results render**

In `renderModalResults`, find the existing post-render pills hydrate block:

```javascript
  if (searchPills.some(s => SCORE_SOURCES.includes(s))) {
    tmdbResults.forEach(r => {
      const item = { media_type: r.media_type, year: r.year, tmdb_id: r.tmdb_id, external_id: r.external_id, tmdb_rating: r.tmdb_rating };
      const pillsId = `spills-${r.media_type}-${(r.external_id || r.tmdb_id || '').toString().replace(/[^a-z0-9]/gi,'_')}`;
      hydratePills(pillsId, item, searchPills);
    });
  }
```

Insert immediately AFTER that block:

```javascript
  tmdbResults.forEach(r => hydrateSmAuthor(r));
```

- [ ] **Step 3: Manual verification**

Run the app with a TMDB key. Search a movie/TV title — results appear **fast**
(no multi-second wait), then the director/creator line fills in a moment later on
each card. Re-searching the same title is instant (cached). Stop the app.

- [ ] **Step 4: Commit**

```bash
git add templates/index.html
git commit -m "perf(ui): lazily hydrate search-card directors after fast results"
```

---

## Final verification

- [ ] **Run the full backend suite**

Run: `python -m pytest tests/ -v`
Expected: all pass (existing + the 4 new Task 1 tests).

- [ ] **JS syntax check the template**

Run:
```bash
python -c "import re,tempfile,os; html=open('templates/index.html',encoding='utf-8').read(); s=re.findall(r'<script>(.*?)</script>',html,re.S); p=os.path.join(tempfile.gettempdir(),'_c.js'); open(p,'w',encoding='utf-8').write('\n;\n'.join(s)); print(p)"
node --check "<printed path>"
```
Expected: no syntax errors.

- [ ] **End-to-end smoke test**

Run the app and walk each item once: Search/Library labels; per-tab pills; finished
histogram + range filter + `X.Y⭐` cards; star slider; date verbs; fast search; instant
add/remove. Stop the app.

---

## Self-review notes (spec coverage)

- §1 labels → Task 2. §2 per-tab pills → Task 5. §3 finished stats → Task 7.
- §4 poster `X.Y⭐` → Task 4. §5 star slider → Task 6. §6 caption → no change.
- §7 date verbs → Task 3. §8a/§8b add/remove perf → Task 8; §8c search perf →
  Task 1 (backend) + Task 9 (client lazy directors).
