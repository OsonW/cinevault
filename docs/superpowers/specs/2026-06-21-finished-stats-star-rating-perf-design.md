# Finished-Tab Stats, Star Rating, Per-Tab Filters & Perf — Design

Date: 2026-06-21
Status: Approved

## Overview

Eight mostly-independent improvements to CineVault, spanning UI clarity, the
finished-tab analytics, the rating widgets, per-tab filter state, and two real
performance fixes. All client work is in `templates/index.html`; the perf item
also touches `app.py`.

The items are independent and can ship/test separately, but land together.

---

## 1 — Dropdown labels: "Search" / "Library"

The two dropdown buttons currently both read **"Filter ▾"** with a funnel icon,
which is redundant (the funnel already signals "filter").

- `renderSearchFilter()` button label: `Filter` → **`Search`**.
- `renderGridControls()` button label: `Filter` → **`Library`**.
- Funnel SVG and the `.dd-caret` stay unchanged.

---

## 2 — Per-tab library pills

Today `gridPills` is a single global array. Make the **library (grid) pill
selection per-tab**, matching how `tabSize` / `tabSort` / `tabCardCols` already
work. **Search pills (`searchPills`) stay global** — search is not tab-scoped.

- Replace `let gridPills = _loadPills('gridPills')` with
  `tabGridPills = { watchlist, watching, finished }`, each loaded from its own
  key (`gridPills_watchlist`, etc.) via `_loadPills`.
- **Migration:** on load, if a legacy `gridPills` key exists, seed any of the
  three per-tab keys that are unset from it, then leave the legacy key in place
  (harmless). The existing one-time `pills_tmdb_default_v1` migration must run
  against all three per-tab arrays.
- Everywhere `gridPills` was read/written now uses `tabGridPills[currentTab]`:
  - `renderGrid` pill hosts + `hydratePills` calls,
  - `renderGridControls` / `_pillSectionsHtml('grid')` selector + `togglePill('grid', …)`,
  - `proactiveRefreshStale` and `refreshRatingsNow` grid-card repaints
    (`renderPills(item, ratings, tabGridPills[currentTab])`).
- `togglePill('grid', src)` mutates `tabGridPills[currentTab]` and persists to
  `gridPills_<currentTab>`.

---

## 3 — Finished-tab stats redesign

Replace the three-box stats bar (Finished / Watching / Avg) — shown only on the
finished tab — with a two-column panel.

### Left column
- Large **`★ 7.4`** = average of finished items with `rating > 0`
  (`'—'` when none).
- A line **`N × 10/10`** = count of finished items with `rating === 10`.

### Right column — histogram
- **10 bars.** Bar `k` (k = 1..10) counts finished items whose rating falls in
  `((k-1).0, k.0]` — i.e. bar 1 = 0.1–1.0, bar 2 = 1.1–2.0, …, bar 10 = 9.1–10.0.
  (`rating === 0` / Unrated is in **no** bar.)
- Bar heights scale to the tallest bar (a min visible height for non-zero bars).
- Under each bar: a **`k⭐`** label.
- **Hover a bar** → a tooltip above it reads **`N titles {k-1}.1–{k}.0`**
  (e.g. bar 4 → `N titles 3.1–4.0`).

### Right column — dual-range rating filter
- A **dual-handle range slider**, domain `0.0–10.0`, **step `0.1`**, two handles
  `X` (low) and `Y` (high) with `X ≤ Y` enforced.
- **Labels:** left shows `X⭐` (e.g. `1.2⭐`), right shows `Y⭐`; a handle at `0`
  shows **`Unrated`** instead. Both at 0 → both read `Unrated`.
- **Filtering:** the slider filters the finished grid to items with
  `X ≤ rating ≤ Y`. Because Unrated = `rating 0`, a low handle at `0` includes
  unrated titles; `X = 0.1` excludes them. This composes with the existing
  media-type/title filters (`sortedItems`).
- **Bar highlight:** bar `k` (covering `((k-1), k]`) turns **purple** when
  `[X, Y]` overlaps that interval, i.e. `X ≤ k` **and** `Y > (k-1)`; otherwise it
  uses the empty-grey colour.
- **Persistence:** store `[X, Y]` in localStorage (`finishedRatingRange`),
  default `[0, 10]` (shows everything). Restored on load.

### Implementation notes
- Dual range = two overlaid native `<input type="range" min=0 max=10 step=0.1>`
  with a highlighted between-handles track and JS clamping (`X ≤ Y`), the
  standard two-thumb pattern — no heavyweight custom control.
- `on input`: update labels, re-highlight bars, and re-render the grid
  (the range participates in `sortedItems`/`renderGrid`).
- The histogram + range live in the finished-tab stats panel only; other tabs are
  unchanged.

---

## 4 — Poster rating "X.Y⭐"

On finished-tab cards (text + default sizes; poster-only is unchanged), replace
the `poster-stars` 10-star row with a single bottom-right-aligned rating:

