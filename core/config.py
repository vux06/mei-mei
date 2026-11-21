"""Settings loader: defaults in config/settings.json, deep-merged and saved atomically."""
from __future__ import annotations

import copy
import json
import os
import threading
from typing import Any

from .paths import PERSONALITY_FILE, SETTINGS_FILE


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Settings:
    """Thread-safe settings object. `get("tts.speed")` / `set("tts.speed", 1.1)`."""

    def __init__(self, path=SETTINGS_FILE):
        self.path = path
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        with self._lock:
            try:
                self._data = json.loads(self.path.read_text())
            except FileNotFoundError:
                self._data = {}

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2) + "\n")
            os.replace(tmp, self.path)

    def get(self, dotted: str, default: Any = None) -> Any:
        with self._lock:
            cur: Any = self._data
            for part in dotted.split("."):
                if not isinstance(cur, dict) or part not in cur:
                    return default
                cur = cur[part]
            return copy.deepcopy(cur)

    def set(self, dotted: str, value: Any, save: bool = True) -> None:
        with self._lock:
            parts = dotted.split(".")
            cur = self._data
            for p in parts[:-1]:
                cur = cur.setdefault(p, {})
            cur[parts[-1]] = value
            if save:
                self.save()

    def section(self, name: str) -> dict:
        return self.get(name, {}) or {}


def load_personality() -> str:
    """Read the editable personality file fresh each time (so edits apply on next reset/restart)."""
    return PERSONALITY_FILE.read_text().strip()
