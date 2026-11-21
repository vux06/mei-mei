#!/usr/bin/env python
"""Mei Mei — personal AI desktop assistant.

  ./run.sh                 normal launch (GUI + always-on wake word)
  ./run.sh --hidden        start in the background (tray / `meimei_ctl.py show` to open the window)
  ./run.sh --headless      no GUI at all (voice + brain only)
  ./run.sh --no-voice      GUI/text only
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _detect_wayland_env() -> None:
    """When launched from a systemd user service the compositor env may be missing — find it."""
    rt = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    os.environ.setdefault("XDG_RUNTIME_DIR", rt)
    if not os.environ.get("WAYLAND_DISPLAY"):
        socks = sorted(p.name for p in Path(rt).glob("wayland-*") if not p.name.endswith(".lock"))
        if socks:
            os.environ["WAYLAND_DISPLAY"] = socks[0]
    if not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        inst = sorted(Path(rt, "hypr").glob("*/"), key=lambda p: p.stat().st_mtime) if Path(rt, "hypr").is_dir() else []
        if inst:
            os.environ["HYPRLAND_INSTANCE_SIGNATURE"] = inst[-1].name


def main() -> int:
    ap = argparse.ArgumentParser(description="Mei Mei desktop assistant")
    ap.add_argument("--hidden", action="store_true", help="start with the window hidden")
    ap.add_argument("--headless", action="store_true", help="run without any GUI")
    ap.add_argument("--no-voice", action="store_true", help="disable the voice pipeline")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    _detect_wayland_env()
    os.environ.setdefault("QT_QPA_PLATFORM", "wayland;xcb")

    # CUDA libs must be preloaded before ctranslate2 is imported anywhere
    from voice import cuda_setup
    cuda_setup.setup()

    from core.app import App
    from core.ipc import IPCServer
    from core.paths import SOCKET_PATH, ensure_dirs
    ensure_dirs()
    if SOCKET_PATH.exists() and IPCServer.alive():
        print("Mei Mei is already running — asking it to show its window.")
        import socket, json
        s = socket.socket(socket.AF_UNIX); s.connect(str(SOCKET_PATH)); s.sendall(b'{"cmd":"show"}\n'); s.close()
        return 0

    app = App(voice=not args.no_voice)
    app.start()

    if args.headless:
        signal.signal(signal.SIGINT, lambda *_: app.shutdown())
        signal.signal(signal.SIGTERM, lambda *_: app.shutdown())
        app.bus.on("activity", lambda e, d: print(f"[{d['level']}] {d['text']}", flush=True))
        app.bus.on("assistant_text", lambda e, d: print(f"MEI: {d['text']}", flush=True))
        while not app.quit_requested.is_set():
            time.sleep(0.3)
        return 0

    from PySide6.QtWidgets import QApplication
    from gui.main_window import Bridge, MainWindow
    from gui.styles import QSS
    qt = QApplication(sys.argv)
    qt.setApplicationName("Mei Mei")
    qt.setDesktopFileName("meimei")
    qt.setStyleSheet(QSS)
    qt.setQuitOnLastWindowClosed(False)
    bridge = Bridge(app.bus)
    win = MainWindow(app, bridge)
    if not (args.hidden or app.settings.get("gui.start_hidden", False)):
        win.show()
    signal.signal(signal.SIGINT, lambda *_: (setattr(win, "really_quit", True), win._quit()))
    signal.signal(signal.SIGTERM, lambda *_: (setattr(win, "really_quit", True), win._quit()))
    from PySide6.QtCore import QTimer
    t = QTimer(); t.timeout.connect(lambda: None); t.start(300)     # let Python signal handlers run
    rc = qt.exec()
    app.shutdown()
    return rc


if __name__ == "__main__":
    sys.exit(main())