- Format via the existing no-trailing-`.0` rule: `7.5⭐`, `10⭐`; **`Unrated`**
  (no star) for `rating 0`.
- New `.poster-rating` element, right-aligned at the bottom of the info block.

---

## 5 — Detail-panel 10-star rating slider

Replace the `<input type="range">` in `renderDetail`'s rating block with a
**10-star slider** supporting decimals.

- **Display:** 10 star glyphs. For rating `R`: `floor(R)` full stars, the next
  star **partially filled** by `R - floor(R)`, the rest empty. Partial fill is a
  grey base `★` with an accent `★` overlaid and clipped to the fill fraction
  (e.g. an inner span with `width: pct%`, `overflow:hidden`).
- **Interaction:** click or drag across the row sets the rating from the pointer
  x-position, **snapped to 0.1** (`round(raw*10)/10`, clamped 0–10). So both the
  value and the visual are quantized to 0.1 — never an infinite-step slider.
- **Save:** on release/change, `saveField(item.id, 'rating', value)` (same as
  today). The numeric label reuses `ratingLabel` (`Unrated` / `X.Y / 10` →
  rendered here as the live value).
- **Accessibility:** `role="slider"`, `aria-valuemin/max/now`, and Left/Right
  arrow keys adjust by ±0.1.

---

## 6 — MDBList caption (no change)

Keep the static **"(resets daily)"** note. The countdown idea is dropped (no
reliable reset timestamp from MDBList).

---

## 7 — Per-tab date verb

The grid card date currently renders `fmtDate(item.date_added)` with no verb.
Prefix it by tab (the underlying `date_added` already mirrors the current-status
date):

- watchlist → **`Added <date>`**
- watching → **`Updated <date>`**
- finished → **`Finished <date>`**

A `{watchlist:'Added', watching:'Updated', finished:'Finished'}[currentTab]` map
drives the prefix in `renderGrid`.

---

## 8 — Performance

Three fixes targeting the reported delays on add, remove, and search.

### 8a. Add from search — no full reload
`addFromModal` currently does `await loadList()` (re-fetch the entire library +
full re-render + proactive sweep) then `switchTab`. Instead:

- `/api/add` (create branch) returns the new row's `id` and `date_added` (it
  already has them via `add_media_entry`'s returned id + a follow-up read).
- The client constructs the new item object locally from the search result `r`
  plus the returned `id`/`date`, **pushes it to `allItems`**, updates
  `inLibrary`, and switches to the watchlist tab selecting it — **without** a
  network reload of the whole list.
- Optimistic button state stays; on error, roll back the local append (mirrors
  today's rollback).

### 8b. Remove from search — no full reload
`removeFromModal` currently does `await loadList()`. Instead, after a successful
`DELETE /api/delete/<id>`, **remove the item from `allItems`** locally, update
`inLibrary`, and re-render the grid if affected (the detail-panel `deleteItem`
already does exactly this).

### 8c. Slow TMDB search — return immediately, hydrate directors lazily
`/api/search` blocks on a `ThreadPoolExecutor` that fetches the director credits
for **all** ~10 results before returning, adding ~10 serial-ish TMDB round-trips
of latency to every search.

- **Server:** remove the blocking director fetch from `/api/search`; return the
  TMDB results immediately (1 upstream call). Add a cached endpoint
  `GET /api/tmdb-director/<media_type>/<tmdb_id>` → `{ "author": "…" }` that wraps
  the existing `_fetch_tmdb_director`, mirroring `/api/tmdb-rating`
  (per-title TTL cache; book/manga unaffected — they carry their own author).
- **Client:** `renderSmCard` shows the author when present; for movie/tv results
  it lazily fetches `/api/tmdb-director/...` per card and fills the author in when
  it arrives (non-blocking), deduped via a small session cache.
- **Verify** the modal search input is debounced and aborts in-flight requests
  (`_modalSearchController` already exists); tighten if a gap is found.

---

## Edge cases
- Finished tab with zero rated items → avg `—`, all histogram bars empty, slider
  full-range; grid still renders.
- Range filter excluding everything → grid shows the normal empty state.
- TMDB director endpoint with no key / no result → `{ "author": "" }`; the card
  simply shows no author.
- Per-tab pill migration must not clobber a tab the user already customised
  (only seed unset keys).
- Star slider on a non-finished item → not shown (rating UI is finished-only, as
  today).

## Testing
- **Backend (pytest):** `/api/search` returns results **without** author and
  without spending director calls; `/api/tmdb-director` fetch + cache + empty
  fallback; `/api/add` create returns the new `id` (and `date_added`).
- **Pure helpers:** histogram bucketing (rating → bar index), bar-overlap
  highlight predicate, and the 0.1 snap function are unit-testable in isolation
  where practical.
- The interactive widgets (dual-range, star slider, hover tooltip) and the
  optimistic add/remove are verified manually.

## Out of scope
- A reset countdown for the MDBList quota.
- Rating histogram/range on non-finished tabs.
- Persisting the finished range filter server-side (localStorage only).
