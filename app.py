import os
import re
import json
import time
import requests
from flask import Flask, jsonify, Response, request, render_template
from dotenv import load_dotenv
from tmdbv3api import TMDb, Movie, TV
from google import genai

from db import (
    init_db,
    get_conn,
    get_all_media, get_media_by_id,
    add_media_entry, update_media_entry, delete_media_entry,
    get_all_chats, get_chat_by_id, get_chat_messages,
    create_chat, append_message, delete_chat, clear_chat_messages,
    get_all_memory,
)

load_dotenv()

app = Flask(__name__)

tmdb          = TMDb()
tmdb.api_key  = os.getenv("TMDB_API_KEY")
movie_api     = Movie()
tv_api        = TV()

gemini        = genai.Client()
GEMINI_MODEL  = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")

init_db()

# ── In-memory caches ─────────────────────────────
poster_cache:   dict[str, tuple[bytes, str]]  = {}
search_cache:   dict[str, list]               = {}
media_cache:    dict[int, dict]               = {}
ai_card_cache:  dict[str, tuple[str, float]]  = {}  # (content, expiry)
memory_cache:   dict[str, str]                = {}

MAX_CHAT_HISTORY = 12
AI_CARD_TTL      = 600
MANGADEX_HEADERS = {"User-Agent": "CineVault/1.0"}

try:
    memory_cache = get_all_memory()
except Exception:
    memory_cache = {}


def _invalidate_media_cache(media_id: int):
    media_cache.pop(media_id, None)
    for k in [k for k in ai_card_cache if k.startswith(f"{media_id}_")]:
        del ai_card_cache[k]


def cached_get_media(media_id: int):
    if media_id not in media_cache:
        media_cache[media_id] = get_media_by_id(media_id)
    return media_cache[media_id]


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


def _iter_gemini_response_tokens(contents):
    stream_fn = getattr(gemini.models, "generate_content_stream", None)
    if stream_fn:
        try:
            for chunk in stream_fn(model=GEMINI_MODEL, contents=contents):
                token = getattr(chunk, "text", "") or ""
                if token:
                    yield token
            return
        except TypeError:
            pass
    resp = gemini.models.generate_content(model=GEMINI_MODEL, contents=contents)
    text = (getattr(resp, "text", "") or "").strip()
    if text:
        yield text


def _generate_ai_content(prompt: str) -> str:
    resp = gemini.models.generate_content(model=GEMINI_MODEL, contents=prompt)
    return resp.text.strip()


@app.route("/")
def index():
    return render_template("index.html")


# ═══════════════════════════════════════════════════
# Search (TMDB, Books, Manga)
# ═══════════════════════════════════════════════════

@app.route("/api/search")
def search():
    query      = request.args.get("q", "").strip()
    media_type = request.args.get("type", "movie")
    if not query:
        return jsonify([])

    cache_key = f"{media_type}:{query.lower()}"
    if cache_key in search_cache:
        return jsonify(search_cache[cache_key])

    try:
        if media_type == "movie":
            results = movie_api.search(query)
            items = [
                {
                    "tmdb_id":     r.id,
                    "external_id": str(r.id),
                    "title":       r.title,
                    "year":        (getattr(r, "release_date", "") or "")[:4],
                    "media_type":  "movie",
                    "poster_path": getattr(r, "poster_path", None),
                    "overview":    getattr(r, "overview", None),
                    "popularity":  getattr(r, "popularity", 0) or 0,
                }
                for r in results if hasattr(r, "id")
            ]
        else:
            results = tv_api.search(query)
            items = [
                {
                    "tmdb_id":     r.id,
                    "external_id": str(r.id),
                    "title":       r.name,
                    "year":        (getattr(r, "first_air_date", "") or "")[:4],
                    "media_type":  "tv",
                    "poster_path": getattr(r, "poster_path", None),
                    "overview":    getattr(r, "overview", None),
                    "popularity":  getattr(r, "popularity", 0) or 0,
                }
                for r in results if hasattr(r, "id")
            ]
        items.sort(key=lambda x: x.get("popularity", 0), reverse=True)
    except Exception as e:
        print(f"Search error: {e}")
        items = []

    items = items[:10]
    search_cache[cache_key] = items
    if len(search_cache) > 100:
        del search_cache[next(iter(search_cache))]
    return jsonify(items)


