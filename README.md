# CineVault

A personal movie, TV, book, and manga tracker with AI-powered insights. Track what you're watching, rate your favorites, and get spoiler-aware recommendations.

## Features

**Track your media** — Organize movies, TV shows, books, and manga into Watchlist, Watching, and Finished  
**Rich search** — Search TMDB, OpenLibrary, and MangaDex with posters and overviews  
**AI-powered insights** — Get spoiler-free pitches, progress recaps, and personalized recommendations  
**Vibe search** — Describe a mood or title and get AI-curated recommendations  
**Ask anything** — Chat with AI about any title (spoiler-aware based on your progress)  
**Rate & review** — Rate out of 10 and add private notes

## API Keys

CineVault requires two free API keys. **You bring your own — they are stored only in your browser and never sent to any server we control.**

| Key | Where to get it |
|-----|----------------|
| **Gemini API Key** | [Google AI Studio → API Keys](https://aistudio.google.com/apikey) — free tier available |
| **TMDB API Key (v3)** | [themoviedb.org → Settings → API](https://www.themoviedb.org/settings/api) — free account required |

When you open the app for the first time, a setup screen will prompt you for both keys. You can reset them at any time using the key icon (🔑) next to the tabs.

## Local Development

### Prerequisites

- Python 3.11 or higher
- No `.env` file needed — keys are entered in the browser

### Setup

```bash
# Clone
git clone https://github.com/OsonW/cinevault.git
cd cinevault

# Mac/Linux
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py

# Windows
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Navigate to http://127.0.0.1:5000/ and enter your API keys when prompted.

## Railway Deployment

CineVault ships with Railway config out of the box. **No environment variables needed** — all API keys are provided by the user in their browser.

### Steps

1. Push this repo to GitHub.

2. Create a new Railway project and connect the GitHub repo.

3. In the Railway service settings, add a **Volume** mounted at `/data`. This persists your SQLite database across deploys.

4. Set one environment variable in Railway:

   | Variable | Value |
   |----------|-------|
   | `GEMINI_MODEL` | `gemini-2.0-flash-lite` (or your preferred model) |

5. Deploy. Railway picks up `railway.json` and starts gunicorn automatically.

6. Open the deployed URL — the key setup modal will appear on first visit.

### What stays in Railway

- Your SQLite database (in the `/data` volume)
- `GEMINI_MODEL` env var (optional — defaults to `gemini-3.1-flash-lite`)

### What stays in the browser

- Gemini API key
- TMDB API key

## Roadmap

- [x] Add manga and book APIs  
- [x] Gemini-powered Vibe Search  
- [x] Spoiler-aware AI chat  
- [x] Browser-side key management  
- [x] Railway deployment
