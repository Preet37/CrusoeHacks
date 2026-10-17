"""Turn-taking rules for Twin: when the mouth speaks, holds, or stays quiet.

The mouth (Responder) is fast and the mind (Listener, Conductor, Diagrammer)
is slow. The mind drops short "work updates" into a queue; this module decides
when the mouth may say them, and when Twin should answer a human directly.

Everything tunable lives in ``RULES``. These are the Coach-editable knobs
(ARCHITECTURE.md sections 7 and 11): the Coach writes overrides to
``server/agents/playbook_values.json`` and calls ``reload()``. Values in that
file are merged over ``DEFAULT_RULES``; missing or broken files fall back to the
defaults.

Pure Python, no network, no imports from other server modules. All times are
integer milliseconds supplied by the caller, so the logic is deterministic and
easy to test.

Typical session wiring::

    state = TurnState()
    # every user_text (interim or final):  note_human_speech(state, now)
    # every final user_text:
    d = on_final_text(state, text, now, has_updates=bool(queue))
    #   "respond" -> run Responder; "hold" -> cancel speech, status hand_raised
    # every ~200 ms:
    d = check_idle(state, now, bool(queue), oldest_age_ms)
    #   "deliver_update" -> speak updates, then mark_interjection(state, now)
    #   "none"/"stale"   -> discard stale updates
    #   "hold"           -> keep status hand_raised
"""

from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple, Optional

# Where the Coach writes its overrides. Module-level so tests can monkeypatch.
PLAYBOOK_VALUES_PATH: Path = Path(__file__).resolve().parent / "agents" / "playbook_values.json"

# Starting values (ARCHITECTURE.md section 7). Coach-editable in M3.
DEFAULT_RULES: dict[str, Any] = {
    # Words that address Twin. Whole-word, case-insensitive.
    # TWIN_NAME env var (if set) is appended at load time.
    "name_aliases": ["twin"],
    # Common speech-recognition mishears of the name, also treated as addressing.
    "name_mishears": ["twins", "tween", "twinn", "twen"],
    # Required silence since the last human speech before an unprompted update.
    "silence_before_update_s": 1.2,
    # Minimum gap between unprompted interjections (work updates).
    "interjection_cooldown_s": 30,
    # How long "not now" keeps Twin quiet (hand raised instead of speaking).
    "not_now_hold_s": 30,
    # Phrases that mean "Twin, be quiet". Whole-phrase, case-insensitive.
    "not_now_phrases": ["not now", "hold on", "wait", "one sec", "give me a sec", "hang on", "stop"],
    # Subset of not_now_phrases that only count when they are the whole
    # utterance or open it ("Wait, ..."), so "I can't wait to ship" or
    # "we should stop the deploy" do not trigger a hold.
    "not_now_start_only": ["wait", "stop"],
    # Work updates older than this are dropped instead of spoken.
    "max_update_age_s": 90,
    # Answer when a human addresses Twin by name (push-to-talk always answers).
    "respond_to_questions_addressed": True,
}

# The live rules. Mutated in place by reload() so `from turn_taking import RULES`
# stays valid for every importer.
RULES: dict[str, Any] = {}


def reload(path: Optional[Path] = None) -> dict[str, Any]:
    """Rebuild ``RULES`` from defaults + playbook overrides + TWIN_NAME env.

    ``path`` defaults to the module-level ``PLAYBOOK_VALUES_PATH`` (read at call
    time). Unreadable or non-object JSON is ignored. Returns ``RULES``.
    """
    merged = copy.deepcopy(DEFAULT_RULES)
    p = Path(path) if path is not None else PLAYBOOK_VALUES_PATH
    try:
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                merged.update(data)
    except (OSError, ValueError):
        pass

    aliases = [str(a).lower() for a in merged.get("name_aliases", [])]
    env_name = os.environ.get("TWIN_NAME", "").strip().lower()
    if env_name and env_name not in aliases:
        aliases.append(env_name)
    merged["name_aliases"] = aliases

    RULES.clear()
    RULES.update(merged)
    _compile()
    return RULES


# ---------------------------------------------------------------- text checks

_name_re: Optional[re.Pattern[str]] = None
_phrase_re: Optional[re.Pattern[str]] = None
_start_re: Optional[re.Pattern[str]] = None

# Small words people say before the real start ("hey, wait", "no no, stop").
_LEAD_FILLERS = r"(?:(?:hey|hi|ok|okay|oh|um|uh|no|yeah|actually|sorry|so)\W+)*"


def _alt(words: list[str]) -> str:
    # Longest first so multi-word phrases win; spaces match any whitespace.
    parts = sorted({w.strip().lower() for w in words if str(w).strip()}, key=len, reverse=True)
    return "|".join(r"\s+".join(map(re.escape, p.split())) for p in parts)


def _compile() -> None:
    global _name_re, _phrase_re, _start_re
    names = _alt(list(RULES.get("name_aliases", [])) + list(RULES.get("name_mishears", [])))
    _name_re = re.compile(rf"\b(?:{names})\b", re.IGNORECASE) if names else None

    start_only = {s.lower() for s in RULES.get("not_now_start_only", [])}
    phrases = [p for p in RULES.get("not_now_phrases", []) if p.lower() not in start_only]
    anywhere = _alt(phrases)
    _phrase_re = re.compile(rf"\b(?:{anywhere})\b", re.IGNORECASE) if anywhere else None

    starts = _alt([p for p in RULES.get("not_now_phrases", []) if p.lower() in start_only])
    _start_re = re.compile(rf"^\W*{_LEAD_FILLERS}(?:{starts})\b", re.IGNORECASE) if starts else None


