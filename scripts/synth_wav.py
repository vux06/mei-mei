#!/usr/bin/env python
"""Synthesize test speech with a Piper voice (default: a different voice than Mei Mei's own) -> 16 kHz float32 .npy/.wav.
Used only by the test-suite; downloads the test voice on first use."""
from __future__ import annotations

import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.download_models import ensure_piper_voice  # noqa: E402

TEST_VOICES = ["en_US-amy-medium", "en_US-ryan-medium", "en_GB-alan-medium"]
_cache: dict = {}


def synth(text: str, voice: str = "en_US-amy-medium", sr_out: int = 16000, speed: float = 1.0) -> np.ndarray:
    from piper import PiperVoice, SynthesisConfig
    if voice not in _cache:
        _cache[voice] = PiperVoice.load(ensure_piper_voice(voice))
    v = _cache[voice]
    pcm = b"".join(c.audio_int16_bytes for c in v.synthesize(text, SynthesisConfig(length_scale=1 / speed)))
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768
    src = v.config.sample_rate
    n = int(len(x) * sr_out / src)
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


def write_wav(path: str, x: np.ndarray, sr: int = 16000) -> None:
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes())


if __name__ == "__main__":
    out = synth(sys.argv[1], *(sys.argv[2:3]))
    write_wav(sys.argv[3] if len(sys.argv) > 3 else "out.wav", out)
