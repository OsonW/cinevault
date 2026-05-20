import sys
import os

# Resolve the project directory from this file's location so no username
# needs to be hardcoded here.
project_home = os.path.dirname(os.path.abspath(__file__))
if project_home not in sys.path:
    sys.path.insert(0, project_home)

# ── Environment variables ────────────────────────────────────────────────────
# Edit these three values before reloading the web app in the PA dashboard.
os.environ.setdefault("DB_DIR",       os.path.expanduser("~/data"))
os.environ.setdefault("SECRET_KEY",   "CHANGE-THIS-TO-A-RANDOM-64-CHAR-STRING")
os.environ.setdefault("GEMINI_MODEL", "gemini-2.0-flash-lite")

# ── WSGI entry point ─────────────────────────────────────────────────────────
from app import app as application  # noqa: E402
