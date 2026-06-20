# CineVault

A personal movie, TV, book, and manga tracker. Track what you're watching, rate your favorites, and keep notes — all in one library.

**Live demo:** https://oson.pythonanywhere.com/

## Features

- **One library, four media types** — Organize movies, TV shows, books, and manga across Watchlist, Watching, and Finished.
- **Rich search** — Look up titles from TMDB (movies/TV), OpenLibrary (books), and MangaDex (manga), complete with posters, authors, and overviews.
- **Progress tracking** — Season/episode for TV, volume/chapter for manga, page for books, timestamp for movies — validated against real season/chapter counts.
- **Rate & note** — Score titles out of 10 and keep private notes.

## How it works

CineVault is a Flask app with a single-page, vanilla-JS front end. Each account gets an isolated SQLite database, so your library is never mixed with anyone else's.

```
Browser (SPA)  ──►  Flask (app.py)  ──►  per-user SQLite (movie_tracker_<id>.db)
                          │
                          └─►  TMDB / OpenLibrary / MangaDex   (search, posters, metadata)
```

| Layer | Tech |
|-------|------|
| Backend | Flask, Flask-Login |
| Auth | Username/password, bcrypt hashing, `SameSite=Strict` session cookies |
| Storage | SQLite — one global `users.db` plus a `movie_tracker_<id>.db` per user |
| Metadata | TMDB, OpenLibrary, MangaDex |
| Front end | Server-rendered Jinja + a single-page vanilla-JS UI |

## API keys

CineVault uses one **free, bring-your-own** API key. On first login a setup screen prompts for it (with step-by-step instructions) and validates it before saving. You can update it anytime via the key icon in the top-right.

| Key | Where to get it | Takes |
|-----|-----------------|-------|
| **TMDB API key** | [TMDB Settings → API](https://www.themoviedb.org/settings/api) → Request an API key (personal use) | ~2 minutes |

Your key is tied to your account and stored server-side. It's used only to make requests to TMDB on your behalf, and is masked (never returned in full) when the settings screen loads.

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

The app starts on http://localhost:5000. Register an account, then enter your TMDB key when prompted.

### Environment variables

All are optional for local development; sensible defaults apply. See `.env.example`.

| Variable | Default | Purpose |
|----------|---------|---------|
| `SECRET_KEY` | random per process | Flask session signing. Set a fixed value in production so sessions survive restarts. Setting it also enables `Secure` cookies and the reverse-proxy `ProxyFix`. |
| `DB_DIR` | `.` (current dir) | Directory where `users.db` and the per-user databases are stored. |
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
app.py                   Flask app: routes, search, caching
auth.py                  Auth blueprint: login/register, rate limiting, key validation
db.py                    Per-user SQLite: media library
users_db.py              Global users DB: accounts and stored API key
templates/index.html     Single-page application UI
templates/login.html     Sign-in / registration page
tests/                   pytest suite
pythonanywhere_wsgi.py   WSGI entry point for PythonAnywhere
```

## License

MIT
