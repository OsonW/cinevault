import os
import re
import json
import time
import secrets
import threading
import requests
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, jsonify, Response, request, render_template, g, redirect, url_for, make_response
from flask_login import login_required, current_user, logout_user
from google import genai

from db import (
    get_user_db_path, init_user_db, get_conn,
    get_all_media, get_media_by_id, get_media_by_external_id,
    add_media_entry, update_media_entry, delete_media_entry,
    get_all_chats, get_chats_by_media_id, get_chat_by_id, get_chat_messages,
    create_chat, append_message, delete_chat, clear_chat_messages,
    update_chat_spoiler_threshold, update_message_snap,
    get_all_memory,
)
from auth import auth_bp, login_manager, rate_limit
from users_db import init_users_db, get_user_keys, delete_user

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
app.config["SESSION_COOKIE_SECURE"]    = _production
app.config["SESSION_COOKIE_HTTPONLY"]  = True
app.config["SESSION_COOKIE_SAMESITE"]  = "Strict"
app.config["REMEMBER_COOKIE_SECURE"]   = _production
app.config["REMEMBER_COOKIE_HTTPONLY"] = True
app.config["REMEMBER_COOKIE_SAMESITE"] = "Strict"

# Reject request bodies larger than 256 KB. Every endpoint in this app deals
# in small JSON payloads — credentials, key strings, library updates, chat
# messages. A megabyte-plus body is almost certainly abuse, and refusing
# early stops a slow client from tying up a worker.
app.config["MAX_CONTENT_LENGTH"] = 256 * 1024

login_manager.init_app(app)
login_manager.login_view = "auth.login_page"
app.register_blueprint(auth_bp)

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")


def _get_tmdb_key() -> str | None:
    if current_user.is_authenticated:
        key = get_user_keys(int(current_user.id)).get("tmdb_key", "").strip()
        if key:
            return key
    return None


def _get_gemini_key() -> str | None:
    if current_user.is_authenticated:
        key = get_user_keys(int(current_user.id)).get("gemini_key", "").strip()
        if key:
            return key
    return None


def _tmdb_params(api_key: str) -> dict:
    return {"api_key": api_key}


def _make_gemini(api_key: str):
    return genai.Client(api_key=api_key)

# Shared across users — keyed by external IDs so safe to share
poster_cache: dict[str, tuple[bytes, str]] = {}
search_cache: dict[str, list]              = {}

# Per-user caches — keyed by user_id
_user_media_cache:   dict[int, dict] = {}
_user_ai_card_cache: dict[int, dict] = {}
_user_memory_cache:  dict[int, dict] = {}

# Lazy initialisation flags
_app_initialized    = False
_initialized_users: set[int] = set()
_init_lock = threading.Lock()

MAX_CHAT_HISTORY = 12
AI_CARD_TTL      = 600
MANGADEX_HEADERS = {"User-Agent": "CineVault/1.0"}

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
        "img-src 'self' data: https://covers.openlibrary.org "
        "https://uploads.mangadex.org https://image.tmdb.org; "
        "connect-src 'self'; "
        "frame-ancestors 'none';"
    )
    return response


@app.before_request
def setup_request():
    global _app_initialized
    if not _app_initialized:
        init_users_db()
        _app_initialized = True

    # Redirect already-logged-in users away from the login page
    if request.path == "/login" and current_user.is_authenticated:
        return redirect(url_for("index"))

    if current_user.is_authenticated:
        uid = current_user.id
        g.user_db_path = get_user_db_path(uid)
        # Guarded so concurrent first-requests don't both spawn the backfill.
        with _init_lock:
            already_init = uid in _initialized_users
            if not already_init:
                _initialized_users.add(uid)
        if not already_init:
            init_user_db(uid)

    # All /api/* routes require authentication
    if request.path.startswith("/api/") and not current_user.is_authenticated:
        return jsonify({"error": "Unauthorized"}), 401


SPOILER_CONSENT_PHRASES = [
    "spoil", "spoilers", "tell me everything",
    "i don't mind spoilers", "i dont mind spoilers",
    "you can spoil", "spoil away",
]


def _cache_search(key: str, items: list) -> None:
    search_cache[key] = items
    if len(search_cache) > SEARCH_CACHE_MAX:
        del search_cache[next(iter(search_cache))]

def _media_cache() -> dict:
    return _user_media_cache.setdefault(current_user.id, {})


def _ai_cache() -> dict:
    return _user_ai_card_cache.setdefault(current_user.id, {})


def _mem_cache() -> dict:
    uid = current_user.id
    if uid not in _user_memory_cache:
        try:
            _user_memory_cache[uid] = get_all_memory()
        except Exception:
            _user_memory_cache[uid] = {}
    return _user_memory_cache[uid]


def _invalidate_media_cache(media_id: int):
    _media_cache().pop(media_id, None)
    ac = _ai_cache()
    for k in [k for k in ac if k.startswith(f"{media_id}_")]:
        del ac[k]


def cached_get_media(media_id: int):
    mc = _media_cache()
    if media_id not in mc:
        mc[media_id] = get_media_by_id(media_id)
    return mc[media_id]


def _progress_str(media: dict) -> str:
    mt = media.get("media_type", "")
    if mt == "tv":
        s, e = media.get("last_season"), media.get("last_episode")
        if s and e:
            return f"Season {s}, Episode {e}"
        if s:
            return f"Season {s}"
    elif mt == "manga":
        v, c = media.get("last_volume"), media.get("last_chapter")
        if v and c:
            return f"Volume {v}, Chapter {c}"
        if v:
            return f"Volume {v}"
        if c:
            return f"Chapter {c}"
    elif mt == "book":
        cp, tp = media.get("current_page"), media.get("total_pages")
        if cp and tp:
            return f"Page {cp} of {tp}"
        if cp:
            return f"Page {cp}"
    return ""


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



