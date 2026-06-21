# Media Scores & Card Display — Design

Date: 2026-06-20
Status: Approved

## Overview

Add per-source rating pills (IMDb, Rotten Tomatoes tomatometer, Rotten Tomatoes
popcornmeter, Metacritic, Letterboxd, MyAnimeList) to library grid cards, search
result cards, and the details panel, sourced from the user's optional MDBList API
key (BYOK, already implemented).

Two related UI changes ship alongside:

1. A **pill selector** dropdown (grid + search) so the user chooses which sources'
   pills appear — replacing a hard-coded set.
2. A **card size-mode** change: replace `Compact / Default / Large` with
   `Text only / Default / Poster only`.

Books and manga are out of scope for ratings (MDBList covers movies/TV only).

## Data source

MDBList detail endpoint returns every rating in a single call:

```
GET https://api.mdblist.com/{provider}/{media_type}/{media_id}?apikey=KEY
```

- `provider = tmdb`, `media_id = tmdb_id`.
- `media_type`: app `movie` → `movie`; app `tv` → `show`.
- Response includes a `ratings` array; we normalize it to a flat dict keyed by
  source, keeping only sources that returned a value:

| Key          | Source                 | Scale      | Display      |
|--------------|------------------------|------------|--------------|
| `imdb`       | IMDb                   | 0–10       | `8.1`        |
| `tomatoes`   | RT Tomatometer         | 0–100      | `94%`        |
| `audience`   | RT Popcornmeter        | 0–100      | `88%`        |
| `metacritic` | Metacritic             | 0–100      | `76`         |
| `letterboxd` | Letterboxd             | 0–5        | `4.2`        |
| `mal`        | MyAnimeList            | 0–10       | `8.5`        |

> Assumption to verify during implementation against a live key: search responses
> expose only the aggregate `score`, not the per-source breakdown. The per-source
> pills therefore require one detail call per title.

## Backend (`app.py`, `db.py`)

### Fetch + normalize
- `_fetch_mdblist_ratings(media_type, tmdb_id, key) -> dict`
  - Maps `tv → show`; returns `{}` for unsupported types (book/manga).
  - GETs the detail endpoint (5–8s timeout, mirrors existing TMDB fetches).
  - Parses `ratings[]` into the normalized dict above. Tolerant of missing/null
    values and unknown sources.

### Endpoint
- `GET /api/ratings/<media_type>/<tmdb_id>` → `{ "ratings": { ... } }`
  1. No MDBList key for the user → `{ "ratings": {} }` (UI renders nothing).
  2. Title exists in `media` with `ratings_updated_at` ≤ 7 days old → return stored.
  3. Title in `media` but stale/missing → fetch, **persist** (`ratings` JSON +
     `ratings_updated_at = now`), return.
  4. Title not in `media` (a search result) → check server **TTL cache** (24h,
     keyed `media_type:tmdb_id`); on miss, fetch + cache, return.
- This single endpoint *is* the hybrid model: persistence for library rows, an
  in-memory TTL cache (reusing the existing `_BoundedCache`) for everything else.
- Rate-limited like the other authed fetch routes.

### DB
- Add to the `media` table: `ratings TEXT` (JSON), `ratings_updated_at TEXT`.
- Idempotent migration mirroring `_migrate_media_dates` (PRAGMA check + `ALTER`).

## Frontend (`templates/index.html`)

### Brand logos & pill rendering
- `SCORE_META`: per-source map → `{ label, svg, tint, format }`.
  - Inline brand-logo SVGs: IMDb (yellow wordmark chip), 🍅 tomatometer (red
    tomato), 🍿 popcornmeter (bucket), Metacritic (M chip, **color-banded** by
    score: green ≥61 / yellow 40–60 / red ≤39), Letterboxd (tri-dot), MAL (blue
    chip).
  - Pills share the height / radius of the existing year + type pills so the row
    reads as one set.
### Toggleable pills

The full pill set is **selector-driven** — including the previously hard-coded
`year` and `media type` badges. The eight toggleable pills, in fixed
left-to-right render order:

`year, type, imdb, tomatoes, audience, metacritic, letterboxd, mal`

- `year` / `type` come from local item data (no fetch).
- The six score pills come from `/api/ratings`.
- `renderPills(item, ratings, selected)` → HTML for the *selected* pills that have
  a value, in the order above. Pills with no value (or a score with no
  data/no MDBList key) are omitted — no auto-substitution, no empty container.
