# UI Fixes, TMDB Ratings & Proactive Refresh — Design

Date: 2026-06-21
Status: Approved

## Overview

Six independent changes to CineVault, spanning a mobile rendering bug, auth
autofill behavior, rating display polish, a new free rating source (TMDB), a
small UI tweak, and a proactive in-browser MDBList refresh strategy.

The six items are independent and can be implemented/tested separately, but ship
together.

---

## 1 — iOS resize flash on refresh

### Problem
On iOS Safari, refreshing the app shows the UI oversized and shifted toward the
bottom-right for ~1 frame before snapping to the correct size. The reflow can
also leave the on-screen keyboard in a bad state.

### Root cause
Root font size is `html { font-size: clamp(12px, 100vw / 120, 22px); }`
([index.html](../../../templates/index.html) head). Every dimension is in `rem`,
so the whole layout scales off this one value. On iOS load/refresh, `100vw` is
briefly evaluated against an unsettled / oversized layout viewport, so the root
font size paints too large, then corrects once the viewport settles.

### Fix
Add a small **synchronous inline script at the very top of `<head>`** (before the
stylesheet) that sets `document.documentElement.style.fontSize` from
`clientWidth / 120`, clamped to `[12, 22]px`. `clientWidth` reflects the real
content width immediately on first paint and excludes the scrollbar, so there is
no `vw` miscalculation and no flash. Re-apply on `resize` and `orientationchange`.

- The CSS `clamp(...)` declaration stays as a no-JS fallback.
- [login.html](../../../templates/login.html) does **not** use `vw`-based rem
  sizing, so it needs no change for this item.

---

## 2 — Sign-in vs Create-account autofill

### Desired behavior
- **Sign In:** clean login form; iOS offers saved-password autofill when a saved
  credential exists; no clutter otherwise.
- **Create Account:** no "Use Strong Password", no suggested usernames, and no
  autofill of saved usernames/passwords. The fields are free-form.

### Approach
Keep the proven two-mode architecture already in
[login.html](../../../templates/login.html):

- **Login mode:** native `type="password"` + `autocomplete="username"` /
  `current-password` so iOS recognizes a login form and offers saved-password
  autofill.
- **Register mode:** convert the password fields to JS-masked `type="text"`
  (bullet rendering, no `type=password`, no `-webkit-text-security`) and set
  `autocomplete="off"`, so iOS sees no credential form → none of the prompts.

### Work
- Verify the **initial paint** is clean login mode (default).
- Verify every mode switch reliably (re)applies all credential signals (and
  removes them in register mode), including the username and confirm fields.
- Remove any fragile / redundant bits; document what each attribute does.

### Caveat (explicit)
iOS autofill is heuristic. This is the most reliable known technique but cannot be
*guaranteed* across every iOS version without device testing — the exact
limitation that made prior fixes hard to validate. We make it as robust as
possible and document the behavior; we do not claim a hard guarantee.

---

## 3 — Rating display

Add a helper `fmtRating(r)`:

- `r <= 0` → `"Unrated"` (no `/ 10` suffix).
- otherwise → strip a trailing `.0`, so `10.0` → `"10"`, `7.5` → `"7.5"`.

Applied in [index.html](../../../templates/index.html) `renderDetail`:

- Initial label: `Unrated` or `"<val> / 10"`.
- The slider `oninput` live update uses the same helper.

The "Avg rating" stat in the stats bar stays a true average (unchanged).

---

## 4 — TMDB rating pill

### Source
TMDB `vote_average` (0–10), **free**, never consumes MDBList quota.

### Pill
- New `tmdb` key. Label `TMDB`. Rendered as a text pill matching IMDb/MAL style
  (e.g. `TMDB 7.8`), value formatted to one decimal.
- Lives in the **TMDB section** of the Filter dropdown, directly **under Year and
  Media type**. Toggleable for **both** grid and search selectors.