def _append_gemini_content(contents: list, role: str, text: str):
    if not text:
        return
    part = {"text": text}
    if contents and contents[-1]["role"] == role:
        contents[-1]["parts"].append(part)
    else:
        contents.append({"role": role, "parts": [part]})


def _iter_gemini_response_tokens(contents, gemini_client):
    stream_fn = getattr(gemini_client.models, "generate_content_stream", None)
    if stream_fn:
        try:
            for chunk in stream_fn(model=GEMINI_MODEL, contents=contents):
                token = getattr(chunk, "text", "") or ""
                if token:
                    yield token
            return
        except TypeError:
            pass
    resp = gemini_client.models.generate_content(model=GEMINI_MODEL, contents=contents)
    text = (getattr(resp, "text", "") or "").strip()
    if text:
        yield text


def _generate_ai_content(prompt: str, gemini_client) -> str:
    resp = gemini_client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
    return resp.text.strip()


@app.route("/auth/cancel-registration", methods=["POST"])
@login_required
@rate_limit(max_requests=5, window_seconds=3600)   # 5/hour per IP
def cancel_registration():
    uid = int(current_user.id)
    # Drop per-user caches and the per-user SQLite file (if it was already created)
    _user_media_cache.pop(uid, None)
    _user_ai_card_cache.pop(uid, None)
    _user_memory_cache.pop(uid, None)
    with _init_lock:
        _initialized_users.discard(uid)
    db_path = get_user_db_path(uid)
    # Remove the main DB file plus any SQLite sidecars (-journal/-wal/-shm)
    # that may exist if a connection was open.
    for path in (db_path, db_path + "-journal", db_path + "-wal", db_path + "-shm"):
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError as e:
            print(f"cancel-registration: failed to remove {path}: {e}")
    logout_user()
    delete_user(uid)
    return jsonify({"status": "ok"})


@app.route("/")
@login_required
def index():
    resp = make_response(render_template("index.html", username=current_user.username))
    # Prevent the browser from restoring the authenticated page from cache
    # after the user logs out (back-button hardening).
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"]        = "no-cache"
    return resp


# ═══════════════════════════════════════════════════
# Search (TMDB, Books, Manga)
# ═══════════════════════════════════════════════════

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
    author = _fetch_tmdb_director(item["media_type"], int(tmdb_id), tmdb_key)
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
                }
                for r in resp.json().get("results", []) if r.get("id")
            ]
        items.sort(key=lambda x: x.get("popularity", 0), reverse=True)
    except Exception as e:
        print(f"Search error: {e}")
        items = []

    items = items[:10]

    # Fetch director/creator for each result in parallel
    if items:
        with ThreadPoolExecutor(max_workers=len(items)) as pool:
            futs = {pool.submit(_fetch_tmdb_director, i["media_type"], i["tmdb_id"], tmdb_key): i for i in items}
            for fut in as_completed(futs):
                futs[fut]["author"] = fut.result()

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


@app.route("/api/book/isbn/<isbn>")
@login_required
def get_book_by_isbn(isbn):
    cache_key = f"isbn:{isbn}"
    if cache_key in search_cache:
        return jsonify(search_cache[cache_key])
    
    try:
        resp = requests.get(f"https://openlibrary.org/isbn/{isbn}.json", timeout=8)
        if resp.status_code != 200:
            return jsonify({"error": "Not found"}), 404
        data = resp.json()
        work_key = data.get("works", [{}])[0].get("key")
        description = ""
        if work_key:
            work_resp = requests.get(f"https://openlibrary.org{work_key}.json", timeout=8)
            if work_resp.status_code == 200:
                work_data = work_resp.json()
                description = work_data.get("description", "")
                if isinstance(description, dict):
                    description = description.get("value", "")

        cover_url = None
        if data.get("covers"):
            cover_id = data["covers"][0]
            cover_url = f"https://covers.openlibrary.org/b/id/{cover_id}-L.jpg"
        result = {
            "external_id": isbn,
            "title": data.get("title", "Unknown"),
            "author": ", ".join(data.get("authors", [])),
            "year": str(data.get("publish_date", ""))[:4],
            "media_type": "book",
            "cover_url": cover_url,
            "total_pages": data.get("number_of_pages"),
            "overview": description[:500] if description else "",
        }
        search_cache[cache_key] = result
        return jsonify(result)

    except Exception as e:
        print(f"ISBN lookup error: {e}")
        return jsonify({"error": "An internal error occurred."}), 500


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
        cover_url = (row or {}).get("cover_url", "")

        if not cover_url:
            try:
                resp = requests.get(
                    f"https://openlibrary.org/works/{item_id}.json",
                    timeout=5
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

        try:
            img_resp = requests.get(cover_url, timeout=10)
            img_resp.raise_for_status()
            content_type = img_resp.headers.get("Content-Type", "image/jpeg")
            return Response(
                img_resp.content,
                mimetype=content_type,
                headers={"Cache-Control": "public, max-age=86400"},
            )
        except Exception as e:
            print(f"Book cover proxy error for {item_id}: {e}")
            return "", 404

    if media_type == "manga":
        row = get_media_by_external_id(item_id, media_type)
        cover_url = (row or {}).get("cover_url", "")

        if not cover_url:
            try:
                cover_url = _fetch_mangadex_cover_url(item_id)
                if cover_url and row:
                    update_media_entry(row["id"], cover_url=cover_url)
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
# TV / Manga constraint info
# ═══════════════════════════════════════════════════

tv_info_cache:    dict[str, dict] = {}
manga_info_cache: dict[str, dict] = {}

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
        allowed = {
            "status", "rating", "last_timestamp",
            "last_season", "last_episode",
            "last_volume", "last_chapter",
            "current_page", "total_pages",
            "notes", "cover_url", "author",
        }
        fields = {k: data[k] for k in allowed if k in data}
        if not _is_safe_cover_url(fields.get("cover_url")):
            fields.pop("cover_url", None)
        update_media_entry(data["id"], **fields)
        _invalidate_media_cache(data["id"])
    else:
        cover_url = data.get("cover_url")
        if not _is_safe_cover_url(cover_url):
            cover_url = None
        media_type = data.get("media_type")
        external_id = data.get("external_id")
        add_media_entry(
            title       = data["title"],
            media_type  = media_type,
            status      = data.get("status", "watchlist"),
            tmdb_id     = data.get("tmdb_id"),
            external_id = external_id,
            cover_url   = cover_url,
            author      = data.get("author"),
            total_pages = data.get("total_pages"),
            overview    = data.get("overview"),
            year        = data.get("year"),
        )
    return jsonify({"status": "ok"})


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


@app.route("/api/media-with-chats/<int:media_id>")
@login_required
def media_with_chats(media_id):
    media = cached_get_media(media_id)
    if not media:
        return jsonify({"error": "Media not found"}), 404
    chats = [
        {**chat, "messages": get_chat_messages(chat["id"])}
        for chat in get_chats_by_media_id(media_id)
    ]
    return jsonify({"media": media, "chats": chats})


@app.route("/api/restore-media-chats", methods=["POST"])
@login_required
def restore_media_chats():
    data       = request.get_json(silent=True) or {}
    media_data = data.get("media")
    chats_data = data.get("chats", [])
    if not media_data:
        return jsonify({"error": "Missing media data"}), 400

    restore_cover = media_data.get("cover_url")
    new_media_id = add_media_entry(
        title       = media_data["title"],
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
                  "last_episode", "last_volume", "last_chapter",
                  "current_page", "total_pages")
        if k in media_data
    }
    if update_fields:
        update_media_entry(new_media_id, **update_fields)

    for chat in chats_data:
        chat_id = create_chat(
            media_id    = new_media_id,
            title       = chat["title"],
            context_tag = chat.get("context_tag"),
        )
        for msg in chat.get("messages", []):
            append_message(chat_id, msg["role"], msg["content"])

    return jsonify({"new_media_id": new_media_id})


