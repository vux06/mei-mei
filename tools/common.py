"""Shared helpers for desktop tools: clean subprocess env, async command runner, result shaping."""
from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path


def clean_env() -> dict[str, str]:
    """Environment for launched programs: no venv / Qt-plugin leakage from Mei Mei's own process."""
    env = dict(os.environ)
    for k in list(env):
        if k.startswith(("QT_PLUGIN", "QML", "VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH")) or k == "LD_LIBRARY_PATH":
            env.pop(k, None)
    env.setdefault("YDOTOOL_SOCKET", f"/run/user/{os.getuid()}/.ydotool_socket")
    venv_bin = str(Path(__file__).resolve().parent.parent / ".venv" / "bin")
    env["PATH"] = ":".join(p for p in env.get("PATH", "").split(":") if p != venv_bin)
    return env


def run(cmd: list[str] | str, timeout: float = 15, shell: bool = False, cwd: str | None = None,
        input_bytes: bytes | None = None) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, shell=shell, cwd=cwd,
                           env=clean_env(), input=input_bytes)
        return p.returncode, p.stdout.decode(errors="replace"), p.stderr.decode(errors="replace")
    except subprocess.TimeoutExpired as e:
        return 124, (e.stdout or b"").decode(errors="replace"), f"timed out after {timeout}s"
    except FileNotFoundError as e:
        return 127, "", f"command not found: {e.filename}"


async def arun(cmd, timeout: float = 15, **kw) -> tuple[int, str, str]:
    return await asyncio.to_thread(run, cmd, timeout, **kw)


def spawn_detached(cmd: list[str] | str, shell: bool = False) -> int:
    """Start a GUI program that outlives Mei Mei."""
    p = subprocess.Popen(cmd, shell=shell, env=clean_env(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    return p.pid


def clip(text: str, limit: int = 6000) -> str:
    if len(text) <= limit:
        return text
    return text[: limit // 2] + f"\n…[{len(text) - limit} chars truncated]…\n" + text[-limit // 2:]
