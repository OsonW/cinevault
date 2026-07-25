import os
import re
import json
import time
import secrets
import threading
import requests
from datetime import datetime, timezone
from collections import OrderedDict
from urllib.parse import urlparse
from flask import Flask, jsonify, Response, request, render_template, g, redirect, url_for
from flask_login import login_required, current_user, logout_user

from db import (
    get_user_db_path, init_user_db,
    get_all_media, get_media_by_id, get_media_by_external_id,
    add_media_entry, update_media_entry, delete_media_entry, set_media_dates,
    set_media_ratings,
    set_media_tmdb_rating,
    take_lazy_refresh_slot, sync_lazy_cap_to_quota,
)
from auth import (
    auth_bp, login_manager, rate_limit, _as_str,
    _client_ip, check_rate_limit, _too_many_requests,
)
from users_db import (
    init_users_db, get_user_keys, delete_user, delete_stale_keyless_users,
)


class _BoundedCache(OrderedDict):
    """OrderedDict capped at maxsize entries; evicts the oldest when full."""
    def __init__(self, maxsize: int):
        super().__init__()
        self._maxsize = maxsize
        self._lock = threading.Lock()

    def __setitem__(self, key, value):
        with self._lock:
            if key in self:
                self.move_to_end(key)
            super().__setitem__(key, value)
            while len(self) > self._maxsize:
                self.popitem(last=False)

    def pop(self, key, *args):
        with self._lock:
            return super().pop(key, *args)


app = Flask(__name__)

# SECRET_KEY: must be set explicitly in production. In dev (no env var set)
# we generate an ephemeral random key per process so sessions are still
# secure — they just don't survive a server restart. Never fall back to a
# hardcoded default, because that would mean anyone reading the source can
# forge session cookies.
_secret = os.environ.get("SECRET_KEY")
if not _secret:
    _secret = secrets.token_hex(32)
    print(
        "WARNING: SECRET_KEY not set; generated an ephemeral random key for "
        "this process. Sessions will be invalidated when the server restarts. "
        "Set SECRET_KEY in your environment for production deployments."
    )
app.secret_key = _secret

# Cookie security: Secure flag only in production (when SECRET_KEY is explicitly
# set), so local HTTP dev sessions still work. SameSite=Strict is safe everywhere
# and is the primary CSRF defence — it blocks cross-site form POSTs and fetches.
_production = bool(os.environ.get("SECRET_KEY"))

# In production the app runs behind a reverse proxy (PythonAnywhere). ProxyFix
# reads exactly one trusted X-Forwarded-For hop and writes the real client IP
# into request.remote_addr, so _client_ip() never has to touch XFF directly.
# Skipped in dev so a missing proxy doesn't silently break local testing.
if _production:
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config["SESSION_COOKIE_SECURE"]    = _production
app.config["SESSION_COOKIE_HTTPONLY"]  = True
app.config["SESSION_COOKIE_SAMESITE"]  = "Strict"
app.config["REMEMBER_COOKIE_SECURE"]   = _production
app.config["REMEMBER_COOKIE_HTTPONLY"] = True
app.config["REMEMBER_COOKIE_SAMESITE"] = "Strict"

# Reject request bodies larger than 256 KB. Every endpoint in this app deals
# in small JSON payloads — credentials, key strings, library updates. A
# megabyte-plus body is almost certainly abuse, and refusing early stops a
# slow client from tying up a worker.
app.config["MAX_CONTENT_LENGTH"] = 256 * 1024

login_manager.init_app(app)
login_manager.login_view = "auth.login_page"
app.register_blueprint(auth_bp)


def _get_tmdb_key() -> str | None:
    if current_user.is_authenticated:
        key = get_user_keys(int(current_user.id)).get("tmdb_key", "").strip()
        if key:
            return key
    return None


def _tmdb_params(api_key: str) -> dict:
    return {"api_key": api_key}


def _get_mdblist_key() -> str | None:
    if current_user.is_authenticated:
        key = get_user_keys(int(current_user.id)).get("mdblist_key", "").strip()
        if key:
            return key
    return None


# MDBList `source` field -> our canonical key. Aliases cover naming differences
# across MDBList API versions (popcornmeter is "popcorn", MAL is "myanimelist").
# Matched case-insensitively.
_MDBLIST_SOURCE_ALIASES = {
    "imdb": "imdb",
    "tomatoes": "tomatoes",
    "audience": "audience", "popcorn": "audience",
    "metacritic": "metacritic",
    "letterboxd": "letterboxd",
    "mal": "mal", "myanimelist": "mal",
}

# App media_type -> MDBList path segment. Only movies/shows are supported.
_MDBLIST_TYPE = {"movie": "movie", "tv": "show"}


def _extract_imdb_id(payload) -> str | None:
    """Best-effort IMDb tconst ('tt…') from an MDBList payload, tolerant of shape
    differences across API versions (flat `imdbid` or nested `ids.imdb`). Returns
    None when absent/malformed so the IMDb pill falls back to a search link."""
    if not isinstance(payload, dict):
        return None
    cand = payload.get("imdbid")
    if not cand:
        ids = payload.get("ids")
        if isinstance(ids, dict):
            cand = ids.get("imdb") or ids.get("imdbid")
    cand = str(cand).strip() if cand else ""
    return cand if cand.startswith("tt") else None


