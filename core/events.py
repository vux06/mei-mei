"""Tiny thread-safe pub/sub bus. Subscribers are called as fn(event_name, data_dict); on("*") gets everything."""
from __future__ import annotations

import logging
import threading
from collections import defaultdict
from typing import Callable

log = logging.getLogger("meimei.events")


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[str, list[Callable]] = defaultdict(list)
        self._lock = threading.Lock()

    def on(self, event: str, fn: Callable) -> None:
        with self._lock:
            self._subs[event].append(fn)

    def emit(self, event: str, **data) -> None:
        with self._lock:
            fns = list(self._subs.get(event, ())) + list(self._subs.get("*", ()))
        for fn in fns:
            try:
                fn(event, data)
            except Exception:  # a broken subscriber must never kill the pipeline
                log.exception("event subscriber failed for %s", event)
