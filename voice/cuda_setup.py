"""Make CTranslate2 find CUDA 12 libs (cuBLAS/cuDNN) from the venv's nvidia-* wheels.

The system CUDA is 13.x, which the CTranslate2 wheel cannot use, so we preload the 12.x runtime libraries that
ship as pip wheels. Safe to call repeatedly; silently does nothing if the wheels are absent (CPU fallback)."""
from __future__ import annotations

import ctypes
import glob
import os
import site
import sys

_done = False


def setup() -> bool:
    global _done
    if _done:
        return True
    roots = site.getsitepackages() + [os.path.join(sys.prefix, "lib", f"python{sys.version_info.major}.{sys.version_info.minor}", "site-packages")]
    loaded = 0
    order = ["cublas/lib/libcublasLt.so*", "cublas/lib/libcublas.so*", "cudnn/lib/libcudnn.so*"]
    for root in dict.fromkeys(roots):
        base = os.path.join(root, "nvidia")
        if not os.path.isdir(base):
            continue
        for pat in order:
            for lib in sorted(glob.glob(os.path.join(base, pat))):
                try:
                    ctypes.CDLL(lib, mode=ctypes.RTLD_GLOBAL)
                    loaded += 1
                    break
                except OSError:
                    continue
        # cuDNN sub-libraries are dlopened lazily by libcudnn; make them discoverable too
        extra = ":".join(glob.glob(os.path.join(base, "*", "lib")))
        os.environ["LD_LIBRARY_PATH"] = extra + ":" + os.environ.get("LD_LIBRARY_PATH", "")
    _done = loaded > 0
    return _done


def cuda_available() -> bool:
    setup()
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False