# ═══════════════════════════════════════════════════
# Vibe Search
# ═══════════════════════════════════════════════════

@app.route("/api/vibe-search/types", methods=["POST"])
@login_required
def vibe_search_types():
    data  = request.get_json(silent=True) or {}
    query = data.get("query", "").strip()
    if not query:
        return jsonify({"types": ["movie", "tv"]}), 400

    gemini_key = _get_gemini_key()
    if not gemini_key:
        return jsonify({"error": "Gemini API key required"}), 401
    gemini_client = _make_gemini(gemini_key)

    type_prompt = (
        f'The user is searching for: "{query}"\n'
        f'Available media types: movie, tv, book, manga\n'
        f'Decide which media types are most relevant.\n'
        f'Rules:\n'
        f'- If the query explicitly names a media type word (e.g. "manga", "anime", "book", "movie", "show"), '
        f'that type MUST be in the list. "anime" maps to tv.\n'
        f'- If the query names a specific title, include only the type(s) that title belongs to.\n'
        f'- If the query is a mood/vibe (e.g. "cozy mysteries", "dark thriller"), pick the types '
        f'where those vibes are most common.\n'
        f'- Return ONLY a JSON array from: ["movie","tv","book","manga"]. No other text.\n'
        f'Examples:\n'
        f'  "manga like one piece" -> ["manga"]\n'
        f'  "similar to naruto manga" -> ["manga","tv"]\n'
        f'  "breaking bad" -> ["tv"]\n'
        f'  "one piece" -> ["manga","tv"]\n'
        f'  "dune" -> ["movie","book"]\n'
        f'  "short and funny" -> ["movie","manga"]\n'
        f'  "dark academia" -> ["book","movie","tv"]\n'
        f'  "studio ghibli" -> ["movie"]\n'
    )
    suggested_types = ["movie", "tv"]
    try:
        resp   = gemini_client.models.generate_content(model=GEMINI_MODEL, contents=type_prompt)
        raw    = re.sub(r"^```[a-z]*\n?", "", resp.text.strip()).rstrip("` \n")
        parsed = json.loads(raw)
        valid  = [t for t in parsed if t in ("movie", "tv", "book", "manga")]
        if valid:
            suggested_types = valid
    except Exception as e:
        print(f"Vibe type detection error: {e}")
    return jsonify({"types": suggested_types})


_SIMILAR_RE = re.compile(
    r'\b(like|similar\s+to|in\s+the\s+style\s+of|reminds?\s+me\s+of|along\s+the\s+lines\s+of)\b',
    re.IGNORECASE,
)


