"""One active logical conversation, identified by a Claude session id.

Lifecycle facts (verified against the Claude Agent SDK, see tests/test_session_lifecycle.py):
  * A session id chosen by us (`ClaudeAgentOptions.session_id`) starts a brand-new, empty conversation.
  * `ClaudeAgentOptions.resume=<id>` continues an existing one, with its full context.
  * An existing session CANNOT be emptied in place. "Clearing" therefore means: archive the old id,
    mint a new uuid, and start the next client with `session_id=<new>` (never `resume`).
    The old transcript stays on disk (archived, flagged in SQLite) but is never loaded again.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass

from .memory import Memory
from .paths import SESSION_FILE


@dataclass
class SessionState:
    session_id: str
    created: float
    generation: int          # increments on every clear; lets IPC callers detect races
    started_fresh: bool      # True until the SDK has actually run a turn in this session
    last_used: float = 0.0


class SessionManager:
    def __init__(self, memory: Memory, path=SESSION_FILE):
        self.memory = memory
        self.path = path
        self._lock = threading.RLock()
        self.state = self._load_or_create()

    # -- persistence ---------------------------------------------------------
    def _load_or_create(self) -> SessionState:
        try:
            d = json.loads(self.path.read_text())
            st = SessionState(d["session_id"], d["created"], d.get("generation", 1),
                              d.get("started_fresh", False), d.get("last_used", 0.0))
            uuid.UUID(st.session_id)  # validate
            self.memory.open_session(st.session_id, "recovered after restart")
            return st
        except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError):
            return self._create(generation=1, note="first run")

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state.__dict__, indent=2))
        os.replace(tmp, self.path)

    def _create(self, generation: int, note: str) -> SessionState:
        st = SessionState(str(uuid.uuid4()), time.time(), generation, started_fresh=True)
        self.state = st
        self.memory.open_session(st.session_id, note)
        self._save()
        return st

    # -- API -----------------------------------------------------------------
    @property
    def session_id(self) -> str:
        return self.state.session_id

    @property
    def resumable(self) -> bool:
        """True if the CLI has already stored turns for this id, i.e. we must `resume` instead of creating."""
        return not self.state.started_fresh

    def mark_used(self, confirmed_session_id: str) -> None:
        """Called with the session id the SDK reported on a ResultMessage."""
        with self._lock:
            if confirmed_session_id != self.state.session_id:
                # The SDK reports a different id than we asked for -> adopt it so we never resume the wrong one.
                self.memory.archive_session(self.state.session_id, "superseded by SDK-reported id")
                self.state.session_id = confirmed_session_id
                self.memory.open_session(confirmed_session_id, "adopted from SDK")
            self.state.started_fresh = False
            self.state.last_used = time.time()
            self._save()

    def clear(self, expect_session_id: str | None = None) -> tuple[str, str]:
        """Archive the active session and mint a fresh one. Returns (old_id, new_id).

        If `expect_session_id` is given and does not match the active session, nothing happens and
        ValueError is raised — this is what stops a stale script from clearing the wrong conversation."""
        with self._lock:
            old = self.state
            if expect_session_id and expect_session_id != old.session_id:
                raise ValueError(f"active session is {old.session_id}, not {expect_session_id}")
            self.memory.archive_session(old.session_id, "cleared by user")
            new = self._create(generation=old.generation + 1, note=f"fresh after clearing {old.session_id[:8]}")
            return old.session_id, new.session_id
