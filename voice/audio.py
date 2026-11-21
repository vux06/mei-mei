"""Microphone capture with device selection and automatic reconnection (PipeWire/Pulse via PortAudio).

The stream always runs locally; frames go to an in-process queue only. Nothing is written to disk.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import threading
import time

import numpy as np
import sounddevice as sd

log = logging.getLogger("meimei.audio")
SAMPLE_RATE = 16000
FRAME = 512            # 32 ms at 16 kHz — the size Silero VAD expects


def list_input_sources(include_monitors: bool = False) -> list[dict]:
    """PipeWire/Pulse capture sources (monitors excluded by default): [{name, description, default}]."""
    try:
        out = subprocess.run(["pactl", "-f", "json", "list", "sources"], capture_output=True, timeout=4).stdout
        default = subprocess.run(["pactl", "get-default-source"], capture_output=True, timeout=4).stdout.decode().strip()
        res = []
        for s in json.loads(out):
            if not include_monitors and (s.get("monitor_of_sink") is not None or ".monitor" in s["name"]
                                         or s["name"].startswith("ii_combine")):
                continue
            res.append({"name": s["name"], "description": s.get("description", s["name"]), "default": s["name"] == default})
        return res
    except Exception:
        return []


def list_output_sinks() -> list[dict]:
    try:
        out = subprocess.run(["pactl", "-f", "json", "list", "sinks"], capture_output=True, timeout=4).stdout
        return [{"name": s["name"], "description": s.get("description", s["name"])} for s in json.loads(out)]
    except Exception:
        return []


def _pactl(*args: str, timeout: float = 5) -> str:
    try:
        return subprocess.run(["pactl", *args], capture_output=True, timeout=timeout, text=True).stdout.strip()
    except Exception:
        return ""


def default_source() -> str:
    return _pactl("get-default-source")


def default_sink() -> str:
    return _pactl("get-default-sink")


class AudioRouter:
    """Optional acoustic echo cancellation using PipeWire's WebRTC AEC (module-echo-cancel).

    Mei Mei's speech is played into a virtual sink that doubles as the AEC reference; her microphone is the
    echo-cancelled virtual source. That is what lets you interrupt her mid-sentence without her own voice being
    heard as a wake word. The module is created only while Mei Mei runs (and unloaded on exit); it never changes
    your default devices."""
    SRC, SINK = "meimei_ec_source", "meimei_ec_sink"

    def __init__(self):
        self.ec_source: str | None = None
        self.ec_sink: str | None = None
        self.module_id: str | None = None
        self.master_source = self.master_sink = ""
        self.error = ""

    def active(self) -> bool:
        return bool(self.ec_source)

    @classmethod
    def cleanup_stale(cls) -> int:
        """Unload echo-cancel modules left behind by a crashed earlier run (only ones carrying OUR names)."""
        n = 0
        for ln in _pactl("list", "short", "modules").splitlines():
            parts = ln.split("\t")
            if len(parts) > 1 and parts[1] == "module-echo-cancel" and f"source_name={cls.SRC}" in ln:
                _pactl("unload-module", parts[0])
                n += 1
        return n

    def setup(self, settings) -> bool:
        if not settings.get("audio.echo_cancel", True):
            self.teardown()
            return False
        src = settings.get("audio.input_source") or default_source()
        snk = settings.get("audio.output_sink") or default_sink()
        names = {s["name"] for s in list_input_sources(include_monitors=True)}
        if not src or src not in names or not snk:
            self.error = "no usable master devices"
            return False
        if self.ec_source and self.master_source == src and self.master_sink == snk and self._loaded():
            return True
        self.teardown()
        self.cleanup_stale()
        out = _pactl("load-module", "module-echo-cancel", "aec_method=webrtc", f"source_master={src}", f"sink_master={snk}",
                     f"source_name={self.SRC}", f"sink_name={self.SINK}",
                     'aec_args="analog_gain_control=0 digital_gain_control=1"', "use_master_format=1")
        if not out.isdigit():
            self.error = "module-echo-cancel failed to load"
            return False
        self.module_id, self.master_source, self.master_sink = out, src, snk
        self.ec_source, self.ec_sink = self.SRC, self.SINK
        return True

    def _loaded(self) -> bool:
        return self.module_id is not None and any(ln.split("\t")[0] == self.module_id
                                                  for ln in _pactl("list", "short", "modules").splitlines())

    def healthy(self) -> bool:
        """False if the devices the AEC was built on have disappeared (e.g. a USB mic was unplugged)."""
        if not self.ec_source:
            return True
        srcs = {s["name"] for s in list_input_sources(include_monitors=True)}
        sinks = {s["name"] for s in list_output_sinks()}
        return self._loaded() and self.master_source in srcs and self.master_sink in sinks and self.SRC in srcs

    def teardown(self) -> None:
        if self.module_id:
            _pactl("unload-module", self.module_id)
        self.module_id = self.ec_source = self.ec_sink = None
        self.cleanup_stale()


router = AudioRouter()


def _pulse_device() -> int | str | None:
    for i, d in enumerate(sd.query_devices()):
        if d["name"] == "pulse" and d["max_input_channels"] > 0:
            return i
    for i, d in enumerate(sd.query_devices()):
        if d["name"] == "pipewire" and d["max_input_channels"] > 0:
            return i
    return None


class HighPass:
    """2nd-order Butterworth high-pass (default 120 Hz) — removes rumble/DC so VAD and Whisper see speech only."""

    def __init__(self, fc: float = 120.0, fs: int = SAMPLE_RATE):
        import math
        w0 = 2 * math.pi * fc / fs
        alpha = math.sin(w0) / (2 * 0.7071)
        cw = math.cos(w0)
        a0 = 1 + alpha
        self.b0, self.b1, self.b2 = (1 + cw) / 2 / a0, -(1 + cw) / a0, (1 + cw) / 2 / a0
        self.a1, self.a2 = -2 * cw / a0, (1 - alpha) / a0
        self.z1 = self.z2 = 0.0

    def __call__(self, x: np.ndarray) -> np.ndarray:
        out = np.empty_like(x)
        b0, b1, b2, a1, a2, z1, z2 = self.b0, self.b1, self.b2, self.a1, self.a2, self.z1, self.z2
        for i, v in enumerate(x.tolist()):          # transposed direct form II
            y = b0 * v + z1
            z1 = b1 * v - a1 * y + z2
            z2 = b2 * v - a2 * y
            out[i] = y
        self.z1, self.z2 = z1, z2
        return out


class MicStream:
    """Supervised input stream. `frames` yields float32 arrays of FRAME samples at 16 kHz."""

    def __init__(self, settings):
        self.settings = settings
        self.frames: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=600)   # ~19 s of backlog
        self.status = "stopped"       # stopped | active | reconnecting | no-device | paused
        self.detail = ""
        self.level = 0.0
        self.on_status = None
        self._stream: sd.InputStream | None = None
        self._want = False
        self._last_frame = 0.0
        self._cb_error = ""
        self._lock = threading.Lock()
        self._buf = np.zeros(0, dtype=np.float32)
        self._hp = HighPass()
        self._opened: str | None = None
        self._last_check = 0.0
        self._opened_default = False
        self._thread: threading.Thread | None = None

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._want:
                return
            self._want = True
            self._thread = threading.Thread(target=self._supervise, name="mic-supervisor", daemon=True)
            self._thread.start()

    def stop(self, status: str = "stopped") -> None:
        with self._lock:
            self._want = False
        self._close()
        self._set_status(status)

    def _set_status(self, status: str, detail: str = "") -> None:
        if status != self.status or detail != self.detail:
            self.status, self.detail = status, detail
            if self.on_status:
                try:
                    self.on_status(status, detail)
                except Exception:
                    log.exception("mic status callback")

    def _close(self) -> None:
        s, self._stream = self._stream, None
        if s is not None:
            try:
                s.abort(ignore_errors=True)
                s.close(ignore_errors=True)
            except Exception:
                pass

    # -- capture -------------------------------------------------------------
    def _callback(self, indata, frames, time_info, status):
        if status:
            self._cb_error = str(status)
        self._last_frame = time.monotonic()
        x = indata[:, 0].astype(np.float32)
        x = self._hp(x) * float(self.settings.get("audio.input_gain", 1.0))
        np.clip(x, -1.0, 1.0, out=x)
        self.level = 0.6 * self.level + 0.4 * float(np.sqrt(np.mean(x * x)))
        self._buf = np.concatenate([self._buf, x]) if self._buf.size else x
        while self._buf.size >= FRAME:
            fr, self._buf = self._buf[:FRAME], self._buf[FRAME:]
            try:
                self.frames.put_nowait(fr)
            except queue.Full:
                try:
                    self.frames.get_nowait()
                    self.frames.put_nowait(fr)
                except queue.Empty:
                    pass

    def _open(self) -> bool:
        wanted = self.settings.get("audio.input_source")
        if self.settings.get("audio.echo_cancel", True):
            router.setup(self.settings)
        sources = {s["name"]: s for s in list_input_sources(include_monitors=True)}
        if not sources:
            self._set_status("no-device", "no capture devices found")
            return False
        detail = "system default"
        self._opened = None
        self._opened_default = False
        if router.active() and router.ec_source in sources:
            os.environ["PULSE_SOURCE"] = router.ec_source
            self._opened = router.ec_source
            detail = "echo-cancelled · " + (sources.get(router.master_source, {}).get("description") or router.master_source)
        elif wanted:
            if wanted in sources:
                os.environ["PULSE_SOURCE"] = wanted
                self._opened = wanted
                detail = sources[wanted]["description"]
            else:
                os.environ.pop("PULSE_SOURCE", None)
                detail = f"'{wanted}' unavailable, using system default"
        else:
            os.environ.pop("PULSE_SOURCE", None)
            d = next((s for s in sources.values() if s["default"]), None)
            detail = d["description"] if d else detail
            self._opened = d["name"] if d else None
            self._opened_default = True
        dev = _pulse_device()
        try:
            self._buf = np.zeros(0, dtype=np.float32)
            self._hp = HighPass()
            self._cb_error = ""
            self._stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", device=dev,
                                          blocksize=FRAME, callback=self._callback, latency="low")
            self._stream.start()
            self._last_frame = time.monotonic()
            self._set_status("active", detail)
            return True
        except Exception as e:
            log.warning("mic open failed: %s", e)
            self._close()
            self._set_status("reconnecting", f"open failed: {e}")
            return False

    def _supervise(self) -> None:
        backoff = 1.0
        while self._want:
            if self._stream is None:
                if self._open():
                    backoff = 1.0
                else:
                    time.sleep(backoff)
                    backoff = min(backoff * 1.5, 5.0)
                    continue
            time.sleep(0.5)
            stale = time.monotonic() - self._last_frame > 2.0
            gone = False
            if time.monotonic() - self._last_check > 1.5:           # cheap device-graph checks
                self._last_check = time.monotonic()
                present = {s["name"] for s in list_input_sources(include_monitors=True)}
                wanted = self.settings.get("audio.input_source")
                if self._opened and self._opened not in present:
                    gone = True                                       # the device we opened vanished
                elif router.active() and not router.healthy():
                    gone = True                                       # the AEC's master device vanished
                elif self._opened_default and not router.active() and default_source() != self._opened:
                    gone = True                                       # system default changed: follow it
                elif wanted and self.detail.endswith("using system default") and wanted in present:
                    gone = True                                       # the preferred mic is back
            if self._stream is not None and (stale or not self._stream.active or gone):
                log.warning("mic stream stalled/disconnected (stale=%s gone=%s) — reopening", stale, gone)
                self._close()
                if gone and router.active() and not router.healthy():
                    router.teardown()
                self._set_status("reconnecting", "microphone lost, retrying")
        self._close()

    def drain(self) -> None:
        try:
            while True:
                self.frames.get_nowait()
        except queue.Empty:
            pass

    def restart(self) -> None:
        """Re-open (e.g. after the user picked another microphone)."""
        self._close()      # supervisor notices and re-opens with the new setting
