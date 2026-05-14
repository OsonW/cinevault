import os
import requests
from flask import Flask, jsonify, redirect, render_template, request, Response
from dotenv import load_dotenv
from tmdbv3api import TMDb, Movie, TV
from google import genai

from db import (
    init_db, get_all_media, get_media_by_id,
    add_media_entry, update_media_entry, delete_media_entry,
    format_episode_string,
)

load_dotenv()

app = Flask(__name__)

# ---------- TMDB ----------
tmdb = TMDb()
tmdb.api_key = os.getenv("TMDB_API_KEY")
movie_api = Movie()
tv_api = TV()

# ---------- Gemini ----------
gemini = genai.Client()
GEMINI_MODEL = "gemini-3.1-flash-lite"

init_db()

# ─────────────────────────────────────────────
# In-memory caches
#
#  poster_cache  : { "movie_123": (bytes, content_type) }
#  search_cache  : { "movie:query string": [ ...results ] }
#
# Both are plain dicts keyed to avoid redundant TMDB round-trips.
# Poster entries are evicted explicitly when a library item is deleted.
# Search results are session-lived (re-populate on server restart, which
# is fine — they're ephemeral lookup results, not user data).
# ─────────────────────────────────────────────
poster_cache: dict[str, tuple[bytes, str]] = {}
search_cache: dict[str, list]              = {}

MOOD_LABELS = {
    "cozy":              "cozy and relaxing",
    "intense":           "intense and gripping",
    "mindless":          "easy and mindless fun",
    "thought-provoking": "thought-provoking and deep",
    "funny":             "funny and lighthearted",
    "emotional":         "emotional and moving",
}


# ═══════════════════════════════════════════════
# UI
# ═══════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html")


# ═══════════════════════════════════════════════
# TMDB — search (server-side cached)
# ═══════════════════════════════════════════════

@app.route("/api/search")
def search():
    query = request.args.get("q", "").strip()
    media_type = request.args.get("type", "movie")
    if not query:
        return jsonify([])

    # Check cache first
    cache_key = f"{media_type}:{query.lower()}"
    if cache_key in search_cache:
        return jsonify(search_cache[cache_key])

    try:
        if media_type == "movie":
            results = movie_api.search(query)
            if results and hasattr(results, '__iter__') and not isinstance(results, str):
                items = [
                    {
                        "tmdb_id": r.id,
                        "title": r.title,
                        "year": (r.release_date or "")[:4],
                        "media_type": "movie",
                        "poster_path": getattr(r, 'poster_path', None),
                    }
                    for r in results if hasattr(r, 'id')
                ]
            else:
                items = []
        else:
            results = tv_api.search(query)
            if results and hasattr(results, '__iter__') and not isinstance(results, str):
                items = [
                    {
                        "tmdb_id": r.id,
                        "title": r.name,
                        "year": (r.first_air_date or "")[:4],
                        "media_type": "tv",
                        "poster_path": getattr(r, 'poster_path', None),
                    }
                    for r in results if hasattr(r, 'id')
                ]
            else:
                items = []
                
        # Store in cache (limit cache size to prevent memory issues)
        search_cache[cache_key] = items[:10]
        # Optional: keep cache manageable (remove oldest if too large)
        if len(search_cache) > 100:
            # Remove first item (oldest)
            first_key = next(iter(search_cache))
            del search_cache[first_key]
            
    except Exception as e:
        print(f"Search error: {e}")
        items = []

    return jsonify(items[:10])


# ═══════════════════════════════════════════════
# TMDB — poster (fetched once, stored as bytes, served directly)
# ═══════════════════════════════════════════════

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

    # Fetch poster path from TMDB metadata endpoint
    endpoint = "movie" if media_type == "movie" else "tv"
    meta_url = f"https://api.themoviedb.org/3/{endpoint}/{tmdb_id}?api_key={tmdb.api_key}"
    try:
        meta = requests.get(meta_url, timeout=5).json()
        path = meta.get("poster_path")
        if not path:
            return "", 404

        img_resp = requests.get(f"https://image.tmdb.org/t/p/w500{path}", timeout=10)
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


# ═══════════════════════════════════════════════
# LIBRARY CRUD
# ═══════════════════════════════════════════════

@app.route("/api/list")
def list_media():
    return jsonify(get_all_media())


