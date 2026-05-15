import os
import requests
from flask import Flask, jsonify, Response, render_template, request
from dotenv import load_dotenv
from tmdbv3api import TMDb, Movie, TV
from google import genai

from db import (
    init_db, get_all_media, get_media_by_id,
    add_media_entry, update_media_entry, delete_media_entry,
)

load_dotenv()

app = Flask(__name__)

# ── TMDB ──────────────────────────────────────────
tmdb = TMDb()
tmdb.api_key = os.getenv("TMDB_API_KEY")
movie_api = Movie()
tv_api    = TV()

# ── Gemini ────────────────────────────────────────
gemini       = genai.Client()
GEMINI_MODEL = "gemini-3.1-flash-lite"

init_db()

# ── In-memory caches ──────────────────────────────
#
#  poster_cache : { "movie_123": (bytes, content_type) }
#    - Populated on first request, evicted on item delete.
#    - Grows with library size; fine for a personal app
#      (~50 KB/poster × 200 titles ≈ 10 MB).
#
#  search_cache : { "movie:query": [...results] }
#    - Session-lived. Capped at 100 entries; oldest evicted first.
#
poster_cache: dict[str, tuple[bytes, str]] = {}
search_cache: dict[str, list]              = {}


# ═══════════════════════════════════════════════════
# UI
# ═══════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html")


# ═══════════════════════════════════════════════════
# TMDB — search
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
                    "title":       r.title,
                    "year":        (r.release_date or "")[:4],
                    "media_type":  "movie",
                    "poster_path": getattr(r, "poster_path", None),
                    "overview":    getattr(r, "overview", None),
                }
                for r in results if hasattr(r, "id")
            ]
        else:
            results = tv_api.search(query)
            items = [
                {
                    "tmdb_id":     r.id,
                    "title":       r.name,
                    "year":        (r.first_air_date or "")[:4],
                    "media_type":  "tv",
                    "poster_path": getattr(r, "poster_path", None),
                    "overview":    getattr(r, "overview", None),
                }
                for r in results if hasattr(r, "id")
            ]
    except Exception as e:
        print(f"Search error: {e}")
        items = []

    items = items[:10]
    search_cache[cache_key] = items
    if len(search_cache) > 100:
        del search_cache[next(iter(search_cache))]

    return jsonify(items)


# ═══════════════════════════════════════════════════
# TMDB — poster
# ═══════════════════════════════════════════════════

