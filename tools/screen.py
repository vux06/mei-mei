"""Screen awareness (grim) and input automation (ydotool + Hyprland cursor + wl-clipboard).

Mouse positioning uses Hyprland's own cursor dispatcher (exact, no pointer-acceleration drift); only the
click itself goes through ydotool's uinput device.
"""
from __future__ import annotations

import base64
import re
import time
from pathlib import Path

from core.paths import SCREENSHOT_DIR
from . import windows
from .common import run

# Linux input-event-codes (US layout) for ydotool raw key events.
KEYS = {c: i for i, c in enumerate("1234567890", start=2)}
KEYS.update({c: i for i, c in zip(range(16, 26), "qwertyuiop")})
KEYS.update({c: i for i, c in zip(range(30, 39), "asdfghjkl")})
KEYS.update({c: i for i, c in zip(range(44, 51), "zxcvbnm")})
KEYS.update({"esc": 1, "escape": 1, "minus": 12, "equal": 13, "backspace": 14, "tab": 15, "enter": 28, "return": 28,
             "ctrl": 29, "control": 29, "shift": 42, "alt": 56, "space": 57, "capslock": 58,
             "f1": 59, "f2": 60, "f3": 61, "f4": 62, "f5": 63, "f6": 64, "f7": 65, "f8": 66, "f9": 67, "f10": 68,
             "f11": 87, "f12": 88, "home": 102, "up": 103, "pageup": 104, "left": 105, "right": 106, "end": 107,
             "down": 108, "pagedown": 109, "insert": 110, "delete": 111, "super": 125, "meta": 125, "win": 125,
             "comma": 51, "period": 52, "dot": 52, "slash": 53, "semicolon": 39, "apostrophe": 40, "grave": 41,
             "backslash": 43, "leftbracket": 26, "rightbracket": 27, "lshift": 42, "rshift": 54, "lctrl": 29,
             "rctrl": 97, "lalt": 56, "ralt": 100, "printscreen": 99, "menu": 127})
MODIFIERS = {"ctrl", "control", "shift", "alt", "super", "meta", "win", "lshift", "rshift", "lctrl", "rctrl", "lalt", "ralt"}
BUTTONS = {"left": "0xC0", "right": "0xC1", "middle": "0xC2"}


def screenshot(target: str = "screen", max_width: int = 1280, save: bool = True) -> dict:
    """Capture with grim. target: 'screen' or 'window' (active window). Returns bytes + metadata."""
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SCREENSHOT_DIR / f"shot-{int(time.time())}.jpg"
    mon = next((m for m in windows.monitors() if m["focused"]), windows.monitors()[0])
    cmd = ["grim", "-t", "jpeg", "-q", "80", "-o", mon["name"]]
    geom = None
    if target == "window":
        w = windows.active_window()
        if w:
            (x, y), (ww, hh) = w["at"], w["size"]
            geom = (x, y, ww, hh)
            cmd = ["grim", "-t", "jpeg", "-q", "80", "-g", f"{x},{y} {ww}x{hh}"]
    full_w = geom[2] if geom else mon["width"]
    scale = min(1.0, max_width / full_w)
    if scale < 1.0:
        cmd += ["-s", f"{scale:.3f}"]
    rc, out, err = run(cmd + [str(path)], timeout=15)
    if rc != 0 or not path.exists():
        raise RuntimeError(f"grim failed: {err.strip()}")
    data = path.read_bytes()
    if not save:
        path.unlink(missing_ok=True)
    return {"data": data, "path": str(path), "scale": scale, "screen": f"{mon['width']}x{mon['height']}",
            "origin": (geom[0], geom[1]) if geom else (mon["x"], mon["y"])}


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def click(x: int, y: int, button: str = "left", double: bool = False) -> None:
    windows.move_cursor(x, y)
    time.sleep(0.05)
    code = BUTTONS.get(button, BUTTONS["left"])
    for _ in range(2 if double else 1):
        run(["ydotool", "click", code], timeout=5)
        time.sleep(0.06)


def scroll(amount: int) -> None:
    """Positive = down, negative = up (in wheel notches)."""
    run(["ydotool", "mousemove", "--wheel", "-x", "0", "-y", str(-int(amount))], timeout=5)


def type_text(text: str) -> None:
    if text.isascii() and all(c.isprintable() or c in "\n\t" for c in text):
        run(["ydotool", "type", "--key-delay", "8", "--", text], timeout=60)
    else:  # layout-safe path for non-ASCII
        old = run(["wl-paste", "--no-newline"], timeout=3)[1]
        run(["wl-copy"], input_bytes=text.encode(), timeout=3)
        time.sleep(0.05)
        press("ctrl+v")
        time.sleep(0.2)
        run(["wl-copy"], input_bytes=old.encode(), timeout=3)


def press(combo: str) -> None:
    """e.g. 'ctrl+shift+t', 'enter', 'alt+left'."""
    parts = [p.strip().lower() for p in re.split(r"\+", combo) if p.strip()]
    codes = []
    for p in parts:
        if p not in KEYS:
            raise ValueError(f"unknown key '{p}'")
        codes.append(KEYS[p])
    seq = [f"{c}:1" for c in codes] + [f"{c}:0" for c in reversed(codes)]
    run(["ydotool", "key", "--key-delay", "20", *seq], timeout=5)


def clipboard_get() -> str:
    return run(["wl-paste", "--no-newline"], timeout=3)[1]


def clipboard_set(text: str) -> None:
    run(["wl-copy"], input_bytes=text.encode(), timeout=3)


def active_is_terminal() -> str | None:
    from core.permissions import TERMINAL_CLASSES
    w = windows.active_window()
    return w["class"] if w and w["class"].lower() in TERMINAL_CLASSES else None
