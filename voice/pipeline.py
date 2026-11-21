"""Always-on, hands-free voice loop.

   mic frames ──► Silero VAD ──► utterance segmenter ──► [WAKE]  tiny-Whisper phrase check ("Hey Mei Mei")
                                                          │ match
                                                          ▼
                                   chime · pause wake detection · [CAPTURE] until VAD says the speaker is done
                                                          │
                                                          ▼
                                       main Whisper (GPU) ──► Assistant.submit() ──► TTS
                                                          │
                                       back to [WAKE] (wake detection also stays armed while she speaks/works,
                                       which is what makes "Hey Mei Mei, stop" barge-in possible)

Audio never leaves the process except as *text* sent to Claude after a wake-word-triggered command.
"""
from __future__ import annotations

import collections
import logging
import threading
import time
from enum import Enum

import numpy as np

from core.events import EventBus
from core.permissions import PermissionManager
from core.state import StateTracker
from . import wake_word as ww
from .audio import FRAME, SAMPLE_RATE, MicStream
from .stt import HALLUCINATIONS, STT
from .vad import StreamingVAD

log = logging.getLogger("meimei.voice")
FRAME_S = FRAME / SAMPLE_RATE


class Mode(Enum):
    WAKE = "wake"          # waiting for "Hey Mei Mei"
    CAPTURE = "capture"    # wake heard; recording the command (no wake word needed)
    PTT = "ptt"            # push-to-talk (fallback)


