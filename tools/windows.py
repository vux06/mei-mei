"""Hyprland window management.

Hyprland >= 0.55 uses a Lua config, so `hyprctl dispatch` takes Lua expressions such as
`hl.dsp.focus({window="address:0x..."})`. Verified against the installed compositor (0.56).
"""
from __future__ import annotations

import json
import re
from typing import Any

from .common import arun, run

MINIMIZED_WS = "special:minimized"   # same convention as the user's quickshell config


def hypr_json(what: str) -> Any:
    rc, out, err = run(["hyprctl", "-j", what], timeout=5)
    if rc != 0:
        raise RuntimeError(f"hyprctl {what} failed: {err.strip() or out.strip()}")
    return json.loads(out)


def available() -> bool:
    rc, _, _ = run(["hyprctl", "version"], timeout=3)
    return rc == 0


def _win(c: dict) -> dict:
    return {"address": c["address"], "class": c.get("class", ""), "title": c.get("title", ""),
            "workspace": c.get("workspace", {}).get("name"), "pid": c.get("pid"),
            "floating": c.get("floating"), "fullscreen": c.get("fullscreen"),
            "minimized": str(c.get("workspace", {}).get("name", "")).startswith(MINIMIZED_WS),
            "at": c.get("at"), "size": c.get("size"), "focused": False}


def list_windows() -> list[dict]:
    act = hypr_json("activewindow")
    act_addr = act.get("address") if isinstance(act, dict) else None
    out = []
    for c in hypr_json("clients"):
        if not c.get("mapped", True):
            continue
        w = _win(c)
        w["focused"] = (w["address"] == act_addr)
        out.append(w)
    return out


def active_window() -> dict | None:
    act = hypr_json("activewindow")
    return _win(act) if isinstance(act, dict) and act.get("address") else None


def find_windows(query: str) -> list[dict]:
    """Match by address, class or title (case-insensitive substring); class matches rank first."""
    q = query.strip().lower()
    wins = list_windows()
    exact = [w for w in wins if w["address"].lower() == q]
    if exact:
        return exact
    by_class = [w for w in wins if q and q in w["class"].lower()]
    by_title = [w for w in wins if q and q in w["title"].lower() and w not in by_class]
    return by_class + by_title


def lua_dispatch(expr: str) -> tuple[bool, str]:
    rc, out, err = run(["hyprctl", "dispatch", expr], timeout=5)
    text = (out + err).strip()
    return (rc == 0 and text.startswith("ok") or text == ""), text


def _sel(w: dict) -> str:
    return f'window="address:{w["address"]}"'


def focus(w: dict) -> tuple[bool, str]:
    if w["minimized"]:  # bring it back from the minimized special workspace first
        ws = hypr_json("activeworkspace").get("id", 1)
        lua_dispatch(f'hl.dsp.window.move({{workspace={ws}, follow=false, {_sel(w)}}})')
    return lua_dispatch(f'hl.dsp.focus({{{_sel(w)}}})')


def close(w: dict) -> tuple[bool, str]:
    return lua_dispatch(f'hl.dsp.window.close({{{_sel(w)}}})')


def minimize(w: dict) -> tuple[bool, str]:
    return lua_dispatch(f'hl.dsp.window.move({{workspace="{MINIMIZED_WS}", follow=false, {_sel(w)}}})')


def restore(w: dict) -> tuple[bool, str]:
    return focus(w)


def toggle_maximize(w: dict) -> tuple[bool, str]:
    return lua_dispatch(f'hl.dsp.window.fullscreen({{mode="maximized", action="toggle", {_sel(w)}}})')


def toggle_fullscreen(w: dict) -> tuple[bool, str]:
    return lua_dispatch(f'hl.dsp.window.fullscreen({{mode="fullscreen", action="toggle", {_sel(w)}}})')


def set_floating(w: dict, floating: bool) -> tuple[bool, str]:
    return lua_dispatch(f'hl.dsp.window.float({{action="{"enable" if floating else "disable"}", {_sel(w)}}})')


def move(w: dict, x: int, y: int) -> tuple[bool, str]:
    if not w["floating"]:
        set_floating(w, True)
    return lua_dispatch(f'hl.dsp.window.move({{x={int(x)}, y={int(y)}, {_sel(w)}}})')


def resize(w: dict, width: int, height: int) -> tuple[bool, str]:
    if not w["floating"]:
        set_floating(w, True)
    return lua_dispatch(f'hl.dsp.window.resize({{x={int(width)}, y={int(height)}, exact=true, {_sel(w)}}})')


def move_to_workspace(w: dict, ws: str) -> tuple[bool, str]:
    return lua_dispatch(f'hl.dsp.window.move({{workspace={_lua_ws(ws)}, follow=false, {_sel(w)}}})')


def switch_workspace(ws: str) -> tuple[bool, str]:
    return lua_dispatch(f'hl.dsp.focus({{workspace={_lua_ws(ws)}}})')


def _lua_ws(ws: str) -> str:
    return ws if re.fullmatch(r"\d+", ws) else '"' + ws.replace('"', "") + '"'


def move_cursor(x: int, y: int) -> tuple[bool, str]:
    return lua_dispatch(f'hl.dsp.cursor.move({{x={int(x)}, y={int(y)}}})')


def monitors() -> list[dict]:
    return [{"name": m["name"], "width": m["width"], "height": m["height"], "scale": m["scale"],
             "x": m["x"], "y": m["y"], "focused": m.get("focused", False),
             "workspace": m.get("activeWorkspace", {}).get("name")} for m in hypr_json("monitors")]
