# Tab-coloured filter bar + length metadata — Design

Date: 2026-07-25

Two independent changes to `templates/index.html` and `app.py`.

1. The grid filter bar adopts the active tab's colour, so each tab's filters read as
   belonging to that tab.
2. The detail panel and search cards show a length figure beside the media type —
   runtime for movies, season count for TV, chapter count for manga, page count for
   books.

---

## Part 1 — Tab-coloured filter bar

### Current state

The three tabs already have distinct colours ([index.html:245-247](../../../templates/index.html#L245-L247)):

| Tab | Colour | Vars |
|---|---|---|
| Watchlist | blue `#6ea8d4` | `--blue`, `--blue-dim`, `--blue-glow` |
| Watching | green `#52b788` | `--green`, `--green-dim`, `--green-glow` |
| Finished | purple `#a78bfa` | `--purple`, `--purple-dim`, `--purple-glow` |

Every control in the grid filter bar hardcodes `--accent` (purple) regardless of which
tab is active, so the filters look identical on all three.

### Approach

`switchTab()` already sets `document.body.dataset.tab` ([index.html:2530](../../../templates/index.html#L2530)),
and the initial paint sets it too ([index.html:2223](../../../templates/index.html#L2223)).
This makes the change pure CSS — no JavaScript.

Introduce a filter-accent triple scoped to `.grid-panel`:

```css
.grid-panel                            { --f-accent: var(--purple); --f-dim: var(--purple-dim); --f-glow: var(--purple-glow); }
body[data-tab="watchlist"] .grid-panel { --f-accent: var(--blue);   --f-dim: var(--blue-dim);   --f-glow: var(--blue-glow); }
body[data-tab="watching"]  .grid-panel { --f-accent: var(--green);  --f-dim: var(--green-dim);  --f-glow: var(--green-glow); }
```

The base rule defaults to purple, so `finished` needs no override and the bar still
renders correctly if `data-tab` is ever missing.

### Rules that switch `--accent` → `--f-accent`

Scope of the change is the grid filter bar only. Poster cards, ratings, the detail
panel, and the search modal keep the global purple accent.

| Element | Rule |
|---|---|
| Grid Filter button | `.grid-panel .size-dd-btn` (border/background/colour) |
| Grid Filter button hover | `.grid-panel .size-dd-btn:hover` |
| Clear filters button hover | `.grid-clear-btn:hover` |
| Title search focus ring | `.grid-filter-input:focus` |
| Title search clear (✕) hover | `.grid-filter-clear:hover` |
| Random button hover | `.grid-random-btn:hover` |
| Card-size dots (active) | `.csz-dot.active::before` |
| Card-size dots (focus ring) | `.csz-dot:focus-visible` |
| Dropdown active row | `.grid-panel .size-dd-item.active` |
| Dropdown checkmark | `.grid-panel .pill-dd-check` |

### Why `.grid-panel` scoping is required

`.size-dd-btn`, `.size-dd-item.active`, and `.pill-dd-check` are shared with the
top-bar search Filter button, which lives in `.search-filter-wrap` inside `.topbar` —
outside `.grid-panel`. Scoping the overrides to `.grid-panel` leaves the search modal's
filter untouched at purple. The grid dropdown menu is rendered inside `.grid-top-bar`
([index.html:2884](../../../templates/index.html#L2884)), so it inherits correctly.

### Non-issues

- `.show-unrated-toggle` embeds a hardcoded `%23a78bfa` checkmark SVG, but the toggle
  only renders on the finished tab, which is purple — no per-tab variants needed.
- `.grid-random-btn` only renders on the watchlist tab.

---

## Part 2 — Length metadata

### Target output

Detail panel, appended to the media-type line, comma-separated:

```
Movie, 1h 43m
TV Show, 4 Seasons
Manga, 91 Chapters
Book, 512 Pages
```

Search cards, on its own line under the director/author, length value only (search
cards do not label the media type today, and adding one was explicitly not wanted):

```
Blade Runner 2049
Denis Villeneuve
2h 44m
```

Real pluralization (`1 Season` / `4 Seasons`), matching the existing house style at
[index.html:2881](../../../templates/index.html#L2881) — not a literal `Season(s)`.

### Data sources

| Type | Value | Source | Extra network cost |
|---|---|---|---|
| Book | `total_pages` | Already on the DB row (`db.py` schema) and already in OpenLibrary search results ([app.py:698](../../../app.py#L698)) | **none** |
| Manga | `attributes.lastChapter` | Already present in the MangaDex search response, just not mapped; `/api/manga-info` already returns it for library items ([app.py:1070](../../../app.py#L1070)) | **none** for search; existing endpoint for detail |
| TV | `number_of_seasons` | TMDB `/tv/{id}` — the director fetch already calls this exact endpoint ([app.py:524](../../../app.py#L524)) | **none**, same call |
| Movie | `runtime` | TMDB `/movie/{id}` | none, via `append_to_response=credits` |

The movie director fetch currently calls `/movie/{id}/credits`. Switching it to
`/movie/{id}?append_to_response=credits` returns credits *and* runtime in the same
single request.

**Net effect: search issues exactly the same number of TMDB calls it does today.** No
new latency, no new DB columns, no migration.

### Server changes (`app.py`)

1. **`_fetch_tmdb_director` → `_fetch_tmdb_meta`**, returning `{"author": str, "length": str}`.
   - movie: `GET /movie/{id}?append_to_response=credits` → director names from
     `credits.crew` where `job == "Director"`, runtime from `runtime`
   - tv: `GET /tv/{id}` → creators from `created_by`, seasons from `number_of_seasons`
   - Formats `length` server-side into its final display string.
   - Returns `{"author": "", "length": ""}` on any exception, matching current behaviour.

2. **`/api/tmdb-director/<type>/<id>` → `/api/tmdb-meta/<type>/<id>`**, returning both
   fields. `_tmdb_director_cache` becomes `_tmdb_meta_cache`, caching the dict; same
   24h TTL and same 2000-entry bound.

3. **`/api/item/<id>/fetch_director`** keeps its route and response shape (the detail
   panel's author path is unchanged) but calls `_fetch_tmdb_meta` and persists
   `author` exactly as before.

4. **Manga search** ([app.py:764](../../../app.py#L764)): add `"total_chapters": attrs.get("lastChapter")`
   to each result dict.

### Client changes (`templates/index.html`)

**Shared helper** — one formatting function used by both surfaces:

```js
function _fmtLength(mediaType, value)   // → '1h 43m' | '4 Seasons' | '91 Chapters' | '512 Pages' | ''
```

Movies: `2h 44m`, or `44m` when under an hour, or `2h` on an exact hour. Any falsy or
non-positive value returns `''`.

**Detail panel** ([index.html:3414](../../../templates/index.html#L3414)):

- The media-type line renders immediately as `Movie` plus an empty
  `<span id="detailLength"></span>`.
- Book: filled synchronously from `item.total_pages` during the initial render — no
  fetch.
- Movie/TV: `fetch('/api/tmdb-meta/<type>/<tmdb_id>')` after paint, filling the span
  with `, 1h 43m` when it lands.
- Manga: `fetch('/api/manga-info/<external_id>')` (existing endpoint, already
  server-cached) → `last_chapter`.
- A module-level `_lengthCache` keyed `"<type>:<id>"` makes re-selecting an item
  instant.
- Guarded by `selectedId === item.id` before writing to the DOM, matching the existing
  director-fetch guard at [index.html:3450](../../../templates/index.html#L3450).

**Search cards**:

- `renderSmCard` emits `<div class="sm-card-length"></div>` under the author line,
  pre-filled when the value is already local (book `total_pages`, manga
  `total_chapters`), empty otherwise.
- `hydrateSmLength(r)` mirrors the existing `hydrateSmAuthor` ([index.html:4122](../../../templates/index.html#L4122)):
  same per-session cache, same in-flight dedupe set, same card lookup by
  `smbtn-<type>-<safeId>`. Movie/TV only — book/manga never fetch.
- Called in the same `if (!loading)` block that already fires `hydrateSmAuthor`
  ([index.html:4100](../../../templates/index.html#L4100)), so it inherits the existing
  deferral that keeps search fast.
- Movie/TV share the `/api/tmdb-meta` response with the author hydration, so the two
  land together off one request. `_smAuthorCache` and the length cache are populated
  from the same fetch.
- **No `…` placeholder** for the length line, unlike the author. A second row of
  animated dots on every card is visual noise; the line simply appears when ready.
- `.sm-card-length` styling matches `.sm-card-author` ([index.html:423](../../../templates/index.html#L423)):
  `font-size: 0.6875rem; color: var(--muted)`.

### Error handling

Every path degrades to showing nothing:

- Missing/zero value → empty string, so the detail panel shows bare `Movie` with no
  trailing comma and the search card's length line stays empty.
- Fetch failure or non-JSON response → caught, treated as missing.
- Missing TMDB key → the endpoint already returns empty fields.
- No length is ever shown for a type without a source (e.g. a book with no
  `number_of_pages_median` from OpenLibrary).

### Existing tests that must be updated

`tests/test_ratings.py` references the renamed symbols directly and will fail until
updated:

| Line | Reference |
|---|---|
| 408 | `monkeypatch.setattr(app_module, "_fetch_tmdb_director", ...)` |
| 662 | same, in `test_search_does_not_block_on_directors` |
| 670-682 | `test_tmdb_director_endpoint` — patches `_fetch_tmdb_director`, hits `/api/tmdb-director/movie/603`, asserts `{"author": ...}` |
| 684-687 | `test_tmdb_director_no_tmdb_key` — asserts `{"author": ""}` on a keyless account |

Each patch target becomes `_fetch_tmdb_meta` returning
`{"author": ..., "length": ...}`, each URL becomes `/api/tmdb-meta/...`, and the
expected payloads gain a `length` key. The cache-hit assertion (second call served from
`_tmdb_meta_cache`) and the "search must not fetch" assertion both carry over unchanged
in intent.

### New tests

The project has no JavaScript test runner (`tests/` is pytest only), so coverage is
server-side plus manual checks.

- `_fetch_tmdb_meta` returns both `author` and `length` for a movie (runtime → `1h 43m`)
  and for a TV show (`number_of_seasons` → `4 Seasons`), from a single faked request.
- `_fetch_tmdb_meta` returns `{"author": "", "length": ""}` when the request raises.
- `_fetch_tmdb_meta` for a movie issues exactly one HTTP request (guards against the
  `append_to_response` consolidation regressing back to two calls).
- Manga search maps `lastChapter` into `total_chapters`, and omits it (as `None`) when
  MangaDex doesn't supply one.

Manual verification:

- Detail panel for one item of each of the four types, plus one item with no available
  length (confirm bare `Movie` with no trailing comma).
- Search for a movie, a TV show, a book, and a manga; confirm the length line appears
  and that book/manga cards fill with no network request.
- Switch tabs and confirm the filter bar recolours while the search modal's Filter
  button stays purple.

---

## Out of scope

- Persisting runtime/season/chapter counts to the database (memory TTL caching matches
  the existing `_tmdb_director_cache` / `tv_info_cache` / `manga_info_cache` pattern and
  avoids a schema migration).
- Any recolouring beyond the grid filter bar.
- Labelling the media type on search cards.
