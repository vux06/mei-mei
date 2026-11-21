"""Terminal command execution (always via bash -c, with timeout and output cap). Gating is done by the caller."""
from __future__ import annotations

import os
import signal
import subprocess

from .common import clean_env, clip


def execute(command: str, cwd: str | None = None, timeout: float = 30) -> dict:
    cwd = os.path.expanduser(cwd) if cwd else os.path.expanduser("~")
    try:
        p = subprocess.Popen(["bash", "-lc", command], cwd=cwd, env=clean_env(), stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        try:
            out, err = p.communicate(timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            out, err = p.communicate()
            timed_out = True
    except OSError as e:
        return {"exit_code": -1, "stdout": "", "stderr": str(e), "timed_out": False}
    return {"exit_code": p.returncode, "timed_out": timed_out,
            "stdout": clip(out.decode(errors="replace")), "stderr": clip(err.decode(errors="replace"), 2000)}