def _resolve_rating_url(canon: str, raw, media_type: str) -> str | None:
    """Turn MDBList's per-rating `url` into an absolute https link to that source's
    title page. MDBList returns these as RELATIVE paths (e.g. Rotten Tomatoes
    "/m/inception", Metacritic "/inception", Letterboxd "/film/inception/"), so each
    needs its own domain — and Metacritic's path omits the {movie|tv} segment, which
    we re-insert. Non-path values (e.g. imdb's url is a number) yield None."""
    if not isinstance(raw, str):
        return None
    path = raw.strip()
    if not path.startswith("/"):
        return None
    if canon in ("tomatoes", "audience"):       # path already carries /m or /tv
        return "https://www.rottentomatoes.com" + path
    if canon == "letterboxd":
        return "https://letterboxd.com" + path
    if canon == "metacritic":                   # path is just /<slug> — add the type
        seg = "tv" if media_type == "tv" else "movie"
        return f"https://www.metacritic.com/{seg}{path}"
    return None


def _fetch_mdblist_ratings(media_type: str, tmdb_id, key: str):
    """Return {source: value} for the sources we display. Returns None on a real
    FAILURE (non-200 / timeout / exception) so callers can tell "the call failed" from
    "the call succeeded but the title has no ratings" ({}) — and not persist/cache a
    failure as authoritative. {} is also returned for inputs we never fetch (unsupported
    type / missing id). One detail call returns every rating, so cost is per-title."""
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
            return None
        payload = resp.json()
        ratings = payload.get("ratings") or []
    except Exception:
        return None
    out = {}
    urls = {}
    for r in ratings:
        if not isinstance(r, dict):
            continue
        canon = _MDBLIST_SOURCE_ALIASES.get(str(r.get("source", "")).lower())
        if not canon:
            continue
        val = r.get("value")
        if val is not None and canon not in out:
            out[canon] = val
        # MDBList hands back the source's own page path per rating — that's what lets a
        # pill open the exact title page (not a search) on every site, including the
        # ones we can't build a link for ourselves (Rotten Tomatoes, Metacritic).
        if canon not in urls:
            resolved = _resolve_rating_url(canon, r.get("url"), media_type)
            if resolved:
                urls[canon] = resolved
    # Attach link metadata so pills can deep-link. Only when we actually have scores:
    # keeping these reserved keys out of an otherwise-empty result preserves the
    # invariant "non-empty dict == has ratings" that the caching and
    # don't-overwrite-good-ratings guards rely on. They're ignored by pill rendering
    # (which only iterates known score sources).
    if out:
        ids = payload.get("ids") if isinstance(payload.get("ids"), dict) else {}
        imdb_id = _extract_imdb_id(payload)
        if imdb_id:
            out["imdb_id"] = imdb_id
        mal_id = ids.get("mal") or payload.get("malid")
        if mal_id:
            out["mal_id"] = str(mal_id)
        if urls:
            out["src_urls"] = urls
    return out


# TMDB rating (vote_average) for non-library titles. Keyed "media_type:tmdb_id".
_tmdb_rating_cache: dict[str, tuple[float, object]] = {}
_TMDB_RATING_TTL = 24 * 3600  # seconds

# TMDB director/creator + length for search results. Keyed "media_type:tmdb_id".
_tmdb_meta_cache: dict[str, tuple[float, dict]] = {}
_TMDB_META_TTL = 24 * 3600  # seconds


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


# Shared across users — keyed by external IDs so safe to share
poster_cache: _BoundedCache = _BoundedCache(500)   # ~100 MB max at avg 200 KB/poster
search_cache: dict[str, list] = {}

# Ratings for non-library titles (search results). Keyed "media_type:tmdb_id".
_ratings_cache: dict[str, tuple[float, dict]] = {}
_RATINGS_TTL = 24 * 3600          # seconds
_RATINGS_MAX_AGE_DAYS = 7         # persisted library ratings refresh after this

# MDBList /user snapshot per user: uid -> (ts, dict). Short TTL so polling the
# status never meaningfully spends the daily quota.
_mdblist_status_cache: dict[int, tuple[float, dict]] = {}
_MDBLIST_STATUS_TTL = 120  # seconds

# Per-user cap on AUTOMATIC (7-day-on-access / sweep) ratings refreshes. Manual
# force refreshes are exempt. The count is persisted in the user's DB (app_meta),
# so it survives restarts and is shared across worker processes; it's reset in
# `api_mdblist_status` when the user's MDBList used-count drops (their daily quota
# refreshed), so the 500 cap resets in lockstep with the quota.
_LAZY_REFRESH_DAILY_CAP = 500


def _take_lazy_refresh_slot(uid: int) -> bool:
    """Consume one auto-refresh slot (persisted, atomic). `uid` is unused — the slot
    lives in the current request's per-user DB — but kept so callers/tests that pass
    it keep working."""
    return take_lazy_refresh_slot(_LAZY_REFRESH_DAILY_CAP)