def _vibe_prompt(query: str, types: list, exclude_titles: list, is_first: bool) -> str:
    type_list    = ", ".join(types)
    exclude_note = (
        f'\nDo NOT include any of these already-shown titles: {json.dumps(exclude_titles)}.'
        if exclude_titles else ""
    )

    # Decide whether the query names a specific title the user wants included.
    similarity_phrasing = bool(_SIMILAR_RE.search(query))
    include_seed = (
        not similarity_phrasing
        and is_first
    )

    if include_seed:
        seed_rule = (
            'If the input names a specific title, include that title as the first result '
            'and fill the remaining slots with closely related titles.'
        )
    else:
        seed_rule = (
            'Do NOT include the title the user mentioned as a seed — '
            'return only titles similar to it or matching the described vibe.'
        )

    type_constraint = (
        f'ALLOWED types for this search: [{type_list}]. '
        f'Every result MUST have a media_type from that list — no exceptions. '
        f'Weight your picks toward the type(s) the user\'s wording implies most strongly '
        f'(e.g. if they said "manga", prefer manga over tv even if both are allowed). '
        f'Never substitute: do not return a tv show when manga was asked for, or a movie when a book was asked for.'
    )

    return (
        f'You are a media recommendation engine.\n'
        f'User input: "{query}"\n'
        f'{type_constraint}\n'
        f'\n'
        f'{seed_rule}\n'
        f'If the input describes a mood or vibe rather than a specific title, recommend media that fits it.\n'
        f'{exclude_note}\n'
        f'Return ONLY a JSON array of exactly 5 objects with EXACTLY these keys:\n'
        f'  "title"      : exact, well-known title string\n'
        f'  "year"       : release year as a string (e.g. "2019") or ""\n'
        f'  "media_type" : must be one of [{type_list}] — no other values allowed\n'
        f'No markdown, no extra keys, no explanation.'
    )


def _enrich_one(rec: dict, tmdb_key: str) -> dict:
    mt          = rec.get("media_type", "movie")
    title       = rec.get("title", "")
    year        = rec.get("year", "")
    poster_path = None
    tmdb_id     = None
    external_id = None
    cover_url   = ""
    author      = ""
    total_pages = None
    overview    = ""

    if mt == "movie":
        try:
            resp = requests.get(
                "https://api.themoviedb.org/3/search/movie",
                params={**_tmdb_params(tmdb_key), "query": title},
                timeout=5,
            )
            for r in resp.json().get("results", []):
                if not r.get("id"):
                    continue
                ry = (r.get("release_date", "") or "")[:4]
                if year and ry and ry != year and tmdb_id:
                    continue
                tmdb_id     = r["id"]
                external_id = str(r["id"])
                poster_path = r.get("poster_path")
                overview    = r.get("overview") or ""
                if ry == year:
                    break
        except Exception as e:
            print(f"TMDB movie enrich skipped for '{title}': {e}")

    elif mt == "tv":
        try:
            resp = requests.get(
                "https://api.themoviedb.org/3/search/tv",
                params={**_tmdb_params(tmdb_key), "query": title},
                timeout=5,
            )
            for r in resp.json().get("results", []):
                if not r.get("id"):
                    continue
                ry = (r.get("first_air_date", "") or "")[:4]
                if year and ry and ry != year and tmdb_id:
                    continue
                tmdb_id     = r["id"]
                external_id = str(r["id"])
                poster_path = r.get("poster_path")
                overview    = r.get("overview") or ""
                if ry == year:
                    break
        except Exception as e:
            print(f"TMDB tv enrich skipped for '{title}': {e}")

    elif mt == "book":
        try:
            resp = requests.get(
                "https://openlibrary.org/search.json",
                params={"q": title, "limit": 1},
                timeout=5,
            )
            for doc in resp.json().get("docs", []):
                cover_id    = doc.get("cover_i")
                cover_url   = f"https://covers.openlibrary.org/b/id/{cover_id}-L.jpg" if cover_id else ""
                external_id = doc.get("key", "").replace("/works/", "")
                author      = ", ".join(doc.get("author_name", []))
                total_pages = doc.get("number_of_pages_median")
                break
        except Exception as e:
            print(f"Book enrich skipped for '{title}': {e}")

    elif mt == "manga":
        try:
            resp = requests.get(
                "https://api.mangadex.org/manga",
                headers=MANGADEX_HEADERS,
                params={
                    "title": title,
                    "limit": 1,
                    "includes[]": ["cover_art", "author"],
                    "availableTranslatedLanguage[]": ["en"],
                },
                timeout=5,
            )
            if resp.status_code == 200:
                data = resp.json().get("data", [])
                if data:
                    m           = data[0]
                    external_id = m["id"]
                    attrs       = m.get("attributes", {})
                    desc        = attrs.get("description", {})
                    raw_desc    = desc.get("en") or next(iter(desc.values()), "")
                    if raw_desc:
                        overview = raw_desc[:300]
                    for rel in m.get("relationships", []):
                        if rel["type"] == "cover_art":
                            fn = rel.get("attributes", {}).get("fileName", "")
                            if fn:
                                cover_url = _mangadex_cover_url(external_id, fn)
                            break
                    for rel in m.get("relationships", []):
                        if rel["type"] == "author":
                            author = rel.get("attributes", {}).get("name", "")
                            break
        except Exception as e:
            print(f"Manga enrich skipped for '{title}': {e}")

    return {
        "title":       title,
        "year":        year,
        "media_type":  mt,
        "overview":    overview,
        "tmdb_id":     tmdb_id,
        "external_id": external_id,
        "poster_path": poster_path,
        "cover_url":   cover_url,
        "author":      author,
        "total_pages": total_pages,
    }


def _enrich_results(recs: list, tmdb_key: str) -> list:
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(_enrich_one, rec, tmdb_key): i for i, rec in enumerate(recs)}
        ordered = [None] * len(recs)
        for future in as_completed(futures):
            ordered[futures[future]] = future.result()
    return [r for r in ordered if r is not None]


@app.route("/api/vibe-search", methods=["POST"])
@login_required
def vibe_search():
    data           = request.get_json(silent=True) or {}
    query          = data.get("query", "").strip()
    types          = data.get("types", ["movie", "tv"])
    exclude_titles = data.get("exclude_titles", [])
    if not query:
        return jsonify({"error": "No query provided"}), 400

    gemini_key = _get_gemini_key()
    if not gemini_key:
        return jsonify({"error": "Gemini API key required"}), 401
    tmdb_key = _get_tmdb_key()
    if not tmdb_key:
        return jsonify({"error": "TMDB API key required"}), 401
    gemini_client = _make_gemini(gemini_key)

    is_first = not exclude_titles
    prompt   = _vibe_prompt(query, types, exclude_titles, is_first)

    try:
        resp = gemini_client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        raw  = re.sub(r"^```[a-z]*\n?", "", resp.text.strip()).rstrip("` \n")
        recs = json.loads(raw)
    except Exception as e:
        print(f"Vibe search AI error: {e}")
        return jsonify({"error": "An internal error occurred."}), 500

    return jsonify(_enrich_results(recs[:5], tmdb_key))


