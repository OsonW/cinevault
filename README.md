# CineVault

A personal movie and TV show tracker with AI-powered insights. Track what you're watching, rate your favorites, and get spoiler-aware recommendations.

## Features

**Track your media** - Organize movies and TV shows into Watchlist, Watching, and Finished  
**Rich search** - Search TMDB for movies and TV shows with posters, ratings, and overviews  
**AI-powered insights** - Get spoiler-free pitches, progress recaps, and personalized recommendations  
**Ask anything** - Chat with AI about any title (spoiler-aware based on your progress)  
**Rate & review** - Rate out of 10 and add private notes

### Prerequisites

- Python 3.11 or higher

CineVault requires two free API keys to work:
- TMDB API key ([Get one here](https://www.themoviedb.org/signup))
- Google Gemini API key ([Get one here](https://aistudio.google.com/))

### Installation

1. 
Clone the repository:
```bash
git clone https://github.com/OsonW/cinevault.git
cd cinevault
```

2. 
After installing, create a `.env` file in the project root:
```env
TMDB_API_KEY=your_tmdb_api_key_here
GEMINI_API_KEY=your_gemini_api_key_here
```

3. 
Install dependencies and virtual environment on Mac/Linux:
```
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Install dependencies and virtual environment on Windows:
```
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

4.
Run the application:
```
python app.py
```

5. 
Navigate to http://127.0.0.1:5000/ on your browser. Enjoy!