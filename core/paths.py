"""Filesystem locations. Source stays in the repo; models and data live outside it."""
from __future__ import annotations

import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = APP_DIR / "config"
SETTINGS_FILE = Path(os.environ.get("MEIMEI_SETTINGS", CONFIG_DIR / "settings.json"))
PERSONALITY_FILE = CONFIG_DIR / "personality.md"

DATA_DIR = Path(os.environ.get("MEIMEI_DATA_DIR", Path.home() / ".local" / "share" / "meimei"))
MODELS_DIR = Path(os.environ.get("MEIMEI_MODELS_DIR", Path.home() / ".local" / "share" / "meimei" / "models"))
WORKSPACE_DIR = DATA_DIR / "workspace"      # cwd for the Claude session (keeps its transcripts separate)
DB_FILE = DATA_DIR / "meimei.db"
SESSION_FILE = DATA_DIR / "session.json"
ACTIVITY_LOG = DATA_DIR / "activity.log"
SCREENSHOT_DIR = DATA_DIR / "screenshots"

_runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/meimei-{os.getuid()}"
RUNTIME_DIR = Path(_runtime) / "meimei"
SOCKET_PATH = Path(os.environ.get("MEIMEI_SOCKET", RUNTIME_DIR / "ctl.sock"))


def ensure_dirs() -> None:
    for d in (DATA_DIR, MODELS_DIR, WORKSPACE_DIR, SCREENSHOT_DIR):
        d.mkdir(parents=True, exist_ok=True)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(RUNTIME_DIR, 0o700)
