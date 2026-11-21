"""Filesystem tools. Pure functions; permission gating happens in the tool layer (tools/registry.py)."""
from __future__ import annotations

import fnmatch
import mimetypes
import os
import shutil
import stat
import time
from collections import defaultdict
from pathlib import Path

from core.permissions import is_secret_path
from .common import run

HOME = Path.home()
MAX_READ = 200_000
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".cache", ".venv", "venv", ".npm", ".cargo", ".rustup",
             ".local/share/Trash", ".mozilla", ".config/chromium", ".config/BraveSoftware", "proc", "sys"}

CATEGORY = {
    "Images": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".heic", ".avif"},
    "Documents": {".pdf", ".doc", ".docx", ".odt", ".txt", ".md", ".rtf", ".epub", ".xls", ".xlsx", ".ods", ".ppt", ".pptx", ".csv"},
    "Archives": {".zip", ".tar", ".gz", ".xz", ".bz2", ".7z", ".rar", ".zst"},
    "Audio": {".mp3", ".flac", ".wav", ".ogg", ".m4a", ".opus"},
    "Video": {".mp4", ".mkv", ".webm", ".avi", ".mov"},
    "Code": {".py", ".js", ".ts", ".c", ".cpp", ".h", ".rs", ".go", ".java", ".sh", ".lua", ".json", ".toml", ".yaml", ".yml"},
    "Installers": {".AppImage", ".deb", ".rpm", ".pkg", ".exe", ".msi", ".iso"},
}


def expand(p: str) -> Path:
    return Path(os.path.expandvars(p)).expanduser()


def resolve(p: str) -> Path:
    path = expand(p)
    if not path.is_absolute():
        path = HOME / path
    return path


def check_readable(path: Path) -> None:
    if is_secret_path(path):
        raise PermissionError(f"{path} looks like a secret/credential file; refusing to read it")


def list_dir(path: str, limit: int = 100) -> list[dict]:
    p = resolve(path)
    out = []
    for e in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))[:limit]:
        try:
            st = e.lstat()
        except OSError:
            continue
        out.append({"name": e.name + ("/" if e.is_dir() else ""), "size": st.st_size,
                    "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))})
    return out


def search(pattern: str, root: str = "~", limit: int = 40, kind: str = "any") -> list[str]:
    """Filename search (glob or substring) under root. Skips caches, VCS dirs and secret locations."""
    base = resolve(root)
    pat = pattern if any(c in pattern for c in "*?[") else f"*{pattern}*"
    hits: list[str] = []
    for dirpath, dirs, files in os.walk(base, followlinks=False):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not is_secret_path(os.path.join(dirpath, d))]
        entries = (files if kind == "file" else dirs if kind == "dir" else files + dirs)
        for name in entries:
            if fnmatch.fnmatch(name.lower(), pat.lower()):
                hits.append(os.path.join(dirpath, name))
                if len(hits) >= limit:
                    return hits
    return hits


def read_text(path: str, max_chars: int = MAX_READ) -> str:
    p = resolve(path)
    check_readable(p)
    if not p.is_file():
        raise FileNotFoundError(f"{p} is not a file")
    raw = p.read_bytes()[: max_chars * 4]
    if b"\x00" in raw[:4096]:
        raise ValueError(f"{p.name} looks like a binary file")
    text = raw.decode("utf-8", errors="replace")
    return text[:max_chars] + (f"\n…[truncated, file is {p.stat().st_size} bytes]" if len(text) > max_chars else "")


def info(path: str) -> dict:
    p = resolve(path)
    st = p.lstat()
    mime = mimetypes.guess_type(str(p))[0] or ("inode/directory" if p.is_dir() else "unknown")
    d = {"path": str(p), "type": "directory" if p.is_dir() else "symlink" if p.is_symlink() else "file",
         "size_bytes": st.st_size, "mode": stat.filemode(st.st_mode), "mime": mime,
         "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
         "owner_uid": st.st_uid}
    if p.is_dir():
        d["entries"] = sum(1 for _ in p.iterdir())
    return d


def make_dir(path: str) -> str:
    p = resolve(path)
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


def move(src: str, dst: str) -> tuple[Path, Path]:
    s, d = resolve(src), resolve(dst)
    if not s.exists() and not s.is_symlink():
        raise FileNotFoundError(f"{s} does not exist")
    if d.is_dir():
        d = d / s.name
    d.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(s), str(d))
    return s, d


def write_text(path: str, content: str) -> Path:
    p = resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


def trash(path: str) -> str:
    """Move to the freedesktop trash (recoverable) rather than unlinking."""
    p = resolve(path)
    if not (p.exists() or p.is_symlink()):
        raise FileNotFoundError(f"{p} does not exist")
    rc, _, err = run(["gio", "trash", str(p)], timeout=30)
    if rc == 0:
        return f"moved {p} to the trash"
    tdir = HOME / ".local/share/Trash/files"
    tdir.mkdir(parents=True, exist_ok=True)
    dest = tdir / f"{p.name}.{int(time.time())}"
    shutil.move(str(p), dest)
    return f"moved {p} to {dest}"


def plan_organize(directory: str, include_dirs: bool = False) -> list[tuple[Path, Path]]:
    """Plan (not execute) sorting loose files in `directory` into category subfolders."""
    root = resolve(directory)
    moves: list[tuple[Path, Path]] = []
    for e in sorted(root.iterdir()):
        if e.name.startswith(".") or (e.is_dir() and not include_dirs):
            continue
        if e.is_dir():
            continue
        ext = e.suffix if e.suffix == ".AppImage" else e.suffix.lower()
        cat = next((c for c, exts in CATEGORY.items() if ext in exts), "Other")
        dst = root / cat / e.name
        if dst.exists():
            dst = root / cat / f"{e.stem}-{int(e.stat().st_mtime)}{e.suffix}"
        moves.append((e, dst))
    return moves


def execute_moves(moves: list[tuple[Path, Path]]) -> int:
    n = 0
    for s, d in moves:
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(s), str(d))
        n += 1
    return n
