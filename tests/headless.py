"""Helpers to run the assistant without GUI/voice (used by the e2e tests)."""
from __future__ import annotations

import tempfile
import threading
from pathlib import Path

from core.assistant import Assistant
from core.config import Settings
from core.events import EventBus
from core.memory import Memory
from core.permissions import PermissionManager
from core.session_manager import SessionManager
from core.state import StateTracker


class FakeTTS:
    def __init__(self):
        self.said: list[str] = []
        self.lock = threading.Lock()

    def speak(self, text, interrupt=False):
        with self.lock:
            self.said.append(text)

    def stop(self):
        pass


def make(tmp: Path | None = None, auto_confirm: bool | None = None):
    tmp = tmp or Path(tempfile.mkdtemp(prefix="meimei-test-"))
    bus = EventBus()
    settings = Settings()
    mem = Memory(tmp / "t.db")
    sessions = SessionManager(mem, tmp / "session.json")
    perms = PermissionManager(bus, settings)
    state = StateTracker(bus)
    tts = FakeTTS()
    a = Assistant(settings, bus, mem, sessions, perms, state, tts)
    log: list[tuple[str, dict]] = []
    bus.on("*", lambda e, d: log.append((e, d)))
    if auto_confirm is not None:
        bus.on("confirm_request", lambda e, d: perms.resolve(d["id"], auto_confirm))
    return a, tts, log, perms, mem, sessions
