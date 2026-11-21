"""Offline text-to-speech with Piper. Queue-managed, never overlapping, interruptible within ~50 ms."""
from __future__ import annotations

import collections
import logging
import os
import queue
import re
import threading
import time

import numpy as np
import sounddevice as sd

from core.paths import MODELS_DIR
from .audio import _pulse_device, router

log = logging.getLogger("meimei.tts")

_CODE_BLOCK = re.compile(r"```.*?```", re.S)
_LINK = re.compile(r"\[([^\]]+)\]\((?:[^)]+)\)")
_URL = re.compile(r"https?://\S+")
_EMOJI = re.compile("[\U00010000-\U0010ffff☀-➿️]")
_SENT = re.compile(r"(?<=[.!?…])\s+(?=[A-Z0-9\"'(])|\n+")


def prepare_speech(text: str, max_chars: int = 420) -> str:
    """Make model output speakable: drop code/markdown/URLs, and keep long replies short."""
    t = _CODE_BLOCK.sub(" (code is on screen) ", text)
    t = _LINK.sub(r"\1", t)
    t = _URL.sub("a link", t)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"^[#>\-*•\s]+", "", t, flags=re.M)
    t = re.sub(r"[*_~|]{1,3}", "", t)
    t = _EMOJI.sub("", t)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > max_chars:
        cut = t[:max_chars]
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        t = (cut[: end + 1] if end > max_chars * 0.4 else cut.rsplit(" ", 1)[0]) + " The rest is on screen."
    return t


