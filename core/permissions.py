"""Permission system: AUTO / CONFIRM / DENY, with prompt-injection ("taint") awareness.

Every desktop tool calls `await perms.gate(...)` before acting. CONFIRM raises a request that the GUI
(buttons), the voice pipeline (say yes/no) or IPC can resolve; no answer within the timeout means DENY.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import re
import shlex
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .events import EventBus

log = logging.getLogger("meimei.perm")


class Level(str, Enum):
    AUTO = "auto"
    CONFIRM = "confirm"
    DENY = "deny"


class PermissionDenied(Exception):
    pass


@dataclass
class ConfirmRequest:
    id: int
    action: str
    summary: str
    reason: str
    future: asyncio.Future = field(repr=False, default=None)


HOME = Path.home()
SECRET_PATTERNS = [
    r"/\.ssh(/|$)", r"/\.gnupg(/|$)", r"/\.aws(/|$)", r"/\.kube(/|$)", r"/\.docker/config",
    r"id_(rsa|ed25519|ecdsa|dsa)", r"\.pem$", r"\.key$", r"\.p12$", r"\.kdbx$", r"/\.netrc$",
    r"/\.claude/\.credentials", r"/\.config/(gh|rclone|Bitwarden|1Password)(/|$)",
    r"/keyrings?(/|$)", r"/\.password-store(/|$)", r"/etc/(shadow|gshadow|sudoers)",
    r"/(logins|key[34]|cookies)\.(json|db|sqlite)", r"/Login Data$", r"/Cookies$", r"/Web Data$",
    r"\.mozilla/firefox/.+/(key4\.db|logins\.json|cookies\.sqlite)", r"/\.cache/.*token",
]
_SECRET_RE = re.compile("|".join(SECRET_PATTERNS))


def is_secret_path(p: str | Path) -> bool:
    try:
        s = str(Path(p).expanduser().resolve())
    except Exception:
        s = str(p)
    return bool(_SECRET_RE.search(s))


# Commands that can never be run, even with confirmation.
HARD_DENY = [
    r"\brm\s+(-\w*\s+)*-\w*[rR]\w*\s+(-\w+\s+)*(/|~|\$HOME|/\*|~/\*)\s*($|;|&|\|)",   # rm -rf / or ~
    r"\bmkfs(\.\w+)?\b", r"\bdd\b[^|;&]*\bof=/dev/", r":\(\)\s*\{.*\};\s*:", r">\s*/dev/(sd|nvme|mmcblk)",
    r"\bshred\b[^|;&]*/dev/", r"\bchmod\s+-R\s+\S+\s+/($|\s)", r"\bchown\s+-R\s+\S+\s+/($|\s)",
    r"\bcryptsetup\s+(luksFormat|erase)", r"\bwipefs\b", r"\bsgdisk\s+--zap",
]
_HARD_DENY_RE = [re.compile(p) for p in HARD_DENY]

# Read-only programs that may run without confirmation (when no secret paths / redirections are involved).
SAFE_PROGRAMS = {
    "ls", "pwd", "whoami", "id", "hostname", "uname", "uptime", "date", "df", "du", "free", "lsblk", "lscpu",
    "lsusb", "lspci", "lsmod", "ip", "ss", "ps", "pgrep", "nvidia-smi", "sensors", "which", "type", "echo",
    "head", "tail", "wc", "grep", "rg", "fd", "stat", "file", "tree", "lsof", "sort", "uniq", "cut",
    "cat", "less", "nmcli", "playerctl", "wpctl", "pactl", "hyprctl", "free", "vmstat", "iostat", "ping",
    "dig", "nslookup", "findmnt", "mount", "dmesg", "journalctl", "systemctl", "pacman", "git", "python",
    "python3", "node", "tail", "basename", "dirname", "realpath", "readlink", "numfmt", "jq", "xdg-mime",
    "fastfetch", "neofetch", "top", "htop", "btop",
}
# program -> predicate over the remaining argv that must be True for "safe".
_SAFE_ARGS = {
    "systemctl": lambda a: bool(a) and (a[0] in {"status", "is-active", "is-enabled", "show", "list-units",
                                                  "list-unit-files", "list-timers", "--user"}),
    "journalctl": lambda a: "--vacuum-size" not in " ".join(a) and "--rotate" not in a,
    "pacman": lambda a: bool(a) and a[0] in {"-Q", "-Qi", "-Ql", "-Qs", "-Qe", "-Qu", "-Qdt", "-Qq", "-Qm", "-Qk", "-Si", "-Ss", "-F"},
    "git": lambda a: bool(a) and a[0] in {"status", "log", "diff", "branch", "show", "remote", "rev-parse", "describe", "ls-files"},
    "hyprctl": lambda a: bool(a) and a[0] in {"clients", "monitors", "workspaces", "activewindow", "activeworkspace",
                                               "devices", "version", "binds", "layers", "cursorpos", "splash",
                                               "-j"},
    "python": lambda a: a[:1] in (["--version"], ["-V"]),
    "python3": lambda a: a[:1] in (["--version"], ["-V"]),
    "node": lambda a: a[:1] in (["--version"], ["-v"]),
    "wpctl": lambda a: bool(a) and a[0] in {"status", "get-volume", "inspect"},
    "pactl": lambda a: bool(a) and a[0] in {"list", "info", "get-sink-volume", "get-source-volume", "get-sink-mute"},
    "nmcli": lambda a: not any(x in a for x in ("connect", "up", "down", "delete", "modify", "add", "reload", "radio")),
    "playerctl": lambda a: bool(a) and a[0] in {"status", "metadata", "position", "volume", "-l", "--list-all"},
    "mount": lambda a: not a,
    "find": lambda a: not any(x in a for x in ("-exec", "-execdir", "-delete", "-ok", "-fprint")),
    "dmesg": lambda a: "-C" not in a and "--clear" not in a,
    "ping": lambda a: "-c" in a,
    "top": lambda a: "-b" in a,
    "htop": lambda a: False, "btop": lambda a: False, "less": lambda a: False,
}
SAFE_PROGRAMS.add("find")
_SHELL_OPERATORS = re.compile(r"(\|\||&&|;|\|)")


def classify_command(cmd: str, extra_prefixes: list[str] | None = None) -> tuple[Level, str]:
    """Classify a shell command. DENY for catastrophic patterns, AUTO for plainly read-only, else CONFIRM."""
    c = cmd.strip()
    if not c:
        return Level.DENY, "empty command"
    for rx in _HARD_DENY_RE:
        if rx.search(c):
            return Level.DENY, "matches a catastrophic-command pattern"
    if re.search(r"\bsudo\b|\bsu\b|\bpkexec\b|\bdoas\b", c):
        return Level.CONFIRM, "privileged command"
    for pre in extra_prefixes or []:
        if c == pre or c.startswith(pre + " "):
            if not re.search(r"[;&|`$<>]", c):
                return Level.AUTO, "user allow-listed prefix"
    if re.search(r">|<\(|\$\(|`|\bcurl\b.*\|\s*(ba)?sh|\bwget\b.*\|\s*(ba)?sh|\n", c):
        return Level.CONFIRM, "redirection, substitution or pipe-to-shell"
    segments = [s for s in _SHELL_OPERATORS.split(c) if s and not _SHELL_OPERATORS.fullmatch(s)]
    for seg in segments:
        try:
            argv = shlex.split(seg)
        except ValueError:
            return Level.CONFIRM, "unparseable command"
        if not argv:
            continue
        prog, args = Path(argv[0]).name, argv[1:]
        if prog not in SAFE_PROGRAMS:
            return Level.CONFIRM, f"'{prog}' is not on the read-only list"
        pred = _SAFE_ARGS.get(prog)
        if pred is not None and not pred(args):
            return Level.CONFIRM, f"'{prog} {' '.join(args[:2])}' may change state"
        for a in args:
            if not a.startswith("-") and ("/" in a or a.startswith("~")) and is_secret_path(a):
                return Level.CONFIRM, "touches a sensitive path"
    return Level.AUTO, "read-only command"


TERMINAL_CLASSES = {"kitty", "alacritty", "foot", "footclient", "wezterm", "konsole", "gnome-terminal",
                    "xterm", "urxvt", "st", "terminator", "tilix", "ghostty", "com.mitchellh.ghostty"}


class PermissionManager:
    def __init__(self, bus: EventBus, settings):
        self.bus = bus
        self.settings = settings
        self.tainted = False           # set when untrusted content (web/screen) entered this turn
        self.pending: dict[int, ConfirmRequest] = {}
        self._ids = itertools.count(1)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.always_confirm = False     # test hook / paranoid mode

    # -- taint ---------------------------------------------------------------
    def set_tainted(self, value: bool, why: str = "") -> None:
        if value and not self.tainted:
            self.bus.emit("activity", level="info", text=f"Untrusted content in context ({why}); risky actions will need confirmation")
        self.tainted = value

    # -- gate ----------------------------------------------------------------
    async def gate(self, action: str, summary: str, level: Level = Level.AUTO, *,
                   taint_sensitive: bool = False, reason: str = "") -> None:
        """Raise PermissionDenied unless the action is allowed (possibly after user confirmation)."""
        if level is Level.DENY:
            self.bus.emit("activity", level="deny", text=f"DENIED {action}: {summary} ({reason})")
            raise PermissionDenied(f"Blocked by safety policy: {reason or action}")
        if level is Level.AUTO and taint_sensitive and self.tainted and not self.settings.get("permissions.ignore_taint", False):
            level, reason = Level.CONFIRM, "this turn involved untrusted web/screen content"
        if level is Level.AUTO and self.always_confirm:
            level, reason = Level.CONFIRM, "paranoid mode"
        if level is Level.AUTO:
            return
        ok = await self._ask(action, summary, reason)
        if not ok:
            raise PermissionDenied("The user declined (or did not answer) the confirmation prompt.")

    async def _ask(self, action: str, summary: str, reason: str) -> bool:
        loop = asyncio.get_running_loop()
        req = ConfirmRequest(next(self._ids), action, summary, reason, loop.create_future())
        self.pending[req.id] = req
        self.bus.emit("confirm_request", id=req.id, action=action, summary=summary, reason=reason)
        self.bus.emit("activity", level="warn", text=f"CONFIRM? {action}: {summary} — {reason}")
        timeout = float(self.settings.get("permissions.confirm_timeout_s", 60))
        try:
            return bool(await asyncio.wait_for(req.future, timeout))
        except asyncio.TimeoutError:
            self.bus.emit("activity", level="warn", text=f"Confirmation timed out: {summary}")
            return False
        finally:
            self.pending.pop(req.id, None)
            self.bus.emit("confirm_done", id=req.id)

    def resolve(self, req_id: int | None, allow: bool) -> bool:
        """Thread-safe: called from GUI/voice threads. req_id=None resolves the oldest pending request."""
        if not self.pending:
            return False
        rid = req_id if req_id in self.pending else min(self.pending)
        req = self.pending.get(rid)
        if not req or req.future.done():
            return False
        req.future.get_loop().call_soon_threadsafe(
            lambda: req.future.done() or req.future.set_result(allow))
        self.bus.emit("activity", level="info", text=f"{'Allowed' if allow else 'Declined'}: {req.summary}")
        return True

    def has_pending(self) -> bool:
        return bool(self.pending)

    def pending_summary(self) -> str:
        return "; ".join(r.summary for r in self.pending.values())
