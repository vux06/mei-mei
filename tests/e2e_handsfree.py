#!/usr/bin/env python
"""End-to-end hands-free test over a REAL PipeWire capture path.

A virtual PipeWire sink acts as "the room": test speech (a Piper voice different from Mei Mei's) is played into it,
and Mei Mei's real MicStream captures that sink's monitor exactly like a microphone. Her own TTS is routed into the
same virtual room, so the self-trigger/echo path is exercised for real.

Run:  .venv/bin/python tests/e2e_handsfree.py        (stop the normal app first; takes ~2-3 minutes, uses Claude)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="meimei-e2e-"))
SINK = "meimei_test_room"        # the "room": what the microphone hears; test speech is played straight into it
SPK = "meimei_test_speakers"     # where Mei Mei's TTS goes; reaches the room attenuated (-12 dB) and delayed (40 ms)
AEC = os.environ.get("E2E_AEC", "0") == "1"

settings = json.loads((ROOT / "config" / "settings.json").read_text())
settings["audio"]["input_source"] = f"{SINK}.monitor"
settings["audio"]["output_sink"] = SPK
settings["audio"]["echo_cancel"] = AEC
(TMP / "settings.json").write_text(json.dumps(settings))
os.environ.update(MEIMEI_SETTINGS=str(TMP / "settings.json"), MEIMEI_DATA_DIR=str(TMP / "data"),
                  MEIMEI_SOCKET=str(TMP / "ctl.sock"), XDG_RUNTIME_DIR=os.environ.get("XDG_RUNTIME_DIR", "/run/user/1000"))

import logging  # noqa: E402
logging.basicConfig(level=logging.INFO, format="      %(name)s: %(message)s")
for n in ("meimei.stt", "meimei.events", "httpx", "claude_agent_sdk", "meimei.ipc", "meimei.assistant", "meimei.tools"):
    logging.getLogger(n).setLevel(logging.WARNING)
from voice import cuda_setup  # noqa: E402
cuda_setup.setup()
import numpy as np  # noqa: E402
from core.app import App  # noqa: E402
from scripts.synth_wav import synth, write_wav  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}", flush=True)
    return ok


def pactl(*a) -> str:
    return subprocess.run(["pactl", *a], capture_output=True, text=True).stdout.strip()


def build_room() -> list[str]:
    mods = [pactl("load-module", "module-null-sink", f"sink_name={SINK}", "sink_properties=device.description=MeiMeiTestRoom"),
            pactl("load-module", "module-null-sink", f"sink_name={SPK}", "sink_properties=device.description=MeiMeiTestSpeakers"),
            pactl("load-module", "module-loopback", f"source={SPK}.monitor", f"sink={SINK}", "latency_msec=40")]
    time.sleep(1.0)
    for blk in pactl("list", "sink-inputs").split("Sink Input #")[1:]:
        if "loopback" in blk.lower() and SINK in blk or f"Sink: " in blk and "loopback" in blk.lower():
            pactl("set-sink-input-volume", blk.split("\n")[0], "25%")
    return mods


def say_into_room(text: str, voice="en_US-amy-medium", speed=1.0, lead=0.3, tail=0.3) -> float:
    x = synth(text, voice, 16000, speed)
    x = np.concatenate([np.zeros(int(16000 * lead), np.float32), x * 0.6, np.zeros(int(16000 * tail), np.float32)])
    wav = TMP / "u.wav"
    write_wav(str(wav), x)
    subprocess.run(["paplay", "-d", SINK, str(wav)], capture_output=True)
    return len(x) / 16000


class Rec:
    def __init__(self, bus):
        self.ev: list[tuple[float, str, dict]] = []
        bus.on("*", lambda e, d: self.ev.append((time.time(), e, d)))

    def mark(self) -> int:
        return len(self.ev)

    def wait(self, name, since=0, timeout=30, pred=lambda d: True):
        t0 = time.time()
        while time.time() - t0 < timeout:
            for t, e, d in self.ev[since:]:
                if e == name and pred(d):
                    return t, d
            time.sleep(0.05)
        return None

    def count(self, name, since=0):
        return sum(1 for _, e, _ in self.ev[since:] if e == name)


def main() -> int:
    mods = build_room()
    print("virtual room modules:", mods, "| AEC:", AEC)
    app = None
    try:
        app = App(voice=True)
        rec = Rec(app.bus)
        t_start = time.time()
        app.start()
        app.ready.wait(60)
        t_w = time.time()
        while time.time() - t_w < 10 and app.voice and app.voice.wake_state() != "listening":
            time.sleep(0.1)
        check("startup: models loaded, wake service armed automatically", app.voice is not None and app.voice.wake_state() == "listening",
              f"({time.time() - t_start:.1f}s, wake={app.voice.wake_state() if app.voice else None}, mic={app.voice.mic.status if app.voice else None})")
        check("startup: STT on GPU", app.stt_info.get("device") == "cuda", str(app.stt_info))
        time.sleep(1.0)

        # ---- 1. two-step: wake phrase alone, then the request
        print("\n1) 'Hey Mei Mei' ... pause ... request")
        m = rec.mark(); t0 = time.time()
        say_into_room("Hey Mei Mei.")
        w = rec.wait("wake_detected", m, 10)
        check("wake phrase detected from the live mic stream", w is not None, f"(+{(w[0] - t0):.2f}s)" if w else "")
        cap = rec.wait("wake_status", m, 3, lambda d: d["state"] == "capturing")
        check("wake detection paused / capture mode entered after wake", cap is not None)
        say_into_room("What is the capital of France?", lead=0.2)
        h = rec.wait("heard", m, 15, lambda d: d.get("kind") == "command")
        check("request transcribed", h is not None and "france" in h[1]["text"].lower(), repr(h[1]["text"]) if h else "")
        a = rec.wait("assistant_text", m, 40)
        check("Claude answered", a is not None and "paris" in a[1]["text"].lower(), repr(a[1]["text"]) if a else "")
        sp = rec.wait("speaking", m, 15, lambda d: d["speaking"])
        check("TTS spoke the answer", sp is not None)
        rec.wait("speaking", m, 40, lambda d: not d["speaking"])
        time.sleep(1.0)
        check("returned to always-on wake detection", app.voice.wake_state() == "listening" and app.voice.mode.value == "wake",
              f"(state={app.voice.wake_state()}, mode={app.voice.mode.value})")
        check("UI state back to IDLE", app.state.current == "IDLE", app.state.current)

        # ---- 2. consecutive interaction without restart: wake + request in one breath, context carried over
        print("\n2) second interaction, inline command, follow-up depends on context")
        m = rec.mark(); t0 = time.time()
        say_into_room("Hey Mei Mei, how many people live there roughly?")
        h = rec.wait("heard", m, 15, lambda d: d.get("kind") == "command")
        check("inline wake+command transcribed (wake phrase stripped)", h is not None and h[1]["text"].lower().startswith("how many"), repr(h[1]["text"]) if h else "")
        a = rec.wait("assistant_text", m, 40)
        check("answer uses previous turn's context ('there' = Paris/France)", a is not None and any(k in a[1]["text"].lower() for k in ("million", "2.", "paris", "11", "12")), repr(a[1]["text"]) if a else "")
        rec.wait("speaking", m, 15, lambda d: not d["speaking"])
        time.sleep(1.0)
        print(f"   end-to-end latency (speech end → first reply text): ~{(a[0] - t0):.1f}s incl. speaking the request" if a else "")

        # ---- 3. barge-in
        print("\n3) interrupt while she is speaking")
        m = rec.mark()
        say_into_room("Hey Mei Mei, tell me a long story about a dragon who learns to code, at least ten sentences.")
        rec.wait("speaking", m, 60, lambda d: d["speaking"])
        time.sleep(1.5)
        was = app.tts.is_speaking()
        m2 = rec.mark(); t0 = time.time()
        say_into_room("Hey Mei Mei, stop.", lead=0.1)
        stopped = rec.wait("speaking", m2, 12, lambda d: not d["speaking"])
        check("was speaking when interrupted", was)
        check("barge-in stopped TTS", stopped is not None, f"(+{(stopped[0] - t0):.1f}s incl. the {2.0:.0f}s utterance)" if stopped else "")
        time.sleep(3)
        check("no overlapping/continuing speech after stop", not app.tts.is_speaking())
        check("wake service still armed afterwards", app.voice.wake_state() == "listening")

        # ---- 4. self-trigger resistance: her own TTS (containing the wake phrase) is heard by her own mic
        print("\n4) her own voice saying 'Hey Mei Mei' must not retrigger")
        app.assistant.interrupt().result(10); time.sleep(2)
        m = rec.mark(); wakes0 = app.voice.stats["wakes"]
        app.tts.speak("Hey Mei Mei. Hey Mei Mei. That is my name, Mei Mei, and I will say it a few more times, Mei Mei.", raw=True)
        rec.wait("speaking", m, 10, lambda d: d["speaking"])
        rec.wait("speaking", m, 30, lambda d: not d["speaking"])
        time.sleep(3)
        check("no self-triggered wake from own TTS", app.voice.stats["wakes"] == wakes0,
              f"(wakes {wakes0}->{app.voice.stats['wakes']}, echo rejections={app.voice.stats['false_wakes_rejected']})")

        # ---- 5. silence / noise / false wake
        print("\n5) silence, noise, false wake phrases")
        m = rec.mark(); wakes0 = app.voice.stats["wakes"]
        noise = (np.random.default_rng(0).normal(0, 0.08, 16000 * 4)).astype(np.float32)
        write_wav(str(TMP / "n.wav"), noise); subprocess.run(["paplay", "-d", SINK, str(TMP / "n.wav")], capture_output=True)
        say_into_room("Hey mom, can you pass the salt please.")
        say_into_room("I think the weather will be nice tomorrow.")
        time.sleep(1.5)
        check("noise and non-wake speech ignored", app.voice.stats["wakes"] == wakes0 and rec.count("heard", m) == 0)

        # ---- 6. wake then silence -> times out and re-arms
        print("\n6) wake word followed by silence")
        app.settings.set("vad.no_speech_timeout_s", 3, save=False)
        m = rec.mark()
        say_into_room("Hey Mei Mei.")
        rec.wait("wake_detected", m, 10)
        time.sleep(5.0)
        check("capture timed out and wake detection re-armed", app.voice.wake_state() == "listening" and app.voice.mode.value == "wake")

        # ---- 7. pause / resume / mic hot-unplug
        print("\n7) pause/resume, microphone disconnect/reconnect")
        app.voice.pause(); time.sleep(0.8)
        check("pause: mic released, wake state 'paused'", app.voice.wake_state() == "paused" and app.voice.mic.status == "paused", f"({app.voice.mic.status})")
        m = rec.mark(); say_into_room("Hey Mei Mei, what is two plus two?")
        time.sleep(2.0)
        check("nothing heard while paused", rec.count("wake_detected", m) == 0)
        app.voice.resume(); time.sleep(1.5)
        check("resume: listening again", app.voice.wake_state() == "listening" and app.voice.mic.status == "active")
        m_loss = rec.mark()
        for x in reversed(mods):
            pactl("unload-module", x)
        mods = []
        time.sleep(5)
        lost = [d for _, e, d in rec.ev[m_loss:] if e == "mic_status" and (d["status"] != "active" or "unavailable" in d.get("detail", ""))]
        check("mic loss detected and reported (status event)", bool(lost), f"({lost[-1] if lost else app.voice.mic.status})")
        mods = build_room()
        t0 = time.time()
        while time.time() - t0 < 25 and not (app.voice.mic.status == "active" and "unavailable" not in app.voice.mic.detail):
            time.sleep(0.3)
        check("mic reconnected to the preferred device automatically", app.voice.mic.status == "active" and "unavailable" not in app.voice.mic.detail, f"(after {time.time() - t0:.1f}s: {app.voice.mic.detail})")
        time.sleep(1.0)
        m = rec.mark(); say_into_room("Hey Mei Mei.")
        check("wake word works again after reconnection", rec.wait("wake_detected", m, 10) is not None)
        time.sleep(1); app.voice.mic.drain()
        print("\n8) push-to-talk fallback (works even while always-on listening is paused)")
        app.voice.pause(); time.sleep(0.8)
        m = rec.mark()
        app.voice.ptt_begin(); time.sleep(0.6)
        say_into_room("What is two plus three?", lead=0.1)
        app.voice.ptt_end()
        h = rec.wait("heard", m, 15, lambda d: d.get("kind") == "command")
        check("PTT captured the request without any wake word", h is not None and any(k in h[1]["text"].lower() for k in ("two plus three", "2 plus 3", "2+3")), repr(h[1]["text"]) if h else "")
        a = rec.wait("assistant_text", m, 40)
        check("PTT request answered", a is not None and any(k in a[1]["text"].lower() for k in ("5", "five")), repr(a[1]["text"]) if a else "")
        rec.wait("speaking", m, 20, lambda d: not d["speaking"]); time.sleep(1)
        check("PTT released the mic again while paused", app.voice.mic.status == "paused", app.voice.mic.status)
        app.voice.resume(); time.sleep(1.5)
        app.voice.disable(); time.sleep(0.8)
        check("disable: fully off, mic released", app.voice.wake_state() == "disabled" and app.voice.mic.status != "active")
        app.voice.enable()
    finally:
        if app:
            app.shutdown()
        for x in reversed(mods):
            pactl("unload-module", x)
    bad = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(bad)}/{len(results)} checks passed")
    for n, _, d in bad:
        print("  FAILED:", n, d)
    return 1 if bad else 0


if __name__ == "__main__":
    rc = main()
    os._exit(rc)
