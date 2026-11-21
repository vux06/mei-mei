"""Local control socket (Unix domain, 0600, peer-uid checked). Used by scripts/clear-context.sh and meimei_ctl.py.

Protocol: one JSON object per line in, one JSON object per line out.
  {"cmd": "status"}
  {"cmd": "clear", "expect_session_id": "<uuid>"}      # refuses if the active session is not that id
  {"cmd": "listen", "action": "pause|resume|enable|disable"}
  {"cmd": "ask", "text": "..."} | {"cmd": "stop_speaking"} | {"cmd": "show"} | {"cmd": "quit"}
"""
from __future__ import annotations

import json
import logging
import os
import socket
import struct
import threading

from .paths import SOCKET_PATH

log = logging.getLogger("meimei.ipc")


class IPCServer:
    def __init__(self, app):
        self.app = app
        self.sock: socket.socket | None = None
        self._running = False

    @staticmethod
    def alive(path=SOCKET_PATH) -> bool:
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(1.0)
        try:
            s.connect(str(path))
            return True
        except OSError:
            return False
        finally:
            s.close()

    def start(self) -> None:
        if SOCKET_PATH.exists():
            if self.alive():
                raise RuntimeError("another Mei Mei instance is already running")
            SOCKET_PATH.unlink()            # stale socket from a crash
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)
        try:
            self.sock.bind(str(SOCKET_PATH))
        finally:
            os.umask(old)
        os.chmod(SOCKET_PATH, 0o600)
        self.sock.listen(4)
        self._running = True
        threading.Thread(target=self._serve, name="ipc", daemon=True).start()
        log.info("control socket at %s", SOCKET_PATH)

    def stop(self) -> None:
        self._running = False
        try:
            if self.sock:
                self.sock.close()
            SOCKET_PATH.unlink(missing_ok=True)
        except OSError:
            pass

    def _serve(self) -> None:
        while self._running:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(60)
            try:
                uid = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))[1]
                if uid != os.getuid():
                    self._send(conn, {"ok": False, "error": "forbidden"})
                    return
                data = b""
                while not data.endswith(b"\n") and len(data) < 65536:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                req = json.loads(data.decode() or "{}")
                self._send(conn, self.dispatch(req))
            except Exception as e:
                log.exception("ipc error")
                try:
                    self._send(conn, {"ok": False, "error": str(e)})
                except OSError:
                    pass

    @staticmethod
    def _send(conn, obj) -> None:
        conn.sendall((json.dumps(obj) + "\n").encode())

    def dispatch(self, req: dict) -> dict:
        app, cmd = self.app, req.get("cmd")
        if cmd == "status":
            return {"ok": True, **app.status()}
        if cmd == "clear":
            try:
                res = app.assistant.clear_context(req.get("expect_session_id")).result(timeout=40)
                return {"ok": True, **res}
            except ValueError as e:
                return {"ok": False, "error": f"refused: {e}"}
        if cmd == "listen":
            if not app.voice:
                return {"ok": False, "error": "voice pipeline not running"}
            act = req.get("action")
            {"pause": app.voice.pause, "resume": app.voice.resume,
             "enable": app.voice.enable, "disable": app.voice.disable}.get(act, lambda: None)()
            return {"ok": act in ("pause", "resume", "enable", "disable"), "wake": app.voice.wake_state()}
        if cmd == "ask":
            app.assistant.submit(req.get("text", ""), source="ipc")
            return {"ok": True}
        if cmd == "stop_speaking":
            app.tts.stop()
            app.assistant.interrupt()
            return {"ok": True}
        if cmd == "show":
            app.bus.emit("show_window")
            return {"ok": True}
        if cmd == "quit":
            threading.Thread(target=app.shutdown, daemon=True).start()
            app.bus.emit("quit_requested")
            return {"ok": True}
        return {"ok": False, "error": f"unknown command {cmd!r}"}