# ═══════════════════════════════════════════════════
# Chats
# ═══════════════════════════════════════════════════

@app.route("/api/chats")
@login_required
def list_chats():
    return jsonify(get_all_chats())


@app.route("/api/chats/<int:chat_id>")
@login_required
def get_chat(chat_id):
    chat = get_chat_by_id(chat_id)
    if not chat:
        return jsonify({"error": "Not found"}), 404
    msgs = get_chat_messages(chat_id)
    return jsonify({**chat, "messages": msgs})


@app.route("/api/chats/new", methods=["POST"])
@login_required
def new_chat():
    data        = request.get_json(silent=True) or {}
    media_id    = data.get("media_id")
    context_tag = data.get("context_tag")

    if media_id:
        media = cached_get_media(media_id)
        title = data.get("title") or "Conversation"
        if not context_tag and media:
            context_tag = media["status"]
    else:
        title = data.get("title", "New chat")

    chat_id = create_chat(media_id, title, context_tag)
    return jsonify({"id": chat_id, "title": title})


def _parse_snap(snap_str: str, media: dict) -> dict:
    """Parse a snap string like 'S4E10' or 'Ch94' into snap_* fields.
    Returns {} if snap_str is null/none/unknown/empty."""
    if not snap_str or not media:
        return {}
    s = snap_str.strip()
    if s.lower() in ("null", "none", "unknown", "n/a", ""):
        return {}
    mt = media.get("media_type", "")
    if mt == "tv":
        m = re.match(r'S(\d+)\s*E(\d+)', s, re.IGNORECASE)
        if m:
            return {"snap_season": int(m.group(1)), "snap_episode": int(m.group(2))}
        # Season-only fallback: "S4" with no episode
        m = re.match(r'S(\d+)$', s, re.IGNORECASE)
        if m:
            return {"snap_season": int(m.group(1)), "snap_episode": None}
    elif mt == "manga":
        m = re.match(r'Ch\.?\s*(\d+(?:\.\d+)?)', s, re.IGNORECASE)
        if m:
            return {"snap_chapter": float(m.group(1))}
    return {}


def _snap_is_spoiler(snap: dict, media: dict) -> bool:
    """Return True if the snapped position is beyond the user's current progress."""
    if not snap or not media:
        return False
    mt = media.get("media_type", "")
    if mt == "tv":
        snap_s = snap.get("snap_season") or 0
        snap_e = snap.get("snap_episode") or 0
        user_s = media.get("last_season") or 0
        user_e = media.get("last_episode") or 0
        return snap_s > user_s or (snap_s == user_s and snap_e > user_e)
    if mt == "manga":
        return (snap.get("snap_chapter") or 0) > (media.get("last_chapter") or 0)
    return False


def _build_structured_system(media: dict, effective_status: str, progress: str,
                              memory_str: str, user_content: str) -> str:
    """Build the system prompt that instructs the model to return structured JSON.

    The model must ALWAYS reply with exactly this JSON shape — no prose outside it:
      {"snap": "<tag>", "answer": "<reply>"}

    For TV:   snap = "S<season>E<episode>" e.g. "S4E10"  — the FURTHEST point the answer
              discusses. If the answer stays within current progress, use the current
              progress tag. If no progress is known, use null.
    For manga: snap = "Ch<chapter>" e.g. "Ch94"
    For all other media: snap = null always.
    """
    mt        = media["media_type"]
    title     = media["title"]
    user_consented_spoilers = any(
        p in user_content.lower() for p in SPOILER_CONSENT_PHRASES
    )

    if effective_status == "finished":
        spoiler_rules = "The user has finished this title. Full discussion of all plot, characters, endings, and themes is fair game."
    elif effective_status == "watchlist":
        spoiler_rules = "The user hasn't started this yet. Stick to vibe, genre, and premise only — no spoilers."
    elif user_consented_spoilers:
        spoiler_rules = "The user is explicitly asking for spoilers. You may freely discuss plot points beyond their current progress."
    else:
        spoiler_rules = (
            f"The user has only reached {progress or 'their current point'}. "
            f"Scope EVERY answer to what has happened up to that point. "
            f"Never reveal plot, character fates, or story outcomes that occur after {progress}."
        )

    if mt == "tv":
        snap_instructions = (
            f'• "snap": The FURTHEST season+episode your answer touches, as "S<N>E<N>" (e.g. "S4E10").\n'
            f'  - If your answer covers only events up to the user\'s current progress ({progress or "unknown"}), '
            f'use that progress point as the snap (e.g. "{_progress_to_snap(media)}").\n'
            f'  - If your answer discusses events BEYOND current progress (spoilers), use the furthest point mentioned.\n'
            f'  - If no episode progress is known at all, use null.\n'
            f'  - Always use exact S#E# format. Never use words like "finale" — figure out the actual episode number.'
        )
    elif mt == "manga":
        snap_instructions = (
            f'• "snap": The FURTHEST chapter your answer touches, as "Ch<N>" (e.g. "Ch94").\n'
            f'  - If your answer covers only events up to current progress ({progress or "unknown"}), '
            f'use that progress point (e.g. "{_progress_to_snap(media)}").\n'
            f'  - If your answer discusses events BEYOND current progress, use the furthest chapter mentioned.\n'
            f'  - If no chapter progress is known, use null.'
        )
    else:
        snap_instructions = '• "snap": Always null for this media type.'

    return (
        f'You are CineVault AI — a chill, enthusiastic media buddy. '
        f'Talk like you\'re texting a friend who loves movies, shows, and books.\n'
        f'Discussing: "{title}" ({mt})\n'
        f'Status: {effective_status} | Progress: {progress or "not tracked"}\n'
        f'Notes: {media.get("notes") or "none"}\n\n'
        f'User facts:\n{memory_str}\n\n'
        f'Spoiler rules:\n{spoiler_rules}\n\n'
        f'Keep replies short and fun — 2-3 sentences. No bullet points.\n\n'
        f'RESPONSE FORMAT — you MUST reply with ONLY this JSON, no text outside it:\n'
        f'{{\n'
        f'  "snap": "<tag or null>",\n'
        f'  "answer": "<your reply here>"\n'
        f'}}\n\n'
        f'SNAP FIELD RULES:\n'
        f'{snap_instructions}'
    )


