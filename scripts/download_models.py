#!/usr/bin/env python
"""Download (only what is missing) the offline models: Whisper STT, Whisper wake-check model, Piper voice."""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.config import Settings  # noqa: E402
from core.paths import MODELS_DIR, ensure_dirs  # noqa: E402

PIPER_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"


def piper_voice_urls(voice: str) -> tuple[str, str]:
    lang, name, quality = voice.split("-", 2)[0], voice.split("-")[1], voice.split("-")[2]
    family = lang.split("_")[0]
    base = f"{PIPER_BASE}/{family}/{lang}/{name}/{quality}/{voice}"
    return base + ".onnx", base + ".onnx.json"


def ensure_piper_voice(voice: str, log=print) -> Path:
    d = MODELS_DIR / "piper"
    d.mkdir(parents=True, exist_ok=True)
    onnx, cfg = d / f"{voice}.onnx", d / f"{voice}.onnx.json"
    if onnx.exists() and cfg.exists() and onnx.stat().st_size > 1_000_000:
        return onnx
    u_onnx, u_cfg = piper_voice_urls(voice)
    for url, dest in ((u_cfg, cfg), (u_onnx, onnx)):
        log(f"  downloading {dest.name} ...")
        tmp = dest.with_suffix(dest.suffix + ".part")
        urllib.request.urlretrieve(url, tmp)
        tmp.replace(dest)
    return onnx


def ensure_whisper(name: str, log=print) -> str:
    from faster_whisper.utils import download_model
    d = MODELS_DIR / "whisper"
    d.mkdir(parents=True, exist_ok=True)
    log(f"  whisper '{name}' (cached after first download) ...")
    return download_model(name, cache_dir=str(d))


def main() -> int:
    ensure_dirs()
    s = Settings()
    print("Models directory:", MODELS_DIR)
    ensure_whisper(s.get("stt.model"))
    wake = s.get("wake_word.wake_model", "auto")
    from voice import cuda_setup
    if wake == "auto":
        wake = None if cuda_setup.cuda_available() else "tiny.en"     # GPU reuses the main model; CPU needs tiny
    if wake and wake != s.get("stt.model"):
        ensure_whisper(wake)
    print("Piper voice:", ensure_piper_voice(s.get("tts.voice")))
    print("All models present.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
