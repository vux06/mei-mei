"""Aggregates the independent activity flags into the single visible assistant state."""
from __future__ import annotations

import threading
import time

from .events import EventBus

IDLE, LISTENING, PROCESSING, EXECUTING, SPEAKING, ERROR = "IDLE", "LISTENING", "PROCESSING", "EXECUTING", "SPEAKING", "ERROR"


class StateTracker:
    def __init__(self, bus: EventBus):
        self.bus = bus
        self._lock = threading.Lock()
        self._flags = {"listening": False, "processing": False, "executing": 0, "speaking": False}
        self._error_until = 0.0
        self.current = IDLE

    def set(self, **flags) -> None:
        with self._lock:
            self._flags.update(flags)
        self._publish()

    def bump_exec(self, delta: int) -> None:
        with self._lock:
            self._flags["executing"] = max(0, self._flags["executing"] + delta)
        self._publish()

    def error(self, seconds: float = 4.0) -> None:
        self._error_until = time.time() + seconds
        self._publish()

    def _compute(self) -> str:
        f = self._flags
        if time.time() < self._error_until:
            return ERROR
        if f["speaking"]:
            return SPEAKING
        if f["listening"]:
            return LISTENING
        if f["executing"] > 0:
            return EXECUTING
        if f["processing"]:
            return PROCESSING
        return IDLE

    def _publish(self) -> None:
        with self._lock:
            new = self._compute()
            changed = new != self.current
            self.current = new
        if changed:
            self.bus.emit("state", state=new)
