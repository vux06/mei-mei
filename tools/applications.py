"""Application discovery, launching, closing, URL opening (XDG desktop entries + Hyprland)."""
from __future__ import annotations

import configparser
import difflib
import os
import re
import shlex
import shutil
import threading
import time
from pathlib import Path

import psutil

from . import windows
from .common import run, spawn_detached

APP_DIRS = [Path("/usr/share/applications"), Path.home() / ".local/share/applications",
            Path("/var/lib/flatpak/exports/share/applications"),
            Path.home() / ".local/share/flatpak/exports/share/applications"]

# Spoken names people use that don't match the desktop entry name.
ALIASES = {"browser": "firefox", "web browser": "firefox", "internet": "firefox", "terminal": "kitty",
           "files": "thunar", "file manager": "thunar", "code": "code", "editor": "code",
           "music": "spotify", "settings": "systemsettings"}
_FIELD_CODES = re.compile(r"\s%[fFuUdDnNickvm]")


class AppEntry:
    def __init__(self, desktop_id: str, name: str, exec_: str, generic: str, wmclass: str, terminal: bool):
        self.id, self.name, self.exec, self.generic, self.wmclass, self.terminal = (
            desktop_id, name, exec_, generic, wmclass, terminal)

    @property
    def command(self) -> str:
        return _FIELD_CODES.sub("", " " + self.exec).strip()

    @property
    def binary(self) -> str:
        try:
            return Path(shlex.split(self.command)[0]).name
        except (ValueError, IndexError):
            return self.id


_cache: tuple[float, list[AppEntry]] = (0.0, [])


def installed_apps(force: bool = False) -> list[AppEntry]:
    global _cache
    if not force and time.time() - _cache[0] < 300 and _cache[1]:
        return _cache[1]
    apps: dict[str, AppEntry] = {}
    for d in APP_DIRS:
        if not d.is_dir():
            continue
        for f in d.rglob("*.desktop"):
            cp = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                cp.read(f, encoding="utf-8")
                e = cp["Desktop Entry"]
            except Exception:
                continue
            if e.get("Type") != "Application" or e.get("NoDisplay", "false").lower() == "true" or e.get("Hidden") == "true":
                continue
            apps[f.stem] = AppEntry(f.stem, e.get("Name", f.stem), e.get("Exec", ""), e.get("GenericName", ""),
                                    e.get("StartupWMClass", ""), e.get("Terminal", "false").lower() == "true")
    _cache = (time.time(), list(apps.values()))
    return _cache[1]


def find_app(query: str) -> AppEntry | None:
    q = query.strip().lower()
    q = ALIASES.get(q, q)
    apps = installed_apps()
    for key in (lambda a: a.id.lower(), lambda a: a.name.lower(), lambda a: a.binary.lower(), lambda a: a.wmclass.lower()):
        for a in apps:
            if key(a) == q:
                return a
    cands = [a for a in apps if q in a.name.lower() or q in a.generic.lower() or q in a.id.lower()]
    if cands:
        return min(cands, key=lambda a: len(a.name))
    names = {a.name.lower(): a for a in apps}
    close = difflib.get_close_matches(q, names, n=1, cutoff=0.78)
    return names[close[0]] if close else None


def open_app(query: str) -> dict:
    """Launch an application (detached). Returns {ok, message, app}."""
    app = find_app(query)
    if app:
        cmd = app.command
        if app.terminal and shutil.which("kitty"):
            cmd = f"kitty {cmd}"
        spawn_detached(cmd, shell=True)
        return {"ok": True, "app": app.name, "message": f"launched {app.name}"}
    if shutil.which(query.split()[0]):
        spawn_detached(shlex.split(query))
        return {"ok": True, "app": query, "message": f"launched {query}"}
    return {"ok": False, "app": query, "message": f"no installed application matches '{query}'"}


def _match_windows(query: str) -> list[dict]:
    app = find_app(query)
    wins = windows.list_windows()
    q = query.lower()
    keys = {q}
    if app:
        keys |= {app.binary.lower(), app.id.lower(), app.wmclass.lower(), app.name.lower()}
    keys.discard("")
    return [w for w in wins if any(k == w["class"].lower() or k in w["class"].lower() for k in keys)]


def running_windows(query: str) -> list[dict]:
    return _match_windows(query)


def is_running(query: str) -> dict:
    wins = _match_windows(query)
    app = find_app(query)
    procs = []
    names = {query.lower()} | ({app.binary.lower(), app.id.lower()} if app else set())
    for p in psutil.process_iter(["name", "pid"]):
        n = (p.info["name"] or "").lower()
        if n in names:
            procs.append(p.info["pid"])
    return {"running": bool(wins or procs), "windows": [f'{w["class"]}: {w["title"]}' for w in wins], "pids": procs[:10]}


def close_app(query: str) -> dict:
    """Politely close all windows of an app (like clicking the X)."""
    wins = _match_windows(query)
    if not wins:
        return {"ok": False, "message": f"no open window for '{query}'"}
    closed = 0
    for w in wins:
        ok, _ = windows.close(w)
        closed += bool(ok)
    return {"ok": closed > 0, "closed": closed, "message": f"asked {closed} window(s) of {query} to close"}


def switch_to_app(query: str) -> dict:
    wins = _match_windows(query)
    if not wins:
        return {"ok": False, "message": f"'{query}' is not running"}
    ok, msg = windows.focus(wins[0])
    return {"ok": ok, "message": f"focused {wins[0]['class']}: {wins[0]['title']}" if ok else msg}


def open_url(url: str) -> dict:
    if not re.match(r"^(https?://|file://|mailto:)", url):
        if re.match(r"^[\w.-]+\.[a-z]{2,}(/.*)?$", url):
            url = "https://" + url
        else:
            return {"ok": False, "message": "only http(s)/file/mailto URLs are allowed"}
    spawn_detached(["xdg-open", url])
    threading.Thread(target=_focus_browser, daemon=True).start()
    return {"ok": True, "message": f"opened {url} in the default browser"}


BROWSER_CLASSES = ("firefox", "zen", "librewolf", "brave", "chromium", "chrome", "vivaldi", "floorp", "waterfox", "opera")


def _focus_browser() -> None:
    """Bring the browser forward so the user actually sees what was opened (best effort)."""
    time.sleep(0.8)
    try:
        wins = [w for w in windows.list_windows() if any(b in w["class"].lower() for b in BROWSER_CLASSES)]
        if wins and not any(w["focused"] for w in wins):
            windows.focus(wins[0])
    except Exception:
        pass


def search_url(query: str, engine: str = "duckduckgo") -> str:
    from urllib.parse import quote_plus
    base = {"google": "https://www.google.com/search?q=", "duckduckgo": "https://duckduckgo.com/?q=",
            "bing": "https://www.bing.com/search?q="}.get(engine, "https://duckduckgo.com/?q=")
    return base + quote_plus(query)