@app.route("/api/add", methods=["POST"])
def add_media():
    data = request.json or {}

    if "id" in data:
        last_season      = data.get("last_season")
        last_episode_num = data.get("last_episode_num")

        last_episode = data.get("last_episode")
        if last_season is not None or last_episode_num is not None:
            existing     = get_media_by_id(data["id"]) or {}
            season       = last_season      if last_season      is not None else existing.get("last_season")
            episode      = last_episode_num if last_episode_num is not None else existing.get("last_episode_num")
            last_episode = format_episode_string(season, episode) or last_episode

        update_media_entry(
            data["id"],
            status           = data.get("status"),
            rating           = data.get("rating"),
            last_timestamp   = data.get("last_timestamp"),
            last_episode     = last_episode,
            last_season      = last_season,
            last_episode_num = last_episode_num,
            notes            = data.get("notes"),
            mood_tags        = data.get("mood_tags"),
        )
    else:
        add_media_entry(
            tmdb_id    = data["tmdb_id"],
            title      = data["title"],
            media_type = data["media_type"],
            status     = data.get("status", "watchlist"),
            mood_tags  = data.get("mood_tags"),
        )

    return jsonify({"status": "ok"})


@app.route("/api/delete/<int:media_id>", methods=["DELETE"])
def delete_media(media_id):
    # Evict the poster from the in-memory cache before deleting the DB row.
    item = get_media_by_id(media_id)
    if item:
        poster_cache.pop(f"{item['media_type']}_{item['tmdb_id']}", None)

    delete_media_entry(media_id)
    return jsonify({"status": "deleted"})


# ═══════════════════════════════════════════════
# AI
# ═══════════════════════════════════════════════

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

    title        = media["title"]
    last_episode = media["last_episode"] or ""
    status       = media["status"]

    if status == "watchlist":
        spoiler_rule = "The user has NOT watched this yet. Give only premise/genre info — no plot, no twists, no deaths."
        progress_ctx = "not started"
    elif status == "watching":
        ep           = f"up to {last_episode}" if last_episode else "early on"
        spoiler_rule = f"The user has only watched {ep}. Refuse to reveal ANYTHING after that point."
        progress_ctx = ep
    else:
        spoiler_rule = "The user has finished this. Full discussion is fine."
        progress_ctx = "completed"

    prompt = f"""You are an enthusiastic but careful movie/TV assistant.
Title: "{title}" | Progress: {progress_ctx}
Rule: {spoiler_rule}
If the answer would spoil anything beyond their progress, reply ONLY: "⚠️ That's past where you are — keep watching!"
Otherwise answer in 2-4 sentences, conversationally and helpfully.

Question: {question}"""

    try:
        resp = gemini.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        return jsonify({"answer": resp.text.strip()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/ai-card", methods=["POST"])
def ai_card():
    """
    Context-aware AI card per status:
      watchlist → spoiler-free pitch
      watching  → TV: recap up to last episode | movie: vibe/cast card
      finished  → 3 similar title recommendations
    """
    data     = request.json or {}
    media_id = data.get("media_id")

    media = get_media_by_id(media_id)
    if not media:
        return jsonify({"error": "Media not found"}), 404

    title        = media["title"]
    media_type   = media["media_type"]
    status       = media["status"]
    last_episode = media["last_episode"] or ""
    mood_tags    = media["mood_tags"] or ""

    mood_str = ""
    if mood_tags:
        labels   = [MOOD_LABELS.get(t, t) for t in mood_tags.split(",") if t]
        mood_str = f"The user tagged it as: {', '.join(labels)}."

    if status == "watchlist":
        prompt = f"""Give a punchy 2-sentence spoiler-free pitch for "{title}" ({media_type}).
Cover: genre/tone, what makes it worth watching, and 2 comparable titles.
No plot details beyond the premise. {mood_str}
Format: plain sentences, no markdown."""

    elif status == "watching":
        if media_type == "tv" and last_episode:
            prompt = f"""The user is watching "{title}" and has reached {last_episode}.
Write 2 sentences recapping story beats that happened BEFORE {last_episode} only. {mood_str}
No spoilers for anything after that point.
Format: plain sentences, no markdown."""
        else:
            prompt = f"""The user is currently watching "{title}".
In 2 sentences, describe the overall tone, standout performances, and what makes this film special — without revealing plot details. {mood_str}
Format: plain sentences, no markdown."""

    else:  # finished
        prompt = f"""The user just finished "{title}". {mood_str}
Recommend exactly 3 similar titles they'd likely enjoy.
Format each as: Title (Year) — one sentence why.
No markdown, just plain text."""

    try:
        resp = gemini.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        return jsonify({"content": resp.text.strip()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True)