_user_media_cache:   dict[int, dict] = {}

_app_initialized    = False
_initialized_users: set[int] = set()
_init_lock = threading.Lock()

# Reap abandoned keyless registrations. An account that never provides a TMDB key
# is deleted after KEYLESS_MAX_AGE so empty rows don't accumulate. The sweep is
# throttled to at most once per KEYLESS_SWEEP_INTERVAL (piggy-backed on incoming
# requests — no background thread, which suits single-/few-worker deployments).
KEYLESS_MAX_AGE        = 3600    # 1 hour lifespan for keyless accounts
KEYLESS_SWEEP_INTERVAL = 600     # run the sweep at most every 10 minutes
_last_keyless_sweep    = 0.0
_sweep_lock = threading.Lock()


def _purge_user_storage(uid: int) -> None:
    """Remove a user's per-user media DB (and its SQLite sidecars) plus any
    in-memory caches/flags. Keyless accounts have no DB file, so this is usually a
    no-op on the filesystem — but it stays correct for legacy/abandoned accounts."""
    _user_media_cache.pop(uid, None)
    with _init_lock:
        _initialized_users.discard(uid)
    db_path = get_user_db_path(uid)
    for path in (db_path, db_path + "-journal", db_path + "-wal", db_path + "-shm"):
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError as e:
            print(f"purge: failed to remove {path}: {e}")


def _maybe_sweep_keyless_users() -> None:
    """Throttled reaper for abandoned keyless accounts. Cheap on the common path
    (just a timestamp compare); runs the actual DELETE at most once per interval."""
    global _last_keyless_sweep
    now = time.time()
    if now - _last_keyless_sweep < KEYLESS_SWEEP_INTERVAL:
        return
    with _sweep_lock:
        if now - _last_keyless_sweep < KEYLESS_SWEEP_INTERVAL:
            return          # another request won the race
        _last_keyless_sweep = now
    try:
        for uid in delete_stale_keyless_users(KEYLESS_MAX_AGE):
            _purge_user_storage(uid)
    except Exception as e:
        print(f"keyless sweep failed: {e}")

MANGADEX_HEADERS = {"User-Agent": "CineVault/1.0"}

# Global default rate limit applied to EVERY endpoint (per client IP + endpoint
# name). Generous enough for normal use — including the burst of poster requests
# a large library fires on a tab switch — while stopping abuse on the many
# endpoints that don't carry a stricter explicit @rate_limit. Routes that DO
# carry one still enforce their own tighter cap on top of this.
GLOBAL_RATE_MAX    = 300
GLOBAL_RATE_WINDOW = 60

# Input validation: allowlists + length caps. The 256 KB MAX_CONTENT_LENGTH
# stops giant bodies; free-text fields get their own tighter per-field caps.
VALID_MEDIA_TYPES = frozenset({"movie", "tv", "book", "manga"})
VALID_STATUSES    = frozenset({"watchlist", "watching", "finished"})
MAX_TITLE_LEN     = 500
MAX_TEXT_LEN      = 5000    # notes / author / overview

_COVER_URL_ALLOWED_HOSTS = frozenset({
    "covers.openlibrary.org",
    "uploads.mangadex.org",
    "image.tmdb.org",
})


def _is_safe_cover_url(url: str | None) -> bool:
    if not url:
        return True
    try:
        p = urlparse(url)
        return p.scheme == "https" and p.hostname in _COVER_URL_ALLOWED_HOSTS
    except Exception:
        return False


SEARCH_CACHE_MAX = 100


@app.after_request
def set_security_headers(response):
    response.headers["X-Frame-Options"]       = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"]        = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; "
        "img-src 'self' data: https://covers.openlibrary.org https://archive.org "
        "https://*.archive.org https://uploads.mangadex.org https://image.tmdb.org; "
        "connect-src 'self'; "
        "frame-ancestors 'none';"
    )

    # Caching policy — single source of truth. Dynamic responses (the HTML app
    # shell, the login page, and every /api/* JSON payload) must NEVER be reused
    # from cache: a stale copy keeps the browser running old inline JS and showing
    # old data (this is what caused "the refresh button doesn't update the grid"
    # on a machine holding an old page). Endpoints that serve genuinely immutable
    # bytes (posters) set their own long-lived Cache-Control above and are left
    # untouched, as is Flask's static handler, which attaches its own validators
    # for cheap conditional revalidation.
    if request.endpoint != "static" and "Cache-Control" not in response.headers:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"]        = "no-cache"
    return response


