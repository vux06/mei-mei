"""The brain: one persistent Claude Agent SDK conversation, local fast-paths, tool wiring and context clearing.

Runs its own asyncio loop in a background thread so the Qt GUI and the audio threads can call into it through
thread-safe methods (`submit`, `interrupt`, `clear_context`, ...), all of which return concurrent futures.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
import time
from typing import Any

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, ResultMessage,
                              TextBlock, ToolResultBlock, ToolUseBlock)

from tools import applications as apps
from tools import system
from tools.registry import SERVER_NAME, ToolContext, build_server
from . import intent_router
from .config import Settings, load_personality
from .events import EventBus
from .memory import Memory
from .paths import WORKSPACE_DIR
from .permissions import PermissionManager
from .session_manager import SessionManager
from .state import StateTracker

log = logging.getLogger("meimei.assistant")
WEB_TOOLS = ["WebSearch", "WebFetch"]
UNTRUSTED_TOOLS = {"WebSearch", "WebFetch"}


class Assistant:
    def __init__(self, settings: Settings, bus: EventBus, memory: Memory, sessions: SessionManager,
                 perms: PermissionManager, state: StateTracker, tts=None):
        self.settings, self.bus, self.memory, self.sessions = settings, bus, memory, sessions
        self.perms, self.state, self.tts = perms, state, tts
        self.voice = None                      # set by main: VoicePipeline (for pause/resume intents)
        self.client: ClaudeSDKClient | None = None
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="assistant-loop", daemon=True)
        self._queue: asyncio.Queue | None = None
        self._turn_lock: asyncio.Lock | None = None
        self._active_turn = False
        self._pending_notes: list[str] = []
        self._connected_session: str | None = None
        self._mcp_names: list[str] = []
        self.last_context_pct: float | None = None
        self.claude_status = "starting"
        perms.loop = self.loop
        bus.on("confirm_request", self._on_confirm_request)

    # ------------------------------------------------------------------ thread plumbing
    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self._queue = asyncio.Queue()
        self._turn_lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()
        self.loop.create_task(self._worker())
        self.loop.run_forever()

    def start(self) -> None:
        self._thread.start()
        while self._queue is None:
            time.sleep(0.01)
        self.call(self._connect())     # warm up in the background

    def call(self, coro) -> concurrent.futures.Future:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def shutdown(self) -> None:
        try:
            self.call(self._disconnect()).result(timeout=8)
        except Exception:
            pass
        def _stop():
            for t in asyncio.all_tasks(self.loop):
                t.cancel()
            self.loop.call_later(0.2, self.loop.stop)
        self.loop.call_soon_threadsafe(_stop)

    # ------------------------------------------------------------------ public API (thread-safe)
    def submit(self, text: str, source: str = "text", interrupt: bool = True) -> concurrent.futures.Future:
        return self.call(self._submit(text, source, interrupt))

    def interrupt(self) -> concurrent.futures.Future:
        return self.call(self._interrupt())

    def clear_context(self, expect_session_id: str | None = None) -> concurrent.futures.Future:
        return self.call(self._clear_context(expect_session_id))

    def status(self) -> dict:
        s = self.sessions.state
        return {"session_id": s.session_id, "generation": s.generation, "claude": self.claude_status,
                "state": self.state.current, "context_percent": self.last_context_pct,
                "busy": self._active_turn, "fresh_session": s.started_fresh}

    # ------------------------------------------------------------------ system prompt / options
    def _system_prompt(self) -> str:
        digest = self.memory.memory_digest()
        env = (f"\n\n## Environment\nToday is {time.strftime('%A, %Y-%m-%d')}, local time {time.strftime('%H:%M')}. "
               "OS: CachyOS Linux, Hyprland (Wayland), Kitty terminal, Fish shell, Firefox browser.")
        mem = ("\n\n## Permanent memory (explicit facts the user asked you to keep)\n" + digest) if digest else \
              "\n\n## Permanent memory\n(empty so far)"
        return load_personality() + env + mem

    def _build_options(self, resume: bool) -> ClaudeAgentOptions:
        server, names, _ = build_server(ToolContext(self.bus, self.perms, self.memory, self.settings))
        self._mcp_names = names
        web = WEB_TOOLS if self.settings.get("claude.allow_web_tools", True) else []
        WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
        kw: dict[str, Any] = dict(
            system_prompt=self._system_prompt(), tools=list(web), allowed_tools=names + web,
            mcp_servers={SERVER_NAME: server}, strict_mcp_config=True, setting_sources=[],
            cwd=str(WORKSPACE_DIR), permission_mode="dontAsk",   # anything not whitelisted is denied, never prompted
            max_turns=int(self.settings.get("claude.max_turns", 30)),
            thinking={"type": "disabled"}, stderr=lambda l: log.debug("claude-cli: %s", l.rstrip()),
        )
        if m := self.settings.get("claude.model"):
            kw["model"] = m
        if e := self.settings.get("claude.effort"):
            kw["effort"] = e
        if resume:
            kw["resume"] = self.sessions.session_id          # continue the SAME logical conversation
        else:
            kw["session_id"] = self.sessions.session_id      # brand-new conversation with our chosen id
        return ClaudeAgentOptions(**kw)

    # ------------------------------------------------------------------ connection management
    async def _connect(self) -> None:
        async with self._connect_lock:
            await self._connect_locked()

    async def _connect_locked(self) -> None:
        if self.client is not None and self._connected_session == self.sessions.session_id:
            return
        await self._disconnect()
        self.claude_status = "connecting"
        self.bus.emit("claude_status", status="connecting")
        resume = self.sessions.resumable
        try:
            self.client = ClaudeSDKClient(self._build_options(resume))
            await self.client.connect()
        except Exception as e:
            if resume:     # the stored session may be gone: be honest about it and start fresh
                log.warning("resume failed (%s); starting a fresh session", e)
                old, new = self.sessions.clear()
                self.bus.emit("session_changed", old=old, new=new, reason="resume failed")
                self.bus.emit("activity", level="warn", text=f"Could not resume session {old[:8]} ({e}); started fresh {new[:8]}")
                self.client = ClaudeSDKClient(self._build_options(False))
                try:
                    await self.client.connect()
                except Exception as e2:
                    return self._fail_connect(e2)
            else:
                return self._fail_connect(e)
        self._connected_session = self.sessions.session_id
        self.claude_status = "connected"
        self.bus.emit("claude_status", status="connected")
        self.bus.emit("activity", level="info",
                      text=f"Claude {'resumed' if resume else 'new'} session {self.sessions.session_id[:8]}")
        self.bus.emit("session_changed", old=None, new=self.sessions.session_id, reason="connected")

    def _fail_connect(self, e: Exception) -> None:
        self.client = None
        self._connected_session = None
        self.claude_status = "error"
        self.bus.emit("claude_status", status="error", error=str(e))
        self.bus.emit("activity", level="error", text=f"Claude connection failed: {e}")
        self.state.error()

    async def _disconnect(self) -> None:
        c, self.client, self._connected_session = self.client, None, None
        if c is not None:
            try:
                await asyncio.wait_for(c.disconnect(), 6)
            except Exception:
                log.debug("disconnect error", exc_info=True)

    # ------------------------------------------------------------------ turns
    async def _submit(self, text: str, source: str, interrupt: bool) -> None:
        if interrupt and self._active_turn:
            await self._interrupt()
        await self._queue.put((text, source))

    async def _interrupt(self) -> None:
        if self.tts:
            self.tts.stop()
        if self._active_turn and self.client is not None:
            try:
                await self.client.interrupt()
                self.bus.emit("activity", level="info", text="Interrupted current task")
            except Exception:
                log.debug("interrupt failed", exc_info=True)

    async def _worker(self) -> None:
        while True:
            text, source = await self._queue.get()
            try:
                async with self._turn_lock:
                    await self._handle(text, source)
            except Exception as e:
                log.exception("turn failed")
                self.bus.emit("activity", level="error", text=f"Turn failed: {e}")
                self.state.set(processing=False, executing=0)
                self.state.error()
                self._say("Something went sideways on my end. Try again?")
                self.client, self._connected_session = None, None   # force a clean reconnect (same session id)

    def _say(self, text: str, interrupt: bool = False) -> None:
        self.bus.emit("assistant_text", text=text)
        self.memory.log_message(self.sessions.session_id, "assistant", text)
        if self.tts:
            self.tts.speak(text, interrupt=interrupt)

    def _user_said(self, text: str, source: str) -> None:
        self.bus.emit("user_text", text=text, source=source)
        self.memory.log_message(self.sessions.session_id, "user", text, source)

    async def _handle(self, text: str, source: str) -> None:
        self._user_said(text, source)
        self.perms.set_tainted(False)           # user input is trusted; taint restarts per user turn
        intent = intent_router.route(text)
        if intent:
            self.bus.emit("activity", level="info", text=f"Local intent: {intent.name}")
            handled = await self._run_local(intent, text)
            if handled:
                return
        await self._ask_claude(text)

    async def _run_local(self, intent: intent_router.LocalIntent, text: str) -> bool:
        a, n = intent.args, intent.name
        note: str | None = None
        reply: str
        try:
            if n == "clear_context":
                await self._clear_context(None, spoken=True)
                return True
            if n == "stop":
                if self.tts:
                    self.tts.stop()
                return True
            if n == "pause_listening":
                if self.voice:
                    self.voice.pause()
                reply = intent_router.pick("pause")
            elif n == "time":
                reply = intent_router.pick("time", t=intent_router.local_time())
            elif n == "volume_set":
                v = (await asyncio.to_thread(system.set_volume, a["value"]))["volume_percent"]
                reply, note = intent_router.pick("volume", v=v), f"volume set to {v}%"
            elif n == "volume_step":
                v = (await asyncio.to_thread(system.change_volume, a["delta"]))["volume_percent"]
                reply, note = intent_router.pick("volume", v=v), f"volume now {v}%"
            elif n == "mute":
                await asyncio.to_thread(system.set_mute, a["mute"])
                reply, note = intent_router.pick("mute" if a["mute"] else "unmute"), ("muted" if a["mute"] else "unmuted") + " system audio"
            elif n == "open_app":
                r = await asyncio.to_thread(apps.open_app, a["name"])
                if not r["ok"]:
                    return False
                reply, note = intent_router.pick("open", app=a["label"]), f"launched {a['label']}"
            elif n == "close_app":
                r = await asyncio.to_thread(apps.close_app, a["name"])
                if not r["ok"]:
                    return False
                reply, note = intent_router.pick("close", app=a["label"]), f"closed {a['label']}"
            else:
                return False
        except Exception as e:
            self._say(intent_router.pick("fail", why=str(e)[:80]))
            return True
        if note:
            self._pending_notes.append(f"[Handled locally: user said \"{text}\" → {note}. You replied \"{reply}\"]")
        self._say(reply)
        return True

    async def _ask_claude(self, text: str) -> None:
        await self._connect()
        if self.client is None:
            self._say("I can't reach Claude right now. Check your login and network.")
            return
        prompt = text
        if self._pending_notes:
            prompt = "\n".join(self._pending_notes) + "\n" + text
        self._active_turn = True
        self.state.set(processing=True)
        t0 = time.time()
        try:
            await self.client.query(prompt)
            self._pending_notes.clear()
            async for msg in self.client.receive_response():
                self._on_message(msg)
        finally:
            self._active_turn = False
            self.state.set(processing=False, executing=0)
        self.bus.emit("activity", level="info", text=f"Turn finished in {time.time() - t0:.1f}s")
        asyncio.create_task(self._update_context_usage())

    def _on_message(self, msg) -> None:
        if isinstance(msg, AssistantMessage):
            for b in msg.content:
                if isinstance(b, TextBlock) and b.text.strip():
                    self._say(b.text.strip())
                elif isinstance(b, ToolUseBlock):
                    if b.name in UNTRUSTED_TOOLS:
                        self.perms.set_tainted(True, b.name)
                    self.state.bump_exec(+1)
                    self.bus.emit("activity", level="tool", text=f"→ {b.name.replace('mcp__mei__', '')}")
                elif isinstance(b, ToolResultBlock):
                    self.state.bump_exec(-1)
        elif isinstance(msg, ResultMessage):
            self.sessions.mark_used(msg.session_id)
            self.memory.bump_turns(msg.session_id)
            if msg.is_error:
                self.bus.emit("activity", level="error", text=f"Claude error: {msg.subtype} {msg.errors or msg.result or ''}")
                self.state.error()
            if msg.terminal_reason and msg.terminal_reason.startswith("aborted"):
                self.bus.emit("activity", level="info", text="Turn was interrupted")
            if msg.total_cost_usd is not None:
                self.bus.emit("cost", usd=msg.total_cost_usd)
            self.bus.emit("session_changed", old=None, new=msg.session_id, reason="turn")

    async def _update_context_usage(self) -> None:
        try:
            if self.client is None:
                return
            u = await asyncio.wait_for(self.client.get_context_usage(), 10)
            self.last_context_pct = float(u.get("percentage", 0))
            self.bus.emit("context_usage", percent=self.last_context_pct, tokens=u.get("totalTokens"), max=u.get("maxTokens"))
            if self.last_context_pct >= float(self.settings.get("claude.context_warn_percent", 70)):
                self.bus.emit("activity", level="warn",
                              text=f"Context is {self.last_context_pct:.0f}% full — say 'clear your context' soon")
        except Exception:
            log.debug("context usage unavailable", exc_info=True)

    # ------------------------------------------------------------------ clearing
    async def _clear_context(self, expect_session_id: str | None, spoken: bool = False) -> dict:
        """Create a FRESH session and make it the active one. Never resumes the old id, never claims otherwise."""
        if self._active_turn:
            await self._interrupt()
        if self.tts and not spoken:
            self.tts.stop()
        old, new = self.sessions.clear(expect_session_id)     # raises ValueError on id mismatch
        self._pending_notes.clear()
        self.perms.set_tainted(False)
        await self._disconnect()
        self.last_context_pct = 0.0
        self.bus.emit("session_changed", old=old, new=new, reason="cleared")
        self.bus.emit("context_usage", percent=0.0, tokens=0, max=None)
        self.bus.emit("transcript_cleared")
        self.bus.emit("activity", level="info",
                      text=f"Context cleared: archived {old[:8]}, new session {new[:8]} (fresh — not resumed)")
        await self._connect()                  # warm: new CLI process with personality re-applied
        if spoken:
            self._say(intent_router.pick("clear"))
        return {"old": old, "new": new, "generation": self.sessions.state.generation,
                "resumed": False, "personality_reapplied": True}

    def _on_confirm_request(self, event: str, data: dict) -> None:
        if self.tts:
            self.tts.speak(f"Quick check. {data['summary'][:140]}. Yes or no?", interrupt=True)
