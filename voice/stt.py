"""Offline speech-to-text with faster-whisper (CTranslate2). GPU (CUDA 12 via pip wheels) with CPU fallback."""
from __future__ import annotations

import logging
import re
import threading
import time

import numpy as np

from core.paths import MODELS_DIR
from . import cuda_setup

log = logging.getLogger("meimei.stt")
# Whisper tends to hallucinate these on near-silence.
HALLUCINATIONS = {"thank you", "thanks for watching", "thank you for watching", "you", "bye", "okay", "the end",
                  "thanks", "so", "uh", "um", "hmm", "mm", "mhm", "oh", "i", "yeah"}


def clean_text(t: str) -> str:
    t = t.strip()
    t = re.sub(r"\[[^\]]*\]|\([^)]*\)", "", t)      # [MUSIC], (sighs)
    return re.sub(r"\s+", " ", t).strip()


class STT:
    def __init__(self, settings):
        self.settings = settings
        self.main = None
        self.wake = None
        self.device = "cpu"
        self.compute = "int8"
        self.load_seconds = 0.0
        self._lock = threading.Lock()       # CTranslate2 models are used from the voice thread only, but be safe
        self._wake_lock = threading.Lock()

    def load(self) -> dict:
        from faster_whisper import WhisperModel
        t0 = time.time()
        cfg = self.settings.section("stt")
        want = cfg.get("device", "auto")
        gpu_ok = want in ("auto", "cuda") and cuda_setup.cuda_available()
        name = cfg.get("model", "small.en")
        root = str(MODELS_DIR / "whisper")
        self.main = None
        if gpu_ok:
            for compute in ([cfg["compute_type"]] if cfg.get("compute_type", "auto") != "auto" else ["float16", "int8_float16"]):
                try:
                    m = WhisperModel(name, device="cuda", compute_type=compute, download_root=root)
                    list(m.transcribe(np.zeros(16000, dtype=np.float32), language="en")[0])   # prove cuDNN/cuBLAS work
                    self.main, self.device, self.compute = m, "cuda", compute
                    break
                except Exception as e:
                    log.warning("GPU load failed (%s, %s): %s", name, compute, e)
        if self.main is None:
            compute = cfg.get("compute_type", "auto")
            compute = "int8" if compute in ("auto", "float16", "int8_float16") else compute
            self.main = WhisperModel(name, device="cpu", compute_type=compute, cpu_threads=6, download_root=root)
            self.device, self.compute = "cpu", compute
        wname = self.settings.get("wake_word.wake_model", "auto")
        if wname in ("auto", name) and self.device == "cuda":
            # Measured best: the main model on the GPU is both faster and more accurate than a CPU tiny model
            # (30/30 recall, 0 false positives on the synthetic eval incl. 8 dB noise) and leaves the CPU idle.
            self.wake, wname = self.main, f"{name} (shared, GPU)"
        else:   # CPU-only machine: a tiny model keeps the always-on check cheap
            wname = "tiny.en" if wname == "auto" else wname
            self.wake = WhisperModel(wname, device="cpu", compute_type="int8", cpu_threads=3, download_root=root)
        self.load_seconds = time.time() - t0
        info = {"model": name, "device": self.device, "compute": self.compute, "wake_model": wname,
                "load_seconds": round(self.load_seconds, 1)}
        log.info("STT ready: %s", info)
        return info

    def _run(self, model, audio: np.ndarray, prompt: str | None, beam: int) -> str:
        segs, _ = model.transcribe(audio, language=self.settings.get("stt.language", "en"), beam_size=beam,
                                   initial_prompt=prompt, condition_on_previous_text=False, without_timestamps=True,
                                   vad_filter=False, temperature=0.0, no_speech_threshold=0.6,
                                   log_prob_threshold=-1.0, compression_ratio_threshold=2.4)
        return clean_text(" ".join(s.text for s in segs))

    def transcribe(self, audio: np.ndarray, prompt: str | None = "Hey Mei Mei.") -> str:
        with self._lock:
            return self._run(self.main, audio, prompt, int(self.settings.get("stt.beam_size", 3)))

    def transcribe_wake(self, audio: np.ndarray) -> str:
        with self._wake_lock:
            return self._run(self.wake, audio, "Hey Mei Mei.", 1)
