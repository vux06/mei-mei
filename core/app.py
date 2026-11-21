"""Wires every component together. GUI-independent so the same object runs headless (tests, service mode)."""
from __future__ import annotations

import logging
import threading
import time

from .assistant import Assistant
from .config import Settings
from .events import EventBus
from .ipc import IPCServer
from .memory import Memory
from .paths import ACTIVITY_LOG, ensure_dirs
from .permissions import PermissionManager
from .session_manager import SessionManager
from .state import StateTracker

log = logging.getLogger("meimei.app")


class App:
    def __init__(self, voice: bool = True):
        ensure_dirs()
        self.settings = Settings()
        self.bus = EventBus()
        self.memory = Memory()
        self.sessions = SessionManager(self.memory)
        self.perms = PermissionManager(self.bus, self.settings)
        self.state = StateTracker(self.bus)
        self.tts = None
        self.stt = None
        self.voice = None
        self.want_voice = voice
        self.ready = threading.Event()
        self.load_error: str | None = None
        self.stt_info: dict = {}
        self.quit_requested = threading.Event()
        self.assistant: Assistant | None = None
        self.ipc: IPCServer | None = None
        self._activity_fh = open(ACTIVITY_LOG, "a", buffering=1)
        self.bus.on("activity", self._log_activity)
        self.bus.on("user_text", lambda e, d: self._log_activity("activity", {"level": "user", "text": d["text"]}))

    def _log_activity(self, event, data) -> None:
        try:
            self._activity_fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{data.get('level', 'info')}] {data['text']}\n")
        except Exception:
            pass

    # ------------------------------------------------------------------
    def start(self) -> None:
        """Start the brain and IPC immediately; load heavy voice models in the background."""
        from .assistant import Assistant
        from voice.tts import TTSEngine
        self.tts = TTSEngine(self.settings, self.bus)
        self.assistant = Assistant(self.settings, self.bus, self.memory, self.sessions, self.perms, self.state, self.tts)
        self.assistant.start()
        self.ipc = IPCServer(self)
        self.ipc.start()
        threading.Thread(target=self._load_voice, name="voice-loader", daemon=True).start()

    def _load_voice(self) -> None:
        try:
            if self.want_voice:
                self.bus.emit("wake_status", state="loading", enabled=True, paused=False, mic="stopped", mic_detail="", stats={})
            else:
                self.bus.emit("wake_status", state="disabled", enabled=False, paused=False, mic="stopped", mic_detail="voice off (--no-voice)", stats={})
            self.tts.load()
            if self.want_voice:
                from voice.audio import router
                if self.settings.get("audio.echo_cancel", True):
                    ok = router.setup(self.settings)
                    self.bus.emit("activity", level="info",
                                  text="Echo cancellation (WebRTC AEC) active — you can interrupt her while she speaks" if ok
                                  else f"Echo cancellation unavailable ({router.error}); using text-based echo rejection")
                from voice.pipeline import VoicePipeline
                from voice.stt import STT
                self.stt = STT(self.settings)
                self.stt_info = self.stt.load()
                self.bus.emit("activity", level="info",
                              text=f"Speech recognition ready: {self.stt_info['model']} on {self.stt_info['device']} "
                                   f"({self.stt_info['compute']}), loaded in {self.stt_info['load_seconds']}s")
                self.voice = VoicePipeline(self.settings, self.bus, self.stt, self.tts, self.assistant, self.perms, self.state)
                self.assistant.voice = self.voice
                self.voice.start()
                self.bus.emit("activity", level="info", text="Always-on listening started — say “Hey Mei Mei”")
            self.ready.set()
            self.bus.emit("ready")
        except Exception as e:
            log.exception("voice load failed")
            self.load_error = str(e)
            self.bus.emit("activity", level="error", text=f"Voice startup failed: {e}")
            self.bus.emit("wake_status", state="error", enabled=True, paused=False, mic="stopped", mic_detail=str(e), stats={})
            self.state.error(10)
            self.ready.set()

    def status(self) -> dict:
        st = self.assistant.status()
        if self.voice:
            st.update(wake=self.voice.wake_state(), mic=self.voice.mic.status, mic_detail=self.voice.mic.detail,
                      listening=self.voice.listening, wake_stats=self.voice.stats)
        else:
            st.update(wake="no-voice", mic="stopped", listening=False)
        st["stt"] = self.stt_info
        return st

    def shutdown(self) -> None:
        try:
            if self.voice:
                self.voice.stop()
                from voice.audio import router
                router.teardown()
            if self.tts:
                self.tts.stop()
            if self.ipc:
                self.ipc.stop()
            if self.assistant:
                self.assistant.shutdown()
        finally:
            self.quit_requested.set()