@app.route("/api/poster/<string:media_type>/<int:tmdb_id>")
def get_poster(media_type, tmdb_id):
    cache_key = f"{media_type}_{tmdb_id}"

    if cache_key in poster_cache:
        img_bytes, content_type = poster_cache[cache_key]
        return Response(
            img_bytes,
            mimetype=content_type,
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    endpoint = "movie" if media_type == "movie" else "tv"
    meta_url = (
        f"https://api.themoviedb.org/3/{endpoint}/{tmdb_id}"
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
            img_bytes,
            mimetype=content_type,
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )
    except Exception:
        return "", 404


# ═══════════════════════════════════════════════════
# LIBRARY CRUD
# ═══════════════════════════════════════════════════

@app.route("/api/list")
def list_media():
    return jsonify(get_all_media())


@app.route("/api/add", methods=["POST"])
def add_media():
    data = request.json or {}

    if "id" in data:
        # Build kwargs only for keys the caller actually sent, so that
        # falsy-but-valid values (rating=0, notes="") are written correctly
        # and fields absent from the payload are never touched.
        allowed = {"status", "rating", "last_timestamp",
                   "last_season", "last_episode", "notes"}
        fields  = {k: data[k] for k in allowed if k in data}
        update_media_entry(data["id"], **fields)
    else:
        add_media_entry(
            tmdb_id    = data["tmdb_id"],
            title      = data["title"],
            media_type = data["media_type"],
            status     = data.get("status", "watchlist"),
        )

    return jsonify({"status": "ok"})


@app.route("/api/delete/<int:media_id>", methods=["DELETE"])
def delete_media(media_id):
    item = get_media_by_id(media_id)
    if not item:
        return jsonify({"status": "not_found"}), 404
    poster_cache.pop(f"{item['media_type']}_{item['tmdb_id']}", None)
    delete_media_entry(media_id)
    return jsonify({"status": "deleted"})


# ═══════════════════════════════════════════════════
# AI helpers
# ═══════════════════════════════════════════════════

def _progress_str(media: dict) -> str:
    """Return e.g. 'Season 2, Episode 4' or '' if not set."""
    s = media.get("last_season")
    e = media.get("last_episode")
    if s and e:
        return f"Season {s}, Episode {e}"
    if s:
        return f"Season {s}"
    return ""


# ═══════════════════════════════════════════════════
# AI endpoints
# ═══════════════════════════════════════════════════

@app.route("/api/ask", methods=["POST"])
def ask_ai():
    """Spoiler-aware Q&A for any title."""
    data     = request.json or {}
    question = data.get("question", "").strip()
    media_id = data.get("media_id")

    if not question:
        return jsonify({"error": "No question provided"}), 400

    media = get_media_by_id(media_id)
    if not media:
        return jsonify({"error": "Media not found"}), 404

    title    = media["title"]
    status   = media["status"]
    progress = _progress_str(media)

    if status == "watchlist":
        spoiler_rule = (
            "The user has NOT watched this yet. "
            "Give only premise/genre info — no plot, no twists, no deaths."
        )
        progress_ctx = "not started"
    elif status == "watching":
        ep           = f"up to {progress}" if progress else "early on"
        spoiler_rule = (
            f"The user has only watched {ep}. "
            "Refuse to reveal ANYTHING after that point."
        )
        progress_ctx = ep
    else:
        spoiler_rule = "The user has finished this. Full discussion is fine."
        progress_ctx = "completed"

    prompt = (
        f'You are an enthusiastic but careful movie/TV assistant.\n'
        f'Title: "{title}" | Progress: {progress_ctx}\n'
        f'Rule: {spoiler_rule}\n'
        f'If the answer would spoil anything beyond their progress, reply ONLY: '
        f'"That\'s past where you are — keep watching!"\n'
        f'Otherwise answer in 2-4 sentences, conversationally and helpfully.\n\n'
        f'Question: {question}'
    )

    try:
        resp = gemini.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        return jsonify({"answer": resp.text.strip()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/ai-card", methods=["POST"])
def ai_card():
    """
    Context-aware AI insight card per status:
      watchlist → spoiler-free pitch
      watching  → TV: recap up to current episode | movie: vibe/cast card
      finished  → 3 similar title recommendations
    """
    data     = request.json or {}
    media_id = data.get("media_id")

    media = get_media_by_id(media_id)
    if not media:
        return jsonify({"error": "Media not found"}), 404

    title      = media["title"]
    media_type = media["media_type"]
    status     = media["status"]
    progress   = _progress_str(media)

    if status == "watchlist":
        prompt = (
            f'Give a punchy 2-sentence spoiler-free pitch for "{title}" ({media_type}). '
            f'Cover: genre/tone, what makes it worth watching, and 2 comparable titles. '
            f'No plot details beyond the premise. '
            f'Format: plain sentences, no markdown.'
        )
    elif status == "watching":
        if media_type == "tv" and progress:
            prompt = (
                f'The user is watching "{title}" and has reached {progress}. '
                f'Write 2 sentences recapping story beats that happened BEFORE {progress} only. '
                f'No spoilers for anything after that point. '
                f'Format: plain sentences, no markdown.'
            )
        else:
            prompt = (
                f'The user is currently watching "{title}". '
                f'In 2 sentences, describe the overall tone, standout performances, '
                f'and what makes this film special — without revealing plot details. '
                f'Format: plain sentences, no markdown.'
            )
    else:  # finished
        prompt = (
            f'The user just finished "{title}". '
            f'Recommend exactly 3 similar titles they\'d likely enjoy. '
            f'Format each as: Title (Year) — one sentence why. '
            f'No markdown, just plain text.'
        )

    try:
        resp = gemini.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        return jsonify({"content": resp.text.strip()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True)