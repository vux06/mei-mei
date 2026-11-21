#!/usr/bin/python3
"""Mei Mei overlay (Hyprland / layer-shell, click-through, above every window).
Bottom-center glow whose color shows my state:
  teal calm      = listening to you          purple = thinking / working
  teal bright    = speaking                  amber  = waiting for your yes/no
  red pulse      = something failed
Extras: a teal streak to wherever I click, and a stream of teal particles to the top-right
corner when I move/organize files. Everything follows ~/.local/share/meimei/activity.log
(speaking is detected from the audio output)."""
import json, math, os, re, subprocess, time
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("GtkLayerShell", "0.1")
from gi.repository import Gtk, GLib, GtkLayerShell
import cairo

LOG = os.path.expanduser("~/.local/share/meimei/activity.log")
SLOW = float(os.environ.get("MEIMEI_SLOW", "1"))
FORCE = os.environ.get("MEIMEI_FORCE", "")          # test: listen|think|speak|wait|error
TEST_FILES = bool(os.environ.get("MEIMEI_FILES"))
MAX_ACTIVE, MAX_WAIT = 60.0, 150.0
TRAVEL, RIPPLE, FADE = 0.55 * SLOW, 0.7 * SLOW, 0.25 * SLOW
FILE_DUR = 2.6
LINE_RE = re.compile(r"^\S+ \S+ \[(\w+)\] (.*)$")
CLICK_RE = re.compile(r"^mouse_click (\{.*\})")


def rgb(r, g, b):
    return (r / 255, g / 255, b / 255)


LIGHT = rgb(60, 230, 210)
MODES = {   # main color, edge color, width scale, height scale
    "listen": (rgb(20, 200, 190), rgb(60, 230, 210), 1.0, 1.0),
    "think": (rgb(169, 100, 255), rgb(140, 110, 255), 1.0, 1.0),
    "speak": (rgb(60, 240, 220), rgb(140, 255, 240), 1.1, 1.2),
    "wait": (rgb(255, 170, 50), rgb(255, 200, 100), 1.0, 1.0),
    "error": (rgb(255, 70, 70), rgb(255, 120, 100), 1.1, 1.2),
}

win = Gtk.Window()
GtkLayerShell.init_for_window(win)
GtkLayerShell.set_layer(win, GtkLayerShell.Layer.OVERLAY)
for edge in (GtkLayerShell.Edge.TOP, GtkLayerShell.Edge.BOTTOM, GtkLayerShell.Edge.LEFT, GtkLayerShell.Edge.RIGHT):
    GtkLayerShell.set_anchor(win, edge, True)
GtkLayerShell.set_exclusive_zone(win, -1)
GtkLayerShell.set_namespace(win, "meimei-listen")
GtkLayerShell.set_keyboard_mode(win, GtkLayerShell.KeyboardMode.NONE)
win.set_app_paintable(True)
win.set_decorated(False)
vis = win.get_screen().get_rgba_visual()
if vis:
    win.set_visual(vis)
area = Gtk.DrawingArea()
win.add(area)

S = {"p": 0.0, "listening": False, "thinking": False, "waiting": False, "speaking": False,
     "err_at": 0.0, "since": 0.0, "wait_at": 0.0, "last_log": 0.0,
     "mode": None, "col": [*MODES["listen"][:2], 1.0, 1.0], "w": 1.0, "h": 1.0}
clicks, files = [], []


def ease(x):
    return 1 - (1 - x) ** 3


def bez(p0, p1, p2, t):
    u = 1 - t
    return (u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0],
            u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1])


def lerp(a, b, k):
    return tuple(a[i] + (b[i] - a[i]) * k for i in range(3))


def mode_now(now):
    if FORCE:
        return FORCE
    if now - S["err_at"] < 1.9:
        return "error"
    if S["waiting"]:
        return "wait"
    if S["speaking"]:
        return "speak"
    if S["thinking"]:
        return "think"
    if S["listening"]:
        return "listen"
    return None


def alpha_for(mode, now):
    if mode == "speak":
        return 0.55 + 0.45 * abs(math.sin(now * 7))
    if mode == "error":
        return (0.5 + 0.5 * abs(math.sin(now * 5))) * max(0.0, 1 - (now - S["err_at"]) / 1.9) if not FORCE else 0.5 + 0.5 * abs(math.sin(now * 5))
    period = {"think": 1.2, "wait": 2.2}.get(mode, 1.8)
    return 0.6 + 0.4 * (0.5 - 0.5 * math.cos(now * 2 * math.pi / period))


