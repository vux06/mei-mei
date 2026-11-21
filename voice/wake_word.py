"""Wake-phrase detection for "Hey Mei Mei".

How it works (all local, all offline):
  1. Silero VAD continuously segments the live microphone stream (cheap, always running).
  2. Only when a speech segment appears, its first few seconds go to a tiny Whisper model (tiny.en, CPU, int8)
     that is primed with the phrase. Nothing is ever sent off the machine.
  3. The transcript's opening words are fuzzy-matched against the wake phrase with a phonetic normaliser, so
     "hey may may", "hey mei-mei", "hey maymay" and "a mei mei" all count, while "hey mom" does not.

Why not a classic keyword-spotting model (openWakeWord)? Those need a model trained for the exact phrase; none
exists for "Hey Mei Mei" and training one needs a synthetic-data pipeline (see README: "Training a dedicated
wake-word model"). This approach recognises the real phrase today, at the cost of a little more CPU and
~0.2–0.4 s extra latency than a dedicated detector. We do NOT substitute a different phrase.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

FILLERS = {"ok", "okay", "uh", "um", "so", "hi", "hello", "yo", "and", "well", "oh", "hmm", "hm"}
_WORD = re.compile(r"[A-Za-z']+")


def phonetic(s: str) -> str:
    """Crude, deliberately forgiving spelling->sound key. 'hey mei mei' -> 'hamama'."""
    s = re.sub(r"[^a-z]", "", s.lower())
    s = re.sub(r"(ei|ey|ay|ae|ai|eigh)", "a", s)
    s = re.sub(r"(ee|ie|ea|y$)", "i", s)
    s = s.replace("e", "i").replace("h", "h")
    s = re.sub(r"(.)\1+", r"\1", s)        # collapse doubled letters
    return s


@dataclass
class WakeMatch:
    matched: bool
    score: float = 0.0
    words_consumed: int = 0
    remainder: str = ""          # transcript text after the wake phrase ("open firefox")
    transcript: str = ""


def threshold_for(sensitivity: float) -> float:
    """sensitivity 0..1 (higher = triggers more easily). 0.6 -> 0.77 similarity required."""
    s = max(0.0, min(1.0, float(sensitivity)))
    return 0.95 - 0.30 * s


def _spans(text: str):
    return [(m.group(), m.start(), m.end()) for m in _WORD.finditer(text)]


EXTRA_PHRASES: list[str] = []      # additional wake words, e.g. a nickname (set from settings wake_word.extra_phrases)


def _targets(phrase: str, allow_bare_name: bool):
    t = [("full", phonetic(phrase))]
    if allow_bare_name:
        t.append(("bare", phonetic(" ".join(phrase.split()[-2:]))))
    for extra in EXTRA_PHRASES:
        t.append(("bare", phonetic(extra)))
    return t


def _best_at(spans, base: int, targets, filler_skip: bool = True) -> tuple[float, int]:
    """Best (score, end_word_index) for a wake window starting at word `base` (or base+1 after a filler word)."""
    best = (0.0, 0)
    for off in (0, 1):
        start = base + off
        if off == 1 and (not filler_skip or spans[base][0].lower() not in FILLERS):
            break
        for n in range(1, 5):
            window = spans[start:start + n]
            if len(window) < n:
                break
            key = phonetic("".join(w for w, _, _ in window))
            for kind, tgt in targets:
                if kind == "bare" and off == 1:
                    continue
                score = difflib.SequenceMatcher(None, key, tgt).ratio()
                if abs(len(key) - len(tgt)) > 2:      # a window must not swallow clearly extra material
                    score -= 0.15
                if score > best[0] + 1e-9:
                    best = (score, start + n)
    return best


def match_wake(text: str, sensitivity: float = 0.6, allow_bare_name: bool = True,
               phrase: str = "hey mei mei", relaxed: bool = False) -> WakeMatch:
    """Wake phrase anchored at the START of the utterance (normal always-on detection)."""
    spans = _spans(text)
    if not spans:
        return WakeMatch(False, transcript=text)
    thr = threshold_for(sensitivity) - (0.08 if relaxed else 0.0)
    score, end = _best_at(spans, 0, _targets(phrase, allow_bare_name))
    if score >= thr:
        rest = text[spans[end - 1][2]:].lstrip(" ,.!?;:-–—")
        return WakeMatch(True, score, end, rest.strip(), text)
    return WakeMatch(False, score, 0, "", text)


def find_wake(text: str, sensitivity: float = 0.6, allow_bare_name: bool = True,
              phrase: str = "hey mei mei", relaxed: bool = False) -> WakeMatch:
    """Wake phrase ANYWHERE in the text (used for barge-in, where her own echo may precede the user's words).
    Returns the best-scoring match; ties go to the latest occurrence."""
    spans = _spans(text)
    thr = threshold_for(sensitivity) - (0.08 if relaxed else 0.0)
    targets = _targets(phrase, allow_bare_name)
    best: tuple[float, int] | None = None
    for base in range(len(spans)):
        score, end = _best_at(spans, base, targets, filler_skip=False)
        if score >= thr and (best is None or score >= best[0] - 1e-9):
            best = (score, end)
    if best is None:
        return WakeMatch(False, transcript=text)
    rest = text[spans[best[1] - 1][2]:].lstrip(" ,.!?;:-–—")
    return WakeMatch(True, best[0], best[1], rest.strip(), text)


def strip_wake(text: str, sensitivity: float, allow_bare: bool, fallback_words: int = 0, anywhere: bool = False) -> str:
    """Remove the wake phrase (and anything before it, if `anywhere`) from a better transcript of the same utterance."""
    m = (find_wake if anywhere else match_wake)(text, sensitivity, allow_bare, relaxed=True)
    if m.matched:
        return m.remainder
    if fallback_words:
        words = text.split()
        return " ".join(words[fallback_words:])
    return text


_YES = re.compile(r"\b(yes|yeah|yep|yup|sure|ok|okay|go ahead|do it|confirm|confirmed|proceed|affirmative|"
                  r"please do|correct|allow|absolutely|definitely|fine)\b", re.I)
_NO = re.compile(r"\b(no|nope|nah|cancel|don'?t|do not|stop|deny|denied|decline|negative|never ?mind|abort|"
                 r"wait|not now)\b", re.I)


def parse_yes_no(text: str) -> bool | None:
    t = text.strip().lower()
    if not t or len(t.split()) > 6:
        return None
    if _NO.search(t):
        return False
    if _YES.search(t):
        return True
    return None


def overlaps_spoken(transcript: str, spoken: str, ratio: float = 0.7) -> bool:
    """True if the transcript is (mostly) just Mei Mei's own voice coming back through the mic."""
    words = [w.lower() for w in _WORD.findall(transcript)]
    if not words or not spoken:
        return False
    pool = {w.lower() for w in _WORD.findall(spoken)}
    hits = sum(1 for w in words if w in pool)
    return hits / len(words) >= ratio


def spoken_contains_wake(spoken: str, sensitivity: float, allow_bare: bool) -> bool:
    """Does Mei Mei's own recent speech contain the wake phrase? (Then a detection must be double-checked.)"""
    return bool(spoken) and find_wake(spoken, sensitivity, allow_bare).matched