def _progress_to_snap(media: dict) -> str:
    """Convert current media progress to a snap string, e.g. 'S3E1' or 'Ch47'."""
    mt = media.get("media_type", "")
    if mt == "tv":
        s = media.get("last_season")
        e = media.get("last_episode")
        if s and e:
            return f"S{s}E{e}"
        if s:
            return f"S{s}E?"
    elif mt == "manga":
        c = media.get("last_chapter")
        if c:
            return f"Ch{c}"
    return "null"


def _parse_structured_response(raw: str, media: dict) -> tuple[str, dict]:
    """Parse the model's JSON envelope. Returns (answer_text, snap_dict).
    Falls back gracefully if the model doesn't comply."""
    # Strip markdown code fences the model sometimes adds
    cleaned = re.sub(r'^```[a-z]*\n?', '', raw.strip(), flags=re.MULTILINE).rstrip('`').strip()
    try:
        obj = json.loads(cleaned)
        answer = str(obj.get("answer", "")).strip()
        snap   = _parse_snap(str(obj.get("snap", "") or ""), media)
        if answer:
            return answer, snap
    except (json.JSONDecodeError, Exception):
        pass
    # Model didn't comply — use the raw text as the answer, snap unknown
    return raw.strip(), {}


def _detect_spoiler(snap: dict, media: dict, user_message: str) -> bool:
    """Returns True if this snap represents content the user did NOT consent to see."""
    if not media or not snap:
        return False
    if media.get("status") in ("finished", "watchlist"):
        return False
    if user_message and any(p in user_message.lower() for p in SPOILER_CONSENT_PHRASES):
        return False
    return _snap_is_spoiler(snap, media)


@app.route("/api/chats/<int:chat_id>/message", methods=["POST"])
@login_required
def chat_message(chat_id):
    data    = request.get_json(silent=True) or {}
    content = data.get("content", "").strip()
    if not content:
        return jsonify({"error": "Empty message"}), 400

    gemini_key = _get_gemini_key()
    if not gemini_key:
        return jsonify({"error": "Gemini API key required"}), 401
    gemini_client = _make_gemini(gemini_key)

    chat = get_chat_by_id(chat_id)
    if not chat:
        return jsonify({"error": "Chat not found"}), 404

    media      = cached_get_media(chat["media_id"]) if chat.get("media_id") else None
    _mem = _mem_cache()
    memory_str = "\n".join(f"- {k}: {v}" for k, v in _mem.items()) if _mem else "None"

    effective_status = chat.get("context_tag") or (media["status"] if media else "finished")
    progress = _progress_str(media) if media else ""

    if media and media.get("media_type") in ("tv", "manga"):
        system = _build_structured_system(media, effective_status, progress, memory_str, content)
    elif media:
        # Non-tv/manga — plain system prompt, no snap envelope needed
        mt = media["media_type"]
        if effective_status == "finished":
            spoiler_rules = "Full discussion allowed."
        elif effective_status == "watchlist":
            spoiler_rules = "Stick to premise/vibe only — no spoilers."
        else:
            spoiler_rules = f"Scope answers to {progress or 'current point'} only."
        system = (
            f"You are CineVault AI — a chill, enthusiastic media buddy.\n"
            f"Discussing: \"{media['title']}\" ({mt})\n"
            f"Status: {effective_status} | Progress: {progress or 'not tracked'}\n"
            f"User facts:\n{memory_str}\n\n"
            f"Spoiler rules: {spoiler_rules}\n"
            f"Keep replies short and fun — 2-3 sentences. No bullet points."
        )
    else:
        system = (
            f"You are CineVault AI — a chill, enthusiastic media buddy. "
            f"Help with any movie/TV/book/manga questions.\n\n"
            f"User facts:\n{memory_str}\n\nKeep it short and fun."
        )

    history = get_chat_messages(chat_id)
    if len(history) > MAX_CHAT_HISTORY:
        trimmed = history[-MAX_CHAT_HISTORY:]
        note    = "[Earlier conversation trimmed]"
    else:
        trimmed = history
        note    = None

    gemini_contents = []
    _append_gemini_content(gemini_contents, "user", system)
    if note:
        _append_gemini_content(gemini_contents, "user", note)
    for msg in trimmed:
        role = "user" if msg["role"] == "user" else "model"
        _append_gemini_content(gemini_contents, role, msg["content"])
    _append_gemini_content(gemini_contents, "user", content)

    append_message(chat_id, "user", content)
    uses_structured = media and media.get("media_type") in ("tv", "manga")

    def generate():
        full_raw = []
        try:
            for token in _iter_gemini_response_tokens(gemini_contents, gemini_client):
                full_raw.append(token)
                if not uses_structured:
                    # Plain media — stream tokens directly to the client
                    yield f"data: {json.dumps({'token': token})}\n\n"

            raw = "".join(full_raw).strip()
            if not raw:
                yield f"data: {json.dumps({'done': True})}\n\n"
                return

            if uses_structured:
                answer, snap = _parse_structured_response(raw, media)
            else:
                answer, snap = raw, {}

            if answer:
                msg_id     = append_message(chat_id, "assistant", answer)
                is_spoiler = _detect_spoiler(snap, media, content)
                meta       = dict(snap)  # copy snap fields into meta

                if snap:
                    update_message_snap(
                        msg_id,
                        snap_season=snap.get("snap_season"),
                        snap_episode=snap.get("snap_episode"),
                        snap_chapter=snap.get("snap_chapter"),
                    )
                elif media and media.get("media_type") in ("tv", "manga"):
                    # Model returned null snap — fall back to current progress
                    mt_local = media.get("media_type")
                    if mt_local == "tv" and (media.get("last_season") or media.get("last_episode")):
                        meta["snap_season"] = media.get("last_season")
                        meta["snap_episode"] = media.get("last_episode")
                        update_message_snap(msg_id,
                            snap_season=meta["snap_season"],
                            snap_episode=meta.get("snap_episode"))
                    elif mt_local == "manga" and media.get("last_chapter"):
                        meta["snap_chapter"] = media.get("last_chapter")
                        update_message_snap(msg_id, snap_chapter=meta["snap_chapter"])

                if is_spoiler:
                    meta["is_spoiler"] = True

                if uses_structured:
                    # Stream the answer in word-sized chunks (buffered from JSON parse)
                    words = re.split(r'(\s+)', answer)
                    for chunk in words:
                        if chunk:
                            yield f"data: {json.dumps({'token': chunk})}\n\n"

                if meta and media and effective_status == "watching":
                    yield f"data: {json.dumps({'meta': meta})}\n\n"

            yield f"data: {json.dumps({'done': True})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/chats/<int:chat_id>", methods=["DELETE"])