def glow(cr, W, H, now):
    p = S["p"]
    if p <= 0.001:
        return
    e = ease(p)
    a = e * alpha_for(S["mode"] or "listen", now)
    c1, c2, _, _ = S["col"]
    cr.save()
    cr.translate(W / 2, H + (1 - e) * 110)
    cr.scale(424 * S["w"], 113 * S["h"])
    g = cairo.RadialGradient(0, 0, 0, 0, 0, 1)
    g.add_color_stop_rgba(0.0, *c1, 0.8 * a)
    g.add_color_stop_rgba(0.5, *c2, 0.3 * a)
    g.add_color_stop_rgba(0.7, *c2, 0.0)
    cr.set_source(g)
    cr.arc(0, 0, 1, 0, 2 * math.pi)
    cr.fill()
    cr.restore()


def orb(cr, x, y, r, a, col=LIGHT):
    g = cairo.RadialGradient(x, y, 0, x, y, r)
    g.add_color_stop_rgba(0.0, 0.85, 1.0, 0.98, a)
    g.add_color_stop_rgba(0.25, *col, 0.8 * a)
    g.add_color_stop_rgba(1.0, *col, 0.0)
    cr.set_source(g)
    cr.arc(x, y, r, 0, 2 * math.pi)
    cr.fill()


def draw_click(cr, c, W, H, now):
    age = now - c["t0"]
    if age > TRAVEL + RIPPLE + FADE:
        return False
    p0, p2 = (W / 2, H - 20), (c["x"], c["y"])
    mid = ((p0[0] + p2[0]) / 2, (p0[1] + p2[1]) / 2)
    dx, dy = p2[0] - p0[0], p2[1] - p0[1]
    dist = math.hypot(dx, dy) or 1
    side = 1 if dx >= 0 else -1
    p1 = (mid[0] - dy / dist * 0.25 * dist * side, mid[1] + dx / dist * 0.25 * dist * side * -1)
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    if age < TRAVEL + FADE:
        t = ease(min(1.0, age / TRAVEL))
        retract = max(0.0, (age - TRAVEL) / FADE)
        tail0 = max(0.0, t - 0.45) + retract * 0.45
        n = 28
        for i in range(n):
            a = tail0 + (t - tail0) * i / n
            b = tail0 + (t - tail0) * (i + 1) / n
            if b <= a:
                continue
            f = (i + 1) / n
            x0, y0 = bez(p0, p1, p2, a)
            x1, y1 = bez(p0, p1, p2, b)
            cr.set_source_rgba(*LIGHT, 0.55 * f * (1 - retract))
            cr.set_line_width(2 + 7 * f)
            cr.move_to(x0, y0)
            cr.line_to(x1, y1)
            cr.stroke()
        hx, hy = bez(p0, p1, p2, t)
        orb(cr, hx, hy, 26, 1 - retract)
    if age >= TRAVEL:
        r = (age - TRAVEL) / RIPPLE
        fade = 1 - max(0.0, (age - TRAVEL - RIPPLE) / FADE)
        orb(cr, c["x"], c["y"], 34 * (1 - min(1, r) * 0.4), max(0.0, 1 - r) * fade)
        for k in range(2):
            rr = r - k * 0.25
            if 0 < rr <= 1:
                cr.set_source_rgba(*LIGHT, 0.9 * (1 - rr) * fade)
                cr.set_line_width(3 - 1.5 * rr)
                cr.arc(c["x"], c["y"], 10 + 55 * ease(rr), 0, 2 * math.pi)
                cr.stroke()
    return True


def draw_files(cr, f, W, H, now):
    age = now - f["t0"]
    if age > FILE_DUR:
        return False
    env = min(1.0, age / 0.3) * min(1.0, (FILE_DUR - age) / 0.5)
    src, dst, ctl = (W / 2, H - 30), (W - 90, 90), (W * 0.75, H * 0.7)
    for i in range(26):
        k = ((age * 0.5 - i * 0.035) % 1.0)
        x, y = bez(src, ctl, dst, ease(k))
        x += math.sin(i + age * 3) * 6
        y += math.cos(i * 2 + age * 3) * 6
        orb(cr, x, y, 10, math.sin(math.pi * k) * 0.9 * env)
    return True


def on_draw(widget, cr):
    W, H = widget.get_allocated_width(), widget.get_allocated_height()
    now = time.time()
    cr.set_operator(cairo.OPERATOR_SOURCE)
    cr.set_source_rgba(0, 0, 0, 0)
    cr.paint()
    cr.set_operator(cairo.OPERATOR_OVER)
    glow(cr, W, H, now)
    files[:] = [f for f in files if draw_files(cr, f, W, H, now)]
    clicks[:] = [c for c in clicks if draw_click(cr, c, W, H, now)]
    return False