@app.before_request
def setup_request():
    global _app_initialized
    if not _app_initialized:
        init_users_db()
        _app_initialized = True

    # Reap abandoned keyless accounts (throttled internally). Cheap on most requests.
    _maybe_sweep_keyless_users()

    # Global rate limit on every endpoint (per client IP + endpoint). Endpoints
    # with their own stricter @rate_limit still enforce that tighter cap too.
    endpoint    = request.endpoint or request.path
    retry_after = check_rate_limit(
        f"{_client_ip()}:global:{endpoint}", GLOBAL_RATE_MAX, GLOBAL_RATE_WINDOW
    )
    if retry_after is not None:
        return _too_many_requests(retry_after)

    if request.path == "/login" and current_user.is_authenticated:
        return redirect(url_for("index"))

    if current_user.is_authenticated:
        uid = current_user.id
        g.user_db_path = get_user_db_path(uid)
        # Provision the per-user media DB ONLY for accounts that have a TMDB key (or
        # whose DB file already exists). Keyless/abandoned accounts therefore never
        # spawn a DB file on disk — only their single users.db row — which defuses
        # empty-account storage-spam attacks.
        g.user_db_ready = current_user.has_tmdb_key or os.path.exists(g.user_db_path)
        if g.user_db_ready:
            with _init_lock:
                already_init = uid in _initialized_users
                if not already_init:
                    _initialized_users.add(uid)
            if not already_init:
                init_user_db(uid)

    if request.path.startswith("/api/") and not current_user.is_authenticated:
        return jsonify({"error": "Unauthorized"}), 401

    # Block any storage-writing API call from a keyless account so a direct POST/DELETE
    # can't create a DB file either. Reads stay allowed (they return empty without
    # touching disk). Keys are saved via /auth/keys, which is not under /api/.
    if (request.path.startswith("/api/")
            and request.method not in ("GET", "HEAD", "OPTIONS")
            and not getattr(g, "user_db_ready", False)):
        return jsonify({"error": "Add your API keys before saving to your library."}), 403


def _cache_search(key: str, items: list) -> None:
    search_cache[key] = items
    if len(search_cache) > SEARCH_CACHE_MAX:
        del search_cache[next(iter(search_cache))]


def _media_cache() -> dict:
    return _user_media_cache.setdefault(current_user.id, {})


def _invalidate_media_cache(media_id: int):
    _media_cache().pop(media_id, None)


def cached_get_media(media_id: int):
    mc = _media_cache()
    if media_id not in mc:
        mc[media_id] = get_media_by_id(media_id)
    return mc[media_id]


def _mangadex_cover_url(manga_id: str, cover_file: str) -> str:
    if not manga_id or not cover_file:
        return ""
    return f"https://uploads.mangadex.org/covers/{manga_id}/{cover_file}.512.jpg"


def _fetch_mangadex_cover_url(manga_id: str) -> str:
    if not manga_id:
        return ""
    resp = requests.get(
        f"https://api.mangadex.org/manga/{manga_id}",
        headers=MANGADEX_HEADERS,
        params={"includes[]": ["cover_art"]},
        timeout=8,
    )
    resp.raise_for_status()
    for rel in resp.json().get("data", {}).get("relationships", []):
        if rel.get("type") == "cover_art":
            cover_file = rel.get("attributes", {}).get("fileName", "")
            return _mangadex_cover_url(manga_id, cover_file)
    return ""


@app.route("/auth/cancel-registration", methods=["POST"])
@login_required
@rate_limit(max_requests=5, window_seconds=3600)   # 5/hour per IP
def cancel_registration():
    data = request.get_json(silent=True) or {}
    username_input = _as_str(data.get("username"))
    if username_input and username_input.lower() != current_user.username.lower():
        return jsonify({"error": "Username does not match"}), 400
    uid = int(current_user.id)
    _purge_user_storage(uid)
    logout_user()
    delete_user(uid)
    return jsonify({"status": "ok"})


@app.route("/")
@login_required
def index():
    # Cache-Control (no-store) is applied centrally in set_security_headers, which
    # also keeps the authenticated page out of cache after logout (back-button
    # hardening) — so we just render and return here.
    return render_template("index.html", username=current_user.username)


# ═══════════════════════════════════════════════════
# Search (TMDB, Books, Manga)
# ═══════════════════════════════════════════════════

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


