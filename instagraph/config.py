import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


DATA_DIR = Path(_env("INSTAGRAPH_DATA", str(ROOT / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "graph.db"
SESSION_PATH = DATA_DIR / "session.json"
AVATAR_DIR = DATA_DIR / "avatars"
AVATAR_DIR.mkdir(exist_ok=True)

IG_USERNAME = _env("IG_USERNAME")
IG_PASSWORD = _env("IG_PASSWORD")
IG_SESSIONID = _env("IG_SESSIONID")

HOST = _env("HOST", "127.0.0.1")
PORT = int(_env("PORT", "8765"))
OPEN_BROWSER = _env("OPEN_BROWSER", "true").lower() in ("1", "true", "yes")


def _detect_browser() -> str | None:
    explicit = _env("BROWSER_PATH")
    if explicit:
        return explicit
    for name in ("chromium", "google-chrome-stable", "google-chrome", "chromium-browser", "brave", "brave-browser"):
        path = shutil.which(name)
        if path:
            return path
    return None


BROWSER_PATH = _detect_browser()