area.connect("draw", on_draw)
win.connect("map", lambda _w: win.get_window() and win.get_window().input_shape_combine_region(cairo.Region(), 0, 0))


def tick():
    now = time.time()
    if (S["listening"] or S["thinking"]) and now - S["since"] > MAX_ACTIVE:
        S["listening"] = S["thinking"] = False
    if S["waiting"] and now - S["wait_at"] > MAX_WAIT:
        S["waiting"] = False
    m = mode_now(now)
    if m:
        S["mode"] = m
        tc1, tc2, tw, th = MODES[m]
        c1, c2, _, _ = S["col"]
        S["col"] = [lerp(c1, tc1, 0.15), lerp(c2, tc2, 0.15), 1, 1]
        S["w"] += (tw - S["w"]) * 0.15
        S["h"] += (th - S["h"]) * 0.15
    target = 1.0 if m else 0.0
    if S["p"] < target:
        S["p"] = min(target, S["p"] + 0.06)
    elif S["p"] > target:
        S["p"] = max(target, S["p"] - 0.05)
    active = S["p"] > 0 or bool(clicks) or bool(files)
    if active and not win.get_visible():
        win.show_all()
    if not active and win.get_visible():
        win.hide()
    if win.get_visible():
        area.queue_draw()
    return True


pos = {"n": os.path.getsize(LOG) if os.path.exists(LOG) else 0}


def handle(level, text):
    now = time.time()
    S["last_log"] = now
    if level == "user":
        S["listening"], S["thinking"], S["since"] = False, True, now
    elif level == "tool":
        m = CLICK_RE.match(text)
        if m:
            try:
                d = json.loads(m.group(1))
                clicks.append({"x": float(d["x"]), "y": float(d["y"]), "t0": now})
            except (ValueError, KeyError):
                pass
        elif text.startswith(("move_path ", "organize_directory ")):
            files.append({"t0": now})
    elif level == "info":
        if text.startswith(("Wake phrase heard", "Barge-in")):
            S["listening"], S["thinking"], S["since"] = True, False, now
        elif text.startswith(("Turn finished", "Local intent", "Interrupted current task", "Turn was interrupted")):
            S["listening"] = S["thinking"] = False
        elif text.startswith(("Allowed:", "Declined:")):
            S["waiting"] = False
    elif level == "warn":
        if text.startswith("CONFIRM?"):
            S["waiting"], S["wait_at"] = True, now
        elif text.startswith("Confirmation timed out"):
            S["waiting"] = False
    elif level in ("error", "deny"):
        if "error_during_execution" not in text:
            S["err_at"] = now


def poll():
    try:
        size = os.path.getsize(LOG)
        if size < pos["n"]:
            pos["n"] = 0
        if size > pos["n"]:
            with open(LOG, "r", errors="replace") as f:
                f.seek(pos["n"])
                chunk = f.read()
                pos["n"] = f.tell()
            for line in chunk.splitlines():
                m = LINE_RE.match(line)
                if m:
                    handle(m.group(1), m.group(2))
    except OSError:
        pass
    return True


main_pid = {"v": None}


def audio_poll():
    """Speaking = a non-corked audio stream from the Mei Mei python process."""
    now = time.time()
    if not (now - S["last_log"] < 40 or S["speaking"]):
        return True
    try:
        if not main_pid["v"] or not os.path.exists(f"/proc/{main_pid['v']}"):
            r = subprocess.run(["pgrep", "-f", "meimei/main.py"], capture_output=True, text=True, timeout=2)
            main_pid["v"] = (r.stdout.split() or [None])[0]
        out = subprocess.run(["pactl", "list", "sink-inputs"], capture_output=True, text=True, timeout=2).stdout
        sp = False
        for blk in out.split("Sink Input #")[1:]:
            if "Corked: no" in blk and main_pid["v"] and f'application.process.id = "{main_pid["v"]}"' in blk:
                sp = True
        S["speaking"] = sp
    except (OSError, subprocess.SubprocessError):
        pass
    return True


GLib.timeout_add(16, tick)
GLib.timeout_add(120, poll)
GLib.timeout_add(350, audio_poll)
if TEST_FILES:
    GLib.timeout_add(3500, lambda: (files.append({"t0": time.time()}), True)[1])
Gtk.main()
