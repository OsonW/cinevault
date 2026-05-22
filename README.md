# CineVault

A personal movie, TV, book, and manga tracker with AI-powered, spoiler-aware insights. Track what you're watching, rate your favorites, and chat with an AI that knows how far along you are.

**Live demo:** https://oson.pythonanywhere.com/

## Features

- **One library, four media types** — Organize movies, TV shows, books, and manga across Watchlist, Watching, and Finished.
- **Rich search** — Look up titles from TMDB (movies/TV), OpenLibrary (books), and MangaDex (manga), complete with posters, authors, and overviews.
- **AI insights** — Spoiler-free pitches for your watchlist, progress-aware recaps for what you're partway through, and recommendations once you finish.
- **Vibe search** — Describe a mood ("cozy mysteries", "dark academia") or a seed title and get AI-curated suggestions you can add in one click.
- **Spoiler-aware chat** — Ask anything about a title. The AI scopes its answers to your tracked progress (season/episode or chapter) and blurs anything beyond it until you choose to reveal it.
- **Progress tracking** — Season/episode for TV, volume/chapter for manga, page for books, timestamp for movies — validated against real season/chapter counts.
- **Rate & note** — Score titles out of 10 and keep private notes.

## How it works

CineVault is a Flask app with a single-page, vanilla-JS front end. Each account gets an isolated SQLite database, so your library and chats are never mixed with anyone else's.

```
Browser (SPA)  ──►  Flask (app.py)  ──►  per-user SQLite (movie_tracker_<id>.db)
                          │
                          ├─►  TMDB / OpenLibrary / MangaDex   (search, posters, metadata)
                          └─►  Google Gemini                   (insights, vibe search, chat)
```

| Layer | Tech |
|-------|------|
| Backend | Flask, Flask-Login |
| Auth | Username/password, bcrypt hashing, `SameSite=Strict` session cookies |
| Storage | SQLite — one global `users.db` plus a `movie_tracker_<id>.db` per user |
| AI | Google Gemini (`google-genai`) |
| Metadata | TMDB, OpenLibrary, MangaDex |
| Front end | Server-rendered Jinja + a single-page vanilla-JS UI |

## API keys

CineVault uses two **free, bring-your-own** API keys. On first login a setup screen prompts for both (with step-by-step instructions) and validates them before saving. You can update them anytime via the key icon in the top-right.

| Key | Where to get it | Takes |
|-----|-----------------|-------|
| **Gemini API key** | [Google AI Studio](https://aistudio.google.com/apikey) → Create API key | ~30 seconds |
| **TMDB API key** | [TMDB Settings → API](https://www.themoviedb.org/settings/api) → Request an API key (personal use) | ~2 minutes |

Your keys are tied to your account and stored server-side. They're used only to make requests to Gemini and TMDB on your behalf, and are masked (never returned in full) when the settings screen loads.

## Running locally

Requires Python 3.10+.

```bash
git clone <repo-url>
cd cinema-tracker

python -m venv venv
# Windows:
venv\Scripts\activate
# macOS/Linux:
source venv/bin/activate

pip install -r requirements.txt
python app.py
```

The app starts on http://localhost:5000. Register an account, then enter your Gemini and TMDB keys when prompted.

### Environment variables

All are optional for local development; sensible defaults apply. See `.env.example`.

| Variable | Default | Purpose |
|----------|---------|---------|
| `SECRET_KEY` | random per process | Flask session signing. Set a fixed value in production so sessions survive restarts. Setting it also enables `Secure` cookies and the reverse-proxy `ProxyFix`. |
| `DB_DIR` | `.` (current dir) | Directory where `users.db` and the per-user databases are stored. |
| `GEMINI_MODEL` | `gemini-3.1-flash-lite` | Gemini model used for all AI features. |
| `PORT` | `5000` | Port the dev server binds to. |
| `FLASK_DEBUG` | `0` | Set to `1` to enable Flask debug mode. |

Generate a secret key with:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

## Tests

```bash
python -m pytest
```

## Deployment

Deployed on **PythonAnywhere** — point the web app's WSGI file at `pythonanywhere_wsgi.py` and set `DB_DIR` and `SECRET_KEY`. See `pythonanywhere_setup.md` for the full walkthrough.

In production, always set a fixed `SECRET_KEY` and a writable `DB_DIR` outside the code directory.

## Project structure

```
app.py                   Flask app: routes, AI orchestration, caching
auth.py                  Auth blueprint: login/register, rate limiting, key validation
db.py                    Per-user SQLite: media, chats, messages, memory
users_db.py              Global users DB: accounts and stored API keys
templates/index.html     Single-page application UI
templates/login.html     Sign-in / registration page
tests/                   pytest suite
pythonanywhere_wsgi.py   WSGI entry point for PythonAnywhere
```

## License

MIT