class VoicePipeline:
    def __init__(self, settings, bus: EventBus, stt: STT, tts, assistant, perms: PermissionManager,
                 state: StateTracker, mic: MicStream | None = None):
        self.settings, self.bus, self.stt, self.tts = settings, bus, stt, tts
        self.assistant, self.perms, self.state = assistant, perms, state
        self.mic = mic or MicStream(settings)
        self.mic.on_status = lambda s, d: self.bus.emit("mic_status", status=s, detail=d)
        self.vad = StreamingVAD()
        self.mode = Mode.WAKE
        ww.EXTRA_PHRASES[:] = [str(x) for x in (settings.get("wake_word.extra_phrases", []) or [])]
        self.enabled = bool(settings.get("wake_word.enabled", True))     # False = fully disabled
        self.paused = bool(settings.get("voice.start_listening_paused", False))
        self._running = False
        self._thread: threading.Thread | None = None
        self._cooldown_until = 0.0
        self._capture_deadline = 0.0
        self._capture_started_followup = False
        self._ptt_active = False
        self._ptt_temp_mic = False
        self._ptt_frames: list[np.ndarray] = []
        self._ptt_event = threading.Event()
        self._followup = 0.0
        self._barge = False
        self._barge_mark = 0
        self._last_level_emit = 0.0
        self.stats = {"wake_checks": 0, "wakes": 0, "false_wakes_rejected": 0, "commands": 0}
        self._reset_segmenter()
        bus.on("speaking", self._on_speaking)

    # ------------------------------------------------------------------ control API (thread-safe)
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, name="voice-loop", daemon=True)
        self._thread.start()
        self._apply_listening_state()

    def stop(self) -> None:
        self._running = False
        self.mic.stop()

    def pause(self) -> None:
        """Stop listening (the microphone stream is closed) until resume()."""
        self.paused = True
        self._apply_listening_state()

    def resume(self) -> None:
        self.paused = False
        self.enabled = True
        self.settings.set("wake_word.enabled", True)
        self._apply_listening_state()

    def disable(self) -> None:
        """Turn always-on listening off completely (persisted). The mic is released."""
        self.enabled = False
        self.settings.set("wake_word.enabled", False)
        self._apply_listening_state()

    def enable(self) -> None:
        self.enabled = True
        self.paused = False
        self.settings.set("wake_word.enabled", True)
        self._apply_listening_state()

    @property
    def listening(self) -> bool:
        return self.enabled and not self.paused

    def wake_state(self) -> str:
        if not self.enabled:
            return "disabled"
        if self.paused:
            return "paused"
        if self.mic.status != "active":
            return "no-mic"
        return "capturing" if self.mode in (Mode.CAPTURE, Mode.PTT) else "listening"

    def _apply_listening_state(self) -> None:
        if self.listening and self._running:
            self.mic.start()
        elif not self._ptt_active:
            self.mic.stop("paused" if self.enabled else "stopped")
            self.mic.drain()
        self._reset_segmenter()
        self.mode = Mode.WAKE
        self.state.set(listening=False)
        self._emit_wake_status()

    def _emit_wake_status(self) -> None:
        self.bus.emit("wake_status", state=self.wake_state(), enabled=self.enabled, paused=self.paused,
                      mic=self.mic.status, mic_detail=self.mic.detail, stats=dict(self.stats))

    def open_capture_window(self, seconds: float) -> None:
        """Listen for a follow-up command without requiring the wake word."""
        if self.listening:
            self.mode = Mode.CAPTURE
            self._capture_deadline = time.time() + seconds
            self._reset_segmenter()
            self.state.set(listening=True)
            self._emit_wake_status()

    # -- push-to-talk fallback --------------------------------------------------
    def ptt_begin(self) -> None:
        if self._ptt_active:
            return
        self.tts.stop()
        self.assistant.interrupt()
        self._ptt_active = True
        self._ptt_frames = []
        if self.mic.status != "active":
            self._ptt_temp_mic = True
            self.mic.start()
            for _ in range(40):
                if self.mic.status == "active":
                    break
                time.sleep(0.05)
        self.mic.drain()
        self.vad.reset()
        self.mode = Mode.PTT
        self.tts.chime("ack")
        self.state.set(listening=True)
        self._emit_wake_status()

    def ptt_end(self) -> None:
        if not self._ptt_active:
            return
        self._ptt_active = False
        frames, self._ptt_frames = self._ptt_frames, []
        self.mode = Mode.WAKE
        self.state.set(listening=False)
        if self._ptt_temp_mic:
            self._ptt_temp_mic = False
            if not self.listening:
                self.mic.stop("paused" if self.enabled else "stopped")
        self._emit_wake_status()
        if frames:
            threading.Thread(target=self._finish_command, args=(frames, None, 0), daemon=True).start()

    # ------------------------------------------------------------------ segmenter state
    def _reset_segmenter(self) -> None:
        self.vad.reset()
        self.preroll: collections.deque = collections.deque(maxlen=int(0.35 / FRAME_S))
        self.in_speech = False
        self.speech_run = 0
        self.silence = 0
        self.buf: list[np.ndarray] = []
        self.checked = False
        self.wake_hit: ww.WakeMatch | None = None
        self._barge_mark = 0

    def _on_speaking(self, event, data) -> None:
        self.state.set(speaking=data["speaking"])
        if not data["speaking"] and self._followup > 0 and not self.assistant._active_turn:
            fu, self._followup = self._followup, 0.0
            self.open_capture_window(fu)          # optional follow-up window (voice.followup_window_s)

    # ------------------------------------------------------------------ main loop
    def _run(self) -> None:
        import queue as _q
        last_status = 0.0
        while self._running:
            now = time.time()
            if now - last_status > 1.0:
                last_status = now
                self._emit_wake_status()
            try:
                frame = self.mic.frames.get(timeout=0.25)
            except _q.Empty:
                continue
            if not (self.listening or self._ptt_active):
                continue
            try:
                self._process(frame)
            except Exception:
                log.exception("voice loop error")
                self._reset_segmenter()
                self.mode = Mode.WAKE
                self.state.set(listening=False)

    def _emit_level(self) -> None:
        now = time.time()
        if now - self._last_level_emit > 0.05:
            self._last_level_emit = now
            self.bus.emit("audio_level", mic=min(1.0, self.mic.level * 6), out=min(1.0, self.tts.out_level * 4))

    def _process(self, frame: np.ndarray) -> None:
        self._emit_level()
        if self.mode is Mode.PTT:
            self._ptt_frames.append(frame)
            if len(self._ptt_frames) * FRAME_S > float(self.settings.get("vad.max_utterance_s", 25)):
                self.ptt_end()
            return

        speaking = self.tts.is_speaking() or (time.time() - self.tts.last_end) < 0.4
        barge_in = bool(self.settings.get("voice.barge_in", True))
        if speaking and not barge_in and self.mode is Mode.WAKE:
            if self.in_speech or self.preroll:
                self._reset_segmenter()
            return        # echo prevention: deaf while she talks (barge-in disabled)
        if time.time() < self._cooldown_until and self.mode is Mode.WAKE and not self.in_speech:
            return

        vcfg = self.settings.section("vad")
        thr = float(vcfg.get("threshold", 0.5))
        if speaking and self.mode is Mode.WAKE:
            thr = min(0.9, thr + 0.25)          # harder to trigger on our own voice leaking into the mic
        p = self.vad.prob(frame)
        is_speech = p >= thr
        end_sil = int(float(vcfg.get("end_silence_ms", 800)) / 1000 / FRAME_S)
        min_start = 3

        if not self.in_speech:
            self.preroll.append(frame)
            if self.mode is Mode.CAPTURE and time.time() > self._capture_deadline:
                self._capture_timeout()
                return
            self.speech_run = self.speech_run + 1 if is_speech else 0
            if self.speech_run >= min_start:
                self.in_speech = True
                self.buf = list(self.preroll)
                self.silence = 0
                self.checked = False
                self.wake_hit = None
                if self.mode is Mode.CAPTURE:
                    self.state.set(listening=True)
            return

        self.buf.append(frame)
        self.silence = 0 if is_speech else self.silence + 1
        dur = len(self.buf) * FRAME_S
        ended = self.silence >= end_sil or dur >= float(vcfg.get("max_utterance_s", 25))

        if self.mode is Mode.WAKE and speaking and not ended and barge_in:
            # While she talks, her own (attenuated) voice forms one long "speech" segment, so look for the wake
            # phrase ANYWHERE with a sliding window instead of only at the start.
            if dur >= 1.2 and len(self.buf) - self._barge_mark >= int(1.0 / FRAME_S):
                self._barge_mark = len(self.buf)
                if self._barge_check():
                    return
        elif self.mode is Mode.WAKE and not self.checked and not ended:
            window = float(self.settings.get("wake_word.check_window_s", 3.5))
            if dur >= window:                       # long utterance: check the opening words early
                self.checked = True
                self._wake_check(final=False)
        if ended:
            self._segment_ended()

    # ------------------------------------------------------------------ segment end handling
    def _audio(self, frames: list[np.ndarray] | None = None) -> np.ndarray:
        fr = frames if frames is not None else self.buf
        return np.concatenate(fr) if fr else np.zeros(0, dtype=np.float32)

    def _segment_ended(self) -> None:
        frames = self.buf
        dur = len(frames) * FRAME_S - self.silence * FRAME_S
        was_hit = self.wake_hit
        mode = self.mode
        self._reset_segmenter()
        if dur < float(self.settings.get("vad.min_speech_ms", 250)) / 1000:
            return
        if mode is Mode.CAPTURE:
            barge, self._barge = self._barge, False
            self.state.set(listening=False)
            self.mode = Mode.WAKE
            self._emit_wake_status()
            self._finish_command(frames, None, 0, barge=barge)
            return
        if was_hit is not None:                      # wake phrase was detected early; this is the whole utterance
            self._accept_wake(was_hit, frames, already_checked=True)
            return
        if self.checked:
            return                                   # long utterance with no wake phrase: ambient speech, ignore
        self.buf = frames
        self._wake_check(final=True)
        self.buf = []

    def _wake_check(self, final: bool) -> None:
        """Transcribe the opening of the current segment with the tiny model and look for the phrase."""
        window = int(float(self.settings.get("wake_word.check_window_s", 3.5)) / FRAME_S)
        frames = self.buf if final else self.buf[:window]
        audio = self._audio(frames)
        if len(audio) < SAMPLE_RATE * 0.3:
            return
        self.stats["wake_checks"] += 1
        t0 = time.time()
        text = self.stt.transcribe_wake(audio)
        log.info("wake-check %.2fs (final=%s speaking=%s): %r", time.time() - t0, final, self.tts.is_speaking(), text)
        if not text:
            return
        # A pending confirmation can be answered with a bare yes/no (no wake word needed).
        if self.perms.has_pending() and self.settings.get("voice.confirm_by_voice", True):
            ans = ww.parse_yes_no(text)
            if ans is not None and not (self.tts.is_speaking() and ww.overlaps_spoken(text, self.tts.spoken_text())):
                self.tts.stop()
                self.perms.resolve(None, ans)
                self.bus.emit("heard", text=text, kind="confirmation")
                self._reset_segmenter()
                return
        m = ww.match_wake(text, float(self.settings.get("wake_word.sensitivity", 0.6)),
                          bool(self.settings.get("wake_word.allow_bare_name", True)),
                          str(self.settings.get("wake_word.phrase", "hey mei mei")))
        if not m.matched:
            return
        if self.tts.is_speaking() or time.time() - self.tts.last_end < 1.0:
            if ww.overlaps_spoken(text, self.tts.spoken_text()):
                self.stats["false_wakes_rejected"] += 1
                self.bus.emit("activity", level="info", text=f"Ignored own voice echo: “{text[:50]}”")
                self._reset_segmenter()
                return
        if final:
            self._accept_wake(m, self.buf, already_checked=False)
        else:
            self.wake_hit = m           # keep collecting until the speaker finishes the command

    def _barge_check(self) -> bool:
        """Sliding-window wake search while she is speaking. True if it interrupted her."""
        n = int(3.0 / FRAME_S)
        audio = self._audio(self.buf[-n:])
        self.stats["wake_checks"] += 1
        text = self.stt.transcribe_wake(audio)
        log.info("barge-check: %r", text)
        if not text:
            return False
        # While she is talking the bar is higher: the FULL phrase is required (never just "Mei Mei") and the
        # match must be stricter, because Whisper can hallucinate the primed phrase out of her own echo.
        sens = max(0.0, float(self.settings.get("wake_word.sensitivity", 0.6)) - 0.2)
        bare = False
        m = ww.find_wake(text, sens, bare, str(self.settings.get("wake_word.phrase", "hey mei mei")))
        if not m.matched:
            return False
        spoken = self.tts.spoken_text()
        if ww.spoken_contains_wake(spoken, sens, True):
            # Her own speech contains the phrase, so only accept if the words AFTER it are new (not her echo).
            if not m.remainder or ww.overlaps_spoken(m.remainder, spoken, 0.6):
                self.stats["false_wakes_rejected"] += 1
                self.bus.emit("activity", level="info", text=f"Ignored own-voice echo: “{text[:50]}”")
                return False
        self.stats["wakes"] += 1
        self.bus.emit("wake_detected", text=text, score=round(m.score, 2), barge_in=True)
        self.bus.emit("activity", level="info", text=f"Barge-in: “{text[:50]}” — stopping speech")
        self.tts.stop()
        self.assistant.interrupt()
        self.tts.chime("ack")
        self._cooldown_until = time.time() + float(self.settings.get("wake_word.cooldown_s", 1.0))
        # keep collecting the rest of what the user is saying, as a normal captured command
        self.buf = self.buf[-n:]
        self.mode = Mode.CAPTURE
        self._barge = True
        self._capture_deadline = time.time() + 30
        self.state.set(listening=True)
        self._emit_wake_status()
        return True

    def _accept_wake(self, m: ww.WakeMatch, frames: list[np.ndarray], already_checked: bool) -> None:
        self.stats["wakes"] += 1
        self.bus.emit("wake_detected", text=m.transcript, score=round(m.score, 2))
        self.bus.emit("activity", level="info", text=f"Wake phrase heard (“{m.transcript[:40]}”, score {m.score:.2f})")
        interrupting = self.tts.is_speaking() or self.assistant._active_turn
        if interrupting:                              # barge-in: cut her off and take the new instruction
            self.tts.stop()
            self.assistant.interrupt()
        self._cooldown_until = time.time() + float(self.settings.get("wake_word.cooldown_s", 1.0))
        inline = len(m.remainder.strip()) >= 2
        if inline:
            self.tts.chime("ack")
            self._finish_command(frames, m, m.words_consumed)
        else:
            self.tts.chime("wake")
            time.sleep(0.25)                          # wake detection is OFF while the chime plays
            self.mic.drain()
            self._reset_segmenter()
            self.mode = Mode.CAPTURE
            self._capture_deadline = time.time() + float(self.settings.get("vad.no_speech_timeout_s", 7))
            self.state.set(listening=True)
            self._emit_wake_status()

    def _capture_timeout(self) -> None:
        self.mode = Mode.WAKE
        self._reset_segmenter()
        self.state.set(listening=False)
        self.bus.emit("activity", level="info", text="No speech after wake word — back to waiting")
        self.tts.chime("error")
        self._emit_wake_status()

    # ------------------------------------------------------------------ command finalisation
    def _finish_command(self, frames: list[np.ndarray], wake: ww.WakeMatch | None, consumed: int, barge: bool = False) -> None:
        audio = self._audio(frames)
        if len(audio) < SAMPLE_RATE * 0.3:
            return
        self.state.set(processing=True)
        try:
            t0 = time.time()
            text = self.stt.transcribe(audio)
            log.info("STT %.2fs: %r", time.time() - t0, text)
        except Exception:
            log.exception("transcription failed")
            self.state.set(processing=False)
            self.state.error()
            return
        self.state.set(processing=False)
        if wake is not None or barge:
            text = ww.strip_wake(text, float(self.settings.get("wake_word.sensitivity", 0.6)),
                                 bool(self.settings.get("wake_word.allow_bare_name", True)),
                                 fallback_words=consumed, anywhere=barge)
        text = text.strip(" ,.-")
        if (wake is not None or barge) and len(text) < 2:
            self.tts.chime("wake")                 # only the wake phrase was said: now listen for the request
            self.open_capture_window(float(self.settings.get("vad.no_speech_timeout_s", 7)))
            return
        if len(text) < 2 or text.lower().strip(".!? ") in HALLUCINATIONS:
            self.bus.emit("activity", level="info", text=f"Heard nothing usable ({text!r})")
            if wake is None:
                self.tts.chime("error")
            return
        self.stats["commands"] += 1
        self.bus.emit("heard", text=text, kind="command")
        # A pending confirmation may also be answered by a captured utterance.
        if self.perms.has_pending() and (ans := ww.parse_yes_no(text)) is not None:
            self.perms.resolve(None, ans)
            return
        self.assistant.submit(text, source="voice", interrupt=True)
        fu = float(self.settings.get("voice.followup_window_s", 0))
        if fu > 0:
            self._followup = fu

    def apply_settings(self) -> None:
        """Called after the settings dialog changes microphone / sensitivity."""
        self.mic.restart()
