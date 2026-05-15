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

tmdb       = TMDb()
tmdb.api_key = os.getenv("TMDB_API_KEY")
movie_api  = Movie()
tv_api     = TV()

gemini       = genai.Client()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")

init_db()

# ── In‑memory caches ────────────────────────────────
poster_cache: dict[str, tuple[bytes, str]] = {}
search_cache: dict[str, list]              = {}
media_cache:  dict[int, dict]             = {}
memory_cache: dict[str, str]              = {}
ai_card_cache: dict[str, tuple[str, float]] = {}  # (content, expiry)

MAX_CHAT_HISTORY = 12
AI_CARD_TTL      = 600

try:
    memory_cache = get_all_memory()
except Exception:
    memory_cache = {}


def _invalidate_media_cache(media_id: int):
    media_cache.pop(media_id, None)
    to_delete = [k for k in ai_card_cache if k.startswith(f"{media_id}_")]
    for k in to_delete:
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
                    "tmdb_id":      r.id,
                    "external_id":  str(r.id),
                    "title":        r.title,
                    "year":         (r.release_date or "")[:4],
                    "media_type":   "movie",
                    "poster_path":  getattr(r, "poster_path", None),
                    "overview":     getattr(r, "overview", None),
                    "popularity":   getattr(r, "popularity", 0) or 0,
                }
                for r in results if hasattr(r, "id")
            ]
            items.sort(key=lambda x: x.get("popularity", 0), reverse=True)
        else:
            results = tv_api.search(query)
            items = [
                {
                    "tmdb_id":      r.id,
                    "external_id":  str(r.id),
                    "title":        r.name,
                    "year":         (r.first_air_date or "")[:4],
                    "media_type":   "tv",
                    "poster_path":  getattr(r, "poster_path", None),
                    "overview":     getattr(r, "overview", None),
                    "popularity":   getattr(r, "popularity", 0) or 0,
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

    cache_key = f"book:{query.lower()}"
    if cache_key in search_cache:
        return jsonify(search_cache[cache_key])

    try:
        gbooks_key = os.getenv("GOOGLE_BOOKS_API_KEY", "")
        params = {"q": query, "maxResults": 10, "printType": "books", "orderBy": "relevance"}
        if gbooks_key:
            params["key"] = gbooks_key
        resp = requests.get(
            "https://www.googleapis.com/books/v1/volumes",
            params=params, timeout=8
        )
        resp.raise_for_status()
        data  = resp.json()
        items = []
        for vol in data.get("items", []):
            info = vol.get("volumeInfo", {})
            img  = info.get("imageLinks", {})
            cover = (
                img.get("thumbnail") or img.get("smallThumbnail") or ""
            ).replace("http://", "https://")
            items.append({
                "external_id": vol["id"],
                "title":       info.get("title", "Unknown"),
                "author":      ", ".join(info.get("authors", [])),
                "year":        (info.get("publishedDate") or "")[:4],
                "media_type":  "book",
                "cover_url":   cover,
                "total_pages": info.get("pageCount"),
                "overview":    info.get("description", ""),
                "popularity":  0,
            })
    except Exception as e:
        print(f"Books search error: {e}")
        items = []

    search_cache[cache_key] = items
    if len(search_cache) > 100:
        del search_cache[next(iter(search_cache))]
    return jsonify(items)


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
            params={
                "title":                query,
                "limit":                10,
                "includes[]":           ["cover_art", "author"],
                "availableTranslatedLanguage[]": ["en"],
                "order[followedCount]": "desc",
            },
            timeout=8,
        )
        resp.raise_for_status()
        data  = resp.json()
        items = []
        for m in data.get("data", []):
            attrs = m.get("attributes", {})
            title = (
                attrs.get("title", {}).get("en")
                or next(iter(attrs.get("title", {}).values()), "Unknown")
            )
            cover_url = ""
            for rel in m.get("relationships", []):
                if rel["type"] == "cover_art":
                    fn = rel.get("attributes", {}).get("fileName", "")
                    if fn:
                        cover_url = f"https://uploads.mangadex.org/covers/{m['id']}/{fn}.256.jpg"
                    break
            author = ""
            for rel in m.get("relationships", []):
                if rel["type"] == "author":
                    author = rel.get("attributes", {}).get("name", "")
                    break
            desc = attrs.get("description", {})
            overview = desc.get("en") or next(iter(desc.values()), "")

            items.append({
                "external_id": m["id"],
                "title":       title,
                "author":      author,
                "year":        str(attrs.get("year") or ""),
                "media_type":  "manga",
                "cover_url":   cover_url,
                "overview":    overview[:300] if overview else "",
                "last_volume": attrs.get("lastVolume"),
                "last_chapter": attrs.get("lastChapter"),
                "popularity":  0,
            })
    except Exception as e:
        print(f"Manga search error: {e}")
        items = []

    search_cache[cache_key] = items
    if len(search_cache) > 100:
        del search_cache[next(iter(search_cache))]
    return jsonify(items)


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

    if media_type in ("book", "manga"):
        row = None
        with get_conn() as conn:
            row = conn.execute(
                "SELECT cover_url FROM media WHERE external_id = ? AND media_type = ?",
                (item_id, media_type),
            ).fetchone()
        cover_url = row["cover_url"] if row and row["cover_url"] else ""
        if not cover_url:
            return "", 404
        try:
            img_resp = requests.get(cover_url, timeout=10)
            img_resp.raise_for_status()
            content_type = img_resp.headers.get("Content-Type", "image/jpeg")
            img_bytes    = img_resp.content
            poster_cache[cache_key] = (img_bytes, content_type)
            return Response(
                img_bytes, mimetype=content_type,
                headers={"Cache-Control": "public, max-age=86400"},
            )
        except Exception:
            return "", 404

    endpoint = "movie" if media_type == "movie" else "tv"
    meta_url = (
        f"https://api.themoviedb.org/3/{endpoint}/{item_id}"
        f"?api_key={tmdb.api_key}"
    )
    try:
        meta = requests.get(meta_url, timeout=5).json()
        path = meta.get("poster_path")
        if not path:
            return "", 404

        img_resp = requests.get(
            f"https://image.tmdb.org/t/p/w500{path}", timeout=10
        )
        img_resp.raise_for_status()

        content_type = img_resp.headers.get("Content-Type", "image/jpeg")
        img_bytes    = img_resp.content
        poster_cache[cache_key] = (img_bytes, content_type)

        return Response(
            img_bytes, mimetype=content_type,
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )
    except Exception:
        return "", 404


# ═══════════════════════════════════════════════════
# Library CRUD + undo support
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
        add_media_entry(
            title       = data["title"],
            media_type  = data["media_type"],
            status      = data.get("status", "watchlist"),
            tmdb_id     = data.get("tmdb_id"),
            external_id = data.get("external_id"),
            cover_url   = data.get("cover_url"),
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
    """Return media item and its associated chats (full data) for undo."""
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
    data = request.json or {}
    media_data = data.get("media")
    chats_data = data.get("chats", [])
    if not media_data:
        return jsonify({"error": "Missing media data"}), 400

    new_media_id = add_media_entry(
        title=media_data["title"],
        media_type=media_data["media_type"],
        status=media_data["status"],
        tmdb_id=media_data.get("tmdb_id"),
        external_id=media_data.get("external_id"),
        cover_url=media_data.get("cover_url"),
        author=media_data.get("author"),
        total_pages=media_data.get("total_pages"),
    )
    update_fields = {k: media_data[k] for k in ["rating", "notes", "last_timestamp", "last_season",
                                                 "last_episode", "last_volume", "last_chapter",
                                                 "current_page", "total_pages"] if k in media_data}
    if update_fields:
        update_media_entry(new_media_id, **update_fields)

    for chat in chats_data:
        chat_id = create_chat(
            media_id=new_media_id,
            title=chat["title"],
            context_tag=chat.get("context_tag")
        )
        for msg in chat.get("messages", []):
            append_message(chat_id, msg["role"], msg["content"])

    return jsonify({"new_media_id": new_media_id})


# ═══════════════════════════════════════════════════
# Vibe Search (two-step)
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
        resp = gemini.models.generate_content(model=GEMINI_MODEL, contents=type_prompt)
        raw  = resp.text.strip()
        raw  = re.sub(r"^```[a-z]*\n?", "", raw).rstrip("` \n")
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
        raw  = resp.text.strip()
        raw  = re.sub(r"^```[a-z]*\n?", "", raw).rstrip("` \n")
        recs = json.loads(raw)
    except Exception as e:
        return jsonify({"error": f"AI error: {e}"}), 500

    enriched = []
    for rec in recs[:8]:
        mt    = rec.get("media_type", "movie")
        title = rec.get("title", "")
        year  = rec.get("year", "")
        reason = rec.get("reason", "")

        poster_path  = None
        overview     = ""
        tmdb_id      = None
        external_id  = None
        cover_url    = ""
        author       = ""
        total_pages  = None

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
                gbooks_key = os.getenv("GOOGLE_BOOKS_API_KEY", "")
                params = {"q": title, "maxResults": 3}
                if gbooks_key:
                    params["key"] = gbooks_key
                br = requests.get(
                    "https://www.googleapis.com/books/v1/volumes",
                    params=params, timeout=5,
                ).json()
                for vol in br.get("items", []):
                    info = vol.get("volumeInfo", {})
                    img  = info.get("imageLinks", {})
                    external_id = vol["id"]
                    cover_url   = (img.get("thumbnail") or "").replace("http://", "https://")
                    author      = ", ".join(info.get("authors", []))
                    total_pages = info.get("pageCount")
                    overview    = info.get("description", "")[:300]
                    break
            elif mt == "manga":
                mr = requests.get(
                    "https://api.mangadex.org/manga",
                    params={"title": title, "limit": 3,
                            "includes[]": ["cover_art", "author"],
                            "availableTranslatedLanguage[]": ["en"]},
                    timeout=5,
                ).json()
                for m in mr.get("data", []):
                    attrs = m.get("attributes", {})
                    external_id = m["id"]
                    for rel in m.get("relationships", []):
                        if rel["type"] == "cover_art":
                            fn = rel.get("attributes", {}).get("fileName", "")
                            if fn:
                                cover_url = f"https://uploads.mangadex.org/covers/{m['id']}/{fn}.256.jpg"
                            break
                    for rel in m.get("relationships", []):
                        if rel["type"] == "author":
                            author = rel.get("attributes", {}).get("name", "")
                            break
                    desc = attrs.get("description", {})
                    overview = (desc.get("en") or next(iter(desc.values()), ""))[:300]
                    break
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
# Chats (streaming, caching, trimmed history)
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
    memory     = memory_cache
    memory_str = "\n".join(f"- {k}: {v}" for k, v in memory.items()) if memory else "None"

    effective_status = chat.get("context_tag") or (media["status"] if media else "finished")

    if media:
        progress = _progress_str(media)
        if effective_status == "finished":
            spoiler_rules = "The user has finished this title. Full discussion of all plot, characters, endings, and themes is allowed."
        elif effective_status == "watchlist":
            spoiler_rules = "watchlist → no spoilers, premise only"
        else:
            spoiler_rules = f"watching → only discuss up to {progress or 'current point'}"

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
        note = "[Earlier conversation trimmed]"
    else:
        trimmed = history
        note = None

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
            return
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
# AI helpers (card + ask)
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


def _extended_ai_card_prompt(media: dict) -> str:
    title = media["title"]
    media_type = media["media_type"]
    status = media["status"]
    progress = _progress_str(media)

    if status == "watchlist":
        spoiler_rule = "Keep this spoiler-free and discuss only premise, tone, style, themes, and comparable titles."
        focus = "why it may be worth starting, what kind of mood it suits, and what to notice going in"
    elif status == "watching":
        spoiler_rule = f"Discuss only up to {progress or 'the user current progress'} and do not reveal anything later."
        focus = "what has been established so far, important character or theme threads, and what makes the current stretch interesting"
    else:
        spoiler_rule = "The user has finished it, so full-work reflection is allowed including endings, twists, and resolution."
        focus = "what makes it memorable, how its themes or craft land, and several thoughtful follow-up recommendations"

    return (
        f'Write an extended insight for a media chat about "{title}" ({media_type}). '
        f'{spoiler_rule} Focus on {focus}. '
        f'Use 4-6 substantial sentences. Be conversational, specific, and useful. '
        f'Do not mention that this is an extended insight; the UI will label it.'
    )


def _generate_ai_content(prompt: str) -> str:
    resp = gemini.models.generate_content(model=GEMINI_MODEL, contents=prompt)
    return resp.text.strip()


def _create_deep_chat(media_id: int, status: str, content: str) -> int:
    chat_id = create_chat(
        media_id    = media_id,
        title       = "Deep Dive",
        context_tag = status,
    )
    append_message(chat_id, "assistant", content)
    return chat_id


@app.route("/api/ai-card", methods=["POST"])
def ai_card():
    data     = request.json or {}
    media_id = data.get("media_id")
    deep     = data.get("deep", False)

    media = cached_get_media(media_id)
    if not media:
        return jsonify({"error": "Media not found"}), 404

    cache_key = _ai_card_cache_key(media)
    now = time.time()
    if cache_key in ai_card_cache:
        content, expiry = ai_card_cache[cache_key]
        if now < expiry:
            result = {"content": content}
            if deep:
                try:
                    extended = _generate_ai_content(_extended_ai_card_prompt(media))
                except Exception as e:
                    return jsonify({"error": str(e)}), 500
                result["chat_id"] = _create_deep_chat(media_id, media["status"], extended)
            return jsonify(result)

    title      = media["title"]
    media_type = media["media_type"]
    status     = media["status"]
    progress   = _progress_str(media)
    depth      = "2 sentences"

    if status == "watchlist":
        prompt = (
            f'Give a punchy spoiler‑free pitch for "{title}" ({media_type}). '
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
        elif media_type == "book":
            if progress:
                prompt = (
                    f'The user is reading "{title}" and is at {progress}. '
                    f'Write {depth} summarising what has happened so far without spoiling anything beyond that point.'
                )
            else:
                prompt = (
                    f'The user is currently reading "{title}". '
                    f'In {depth}, describe the overall tone, standout elements, '
                    f'and what makes it special — without revealing plot details.'
                )
        else:
            prompt = (
                f'The user is currently experiencing "{title}" ({media_type}). '
                f'In {depth}, describe the overall tone, standout elements, '
                f'and what makes it special — without revealing plot details.'
            )
    else:
        count = "3"
        prompt = (
            f'The user just finished "{title}" ({media_type}). '
            f'Recommend exactly {count} similar titles they\'d likely enjoy. '
            f'Format each as: Title (Year) — one sentence why. '
            f'No markdown, just plain text.'
        )

    try:
        content = _generate_ai_content(prompt)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    ai_card_cache[cache_key] = (content, now + AI_CARD_TTL)

    result = {"content": content}
    if deep:
        try:
            extended = _generate_ai_content(_extended_ai_card_prompt(media))
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        result["chat_id"] = _create_deep_chat(media_id, status, extended)

    return jsonify(result)


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
    app.run(debug=True)