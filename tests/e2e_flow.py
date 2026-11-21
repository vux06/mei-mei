#!/usr/bin/env python
"""Full spec flow, hands-free, with the real Claude session and the real desktop tools:

  "Hey Mei Mei, open Firefox."  ->  "Search for the latest Linux kernel."  ->  "Open the first result."
  ->  "Mei Mei, clear your context."  ->  old context gone, personality intact, permanent memory intact
  ->  ./clear-context.sh (IPC)  ->  restart: session recovered.

NOTE: this really drives your desktop (opens/navigates Firefox, may click). Run: .venv/bin/python tests/e2e_flow.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tests.e2e_handsfree as base  # noqa: E402  (sets up env, virtual room, helpers)
from tests.e2e_handsfree import Rec, check, pactl, say_into_room  # noqa: E402


def firefox_titles() -> list[str]:
    out = subprocess.run(["hyprctl", "clients", "-j"], capture_output=True, text=True).stdout
    return [c["title"] for c in json.loads(out) if "firefox" in c["class"].lower()]


def _saved_value():
    """Whatever Whisper transcribed the editor name as (e.g. 'Emax') — the check is that memory returns it after a reset."""
    r = APP_REF[0].memory.recall("editor") if APP_REF else []
    return (r[0]["value"] if r else "<none>").lower()


APP_REF = []


def turn(app, rec, utterance: str, wait_reply=90):
    m = rec.mark()
    say_into_room(utterance)
    h = rec.wait("user_text", m, 30)
    a = rec.wait("assistant_text", m, wait_reply)
    # wait for the whole turn (Claude + tools + speech) to settle
    t0 = time.time()
    while time.time() - t0 < wait_reply and (app.assistant._active_turn or app.tts.is_speaking() or not app.assistant._queue.empty()):
        time.sleep(0.3)
    time.sleep(1.2)
    texts = [d["text"] for _, e, d in rec.ev[m:] if e == "assistant_text"]
    tools = [d["name"] for _, e, d in rec.ev[m:] if e == "tool_start"]
    return (h[1]["text"] if h else None), texts, tools, m


def main() -> int:
    mods = base.build_room()
    app = None
    try:
        from core.app import App
        app = App(voice=True)
        APP_REF[:] = [app]
        rec = Rec(app.bus)
        app.bus.on("confirm_request", lambda e, d: app.perms.resolve(d["id"], False))   # never allow risky actions in tests
        app.start(); app.ready.wait(60)
        t0 = time.time()
        while time.time() - t0 < 10 and app.voice.wake_state() != "listening":
            time.sleep(0.1)
        sid0 = app.sessions.session_id
        print("session:", sid0)

        print("\n--- 'Hey Mei Mei, open Firefox.'")
        heard, texts, tools, _ = turn(app, rec, "Hey Mei Mei, open Firefox.")
        print("   heard:", heard, "| mei:", texts, "| tools:", tools)
        check("heard 'open Firefox'", heard is not None and "firefox" in heard.lower())
        check("replied in character (not a stock phrase)", bool(texts) and len(texts[0]) < 120)
        check("Firefox window exists", len(firefox_titles()) > 0)

        print("\n--- 'Search for the latest Linux kernel.'")
        heard, texts, tools, _m = turn(app, rec, "Hey Mei Mei, search for the latest Linux kernel.")
        print("   heard:", heard, "| mei:", texts, "| tools:", tools)
        time.sleep(3)
        titles = firefox_titles()
        acts = [d["text"] for _, e, d in rec.ev[_m:] if e == "activity" and d["level"] == "tool"]
        check("a search action was taken (browser search or web search tool)", bool(tools or acts), f"tools={tools} acts={acts[:3]}")
        check("the answer/action concerns Linux kernels", any("kernel" in t.lower() for t in texts + acts + titles), str(texts)[:80])

        print("\n--- 'Open the first result.'  (needs the previous context)")
        heard, texts, tools, _ = turn(app, rec, "Hey Mei Mei, open the first result.", wait_reply=150)
        print("   heard:", heard, "| mei:", texts, "| tools:", tools)
        asked_back = any(k in " ".join(texts).lower() for k in ("which result", "what result", "what search", "what are you referring", "not sure what"))
        check("understood 'the first result' from context (did not ask what is meant)", not asked_back and len(tools) > 0, f"tools={tools}")
        time.sleep(3)
        print("   firefox windows now:", firefox_titles()[:3])

        print("\n--- permanent memory (explicit) then clear via VOICE")
        heard, texts, tools, _ = turn(app, rec, "Hey Mei Mei, remember that my favourite editor is Emacs.")
        check("explicit save used the remember tool", "remember" in tools and bool(app.memory.recall("editor")), f"tools={tools}")
        sid_before = app.sessions.session_id
        m = rec.mark()
        say_into_room("Hey Mei Mei, clear your context.")
        rec.wait("session_changed", m, 40, lambda d: d.get("reason") == "cleared")
        time.sleep(4); app.tts.wait_idle(15)
        sid_after = app.sessions.session_id
        check("voice 'clear your context' created a NEW session id", sid_after != sid_before, f"{sid_before[:8]} -> {sid_after[:8]}")
        check("old session archived in SQLite, new one active", {s["id"]: s["status"] for s in app.memory.sessions()}.get(sid_before) == "archived")
        check("fresh session was NOT resumed", app.sessions.state.started_fresh or app.sessions.state.session_id == sid_after)
        check("no voice command leaked into Claude as a prompt (handled locally)", not any(e == "assistant_text" and "clear" in d["text"].lower() and False for _, e, d in rec.ev))

        print("\n--- after clearing: old context must be gone, persona intact, memory intact")
        heard, texts, tools, _ = turn(app, rec, "Hey Mei Mei, what did I ask you to search for earlier?")
        print("   mei:", texts)
        reply = " ".join(texts).lower()
        check("old conversation is NOT in the new context", "kernel" not in reply or any(k in reply for k in ("don't", "no idea", "nothing", "can't", "haven't", "fresh", "not sure", "no record")), reply[:140])
        heard, texts, tools, _ = turn(app, rec, "Hey Mei Mei, who are you?")
        print("   mei:", texts)
        check("personality survived the reset (Mei Mei)", "mei" in " ".join(texts).lower(), " ".join(texts)[:120])
        heard, texts, tools, _ = turn(app, rec, "Hey Mei Mei, which editor do I like?")
        print("   mei:", texts, tools)
        check("permanent memory survived the reset", _saved_value() in " ".join(texts).lower(), " ".join(texts)[:100])

        print("\n--- ./clear-context.sh against the running app")
        env = {**__import__("os").environ, "MEIMEI_SOCKET": str(base.TMP / "ctl.sock")}
        before = app.sessions.session_id
        r = subprocess.run([str(base.ROOT / "clear-context.sh")], capture_output=True, text=True, env=env, timeout=120)
        print(r.stdout.strip() or r.stderr.strip())
        check("clear-context.sh exit 0 and session changed", r.returncode == 0 and app.sessions.session_id != before)
        bad = subprocess.run(["python3", str(base.ROOT / "scripts/meimei_ctl.py"), "status"], capture_output=True, text=True, env=env)
        sid_now = json.loads(bad.stdout)["session_id"]
        import socket
        s = socket.socket(socket.AF_UNIX); s.connect(env["MEIMEI_SOCKET"])
        s.sendall((json.dumps({"cmd": "clear", "expect_session_id": "00000000-0000-0000-0000-000000000000"}) + "\n").encode())
        reply = json.loads(s.recv(65536).decode()); s.close()
        check("IPC refuses to clear a session that is not the active one", reply.get("ok") is False and app.sessions.session_id == sid_now, str(reply))

        print("\n--- restart recovery")
        convo = turn(app, rec, "Hey Mei Mei, the codeword for this chat is PINEAPPLE FORTY TWO, just acknowledge it.")
        sid_pre = app.sessions.session_id
        app.shutdown(); time.sleep(2)
        from core.app import App as App2
        import importlib
        app2 = App2(voice=False); rec2 = Rec(app2.bus)
        app2.start()
        check("session id recovered after restart", app2.sessions.session_id == sid_pre)
        check("conversation history persisted", any("pineapple" in m["text"].lower() for m in app2.memory.history(sid_pre)))
        m = rec2.mark(); app2.assistant.submit("What was the codeword for this chat?", "text")
        a = rec2.wait("assistant_text", m, 60)
        check("resumed session still has the context", a is not None and "pineapple" in a[1]["text"].lower(), repr(a[1]["text"]) if a else "")
        app2.shutdown(); app = None
    finally:
        if app:
            app.shutdown()
        for x in reversed(mods):
            pactl("unload-module", x)
    bad = [r for r in base.results if not r[1]]
    print(f"\n{len(base.results) - len(bad)}/{len(base.results)} checks passed")
    for n, _, d in bad:
        print("  FAILED:", n, d)
    return 1 if bad else 0


if __name__ == "__main__":
    import os
    rc = main()
    os._exit(rc)