- **On by default:** add `tmdb` to `DEFAULT_PILLS`.
- Render order: insert `tmdb` into `PILL_ORDER` right after `year, type`.
- Register `tmdb` in `PILL_LABELS`, `_VALID_PILLS`, and the TMDB
  `PILL_SECTIONS` entry.
- Books/manga: no TMDB pill (movie/tv only).

### Data flow
- **DB:** add `tmdb_rating REAL` to the `media` table via an idempotent migration
  (mirrors the existing `ratings` migration in [db.py](../../../db.py)).
- **Search:** include `vote_average` in the `/api/search` payload as
  `tmdb_rating` (movie + tv). Search pills render instantly from local item data
  (like year/type — no fetch).
- **Add:** thread `tmdb_rating` through `add_media_entry` so newly added items
  persist it. `/api/add` accepts it from the search-result payload the client
  already sends.
- **Backfill (existing library items with no stored value):** a new **free**
  endpoint `GET /api/tmdb-rating/<media_type>/<tmdb_id>` fetches the TMDB detail
  `vote_average`, persists it to `tmdb_rating`, and is cached (TTL cache for
  non-library, persisted for library rows). The client hydrates it lazily per
  visible card when the `tmdb` pill is selected and `item.tmdb_rating` is null —
  no MDBList quota involved.

### Rendering
`renderPills` handles `tmdb` like `year`/`type`: read the value from
`item.tmdb_rating` (local), not from the MDBList `ratings` dict. Omit the pill
when the value is null/absent.

---

## 5 — Larger dropdown arrow

The `▾` glyph currently uses an inline `style="font-size:0.5625rem;opacity:0.7"`
on multiple buttons (both "Filter" buttons and the pill buttons). Replace those
inline styles with a shared CSS class (e.g. `.dd-caret`) at a larger size, so
**all** dropdown carets grow consistently.

---

## 6 — Proactive MDBList refresh (in-browser)

No background scheduler exists and the hosting can't run tasks while the user is
away, so all refresh logic is **client-driven while the user is in the app**.
MDBList quota is per-user BYOK; `/api/mdblist-status` exposes `remaining`.

### 6a. Manual refresh in the details panel
- Add a refresh **⟳ icon** next to the score pills in `renderDetail`.
- Clicking force-refreshes that title's MDBList ratings on the spot, re-renders
  the pills, and updates the "last updated" label to **"just now"**.
- **Greyed out / no-op** when there is no MDBList key or quota is exhausted.
- **Debounce:** while the label still reads **"just now"** (i.e. `ratings_updated_at`
  is < 60s ago), a second click does nothing. Once the label ticks to "1m ago",
  refresh is allowed again. This prevents spam and wasted quota.

### 6b. "Last updated" label
- Shown next to the refresh icon, computed from `ratings_updated_at`:
  - `< 60s` → `just now`
  - `< 60m` → `Xm ago`
  - `< 24h` → `Xh ago`
  - `< 7d`  → `Xd ago`
- Only shown for movie/tv titles that have an MDBList key and a stored timestamp.

### 6c. Proactive hourly refresh sweep (7-day ceiling, ≤500/day)
- A **background sweep** runs **on launch and then once per hour** while the tab
  stays open. Each sweep refreshes the MDBList ratings of all movie/tv items whose
  ratings are **missing or ≥7 days old**, **oldest-first**.
- The hourly cadence exists so items are picked up as they **newly cross** the
  7-day line (e.g. an item at 6d23h this hour becomes eligible next hour).
- Each refresh consumes one server lazy-refresh slot (see 6e). When the daily
  **500** cap is reached, the remaining stale items are **skipped and stay stale
  until the next day** — so 7 days is the effective freshness ceiling, bounded by
  500 refreshes/day.