def _strip_leading_name(text: str) -> str:
    """Drop a leading address like "Twin," so "Twin, wait" reads as "wait"."""
    if _name_re is None:
        return text
    m = re.match(rf"^\W*{_LEAD_FILLERS}", text, re.IGNORECASE)
    rest = text[m.end():] if m else text
    n = _name_re.match(rest)
    return rest[n.end():] if n else text


def mentions_name(text: str) -> bool:
    """True if ``text`` addresses Twin by name (aliases or known mishears).

    Whole-word and case-insensitive: "Twin?", "hey TWIN," and "twins" match;
    "between" and "twinkle" do not.
    """
    return bool(text and _name_re is not None and _name_re.search(text))


def is_not_now(text: str) -> bool:
    """True if ``text`` tells Twin to hold off ("not now", "hang on", ...).

    Phrases in ``not_now_start_only`` (default "wait", "stop") only count as the
    whole utterance or at its start, optionally after a filler or Twin's name.
    """
    if not text:
        return False
    if _phrase_re is not None and _phrase_re.search(text):
        return True
    if _start_re is not None:
        if _start_re.search(text) or _start_re.search(_strip_leading_name(text)):
            return True
    return False


# ---------------------------------------------------------------- state

@dataclass
class TurnState:
    """Per-session speaking state. Times are epoch milliseconds (or any
    monotonic ms clock, as long as the caller is consistent)."""

    last_human_speech_ms: int = 0
    last_interjection_ms: Optional[int] = None  # None = never interjected
    hold_until_ms: int = 0
    twin_speaking: bool = False
    push_to_talk_active: bool = False
    muted: bool = False


class Decision(NamedTuple):
    """What the session should do now.

    action: "respond" | "deliver_update" | "hold" | "none"
    reason: short machine-readable why (e.g. "name", "push_to_talk",
            "not_now", "stale", "cooldown", "silence").
    """

    action: str
    reason: str = ""


def note_human_speech(state: TurnState, now_ms: int) -> None:
    """Record that a human is talking (call on interim and final text)."""
    state.last_human_speech_ms = max(state.last_human_speech_ms, now_ms)


def mark_interjection(state: TurnState, now_ms: int) -> None:
    """Record that Twin just spoke unprompted (starts the cooldown)."""
    state.last_interjection_ms = now_ms


def _ms(seconds_key: str) -> float:
    return float(RULES[seconds_key]) * 1000.0


def on_final_text(state: TurnState, text: str, now_ms: int, has_updates: bool = False) -> Decision:
    """Decide what to do after a human's final sentence.

    Order: muted -> "none"; not-now phrase -> start hold, "hold" (caller sets
    status hand_raised and cancels any speech); addressed by name or
    push-to-talk -> "respond" (allowed during a hold, and clears it);
    otherwise "none". Also counts as human speech for silence gating.
    ``has_updates`` is accepted for symmetry; pending updates are delivered by
    ``check_idle`` or woven into a reply by the Responder.
    """
    note_human_speech(state, now_ms)
    if state.muted:
        return Decision("none", "muted")
    if is_not_now(text):
        state.hold_until_ms = int(now_ms + _ms("not_now_hold_s"))
        return Decision("hold", "not_now")
    if state.push_to_talk_active:
        state.hold_until_ms = 0
        return Decision("respond", "push_to_talk")
    if RULES.get("respond_to_questions_addressed", True) and mentions_name(text):
        state.hold_until_ms = 0
        return Decision("respond", "name")
    return Decision("none", "not_addressed")


def check_idle(state: TurnState, now_ms: int, has_updates: bool, oldest_update_age_ms: int = 0) -> Decision:
    """Periodic check (~every 200 ms): may Twin speak a pending work update?

    Returns "deliver_update" only if updates are pending, not muted, Twin is
    not already speaking, the not-now hold has expired, the room has been
    silent for ``silence_before_update_s`` and the last interjection was at
    least ``interjection_cooldown_s`` ago. Returns "none"/"stale" when the
    oldest update is older than ``max_update_age_s`` (caller discards it), and
    "hold"/"not_now" while a hold is active with updates pending (UI shows
    hand_raised). The caller should call ``mark_interjection`` after speaking.
    """
    if not has_updates:
        return Decision("none", "no_updates")
    if state.muted:
        return Decision("none", "muted")
    if oldest_update_age_ms > _ms("max_update_age_s"):
        return Decision("none", "stale")
    if now_ms < state.hold_until_ms:
        return Decision("hold", "not_now")
    if state.twin_speaking:
        return Decision("none", "twin_speaking")
    if now_ms - state.last_human_speech_ms < _ms("silence_before_update_s"):
        return Decision("none", "waiting_for_silence")
    if state.last_interjection_ms is not None and now_ms - state.last_interjection_ms < _ms("interjection_cooldown_s"):
        return Decision("none", "cooldown")
    return Decision("deliver_update", "silence")


reload()

__all__ = [
    "RULES",
    "DEFAULT_RULES",
    "PLAYBOOK_VALUES_PATH",
    "TurnState",
    "Decision",
    "reload",
    "mentions_name",
    "is_not_now",
    "on_final_text",
    "check_idle",
    "mark_interjection",
    "note_human_speech",
]
