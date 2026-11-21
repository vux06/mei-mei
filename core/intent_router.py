"""Local fast-path for plainly mechanical commands, so they don't need a Claude round-trip.

Anything not matched here goes to Claude with the full conversation context. Locally handled commands are
reported to Claude on its next turn (see Assistant.pending_notes) so context is never silently lost.
"""
from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, field

from tools import applications as apps
from tools import windows

_POLITE = re.compile(r"^(?:(?:hey |ok |okay )?mei ?mei[, ]*)?(?:please |can you |could you |would you |will you |go ahead and |just )*", re.I)
_TAIL = re.compile(r"[\s.!?,]*(?:please|for me|now|thanks|thank you)?[\s.!?,]*$", re.I)


@dataclass
class LocalIntent:
    name: str
    args: dict = field(default_factory=dict)


CLEAR_RE = re.compile(
    r"^(?:clear|reset|wipe|flush|empty|forget)(?: out)?(?: all)?(?: of)?(?: your| the| our| this)? "
    r"(?:context|conversation|chat|chat history|history|short[- ]term memory)$"
    r"|^start (?:over|afresh|a (?:new|fresh) (?:conversation|session|chat))$|^new (?:conversation|session|chat)$", re.I)
STOP_RE = re.compile(r"^(?:stop|stop talking|stop speaking|shut up|be quiet|quiet|silence|cancel|never ?mind|enough|that's enough|hush)$", re.I)
PAUSE_RE = re.compile(r"^(?:pause listening|stop listening|go to sleep|sleep|mute (?:yourself|your (?:mic|microphone))|disable (?:the )?wake ?word)$", re.I)
TIME_RE = re.compile(r"^(?:what(?:'s| is) the time|what time is it|tell me the time|time please|what(?:'s| is) the time right now)$", re.I)
VOL_SET_RE = re.compile(r"^(?:set|put|make|turn)(?: the)?(?: (?:system|master))? volume (?:to|at) (\d{1,3})(?: ?(?:percent|%))?$", re.I)
VOL_STEP_RE = re.compile(r"^(?:turn |make )?(?:the )?(?:volume |sound |it )?(up|down)(?: a (?:bit|little|notch))?$"
                         r"|^(?:turn|make)(?: the)? (?:volume|sound|it) (up|down)(?: a (?:bit|little|notch))?$"
                         r"|^(?:volume|sound) (up|down)$|^(?:raise|lower|increase|decrease|reduce) (?:the )?volume$", re.I)
MUTE_RE = re.compile(r"^(mute|unmute)(?: the)?(?: (?:volume|sound|audio|speakers?|computer|system))?$", re.I)
OPEN_RE = re.compile(r"^(?:open|launch|start|fire up|run)(?: up)? (?:the )?(.+?)(?: app| application| program)?$", re.I)
CLOSE_RE = re.compile(r"^(?:close|quit|exit|kill)(?: the)? (.+?)(?: app| application| window)?$", re.I)
CONJUNCTIONS = re.compile(r"\b(and|then|after that|also|http|www|\.com|\.org|search|for|to|about)\b", re.I)

REPLIES = {
    "clear": ["Done. Fresh slate. I remember who I am, just not what we were rambling about.",
              "Context wiped. Permanent memory's still intact, so I haven't forgotten you.",
              "Clean slate. Go ahead, impress me."],
    "open": ["{app} coming right up.", "Opening {app}.", "{app}, on it.", "{app}? Fine. Launching."],
    "close": ["Closing {app}.", "{app} is on its way out.", "Bye-bye {app}."],
    "volume": ["Volume's at {v} percent.", "{v} percent. Happy now?", "Done, {v} percent."],
    "mute": ["Muted. Blissful silence.", "Muted."],
    "unmute": ["Sound's back.", "Unmuted."],
    "pause": ["Going deaf. Resume me from the app when you miss me."],
    "time": ["It's {t}.", "{t}. Time flies when you're talking to me."],
    "fail": ["That didn't work: {why}.", "Couldn't do it: {why}."],
}


def pick(key: str, **kw) -> str:
    return random.choice(REPLIES[key]).format(**kw)


def normalize(text: str) -> str:
    t = text.strip()
    t = _POLITE.sub("", t, count=1)
    t = _TAIL.sub("", t).strip()
    return t


def route(text: str) -> LocalIntent | None:
    t = normalize(text)
    if not t:
        return None
    if CLEAR_RE.match(t):
        return LocalIntent("clear_context")
    if STOP_RE.match(t):
        return LocalIntent("stop")
    if PAUSE_RE.match(t):
        return LocalIntent("pause_listening")
    if TIME_RE.match(t):
        return LocalIntent("time")
    m = VOL_SET_RE.match(t)
    if m:
        return LocalIntent("volume_set", {"value": int(m.group(1))})
    m = VOL_STEP_RE.match(t)
    if m:
        word = next((g for g in m.groups() if g), None) or ("up" if re.match(r"raise|increase", t, re.I) else "down")
        return LocalIntent("volume_step", {"delta": 10 if word.lower() == "up" else -10})
    m = MUTE_RE.match(t)
    if m:
        return LocalIntent("mute", {"mute": m.group(1).lower() == "mute"})
    m = OPEN_RE.match(t)
    if m and not CONJUNCTIONS.search(m.group(1)):
        app = apps.find_app(m.group(1))
        if app and (app.name.lower() == m.group(1).lower() or app.binary.lower() == m.group(1).lower()
                    or m.group(1).lower() in apps.ALIASES or app.id.lower() == m.group(1).lower()):
            return LocalIntent("open_app", {"name": m.group(1), "label": app.name})
    m = CLOSE_RE.match(t)
    if m and not CONJUNCTIONS.search(m.group(1)) and m.group(1).lower() not in ("it", "that", "this", "them", "the window"):
        try:
            if apps.running_windows(m.group(1)):
                return LocalIntent("close_app", {"name": m.group(1), "label": m.group(1)})
        except Exception:
            pass
    return None


def local_time() -> str:
    return time.strftime("%-I:%M %p")