def _safe_float(value):
    """MangaDex chapter numbers are free text ("91", "91.5", "", "Oneshot").
    Coerce to float, or None when it isn't a number — one bad value must not
    take down the whole search response."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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
        elif media_type == "tv":
            url = f"https://api.themoviedb.org/3/tv/{tmdb_id}"
            data = requests.get(url, params=params, timeout=3).json()
            names = [c["name"] for c in data.get("created_by", [])]
            seasons = data.get("number_of_seasons") or 0
            length = f"{seasons} Season{'' if seasons == 1 else 's'}" if seasons else ""
        else:
            return {"author": "", "length": ""}
        return {"author": ", ".join(names[:3]), "length": length}
    except Exception:
        return {"author": "", "length": ""}


@app.route("/api/item/<int:item_id>/fetch_director")
@login_required
def fetch_item_director(item_id):
    item = cached_get_media(item_id)
    if not item:
        return jsonify({"author": ""})
    if item.get("author"):
        return jsonify({"author": item["author"]})
    if item.get("media_type") not in ("movie", "tv"):
        return jsonify({"author": ""})
    tmdb_id = item.get("tmdb_id") or item.get("external_id")
    if not tmdb_id:
        return jsonify({"author": ""})
    tmdb_key = _get_tmdb_key()
    if not tmdb_key:
        return jsonify({"error": "TMDB API key required"}), 401
    author = _fetch_tmdb_meta(item["media_type"], int(tmdb_id), tmdb_key)["author"]
    if author:
        update_media_entry(item_id, author=author)
        _invalidate_media_cache(item_id)
    return jsonify({"author": author})


@app.route("/api/item/<int:item_id>/overview")
@login_required
def fetch_item_overview(item_id):
    item = cached_get_media(item_id)
    if not item:
        return jsonify({"overview": ""}), 404
    if item.get("overview"):
        return jsonify({"overview": item["overview"]})
    media_type = item["media_type"]
    tmdb_id = item.get("tmdb_id") or item.get("external_id")
    overview = ""
    if media_type in ("movie", "tv") and tmdb_id:
        tmdb_key = _get_tmdb_key()
        if not tmdb_key:
            return jsonify({"error": "TMDB API key required"}), 401
        try:
            endpoint = "movie" if media_type == "movie" else "tv"
            url = f"https://api.themoviedb.org/3/{endpoint}/{tmdb_id}"
            data = requests.get(url, params=_tmdb_params(tmdb_key), timeout=5).json()
            overview = data.get("overview", "") or ""
        except Exception as e:
            print(f"Overview fetch error for {item_id}: {e}")
    if overview:
        update_media_entry(item_id, overview=overview)
        _invalidate_media_cache(item_id)
    return jsonify({"overview": overview})


@app.route("/api/search")
@login_required
def search():
    query      = request.args.get("q", "").strip()
    media_type = request.args.get("type", "movie")
    if not query:
        return jsonify([])

    tmdb_key = _get_tmdb_key()
    if not tmdb_key:
        return jsonify({"error": "TMDB API key required"}), 401

    cache_key = f"{media_type}:{query.lower()}"
    if cache_key in search_cache:
        return jsonify(search_cache[cache_key])

    try:
        if media_type == "movie":
            resp = requests.get(
                "https://api.themoviedb.org/3/search/movie",
                params={**_tmdb_params(tmdb_key), "query": query},
                timeout=8,
            )
            resp.raise_for_status()
            items = [
                {
                    "tmdb_id":     r["id"],
                    "external_id": str(r["id"]),
                    "title":       r.get("title", ""),
                    "year":        (r.get("release_date", "") or "")[:4],
                    "media_type":  "movie",
                    "poster_path": r.get("poster_path"),
                    "overview":    r.get("overview"),
                    "popularity":  r.get("popularity", 0) or 0,
                    "tmdb_rating": r.get("vote_average"),
                }
                for r in resp.json().get("results", []) if r.get("id")
            ]
        else:
            resp = requests.get(
                "https://api.themoviedb.org/3/search/tv",
                params={**_tmdb_params(tmdb_key), "query": query},
                timeout=8,
            )
            resp.raise_for_status()
            items = [
                {
                    "tmdb_id":     r["id"],
                    "external_id": str(r["id"]),
                    "title":       r.get("name", ""),
                    "year":        (r.get("first_air_date", "") or "")[:4],
                    "media_type":  "tv",
                    "poster_path": r.get("poster_path"),
                    "overview":    r.get("overview"),
                    "popularity":  r.get("popularity", 0) or 0,
                    "tmdb_rating": r.get("vote_average"),
                }
                for r in resp.json().get("results", []) if r.get("id")
            ]
        items.sort(key=lambda x: x.get("popularity", 0), reverse=True)
    except Exception as e:
        print(f"Search error: {e}")
        items = []

    items = items[:10]

    _cache_search(cache_key, items)
    return jsonify(items)


@app.route("/api/search/books")
@login_required
def search_books():
    query = request.args.get("q", "").strip()
    if not query or len(query) < 3:
        return jsonify([])

    cache_key = f"book:{query.lower()}"
    if cache_key in search_cache:
        return jsonify(search_cache[cache_key])

    try:
        resp = requests.get(
            "https://openlibrary.org/search.json",
            params={"q": query, "limit": 10},
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json()
        items = []
        for doc in data.get("docs", []):
            cover_id = doc.get("cover_i")
            cover_url = None
            if cover_id:
                cover_url = f"https://covers.openlibrary.org/b/id/{cover_id}-L.jpg"
            author_names = doc.get("author_name", [])
            if isinstance(author_names, list):
                author = ", ".join(author_names)
            else:
                author = str(author_names)
            year = doc.get("first_publish_year")
            if not year:
                pub_date = doc.get("publish_date")
                if pub_date and len(str(pub_date)) >= 4:
                    year = str(pub_date)[:4]
                else:
                    year = ""

            items.append({
                "external_id": doc.get("key", "").replace("/works/", ""),
                "title": doc.get("title", "Unknown"),
                "author": author,
                "year": str(year),
                "media_type": "book",
                "cover_url": cover_url,
                "total_pages": doc.get("number_of_pages_median"),
                "overview": "",
                "popularity": doc.get("ratings_average", 0) or 0,
            })
        _cache_search(cache_key, items)
        return jsonify(items)

    except requests.exceptions.HTTPError as e:
        print(f"Open Library HTTP error: {e}")
        return jsonify([])
    except Exception as e:
        print(f"Open Library search error: {e}")
        return jsonify([])


@app.route("/api/search/manga")
@login_required
def search_manga():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify([])

    cache_key = f"manga:{query.lower()}"
    if cache_key in search_cache:
        return jsonify(search_cache[cache_key])

    try:
        resp = requests.get(
            "https://api.mangadex.org/manga",
            headers=MANGADEX_HEADERS,
            params={
                "title": query,
                "limit": 10,
                "includes[]": ["cover_art", "author"],
                "availableTranslatedLanguage[]": ["en"],
                "order[relevance]": "desc",
            },
            timeout=10,
        )
        if resp.status_code != 200:
            return jsonify([])

        data = resp.json()
        items = []
        for manga in data.get("data", []):
            manga_id = manga["id"]
            attrs = manga.get("attributes", {})

            title_data = attrs.get("title", {})
            title = title_data.get("en") or next(iter(title_data.values()), "Unknown")

            cover_file = None
            for rel in manga.get("relationships", []):
                if rel["type"] == "cover_art":
                    cover_file = rel.get("attributes", {}).get("fileName", "")
                    break

            cover_url = None
            if cover_file:
                cover_url = _mangadex_cover_url(manga_id, cover_file)

            author = ""
            for rel in manga.get("relationships", []):
                if rel["type"] == "author":
                    author = rel.get("attributes", {}).get("name", "")
                    break

            desc = attrs.get("description", {})
            overview = desc.get("en") or next(iter(desc.values()), "")

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
                "total_chapters": _safe_float(attrs.get("lastChapter")),
                "popularity": 0,
            })

        _cache_search(cache_key, items)
        return jsonify(items)

    except Exception as e:
        print(f"Manga search error: {e}")
        return jsonify([])


# ═══════════════════════════════════════════════════
# Posters
# ═══════════════════════════════════════════════════

@app.route("/api/poster/<string:media_type>/<path:item_id>")
@login_required
def get_poster(media_type, item_id):
    if media_type == "book":
        row = get_media_by_external_id(item_id, media_type)
        cover_url = (row or {}).get("cover_url") or ""

        if not cover_url:
            try:
                resp = requests.get(
                    f"https://openlibrary.org/works/{item_id}.json",
                    timeout=5,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("covers"):
                        cover_id = data["covers"][0]
                        cover_url = f"https://covers.openlibrary.org/b/id/{cover_id}-L.jpg"
            except Exception as e:
                print(f"Open Library fetch error for {item_id}: {e}")

        if not cover_url or not _is_safe_cover_url(cover_url):
            return "", 404
        return redirect(cover_url, code=302)

    if media_type == "manga":
        row = get_media_by_external_id(item_id, media_type)
        cover_url = (row or {}).get("cover_url", "")

        if not cover_url:
            try:
                cover_url = _fetch_mangadex_cover_url(item_id)
                if cover_url and row:
                    update_media_entry(row["id"], cover_url=cover_url)
                    _invalidate_media_cache(row["id"])
            except Exception:
                cover_url = ""

        if not cover_url or not _is_safe_cover_url(cover_url):
            return "", 404
        return redirect(cover_url, code=302)

    cache_key = f"{media_type}_{item_id}"
    if cache_key in poster_cache:
        img_bytes, content_type = poster_cache[cache_key]
        return Response(
            img_bytes,
            mimetype=content_type,
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    tmdb_key = _get_tmdb_key()
    if not tmdb_key:
        return "", 401

    endpoint = "movie" if media_type == "movie" else "tv"
    meta_url = f"https://api.themoviedb.org/3/{endpoint}/{item_id}"
    try:
        meta = requests.get(meta_url, params=_tmdb_params(tmdb_key), timeout=5).json()
        path = meta.get("poster_path")
        if not path:
            return "", 404
        img_resp = requests.get(f"https://image.tmdb.org/t/p/w500{path}", timeout=10)
        img_resp.raise_for_status()
        content_type = img_resp.headers.get("Content-Type", "image/jpeg")
        img_bytes = img_resp.content
        poster_cache[cache_key] = (img_bytes, content_type)
        return Response(
            img_bytes, mimetype=content_type,
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )
    except Exception as e:
        print(f"TMDB poster error for {media_type}/{item_id}: {e}")
        return "", 404


# ═══════════════════════════════════════════════════
# Ratings
# ═══════════════════════════════════════════════════

@app.route("/api/ratings/<media_type>/<tmdb_id>")
@login_required
def api_ratings(media_type, tmdb_id):
    key = _get_mdblist_key()
    if not key:
        return jsonify({"ratings": {}})

    force = request.args.get("force") == "1"
    # Read-only mode: serve stored/cached ratings (free) and NEVER attempt a live MDBList
    # call. The client sends this when the MDBList quota is exhausted, so cached pills
    # still render without spending a (doomed) call.
    norefresh = request.args.get("norefresh") == "1"
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
        # Refresh when forced (manual button, exempt from cap) or stale-and-under-cap —
        # but never in read-only mode.
        if not norefresh and (force or (not fresh and _take_lazy_refresh_slot(uid))):
            data = _fetch_mdblist_ratings(media_type, tmdb_id, key)
            # Never let a failed (None) OR empty ({}) upstream result overwrite good stored
            # ratings: MDBList intermittently 200s with no ratings under load, and on
            # failure data is None. Either would blank the title's pills (for 7 days, since
            # a fresh stamp marks it "fresh") and lock the user out of retrying. Keep what
            # we have and don't restamp, so the pills stay and a retry is allowed now.
            if data is None or (not data and stored):
                return jsonify({"ratings": stored, "updated_at": row.get("ratings_updated_at")})
            now = datetime.now(timezone.utc).isoformat()
            set_media_ratings(row["id"], json.dumps(data), now)
            return jsonify({"ratings": data, "updated_at": now})
        # Fresh, or cap hit: serve what we have.
        return jsonify({"ratings": stored, "updated_at": row.get("ratings_updated_at")})

    # Non-library (search result): TTL cache.
    ck = f"{media_type}:{tmdb_id}"
    hit = _ratings_cache.get(ck)
    if hit and (time.time() - hit[0]) < _RATINGS_TTL:
        return jsonify({"ratings": hit[1]})
    if norefresh:
        return jsonify({"ratings": {}})   # read-only: no cache hit, don't call MDBList
    data = _fetch_mdblist_ratings(media_type, tmdb_id, key)
    if data is None:
        return jsonify({"ratings": {}})   # transient failure — don't TTL-cache it
    if len(_ratings_cache) > 2000:
        _ratings_cache.pop(next(iter(_ratings_cache)))
    _ratings_cache[ck] = (time.time(), data)
    return jsonify({"ratings": data})


@app.route("/api/mdblist-status")
@login_required
def api_mdblist_status():
    key = _get_mdblist_key()
    if not key:
        return jsonify({"has_key": False, "limit": None, "used": None, "remaining": None})
    uid = int(current_user.id)
    hit = _mdblist_status_cache.get(uid)
    if hit and (time.time() - hit[0]) < _MDBLIST_STATUS_TTL:
        return jsonify(hit[1])
    out = {"has_key": True, "limit": None, "used": None, "remaining": None}
    try:
        resp = requests.get("https://api.mdblist.com/user", params={"apikey": key}, timeout=8)
        if resp.status_code == 200:
            d = resp.json()
            limit, used = d.get("api_requests"), d.get("api_requests_count")
            remaining = (limit - used) if isinstance(limit, int) and isinstance(used, int) else None
            out = {"has_key": True, "limit": limit, "used": used, "remaining": remaining}
            # A drop in the used-count means the MDBList quota refreshed; reset our
            # auto-refresh cap so it tracks the same daily window as the quota.
            if isinstance(used, int):
                sync_lazy_cap_to_quota(used)
    except Exception:
        pass
    _mdblist_status_cache[uid] = (time.time(), out)
    return jsonify(out)


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


# ═══════════════════════════════════════════════════
# TV / Manga constraint info
# ═══════════════════════════════════════════════════

tv_info_cache:    _BoundedCache = _BoundedCache(500)
manga_info_cache: _BoundedCache = _BoundedCache(500)

@app.route("/api/tv-info/<int:tmdb_id>")
@login_required
def tv_info(tmdb_id):
    key = str(tmdb_id)
    if key in tv_info_cache:
        return jsonify(tv_info_cache[key])
    tmdb_key = _get_tmdb_key()
    if not tmdb_key:
        return jsonify({"error": "TMDB API key required"}), 401
    try:
        url = f"https://api.themoviedb.org/3/tv/{tmdb_id}"
        resp = requests.get(url, params=_tmdb_params(tmdb_key), timeout=8)
        resp.raise_for_status()
        data = resp.json()
        seasons = data.get("seasons", [])
        real_seasons = [s for s in seasons if s.get("season_number", 0) > 0]
        if not real_seasons:
            real_seasons = seasons
        info = {
            "num_seasons": len(real_seasons),
            "seasons": [
                {"season_number": s["season_number"], "episode_count": s.get("episode_count", 1)}
                for s in real_seasons
            ]
        }
        tv_info_cache[key] = info
        return jsonify(info)
    except Exception as e:
        print(f"TV info error for {tmdb_id}: {e}")
        return jsonify({"error": "An internal error occurred."}), 500


@app.route("/api/manga-info/<path:manga_id>")
@login_required
def manga_info(manga_id):
    if manga_id in manga_info_cache:
        return jsonify(manga_info_cache[manga_id])
    try:
        resp = requests.get(
            f"https://api.mangadex.org/manga/{manga_id}",
            headers=MANGADEX_HEADERS,
            params={"includes[]": []},
            timeout=8,
        )
        resp.raise_for_status()
        attrs = resp.json().get("data", {}).get("attributes", {})
        last_chapter = attrs.get("lastChapter")
        info = {
            "last_chapter": float(last_chapter) if last_chapter else None,
        }
        manga_info_cache[manga_id] = info
        return jsonify(info)
    except Exception as e:
        print(f"Manga info error for {manga_id}: {e}")
        return jsonify({"error": "An internal error occurred."}), 500


# ═══════════════════════════════════════════════════
# Library CRUD
# ═══════════════════════════════════════════════════

@app.route("/api/list")
@login_required
def list_media():
    return jsonify(get_all_media())


@app.route("/api/add", methods=["POST"])
@login_required
def add_media():
    data = request.get_json(silent=True) or {}
    if "id" in data:
        status = data.get("status")
        if status is not None and status not in VALID_STATUSES:
            return jsonify({"error": "Invalid status"}), 400
        allowed = {
            "status", "rating", "last_timestamp",
            "last_season", "last_episode", "last_chapter",
            "current_page", "total_pages",
            "notes", "cover_url", "author",
        }
        fields = {k: data[k] for k in allowed if k in data}
        if fields.get("rating") is not None:
            try:
                fields["rating"] = float(fields["rating"])
            except (TypeError, ValueError):
                return jsonify({"error": "Invalid rating"}), 400
            if not 0 <= fields["rating"] <= 10:
                return jsonify({"error": "Rating out of range"}), 400
        for f in ("notes", "author"):
            v = fields.get(f)
            if isinstance(v, str) and len(v) > MAX_TEXT_LEN:
                return jsonify({"error": f"{f} too long"}), 400
        if not _is_safe_cover_url(fields.get("cover_url")):
            fields.pop("cover_url", None)
        update_media_entry(data["id"], **fields)
        _invalidate_media_cache(data["id"])
        updated = get_media_by_id(data["id"])
        return jsonify({
            "status": "ok",
            "date_added": updated["date_added"] if updated else None,
        })
    else:
        title = data.get("title")
        if not isinstance(title, str) or not title.strip():
            return jsonify({"error": "Title required"}), 400
        if len(title) > MAX_TITLE_LEN:
            return jsonify({"error": "Title too long"}), 400
        media_type = data.get("media_type")
        if media_type not in VALID_MEDIA_TYPES:
            return jsonify({"error": "Invalid media_type"}), 400
        status = data.get("status", "watchlist")
        if status not in VALID_STATUSES:
            return jsonify({"error": "Invalid status"}), 400
        cover_url = data.get("cover_url")
        if not _is_safe_cover_url(cover_url):
            cover_url = None
        tmdb_rating = data.get("tmdb_rating")
        if tmdb_rating is not None:
            try:
                tmdb_rating = float(tmdb_rating)
            except (TypeError, ValueError):
                tmdb_rating = None
            # TMDB vote_average is always 0–10; reject inf/nan/out-of-range.
            if tmdb_rating is not None and not (0.0 <= tmdb_rating <= 10.0):
                tmdb_rating = None
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


@app.route("/api/delete/<int:media_id>", methods=["DELETE"])
@login_required
def delete_media(media_id):
    item = cached_get_media(media_id)
    if not item:
        return jsonify({"status": "not_found"}), 404
    poster_cache.pop(
        f"{item['media_type']}_{item.get('external_id') or item.get('tmdb_id')}",
        None,
    )
    delete_media_entry(media_id)
    _invalidate_media_cache(media_id)
    return jsonify({"status": "deleted"})


@app.route("/api/media-backup/<int:media_id>")
@login_required
def media_backup(media_id):
    media = cached_get_media(media_id)
    if not media:
        return jsonify({"error": "Media not found"}), 404
    return jsonify({"media": media})


@app.route("/api/restore-media", methods=["POST"])
@login_required
def restore_media():
    data       = request.get_json(silent=True) or {}
    media_data = data.get("media")
    if not isinstance(media_data, dict):
        return jsonify({"error": "Missing media data"}), 400

    title = media_data.get("title")
    if not isinstance(title, str) or not title.strip():
        return jsonify({"error": "Title required"}), 400
    if media_data.get("media_type") not in VALID_MEDIA_TYPES:
        return jsonify({"error": "Invalid media_type"}), 400
    if media_data.get("status") not in VALID_STATUSES:
        return jsonify({"error": "Invalid status"}), 400

    restore_cover = media_data.get("cover_url")
    new_media_id = add_media_entry(
        title       = title,
        media_type  = media_data["media_type"],
        status      = media_data["status"],
        tmdb_id     = media_data.get("tmdb_id"),
        external_id = media_data.get("external_id"),
        cover_url   = restore_cover if _is_safe_cover_url(restore_cover) else None,
        author      = media_data.get("author"),
        total_pages = media_data.get("total_pages"),
    )
    update_fields = {
        k: media_data[k]
        for k in ("rating", "notes", "last_timestamp", "last_season",
                  "last_episode", "last_chapter",
                  "current_page", "total_pages")
        if k in media_data
    }
    if update_fields:
        update_media_entry(new_media_id, **update_fields)

    # Restore the original per-status dates (overriding the today-seeded ones) so
    # undo brings the item back exactly as it was, not freshly "added today".
    date_fields = {
        k: media_data[k]
        for k in ("date_added", "date_watchlist", "date_watching", "date_finished")
        if isinstance(media_data.get(k), str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", media_data[k])
    }
    if date_fields:
        set_media_dates(new_media_id, **date_fields)

    return jsonify({"new_media_id": new_media_id})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=os.environ.get("FLASK_DEBUG", "0") == "1", use_reloader=True, host="0.0.0.0", port=port, threaded=True)