class TTSEngine:
    def __init__(self, settings, bus):
        self.settings, self.bus = settings, bus
        self.voice = None
        self.sample_rate = 22050
        self._text_q: "queue.Queue[tuple[int, str]]" = queue.Queue()
        self._pcm_q: "queue.Queue[tuple[int, bytes | None]]" = queue.Queue(maxsize=8)
        self._gen = 0
        self._gen_lock = threading.Lock()
        self._stream: sd.RawOutputStream | None = None
        self._pending = 0                 # sentences queued or playing
        self._pend_lock = threading.Lock()
        self.last_end = 0.0
        self.recent: collections.deque = collections.deque(maxlen=12)   # (time, text) of what we said
        self.out_level = 0.0
        self.muted = False
        self._idle = threading.Event()
        self._idle.set()
        self._stop_evt = threading.Event()
        self.speaking = False

    # -- setup ---------------------------------------------------------------
    def load(self) -> None:
        from piper import PiperVoice
        from scripts.download_models import ensure_piper_voice
        path = ensure_piper_voice(self.settings.get("tts.voice"))
        self.voice = PiperVoice.load(path)
        self.sample_rate = self.voice.config.sample_rate
        threading.Thread(target=self._synth_loop, name="tts-synth", daemon=True).start()
        threading.Thread(target=self._play_loop, name="tts-play", daemon=True).start()
        log.info("TTS ready (%s @ %d Hz)", self.settings.get("tts.voice"), self.sample_rate)

    # -- public API ----------------------------------------------------------
    def is_speaking(self) -> bool:
        return self._pending > 0 or self.speaking

    def speak(self, text: str, interrupt: bool = False, raw: bool = False) -> None:
        if not self.settings.get("tts.enabled", True) or self.voice is None or self.muted:
            return
        if interrupt:
            self.stop()
        clean = text if raw else prepare_speech(text, int(self.settings.get("tts.max_spoken_chars", 100000)))
        if not clean:
            return
        self.recent.append((time.time(), clean))
        sentences = [s.strip() for s in _SENT.split(clean) if s.strip()]
        # merge very short fragments so the voice has natural phrasing
        merged: list[str] = []
        for s in sentences:
            if merged and len(merged[-1]) < 25:
                merged[-1] += " " + s
            else:
                merged.append(s)
        with self._gen_lock:
            gen = self._gen
        with self._pend_lock:
            self._pending += len(merged)
            self._idle.clear()
        for s in merged:
            self._text_q.put((gen, s))

    def stop(self) -> None:
        """Interrupt immediately: drop queued sentences and cut the audio that is playing."""
        with self._gen_lock:
            self._gen += 1
        for q in (self._text_q, self._pcm_q):
            try:
                while True:
                    q.get_nowait()
            except queue.Empty:
                pass
        with self._pend_lock:
            self._pending = 0
        st = self._stream
        if st is not None:
            try:
                st.abort(ignore_errors=True)
            except Exception:
                pass
        self._stop_evt.set()
        self._set_speaking(False)
        self._idle.set()

    def wait_idle(self, timeout: float | None = None) -> bool:
        return self._idle.wait(timeout)

    def spoken_text(self, window_s: float = 20.0) -> str:
        now = time.time()
        return " ".join(t for ts, t in self.recent if now - ts < window_s)

    def chime(self, kind: str = "wake") -> None:
        """Short non-blocking tone (activation / acknowledgement / error)."""
        if not self.settings.get("wake_word.activation_sound", True) and kind == "wake":
            return
        tones = {"wake": [(880, 0.07), (1320, 0.09)], "ack": [(1100, 0.05)], "error": [(440, 0.1), (330, 0.14)]}[kind]
        sr = 24000
        parts = []
        for f, d in tones:
            t = np.arange(int(sr * d)) / sr
            env = np.minimum(1, np.minimum(t / 0.008, (d - t) / 0.02))
            parts.append(0.28 * np.sin(2 * np.pi * f * t) * env)
        pcm = (np.concatenate(parts) * 32767 * float(self.settings.get("tts.volume", 0.9))).astype(np.int16)
        threading.Thread(target=self._play_blocking, args=(pcm.tobytes(), sr), daemon=True).start()

    # -- internals -------------------------------------------------------------
    def _set_speaking(self, v: bool) -> None:
        if v != self.speaking:
            self.speaking = v
            if not v:
                self.last_end = time.time()
                self.out_level = 0.0
            self.bus.emit("speaking", speaking=v)

    def _synth_loop(self) -> None:
        from piper import SynthesisConfig
        while True:
            gen, text = self._text_q.get()
            if gen != self._gen:
                continue
            speed = max(0.5, min(2.0, float(self.settings.get("tts.speed", 1.0))))
            cfg = SynthesisConfig(length_scale=1.0 / speed, volume=float(self.settings.get("tts.volume", 0.9)))
            try:
                for chunk in self.voice.synthesize(text, syn_config=cfg):
                    if gen != self._gen:
                        break
                    self._pcm_q.put((gen, chunk.audio_int16_bytes))
            except Exception:
                log.exception("synthesis failed")
            self._pcm_q.put((gen, None))      # sentence boundary marker

    def _open_stream(self, sr: int) -> sd.RawOutputStream:
        sink = router.ec_sink if router.active() else self.settings.get("audio.output_sink")
        if sink:
            os.environ["PULSE_SINK"] = sink
        else:
            os.environ.pop("PULSE_SINK", None)
        out = None
        for i, d in enumerate(sd.query_devices()):
            if d["name"] == "pulse" and d["max_output_channels"] > 0:
                out = i
                break
        st = sd.RawOutputStream(samplerate=sr, channels=1, dtype="int16", device=out, latency="low")
        st.start()
        return st

    def _play_loop(self) -> None:
        while True:
            gen, pcm = self._pcm_q.get()
            if gen != self._gen:
                continue
            if pcm is None:                       # one sentence done
                with self._pend_lock:
                    self._pending = max(0, self._pending - 1)
                    if self._pending == 0:
                        self._set_speaking(False)
                        self._idle.set()
                continue
            self._stop_evt.clear()
            try:
                if self._stream is None or not self._stream.active:
                    self._stream = self._open_stream(self.sample_rate)
                self._set_speaking(True)
                arr = np.frombuffer(pcm, dtype=np.int16)
                step = 1024
                for i in range(0, len(arr), step):
                    if gen != self._gen or self._stop_evt.is_set():
                        break
                    block = arr[i:i + step]
                    self.out_level = float(np.sqrt(np.mean((block.astype(np.float32) / 32768) ** 2)))
                    self._stream.write(block.tobytes())
            except Exception as e:
                if gen == self._gen:
                    log.warning("playback error: %s", e)
                    self._stream = None

    def _play_blocking(self, pcm: bytes, sr: int) -> None:
        try:
            st = self._open_stream(sr)
            st.write(pcm)
            time.sleep(0.05)
            st.stop()
            st.close()
        except Exception:
            log.debug("chime failed", exc_info=True)