- **Daily dormancy:** if a sweep reaches the 500 cap (or MDBList quota is
  exhausted), **all sweeps pause for the rest of the UTC day** and resume after
  the daily reset (tracked client-side by the UTC date on which the cap was hit).
  The hourly timer keeps ticking but each tick is a no-op until the date rolls
  over.
- Each sweep:
  - **starts once per page load** (a single scheduler; the many `loadList()`
    callers don't re-trigger it),
  - is **non-blocking** (fire-and-forget; the grid renders immediately),
  - **stops early** when it detects the cap is exhausted — a refreshed item whose
    `updated_at` did **not** advance to ~now signals the cap is hit, so the sweep
    aborts the rest (and marks the day dormant) instead of firing hundreds of
    pointless requests,
  - is **skipped entirely** when there is no MDBList key or the quota is exhausted,
  - paces requests sequentially to stay under the per-endpoint rate limit.
- **No scroll/viewport-triggered refresh.** Besides this sweep, ratings also
  refresh via the manual button (6a) and whenever `/api/ratings` is hit by normal
  grid pill hydration — all subject to the same 500/day cap.

### 6e. Daily cap on lazy refreshes (500/day)
- A **per-user daily counter** caps the **automatic 7-day-on-access** refreshes at
  **500 per day**. Once the cap is hit, further stale items are served from their
  existing stored ratings (or `{}` if none yet) **without re-fetching**; they
  become eligible again the next day, so the next batch of stale items refreshes
  then.
- Counter state: in-memory `{uid: (date_utc, count)}`, reset when the UTC date
  rolls over. (Resets on process restart — acceptable for a soft daily ceiling.)
- **The manual ⟳ button (6a) is exempt** from this cap — it is user-initiated and
  always allowed, bounded only by its 60s debounce and the MDBList quota itself.
- Implemented server-side in `/api/ratings`: the lazy (non-`force`) stale path
  checks/increments the counter before fetching; the `?force=1` manual path skips
  it.

### 6d. Backend support
- `/api/ratings/<media_type>/<tmdb_id>` gains:
  - `?force=1` → bypass the 7-day freshness check **and** the TTL cache; always
    re-fetch and persist. Used by the manual refresh button.
  - Response includes `updated_at` (the persisted `ratings_updated_at`) so the
    client can render the label without a separate call.
- TMDB refresh (item 4) is a separate free endpoint and never touches MDBList
  quota.

### Tunables (chosen)
- Manual-refresh debounce window: **60 seconds** ("just now").
- Proactive sweep cadence: **on launch + every 1 hour** while open.
- Sweep eligibility: items **missing or ≥7 days old**, oldest-first.
- Daily cap on lazy/proactive refreshes: **500 per user per day** (manual exempt);
  sweeps go dormant for the rest of the UTC day once hit.

---

## Edge cases
- No MDBList key → no score pills, no refresh icon, no "updated" label; TMDB pill
  still works (free).
- Quota exhausted → MDBList pills paused (existing behavior); refresh icon greyed;
  TMDB unaffected.
- TMDB pill on a title TMDB has no score for → pill omitted; card still renders.
- Books/manga → no TMDB pill, no MDBList refresh (already out of scope).
- Old library rows missing `tmdb_rating` → lazily backfilled (free) when visible.

## Testing
- **Ratings backend:** unit-test `/api/ratings?force=1` (bypasses freshness and
  cache) and that the response carries `updated_at`; test
  `/api/tmdb-rating` fetch + persist + cache, and `{}` when no value.
- **DB:** test the `tmdb_rating` migration is idempotent and that add/list
  round-trips the value.
- **Rating format:** unit-test `fmtRating` (0 → Unrated, 10.0 → 10, 7.5 → 7.5).
- Frontend behaviors (manual-refresh debounce, "last updated" label, iOS rem) are
  verified manually; pure helpers are unit-tested where practical.

## Out of scope
- Server-side / background refresh while the user is away (no scheduler).
- Ratings for books/manga.
- Sorting/filtering the library by external scores.
