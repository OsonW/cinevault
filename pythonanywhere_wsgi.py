import sys
import os

# Resolve the project directory from this file's location so no username
# needs to be hardcoded here.
project_home = os.path.dirname(os.path.abspath(__file__))
if project_home not in sys.path:
    sys.path.insert(0, project_home)

# ── Environment variables ────────────────────────────────────────────────────
# Edit these three values before reloading the web app in the PA dashboard.
os.environ.setdefault("DB_DIR",       "/home/Oson/data/")
os.environ.setdefault("SECRET_KEY",   "23c3ef931d05672f4ce9b21fe46b71209f530dcd78fcf9c382a316130b7c0545")
os.environ.setdefault("GEMINI_MODEL", "gemini-3.1-flash-lite")

# ── WSGI entry point ─────────────────────────────────────────────────────────
from app import app as application  # noqa: E402