@app.route("/api/search/books")
def search_books():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify([])
    
    # Trim the query if it's too short to avoid 422 errors
    if len(query) < 2:
        return jsonify([])

    cache_key = f"book:{query.lower()}"
    if cache_key in search_cache:
        return jsonify(search_cache[cache_key])

    try:
        # Removing the 'fields' parameter to avoid 422 errors
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
            
            # Build author string safely
            author_names = doc.get("author_name", [])
            if isinstance(author_names, list):
                author = ", ".join(author_names)
            else:
                author = str(author_names)
            
            year = doc.get("first_publish_year")
            if not year:
                # Try to get year from publish_date if first_publish_year is missing
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
        
        search_cache[cache_key] = items
        if len(search_cache) > 100:
            del search_cache[next(iter(search_cache))]
        return jsonify(items)
        
    except requests.exceptions.HTTPError as e:
        print(f"Open Library HTTP error: {e}")
        if e.response.status_code == 422:
            print("   -> This is likely due to a malformed request. Try a longer search term.")
        return jsonify([])
    except Exception as e:
        print(f"Open Library search error: {e}")
        return jsonify([])


@app.route("/api/book/isbn/<isbn>")
def get_book_by_isbn(isbn):
    """Get book details by ISBN"""
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
        return jsonify({"error": str(e)}), 500


@app.route("/api/search/manga")
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

            # Title
            title_data = attrs.get("title", {})
            title = title_data.get("en") or next(iter(title_data.values()), "Unknown")

            # Cover file name
            cover_file = None
            for rel in manga.get("relationships", []):
                if rel["type"] == "cover_art":
                    cover_file = rel.get("attributes", {}).get("fileName", "")
                    break

            cover_url = None
            if cover_file:
                cover_url = _mangadex_cover_url(manga_id, cover_file)

            # Author
            author = ""
            for rel in manga.get("relationships", []):
                if rel["type"] == "author":
                    author = rel.get("attributes", {}).get("name", "")
                    break

            # Description
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

        search_cache[cache_key] = items
        return jsonify(items)

    except Exception as e:
        print(f"Manga search error: {e}")
        return jsonify([])
    

# ═══════════════════════════════════════════════════
# Poster proxy
# ═══════════════════════════════════════════════════