@login_required
def del_chat(chat_id):
    delete_chat(chat_id)
    return jsonify({"status": "deleted"})


@app.route("/api/chats/<int:chat_id>/reset", methods=["POST"])
@login_required
def reset_chat(chat_id):
    if not get_chat_by_id(chat_id):
        return jsonify({"error": "Chat not found"}), 404
    clear_chat_messages(chat_id)
    return jsonify({"status": "reset"})


@app.route("/api/chats/<int:chat_id>/spoiler-threshold", methods=["POST"])
@login_required
def update_chat_spoiler(chat_id):
    data    = request.get_json(silent=True) or {}
    season  = data.get("season")
    episode = data.get("episode")
    chapter = data.get("chapter")
    update_chat_spoiler_threshold(chat_id, season=season, episode=episode, chapter=chapter)
    return jsonify({"status": "ok"})


# ═══════════════════════════════════════════════════
# AI card + quick Q&A
# ═══════════════════════════════════════════════════

def _ai_card_cache_key(media: dict) -> str:
    if media["status"] == "watchlist":
        return f"{media['id']}_watchlist"
    if media["status"] == "finished":
        return f"{media['id']}_finished"
    if media["media_type"] == "movie":
        return f"{media['id']}_watching_movie"
    if media["media_type"] == "book":
        return f"{media['id']}_watching_book"
    if media["media_type"] == "manga":
        return f"{media['id']}_watching_manga_v{media.get('last_volume', 0)}c{media.get('last_chapter', 0)}"
    return f"{media['id']}_watching_S{media.get('last_season', 0)}E{media.get('last_episode', 0)}"


def _ai_card_prompt(media: dict) -> str:
    title      = media["title"]
    media_type = media["media_type"]
    status     = media["status"]
    progress   = _progress_str(media)
    depth      = "2 sentences"

    if status == "watchlist":
        return (
            f'Give a punchy spoiler-free pitch for "{title}" ({media_type}). '
            f'Cover: genre/tone, what makes it worth watching/reading, and 2 comparable titles. '
            f'No plot details beyond the premise. Length: {depth}.'
        )
    if status == "watching":
        if media_type == "tv" and progress:
            return (
                f'The user is watching "{title}" and has reached {progress}. '
                f'Write {depth} recapping story beats BEFORE {progress} only. No spoilers beyond that.'
            )
        if media_type == "manga" and progress:
            return (
                f'The user is reading "{title}" and has reached {progress}. '
                f'Write {depth} recapping story beats before {progress} only. No spoilers beyond that.'
            )
        if media_type == "book":
            return (
                f'The user is currently reading "{title}". '
                f'Write {depth} describing the overall vibe, themes, and what makes it a great read — no specific plot spoilers.'
            )
        # movie
        return (
            f'The user is currently watching "{title}" ({media_type}). '
            f'In {depth}, describe the overall tone, standout elements, '
            f'and what makes it special — without revealing plot details.'
        )
    return (
        f'The user just finished "{title}" ({media_type}). '
        f'Recommend exactly 3 similar titles they\'d likely enjoy. '
        f'Format each as: Title (Year) — one sentence why. '
        f'No markdown, just plain text.'
    )


@app.route("/api/ai-card", methods=["POST"])
@login_required
def ai_card():
    data     = request.get_json(silent=True) or {}
    media_id = data.get("media_id")

    gemini_key = _get_gemini_key()
    if not gemini_key:
        return jsonify({"error": "Gemini API key required"}), 401
    gemini_client = _make_gemini(gemini_key)

    media = cached_get_media(media_id)
    if not media:
        return jsonify({"error": "Media not found"}), 404

    cache_key = _ai_card_cache_key(media)
    now       = time.time()
    ac        = _ai_cache()
    if cache_key in ac:
        content, expiry = ac[cache_key]
        if now < expiry:
            return jsonify({"content": content})

    prompt = _ai_card_prompt(media)
    try:
        content = _generate_ai_content(prompt, gemini_client)
    except Exception as e:
        print(f"AI card error for media {media_id}: {e}")
        return jsonify({"error": "An internal error occurred."}), 500

    ac[cache_key] = (content, now + AI_CARD_TTL)
    return jsonify({"content": content})


