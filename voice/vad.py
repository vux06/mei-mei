"""Streaming Silero VAD (ONNX, CPU) using the model bundled with faster-whisper."""
from __future__ import annotations

import os

import numpy as np
import onnxruntime as ort

import faster_whisper

CONTEXT = 64


class StreamingVAD:
    def __init__(self):
        path = os.path.join(os.path.dirname(faster_whisper.__file__), "assets", "silero_vad_v6.onnx")
        so = ort.SessionOptions()
        so.inter_op_num_threads = so.intra_op_num_threads = 1
        so.log_severity_level = 4
        self.session = ort.InferenceSession(path, providers=["CPUExecutionProvider"], sess_options=so)
        self.reset()

    def reset(self) -> None:
        self.h = np.zeros((1, 1, 128), dtype=np.float32)
        self.c = np.zeros((1, 1, 128), dtype=np.float32)
        self.context = np.zeros(CONTEXT, dtype=np.float32)

    def prob(self, frame: np.ndarray) -> float:
        """frame: 512 float32 samples @16 kHz -> speech probability."""
        x = np.concatenate([self.context, frame])[None, :].astype(np.float32)
        out, self.h, self.c = self.session.run(None, {"input": x, "h": self.h, "c": self.c})
        self.context = frame[-CONTEXT:]
        return float(out.reshape(-1)[0])