@app.route("/api/poster/<string:media_type>/<path:item_id>")
def get_poster(media_type, item_id):
    cache_key = f"{media_type}_{item_id}"

    if cache_key in poster_cache:
        img_bytes, content_type = poster_cache[cache_key]
        return Response(
            img_bytes,
            mimetype=content_type,
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    # Handle Books with Open Library API
    if media_type == "book":
        with get_conn() as conn:
            row = conn.execute(
                "SELECT id, cover_url FROM media WHERE external_id = ? AND media_type = ?",
                (item_id, media_type),
            ).fetchone()
        cover_url = row["cover_url"] if row and row["cover_url"] else ""
        
        if not cover_url:
            # Try to fetch from Open Library by work ID
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
        
        if not cover_url:
            return "", 404
            
        try:
            img_resp = requests.get(cover_url, timeout=10)
            img_resp.raise_for_status()
            content_type = img_resp.headers.get("Content-Type", "image/jpeg")
            img_bytes = img_resp.content
            poster_cache[cache_key] = (img_bytes, content_type)
            return Response(
                img_bytes, mimetype=content_type,
                headers={"Cache-Control": "public, max-age=86400"},
            )
        except Exception as e:
            print(f"Book poster download error for {cover_url}: {e}")
            return "", 404

    # Handle Manga with MangaDex API
    if media_type == "manga":
        with get_conn() as conn:
            row = conn.execute(
                "SELECT id, cover_url FROM media WHERE external_id = ? AND media_type = ?",
                (item_id, media_type),
            ).fetchone()
        cover_url = row["cover_url"] if row and row["cover_url"] else ""

        # Fetch a missing cover URL from MangaDex.
        if not cover_url:
            try:
                cover_url = _fetch_mangadex_cover_url(item_id)
                if cover_url and row:
                    update_media_entry(row["id"], cover_url=cover_url)
            except Exception:
                cover_url = ""

        if not cover_url:
            return "", 404

        try:
            img_resp = requests.get(cover_url, timeout=15, headers=MANGADEX_HEADERS)
            img_resp.raise_for_status()
            content_type = img_resp.headers.get("Content-Type", "image/jpeg")
            img_bytes = img_resp.content
            poster_cache[cache_key] = (img_bytes, content_type)
            return Response(
                img_bytes, mimetype=content_type,
                headers={"Cache-Control": "public, max-age=86400"},
            )
        except Exception:
            return "", 404

    # Handle Movies and TV Shows with TMDB
    endpoint = "movie" if media_type == "movie" else "tv"
    meta_url = f"https://api.themoviedb.org/3/{endpoint}/{item_id}?api_key={tmdb.api_key}"
    try:
        meta = requests.get(meta_url, timeout=5).json()
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
# Library CRUD
# ═══════════════════════════════════════════════════

@app.route("/api/list")
def list_media():
    return jsonify(get_all_media())


@app.route("/api/add", methods=["POST"])
def add_media():
    data = request.json or {}
    
    if "id" in data:
        allowed = {
            "status", "rating", "last_timestamp",
            "last_season", "last_episode",
            "last_volume", "last_chapter",
            "current_page", "total_pages",
            "notes", "cover_url", "author",
        }
        fields = {k: data[k] for k in allowed if k in data}
        update_media_entry(data["id"], **fields)
        _invalidate_media_cache(data["id"])
    else:
        cover_url = data.get("cover_url")
        media_type = data.get("media_type")
        external_id = data.get("external_id")
        
        if media_type == "manga" and not cover_url and external_id:
            try:
                cover_url = _fetch_mangadex_cover_url(external_id)
            except Exception:
                cover_url = None
        
        add_media_entry(
            title       = data["title"],
            media_type  = media_type,
            status      = data.get("status", "watchlist"),
            tmdb_id     = data.get("tmdb_id"),
            external_id = external_id,
            cover_url   = cover_url,
            author      = data.get("author"),
            total_pages = data.get("total_pages"),
        )
    
    return jsonify({"status": "ok"})


@app.route("/api/delete/<int:media_id>", methods=["DELETE"])
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
def media_with_chats(media_id):
    """Return media item and its chats for undo support."""
    media = cached_get_media(media_id)
    if not media:
        return jsonify({"error": "Media not found"}), 404
    with get_conn() as conn:
        chats_rows = conn.execute(
            "SELECT * FROM chats WHERE media_id = ?", (media_id,)
        ).fetchall()
    chats = []
    for chat_row in chats_rows:
        msgs = get_chat_messages(chat_row["id"])
        chats.append({**dict(chat_row), "messages": msgs})
    return jsonify({"media": media, "chats": chats})


@app.route("/api/restore-media-chats", methods=["POST"])
def restore_media_chats():
    data       = request.json or {}
    media_data = data.get("media")
    chats_data = data.get("chats", [])
    if not media_data:
        return jsonify({"error": "Missing media data"}), 400

    new_media_id = add_media_entry(
        title       = media_data["title"],
        media_type  = media_data["media_type"],
        status      = media_data["status"],
        tmdb_id     = media_data.get("tmdb_id"),
        external_id = media_data.get("external_id"),
        cover_url   = media_data.get("cover_url"),
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
def vibe_search_types():
    data  = request.json or {}
    query = data.get("query", "").strip()
    if not query:
        return jsonify({"types": ["movie", "tv"]}), 400

    type_prompt = (
        f'The user is searching for: "{query}"\n'
        f'Available media types: movie, tv, book, manga\n'
        f'Decide which media types are most relevant.\n'
        f'Rules:\n'
        f'- If the query looks like a specific title (e.g. "breaking bad", "one piece", "dune"), '
        f'include the type(s) that title belongs to.\n'
        f'- If the query is a mood/vibe (e.g. "cozy mysteries", "dark thriller"), pick the types '
        f'where those vibes are most common.\n'
        f'- Return ONLY a JSON array from: ["movie","tv","book","manga"]. No other text.\n'
        f'Examples:\n'
        f'  "breaking bad" -> ["tv"]\n'
        f'  "one piece" -> ["manga","tv"]\n'
        f'  "dune" -> ["movie","book"]\n'
        f'  "short and funny" -> ["movie","manga"]\n'
        f'  "dark academia" -> ["book","movie","tv"]\n'
        f'  "studio ghibli" -> ["movie"]\n'
    )
    suggested_types = ["movie", "tv"]
    try:
        resp   = gemini.models.generate_content(model=GEMINI_MODEL, contents=type_prompt)
        raw    = re.sub(r"^```[a-z]*\n?", "", resp.text.strip()).rstrip("` \n")
        parsed = json.loads(raw)
        valid  = [t for t in parsed if t in ("movie", "tv", "book", "manga")]
        if valid:
            suggested_types = valid
    except Exception as e:
        print(f"Vibe type detection error: {e}")
    return jsonify({"types": suggested_types})


@app.route("/api/vibe-search", methods=["POST"])
def vibe_search():
    data  = request.json or {}
    query = data.get("query", "").strip()
    types = data.get("types", ["movie", "tv"])
    if not query:
        return jsonify({"error": "No query provided"}), 400

    type_list = ", ".join(types)
    prompt = (
        f'You are a media recommendation engine.\n'
        f'User input: "{query}"\n'
        f'Media types to search: {type_list}.\n'
        f'\n'
        f'IMPORTANT: If the input looks like a specific title (e.g. "breaking bad", "one piece"),'
        f' include that title as the first result and add similar titles after it.\n'
        f'If the input is a mood or vibe description, recommend media that fits it.\n'
        f'\n'
        f'Return ONLY a JSON array of up to 8 objects with EXACTLY these keys:\n'
        f'  "title"      : exact, well-known title string\n'
        f'  "year"       : release year as a string (e.g. "2019") or ""\n'
        f'  "media_type" : one of {type_list}\n'
        f'  "reason"     : one sentence why it fits\n'
        f'No markdown, no extra keys, no explanation.'
    )

    try:
        resp = gemini.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        raw  = re.sub(r"^```[a-z]*\n?", "", resp.text.strip()).rstrip("` \n")
        recs = json.loads(raw)
    except Exception as e:
        return jsonify({"error": f"AI error: {e}"}), 500

    enriched = []
    for rec in recs[:8]:
        mt          = rec.get("media_type", "movie")
        title       = rec.get("title", "")
        year        = rec.get("year", "")
        reason      = rec.get("reason", "")
        poster_path = None
        overview    = ""
        tmdb_id     = None
        external_id = None
        cover_url   = ""
        author      = ""
        total_pages = None

        try:
            if mt == "movie":
                results = movie_api.search(title)
                for r in results:
                    if hasattr(r, "id"):
                        ry = (r.release_date or "")[:4]
                        if year and ry and ry != year and tmdb_id:
                            continue
                        tmdb_id     = r.id
                        external_id = str(r.id)
                        poster_path = getattr(r, "poster_path", None)
                        overview    = getattr(r, "overview", "") or ""
                        if ry == year:
                            break
            elif mt == "tv":
                results = tv_api.search(title)
                for r in results:
                    if hasattr(r, "id"):
                        ry = (r.first_air_date or "")[:4]
                        if year and ry and ry != year and tmdb_id:
                            continue
                        tmdb_id     = r.id
                        external_id = str(r.id)
                        poster_path = getattr(r, "poster_path", None)
                        overview    = getattr(r, "overview", "") or ""
                        if ry == year:
                            break
            elif mt == "book":
                try:
                    resp = requests.get(
                        "https://openlibrary.org/search.json",
                        params={"q": title, "limit": 1},
                        timeout=5,
                    )
                    data = resp.json()
                    for doc in data.get("docs", []):
                        cover_id = doc.get("cover_i")
                        if cover_id:
                            cover_url = f"https://covers.openlibrary.org/b/id/{cover_id}-L.jpg"
                        external_id = doc.get("key", "").replace("/works/", "")
                        author = ", ".join(doc.get("author_name", []))
                        total_pages = doc.get("number_of_pages_median")
                        description = doc.get("first_sentence", [""])[0] if doc.get("first_sentence") else ""
                        overview = description[:300] if description else ""
                        break
                except Exception as enrich_err:
                    print(f"Book enrich error for {title}: {enrich_err}")
            elif mt == "manga":
                try:
                    headers = {"User-Agent": "CineVault/1.0"}
                    resp = requests.get(
                        "https://api.mangadex.org/manga",
                        headers=headers,
                        params={
                            "title": title,
                            "limit": 1,
                            "includes[]": ["cover_art", "author"],
                            "availableTranslatedLanguage[]": ["en"],
                        },
                        timeout=5,
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        if data.get("data"):
                            m = data["data"][0]
                            attrs = m.get("attributes", {})
                            
                            # Get cover URL
                            for rel in m.get("relationships", []):
                                if rel["type"] == "cover_art":
                                    fn = rel.get("attributes", {}).get("fileName", "")
                                    if fn:
                                        cover_url = _mangadex_cover_url(m["id"], fn)
                                    break
                            
                            external_id = m["id"]
                            
                            # Get author
                            for rel in m.get("relationships", []):
                                if rel["type"] == "author":
                                    author = rel.get("attributes", {}).get("name", "")
                                    break
                            
                            overview = attrs.get("description", {}).get("en", "")[:300]
                except Exception as enrich_err:
                    print(f"Manga enrich error for {title}: {enrich_err}")
        except Exception as enrich_err:
            print(f"Enrich error for {title}: {enrich_err}")

        enriched.append({
            "title":       title,
            "year":        year,
            "media_type":  mt,
            "reason":      reason,
            "overview":    overview,
            "tmdb_id":     tmdb_id,
            "external_id": external_id,
            "poster_path": poster_path,
            "cover_url":   cover_url,
            "author":      author,
            "total_pages": total_pages,
        })

    return jsonify(enriched)


# ═══════════════════════════════════════════════════
# Chats
# ═══════════════════════════════════════════════════

@app.route("/api/chats")
def list_chats():
    return jsonify(get_all_chats())


@app.route("/api/chats/<int:chat_id>")
def get_chat(chat_id):
    chat = get_chat_by_id(chat_id)
    if not chat:
        return jsonify({"error": "Not found"}), 404
    msgs = get_chat_messages(chat_id)
    return jsonify({**chat, "messages": msgs})


@app.route("/api/chats/new", methods=["POST"])
def new_chat():
    data        = request.json or {}
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


@app.route("/api/chats/<int:chat_id>/message", methods=["POST"])
def chat_message(chat_id):
    data    = request.json or {}
    content = data.get("content", "").strip()
    if not content:
        return jsonify({"error": "Empty message"}), 400

    chat = get_chat_by_id(chat_id)
    if not chat:
        return jsonify({"error": "Chat not found"}), 404

    media      = cached_get_media(chat["media_id"]) if chat.get("media_id") else None
    memory_str = "\n".join(f"- {k}: {v}" for k, v in memory_cache.items()) if memory_cache else "None"

    effective_status = chat.get("context_tag") or (media["status"] if media else "finished")

    if media:
        progress = _progress_str(media)
        if effective_status == "finished":
            spoiler_rules = "The user has finished this title. Full discussion of all plot, characters, endings, and themes is allowed."
        elif effective_status == "watchlist":
            spoiler_rules = "The user has not started this yet. Discuss only premise, genre, and tone — no plot details or spoilers."
        else:
            spoiler_rules = f"The user has only reached {progress or 'their current point'}. Do not reveal anything beyond that."

        system = (
            f"You are CineVault AI, an enthusiastic media companion.\n"
            f"Discussing: \"{media['title']}\" ({media['media_type']})\n"
            f"Status: {effective_status}\n"
            f"Progress: {progress or 'not tracked'}\n"
            f"Notes: {media.get('notes') or 'none'}\n\n"
            f"User facts:\n{memory_str}\n\n"
            f"Spoiler rules:\n{spoiler_rules}\n"
            f"Be conversational, insightful, fun. 2-4 sentences per reply."
        )
    else:
        system = (
            f"You are CineVault AI, an enthusiastic media companion. "
            f"Help with any movie/TV/book/manga questions.\n\n"
            f"User facts:\n{memory_str}\n\n"
            f"Be conversational, knowledgeable, and fun."
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

    def generate():
        full_answer = []
        try:
            for token in _iter_gemini_response_tokens(gemini_contents):
                full_answer.append(token)
                yield f"data: {json.dumps({'token': token})}\n\n"
            answer = "".join(full_answer).strip()
            if answer:
                append_message(chat_id, "assistant", answer)
            yield f"data: {json.dumps({'done': True})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/chats/<int:chat_id>", methods=["DELETE"])
def del_chat(chat_id):
    delete_chat(chat_id)
    return jsonify({"status": "deleted"})


@app.route("/api/chats/<int:chat_id>/reset", methods=["POST"])
def reset_chat(chat_id):
    if not get_chat_by_id(chat_id):
        return jsonify({"error": "Chat not found"}), 404
    clear_chat_messages(chat_id)
    return jsonify({"status": "reset"})


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
        return f"{media['id']}_watching_book_p{media.get('current_page', 0)}"
    if media["media_type"] == "manga":
        return f"{media['id']}_watching_manga_v{media.get('last_volume', 0)}c{media.get('last_chapter', 0)}"
    return f"{media['id']}_watching_S{media.get('last_season', 0)}E{media.get('last_episode', 0)}"


@app.route("/api/ai-card", methods=["POST"])
def ai_card():
    data     = request.json or {}
    media_id = data.get("media_id")

    media = cached_get_media(media_id)
    if not media:
        return jsonify({"error": "Media not found"}), 404

    cache_key = _ai_card_cache_key(media)
    now       = time.time()
    if cache_key in ai_card_cache:
        content, expiry = ai_card_cache[cache_key]
        if now < expiry:
            return jsonify({"content": content})

    title      = media["title"]
    media_type = media["media_type"]
    status     = media["status"]
    progress   = _progress_str(media)
    depth      = "2 sentences"

    if status == "watchlist":
        prompt = (
            f'Give a punchy spoiler-free pitch for "{title}" ({media_type}). '
            f'Cover: genre/tone, what makes it worth watching/reading, and 2 comparable titles. '
            f'No plot details beyond the premise. Length: {depth}.'
        )
    elif status == "watching":
        if media_type == "tv" and progress:
            prompt = (
                f'The user is watching "{title}" and has reached {progress}. '
                f'Write {depth} recapping story beats BEFORE {progress} only. No spoilers beyond that.'
            )
        elif media_type == "manga" and progress:
            prompt = (
                f'The user is reading "{title}" and has reached {progress}. '
                f'Write {depth} recapping story beats before {progress} only. No spoilers beyond that.'
            )
        elif media_type == "book" and progress:
            prompt = (
                f'The user is reading "{title}" and is at {progress}. '
                f'Write {depth} summarising what has happened so far without spoiling anything beyond that point.'
            )
        else:
            prompt = (
                f'The user is currently experiencing "{title}" ({media_type}). '
                f'In {depth}, describe the overall tone, standout elements, '
                f'and what makes it special — without revealing plot details.'
            )
    else:
        prompt = (
            f'The user just finished "{title}" ({media_type}). '
            f'Recommend exactly 3 similar titles they\'d likely enjoy. '
            f'Format each as: Title (Year) — one sentence why. '
            f'No markdown, just plain text.'
        )

    try:
        content = _generate_ai_content(prompt)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    ai_card_cache[cache_key] = (content, now + AI_CARD_TTL)
    return jsonify({"content": content})


@app.route("/api/ask", methods=["POST"])
def ask_ai():
    data     = request.json or {}
    question = data.get("question", "").strip()
    media_id = data.get("media_id")
    chat_id  = data.get("chat_id")

    if not question:
        return jsonify({"error": "No question provided"}), 400

    media = cached_get_media(media_id)
    if not media:
        return jsonify({"error": "Media not found"}), 404

    title    = media["title"]
    status   = media["status"]
    progress = _progress_str(media)

    if status == "watchlist":
        spoiler_rule = "The user has NOT watched/read this yet. Give only premise/genre info — no plot, no twists, no deaths."
        progress_ctx = "not started"
    elif status == "watching":
        ep           = f"up to {progress}" if progress else "early on"
        spoiler_rule = f"The user has only experienced {ep}. Refuse to reveal ANYTHING after that point."
        progress_ctx = ep
    else:
        spoiler_rule = "The user has finished this. Full discussion of plot, characters, endings, themes, and twists is allowed."
        progress_ctx = "completed"

    prompt = (
        f'You are an enthusiastic but careful media assistant.\n'
        f'Title: "{title}" ({media["media_type"]}) | Progress: {progress_ctx}\n'
        f'Rule: {spoiler_rule}\n'
        f'Answer in 2-4 sentences, conversationally and helpfully.\n\n'
        f'Question: {question}'
    )

    try:
        resp   = gemini.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        answer = resp.text.strip()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    if not chat_id:
        chat_id = create_chat(
            media_id    = media_id,
            title       = "Q&A",
            context_tag = status,
        )
    append_message(chat_id, "user", question)
    append_message(chat_id, "assistant", answer)

    return jsonify({"answer": answer, "chat_id": chat_id})


if __name__ == "__main__":
    app.run(debug=True, use_reloader=True)