@app.route("/api/ai-card/stream", methods=["POST"])
@login_required
def ai_card_stream():
    data     = request.get_json(silent=True) or {}
    media_id = data.get("media_id")

    gemini_key = _get_gemini_key()
    if not gemini_key:
        return jsonify({"error": "Gemini API key required"}), 401
    gemini_client = _make_gemini(gemini_key)

    media = cached_get_media(media_id)
    if not media:
        return jsonify({"error": "Media not found"}), 404

    cache_key = _ai_card_cache_key(media)
    now       = time.time()
    ac        = _ai_cache()
    if cache_key in ac:
        content, expiry = ac[cache_key]
        if now < expiry:
            def cached_stream():
                yield f"data: {json.dumps({'token': content})}\n\n"
                yield f"data: {json.dumps({'done': True})}\n\n"
            return Response(cached_stream(), mimetype="text/event-stream",
                            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    prompt = _ai_card_prompt(media)

    def generate():
        full = []
        try:
            for token in _iter_gemini_response_tokens([{"role": "user", "parts": [{"text": prompt}]}], gemini_client):
                full.append(token)
                yield f"data: {json.dumps({'token': token})}\n\n"
            content = "".join(full).strip()
            ac[cache_key] = (content, time.time() + AI_CARD_TTL)
            yield f"data: {json.dumps({'done': True})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/ask", methods=["POST"])
@login_required
def ask_ai():
    data          = request.get_json(silent=True) or {}
    question      = data.get("question", "").strip()
    media_id      = data.get("media_id")
    chat_id       = data.get("chat_id")
    allow_spoilers = data.get("allow_spoilers", False)

    if not question:
        return jsonify({"error": "No question provided"}), 400

    gemini_key = _get_gemini_key()
    if not gemini_key:
        return jsonify({"error": "Gemini API key required"}), 401
    gemini_client = _make_gemini(gemini_key)

    media = cached_get_media(media_id)
    if not media:
        return jsonify({"error": "Media not found"}), 404

    title    = media["title"]
    status   = media["status"]
    progress = _progress_str(media)
    mt       = media["media_type"]

    uses_structured = mt in ("tv", "manga")

    if uses_structured:
        # Use structured system prompt so snap comes back with the answer
        effective_status = "watching" if status == "watching" else status
        if allow_spoilers:
            effective_status = "finished"  # treat as finished so full discussion allowed
        system = _build_structured_system(media, effective_status, progress, "", question)
        prompt = f"{system}\n\nQuestion: {question}"
    else:
        if allow_spoilers:
            spoiler_rule = "Full discussion allowed."
            progress_ctx = "spoilers allowed"
        elif status == "watchlist":
            spoiler_rule = "Premise/genre only — no plot spoilers."
            progress_ctx = "not started"
        elif status == "watching":
            ep           = f"up to {progress}" if progress else "early on"
            spoiler_rule = f"Scope to {ep} only."
            progress_ctx = ep
        else:
            spoiler_rule = "Full discussion allowed."
            progress_ctx = "completed"
        prompt = (
            f'You are a fun, casual movie/media buddy.\n'
            f'Title: "{title}" ({mt}) | Progress: {progress_ctx}\n'
            f'Rule: {spoiler_rule}\n'
            f'Keep it short — 2-3 sentences. No bullet points.\n\n'
            f'Question: {question}'
        )

    try:
        resp    = gemini_client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        raw     = resp.text.strip()
    except Exception as e:
        print(f"Ask AI error for media {media_id}: {e}")
        return jsonify({"error": "An internal error occurred."}), 500

    if uses_structured:
        answer, snap = _parse_structured_response(raw, media)
    else:
        answer, snap = raw, {}

    if not chat_id:
        try:
            name_prompt = (
                f'Give a short 2-4 word chat title for this question about "{title}": "{question}". '
                f'Just the title, nothing else. No quotes.'
            )
            name_resp = gemini_client.models.generate_content(model=GEMINI_MODEL, contents=name_prompt)
            chat_name = name_resp.text.strip()[:60] or question[:40]
        except Exception:
            chat_name = question[:40]
        chat_id = create_chat(media_id=media_id, title=chat_name, context_tag=status)

    append_message(chat_id, "user", question)
    msg_id = append_message(chat_id, "assistant", answer)

    is_spoiler = _detect_spoiler(snap, media, question)
    snap_data  = dict(snap)

    if not snap_data and uses_structured:
        # Model returned null snap — fall back to current progress
        if mt == "tv" and (media.get("last_season") or media.get("last_episode")):
            snap_data["snap_season"] = media.get("last_season")
            snap_data["snap_episode"] = media.get("last_episode")
        elif mt == "manga" and media.get("last_chapter"):
            snap_data["snap_chapter"] = media.get("last_chapter")

    if msg_id and snap_data:
        update_message_snap(
            msg_id,
            snap_season=snap_data.get("snap_season"),
            snap_episode=snap_data.get("snap_episode"),
            snap_chapter=snap_data.get("snap_chapter"),
        )

    return jsonify({"answer": answer, "chat_id": chat_id, "is_spoiler": is_spoiler, **snap_data})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=os.environ.get("FLASK_DEBUG", "0") == "1", use_reloader=True, host="0.0.0.0", port=port, threaded=True)