- Score pills load **lazily per card** via `/api/ratings`, with an in-memory
  session map keyed by `media_type:tmdb_id` to dedupe within a page session.

### Pill selector (grid + search)
- A dropdown styled like the existing size dropdown (`.size-dd-*`) but
  **multi-select**: each row is a pill with a checkbox; clicking toggles without
  closing the menu. Button label shows e.g. `Pills (5) ▾`.
- The menu is split into **two labelled sections**:
  - **TMDB** — `Year`, `Media type` (local data, always available).
  - **MDBList** `(limited use)` — `IMDb`, `🍅 Tomatometer`, `🍿 Popcornmeter`,
    `Metacritic`, `Letterboxd`, `MyAnimeList` (consume the API quota).
- A small caption under the MDBList header shows the remaining daily calls (e.g.
  `247 left today`).

### Quota-exhausted state
- A `GET /api/mdblist-status` endpoint returns `{ has_key, limit, used, remaining }`
  from MDBList `/user` (`remaining = api_requests − api_requests_count`),
  server-cached ~2 min per user to avoid spending the quota on status checks.
- The frontend refreshes status on load and whenever a pill dropdown opens.
- When `remaining ≤ 0`: the **MDBList section rows are greyed out and
  non-interactive** in *both* the grid and search selectors, and the caption
  changes to a clear reason, e.g. `⚠ No API calls left today — resets daily.
  Score pills paused.` TMDB rows stay fully usable.
- While exhausted, the client **skips new ratings fetches** (they would fail);
  already-cached/persisted pills keep rendering.
- **Grid selector**: in the grid top bar's right group, next to the size dropdown.
  Controls pills on library cards.
- **Search selector**: next to the search type buttons (`#searchTypeBtns`), with a
  small uppercase caption **`FILTER SEARCH RATINGS`** above it. Controls pills on
  search result cards (`renderSmCard`).
- Selections are **independent** between grid and search, each persisted in
  `localStorage` (`gridPills`, `searchPills`) as a global preference (not per-tab).
- Default selection for both: `year, type, imdb, metacritic, tomatoes`.

### Card size-mode change
Replace `_VALID_SIZES = {small, medium, large}` and the dropdown options with:

| Mode key  | Label       | Renders                                          |
|-----------|-------------|--------------------------------------------------|
| `text`    | Text only   | No poster. Selected pills, title, author, date, stars |
| `default` | Default     | Poster + selected pills, title, author, date, stars   |
| `poster`  | Poster only | Poster only — pure image, nothing else                |

- `Large` (poster + full overview) is removed.
- **Migration of stored prefs**: on load, map legacy values `small → text`,
  `medium → default`, `large → default`; default remains `default`.
- The column-count dots (`.csz-dot`) are unchanged and apply in all modes.
- **Poster-only** is a pure poster image — no info block, no pills, no stars, and
  **no hover tooltip or caption**. Built for scroll-and-look browsing.
- Pills (incl. year/type) appear in `text` and `default` modes only.

## Edge cases
- User has no MDBList key → `/api/ratings` returns `{}`; no pills anywhere; no error.
- Title missing some/all selected sources → those pills omitted; card still renders.
- Book/manga → no rating fetch, no pills.
- Metacritic color band derived client-side from the numeric value.
- Stale persisted ratings refresh on details-panel open (path 3 above).

## Quota notes (per-user 1,000/day, BYOK)
- Library grid is ~free after the first persist per title.
- Search costs ~1 call per *new* title (server-cached by `media_type:tmdb_id`),
  per the chosen "full pills on search" behavior.
- One detail call returns all sources, so cost is per-title, never per-source.

## Resolved decisions
1. Grid vs search pill selections are **independent**, each persisted separately.
2. **Poster-only** is a pure poster — no tooltip, no caption.
3. Year and media type are **toggleable pills** in the selector.
4. Default selection (grid + search): `year, type, imdb, metacritic, tomatoes`.
5. The search selector carries a `FILTER SEARCH RATINGS` caption above it.

## Out of scope
- Ratings for books/manga.
- Sorting/filtering the library by external scores (separate feature).
- Batch rating prefetch / background refresh jobs.
