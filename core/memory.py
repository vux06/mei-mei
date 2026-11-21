"""SQLite storage.

Two deliberately separate concerns live in one database file:

* PERMANENT MEMORY (`memory`, `preferences`, `folders`): explicit, user-approved facts. Survives context clears.
* CONVERSATION LOG (`sessions`, `messages`): history of what was said, per session id. Archived (never deleted)
  when the context is cleared; it is NOT fed back into Claude. It exists for the GUI transcript and for audit.

Clearing context touches only the active-session pointer — never the permanent-memory tables.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from .paths import DB_FILE

SCHEMA = """
CREATE TABLE IF NOT EXISTS memory (
    key TEXT PRIMARY KEY, value TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS preferences (
    key TEXT PRIMARY KEY, value TEXT NOT NULL, updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS folders (
    name TEXT PRIMARY KEY, path TEXT NOT NULL, updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY, started REAL NOT NULL, ended REAL, status TEXT NOT NULL DEFAULT 'active',
    turns INTEGER NOT NULL DEFAULT 0, note TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, ts REAL NOT NULL,
    role TEXT NOT NULL, text TEXT NOT NULL, source TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
"""


class Memory:
    def __init__(self, path: Path = DB_FILE):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    def _exec(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._db.execute(sql, args)
            rows = cur.fetchall()
            self._db.commit()
            return rows

    # ---- permanent memory --------------------------------------------------
    def remember(self, key: str, value: str) -> None:
        now = time.time()
        self._exec(
            "INSERT INTO memory(key,value,created,updated) VALUES(?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
            (key.strip().lower(), value.strip(), now, now),
        )

    def forget(self, key: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM memory WHERE key=?", (key.strip().lower(),))
            self._db.commit()
            return cur.rowcount > 0

    def recall(self, query: str = "", limit: int = 20) -> list[dict]:
        q = f"%{query.strip().lower()}%"
        rows = self._exec(
            "SELECT key,value FROM memory WHERE lower(key) LIKE ? OR lower(value) LIKE ? "
            "ORDER BY updated DESC LIMIT ?", (q, q, limit))
        return [dict(r) for r in rows]

    def all_memory(self) -> list[dict]:
        return [dict(r) for r in self._exec("SELECT key,value FROM memory ORDER BY key")]

    def set_pref(self, key: str, value: str) -> None:
        self._exec("INSERT INTO preferences(key,value,updated) VALUES(?,?,?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
                   (key, value, time.time()))

    def get_pref(self, key: str, default: str | None = None) -> str | None:
        rows = self._exec("SELECT value FROM preferences WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def all_prefs(self) -> dict[str, str]:
        return {r["key"]: r["value"] for r in self._exec("SELECT key,value FROM preferences")}

    def set_folder(self, name: str, path: str) -> None:
        self._exec("INSERT INTO folders(name,path,updated) VALUES(?,?,?) "
                   "ON CONFLICT(name) DO UPDATE SET path=excluded.path, updated=excluded.updated",
                   (name.strip().lower(), path, time.time()))

    def folders(self) -> dict[str, str]:
        return {r["name"]: r["path"] for r in self._exec("SELECT name,path FROM folders")}

    def memory_digest(self, max_chars: int = 1800) -> str:
        """Compact text of permanent memory for the system prompt (reapplied on every new session)."""
        lines = [f"- {m['key']}: {m['value']}" for m in self.all_memory()]
        lines += [f"- preference {k}: {v}" for k, v in self.all_prefs().items()]
        lines += [f"- folder '{k}': {v}" for k, v in self.folders().items()]
        out, total = [], 0
        for ln in lines:
            total += len(ln) + 1
            if total > max_chars:
                out.append("- (more entries available via the recall tool)")
                break
            out.append(ln)
        return "\n".join(out)

    # ---- session metadata + conversation log ------------------------------
    def open_session(self, session_id: str, note: str = "") -> None:
        self._exec("INSERT OR IGNORE INTO sessions(id,started,status,note) VALUES(?,?,'active',?)",
                   (session_id, time.time(), note))

    def archive_session(self, session_id: str, note: str = "") -> None:
        self._exec("UPDATE sessions SET status='archived', ended=?, note=COALESCE(NULLIF(?,''),note) WHERE id=?",
                   (time.time(), note, session_id))

    def bump_turns(self, session_id: str) -> None:
        self._exec("UPDATE sessions SET turns=turns+1 WHERE id=?", (session_id,))

    def log_message(self, session_id: str, role: str, text: str, source: str = "") -> None:
        self._exec("INSERT INTO messages(session_id,ts,role,text,source) VALUES(?,?,?,?,?)",
                   (session_id, time.time(), role, text, source))

    def history(self, session_id: str, limit: int = 200) -> list[dict]:
        rows = self._exec("SELECT ts,role,text,source FROM messages WHERE session_id=? "
                          "ORDER BY id DESC LIMIT ?", (session_id, limit))
        return [dict(r) for r in reversed(rows)]

    def sessions(self, limit: int = 20) -> list[dict]:
        return [dict(r) for r in self._exec("SELECT * FROM sessions ORDER BY started DESC LIMIT ?", (limit,))]
