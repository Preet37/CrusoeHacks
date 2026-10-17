"""Wires one Session to the mouth (turn-taking + Responder + TTS) and the mind (Listener).

attach(session) is idempotent. It:
  - sends every human final line to the Listener (mind path, background)
  - runs turn_taking.on_final_text for the mouth path (respond / hold)
  - runs an idle loop that delivers queued work updates at natural pauses
  - recreates the Listener on clear_board / reset (before state is cleared)
"""
from __future__ import annotations

import asyncio
import re
import itertools
import json
import logging
import time
from pathlib import Path

import config
import turn_taking
from agents import responder

log = logging.getLogger("twin.mouth")

IDLE_TICK_S = 0.2
DECIDE_TIMEOUT_S = 4.5
SKETCH_ACK = "Got it, sketching that now."
UNNAMED_PAUSE_MS = 1200      # answer an unnamed question after this much silence
MID_SENTENCE_GAP_MS = 600    # a human counts as mid-sentence until this gap
QUIET_AFTER_HOLD_MS = 2000   # after "not now", a held update may come out once quiet this long
_QUESTION_START = ("what", "how", "why", "should", "could", "can", "do ", "does", "is ", "are ", "would", "which", "who", "any thoughts")


# Step 1 of a visual turn (15:17): say WHAT is coming, instantly (pre-synthesized at startup).
VISUAL_PHRASES = {
    "architecture": "Here's how those pieces connect.",
    "sequence": "Let me lay out the flow.",
    "chart": "I'll chart those numbers.",
    "mockup": "Here's a rough screen.",
    "table": "Let me compare those side by side.",
    "code": "Here's the relevant code.",
    "3d": "Let me show it in 3D.",
}
FACT_PHRASE = "Let me pull up what the repo says."
# Proactive backchannel: one short reaction at the first gap after a substantive (non-visual) line.
BACKCHANNEL_PHRASES = {
    "decision": "Good call, let me check what that touches.",
    "claim": "Let me check that against the repo.",
    "question": "Good question, one sec.",
    "task": "Got it, I'll note that.",
    "system_fact": "Got it, adding that.",
}
BACKCHANNEL_MIN_CONF = 0.6
BACKCHANNEL_MIN_WORDS = 4
BACKCHANNEL_GAP_MS = 600          # speak at the first human pause this long
BACKCHANNEL_MAX_WAIT_MS = 4000    # the room never paused: drop it
BACKCHANNEL_RATE_MS = 10000       # at most one per room per 10 s
BACKCHANNEL_FOLLOW_MS = 15000     # a contribution may chain onto a backchannel this recent
PREWARM_PHRASES = list(dict.fromkeys([*VISUAL_PHRASES.values(), FACT_PHRASE, SKETCH_ACK,
                                      *BACKCHANNEL_PHRASES.values()]))

# Contribution speech (Codex 15:23 contract: broadcast {"type": "contribution", show, current,
# impact[], risks[], question, sources[]}, also session.latest_contribution).
CONTRIB_MAX_SENTENCES = 4
CONTRIB_MAX_WORDS = 30
CONTRIB_PHRASE_WAIT_MS = 3000     # addressed: let the step-1 phrase finish first (max)
CONTRIB_BUSY_WAIT_MS = 8000       # addressed: let an already-speaking reply finish (max)
CONTRIB_GAP_MS = 700              # proactive: speak at the next gap this long
CONTRIB_GAP_WAIT_MS = 12000       # proactive: give up if the room never pauses
CONTRIB_TRIGGER_TTL_MS = 30000    # a visual trigger older than this no longer counts
NARRATE_MAX_LABELS = 4
_SHAPE_OPS = {"add_box", "add_sticky", "add_text", "add_mermaid", "add_chart", "add_table",
              "add_code", "add_mock", "add_3d", "sketch"}
_CODE_CHANGE_CLAIM = re.compile(
    r"\b(i|we)('ve| have)?\s+(just\s+)?(changed|updated|modified|fixed|edited|refactored|committed|pushed|"
    r"rewrote|rewritten|implemented|patched|deployed)\b", re.I)
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")


def visual_phrase(decision) -> str:
    """The <=0.5 s line for a predicted visual: what Twin is about to show."""
    visual, _ = _choice(decision, "wants_visual") if isinstance(decision, dict) else (None, 0.0)
    if visual in VISUAL_PHRASES:
        return VISUAL_PHRASES[visual]
    kind, _ = _choice(decision, "kind") if isinstance(decision, dict) else (None, 0.0)
    return FACT_PHRASE if kind == "system_fact" else SKETCH_ACK


def backchannel_phrase(decision, text: str) -> str:
    """The proactive one-liner for a substantive line; "" when none applies."""
    if not isinstance(decision, dict) or len(str(text or "").split()) < BACKCHANNEL_MIN_WORDS:
        return ""
    kind, conf = _choice(decision, "kind")
    if kind not in BACKCHANNEL_PHRASES or conf < BACKCHANNEL_MIN_CONF:
        return ""
    return BACKCHANNEL_PHRASES[kind]


def _sentence(text) -> str:
    """First sentence of text, capped, ending in punctuation. File names (auth/login.py) survive."""
    t = " ".join(str(text or "").split())
    if not t:
        return ""
    t = _SENT_SPLIT.split(t, maxsplit=1)[0].strip()
    words = t.split()
    if len(words) > CONTRIB_MAX_WORDS:
        t = " ".join(words[:CONTRIB_MAX_WORDS]).rstrip(",;:")
    if t and t[-1] not in ".!?":
        t += "."
    return t


def narration(labels: list[str]) -> str:
    """One sentence grounded in labels that were actually drawn; "" when none."""
    labels = [l for l in labels if l][:NARRATE_MAX_LABELS]
    if not labels:
        return ""
    if len(labels) == 1:
        return f"That's {labels[0]} on the board."
    return "Left is " + ", then ".join(labels) + "."


def compose_contribution(c: dict, labels: list[str] | None = None) -> str:
    """narration?, current, top impact, top risk, question -> at most 4 short sentences.

    The question is always kept; risk then impact are trimmed first. Sentences that
    claim code was changed are dropped (Twin only reads the repo).
    """
    if not isinstance(c, dict):
        return ""
    def first(v):
        if isinstance(v, (list, tuple)):
            v = next((x for x in v if str(x or "").strip()), "")
        return _sentence(v)
    q = _sentence(c.get("question"))
    if q and q[-1] != "?":
        q = q.rstrip(".!") + "?"
    body = [narration(labels or []), _sentence(c.get("current")), first(c.get("impact")), first(c.get("risks"))]
    body = [b for b in body if b and not _CODE_CHANGE_CLAIM.search(b)]
    if q and _CODE_CHANGE_CLAIM.search(q):
        q = ""
    body = body[:CONTRIB_MAX_SENTENCES - (1 if q else 0)]
    return " ".join(body + ([q] if q else [])).strip()


_DRAW_CLAIM = re.compile(r"\b(drew|drawn|draw|built|build|sketch\w*|added|put|placed|diagram|board)\b", re.I)


def board_is_empty(session) -> bool:
    board = getattr(session, "board", None)
    try:
        snap = board.snapshot() if board is not None else {}
    except Exception:  # noqa: BLE001
        return False
    return not (snap.get("boxes") or snap.get("edges") or snap.get("shapes"))


def drop_false_draw_claims(session, updates: list[str]) -> list[str]:
    """B2: never say "here's what I built" when nothing is on the board (e.g. a rejected drawing)."""
    if not updates or not board_is_empty(session):
        return updates
    kept = [u for u in updates if not _DRAW_CLAIM.search(u)]
    if len(kept) != len(updates):
        log.info("[%s] dropped %d draw-claim update(s) on an empty board", getattr(session, "room", "?"), len(updates) - len(kept))
    return kept


def addressed_without_name(decision: dict, text: str) -> bool:
    """JEV says the line is directed at Twin AND it really is a question. A score alone
    never triggers speech (Nano has scored small talk as addressed)."""
    import turn_taking as tt
    if tt.mentions_name(text):
        return False  # the lexical path already answers it
    a = decision.get("addressed") if isinstance(decision, dict) else None
    score = a.get("noul") if isinstance(a, dict) else None
    if not isinstance(score, (int, float)) or score < 0.75:
        return False
    k = decision.get("kind") or {}
    is_q_kind = isinstance(k, dict) and k.get("choice") == "question" and (k.get("confidence") or 0) >= 0.6
    t = (text or "").strip().lower()
    looks_q = t.endswith("?") or t.startswith(_QUESTION_START)
    return bool(looks_q and (is_q_kind or t.endswith("?")) and len(t.split()) >= 3)

# PC-20 ghost card: broadcast-only placeholder while the Diagrammer works.
GHOST_TIMEOUT_S = 15.0
GHOST_VISUAL_MIN = 0.6
GHOST_FACT_MIN = 0.7
GHOST_TEXT = "Twin is sketching\u2026"
GHOST_X, GHOST_Y = 820, 80
_NON_SHAPE_OPS = {"remove", "frame", "focus", "highlight"}
LATENCY_LOG = Path(__file__).resolve().parent / "learning" / "runs" / "latency.jsonl"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _choice(decision: dict, key: str) -> tuple[str | None, float]:
    v = decision.get(key)
    if not isinstance(v, dict):
        return None, 0.0
    try:
        conf = float(v.get("confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    return v.get("choice"), conf


def wants_ghost(decision) -> bool:
    """A reflex decision that predicts a drawing is coming."""
    if not isinstance(decision, dict):
        return False
    visual, vconf = _choice(decision, "wants_visual")
    if visual and visual != "none" and vconf >= GHOST_VISUAL_MIN:
        return True
    kind, kconf = _choice(decision, "kind")
    return kind == "system_fact" and kconf >= GHOST_FACT_MIN


def _log_first_shape(first_shape_ms: int, ghost_ms: int) -> None:
    row = {"ts": _now_ms(), "stage": "first_shape", "first_shape_ms": first_shape_ms, "ghost_ms": ghost_ms}
    try:
        LATENCY_LOG.parent.mkdir(parents=True, exist_ok=True)
        with LATENCY_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except OSError:
        log.warning("could not write first_shape latency")


def _make_listener(session):
    try:
        from agents.listener import Listener
    except Exception:  # noqa: BLE001 - mind is optional; the mouth must still work
        log.exception("Listener unavailable; drawing disabled")
        return None
    return Listener(session)


class Mouth:
    def __init__(self, session) -> None:
        self.session = session
        self.state = turn_taking.TurnState()
        self.listener = _make_listener(session)
        self._updates_since: int | None = None
        self._idle_task: asyncio.Task | None = None
        self._turn: dict | None = None
        self._mind_lock = asyncio.Lock()
        self._ghost: dict | None = None  # {"id", "line_ts", "ghost_ms", "timer"}
        self._ghost_ids = itertools.count(1)
        self._ghost_tasks: set[asyncio.Task] = set()
        self._phrase_until_ms = 0          # when the step-1 phrase finishes playing
        self._visual_turn: dict | None = None  # {"ts", "addressed"} of the latest visual line
        self._shape_labels: list[tuple[float, int, str]] = []  # (x, order, label) drawn since then
        self._spoken_contribs: set[str] = set()
        self._last_backchannel_ms = 0
        self._backchannel: dict | None = None  # {"line_ts", "started_ms", "until_ms"} of the last one spoken

    # ------------------------------------------------------------ wiring
    def attach(self) -> None:
        s = self.session
        try:
            from brain import runtime as mind_runtime
            mind_runtime.attach(s)  # code map, sourced memory, repo connector (no network)
        except Exception:  # noqa: BLE001 - the meeting must work without the brain
            log.exception("brain runtime unavailable")
        s.decide_and_respond = self.on_final
        s.on_final_text(self.on_final_mind)
        s.on_reset(self.on_reset)
        observers = getattr(s, "_broadcast_observers", None)
        if observers is not None and self._on_broadcast not in observers:
            observers.append(self._on_broadcast)
        self._register_draw_repair()
        self.ensure_idle_loop()

    def _register_draw_repair(self) -> None:
        """PREET-CODEX's Diagrammer may fix a shape the browser failed to render."""
        try:
            from agents import diagrammer
        except Exception:  # noqa: BLE001
            return
        repair = getattr(diagrammer, "repair_draw", None)
        if repair is not None and hasattr(self.session, "on_draw_error"):
            self.session.on_draw_error(repair)

    def ensure_idle_loop(self) -> None:
        loop = asyncio.get_running_loop()
        t = self._idle_task
        if t is None or t.done() or t.get_loop() is not loop:
            self._idle_task = loop.create_task(self._idle_loop())

    def _sync(self) -> None:
        s, st = self.session, self.state
        st.last_human_speech_ms = max(st.last_human_speech_ms, s.last_human_speech_ts or 0)
        st.twin_speaking = bool(s.speaking or s.current_say_id)
        st.push_to_talk_active = bool(s.push_to_talk_active)
        st.muted = bool(s.muted)

    # ------------------------------------------------------------ mind path
    async def on_final_mind(self, session, line: dict) -> None:
        """One JEV/Nano reflex decision per final line, then hand it to the Listener.

        The lock keeps lines in arrival order even when one decision is slow.
        Fails open: no decision just means the Listener does its normal work.
        """
        async with self._mind_lock:
            listener = self.listener
            if listener is None:
                return
            decision = await self._decide(session, line)
            if decision is not None:
                line["decision"] = decision
                if wants_ghost(decision):
                    self._mark_visual_turn(session, line, decision)
                    try:
                        await self._show_ghost(session, line)
                    except Exception:  # noqa: BLE001 - the ghost is cosmetic
                        log.exception("ghost card failed")
                    try:
                        await self._say_cached(session, visual_phrase(decision))  # step 1: what's coming
                    except Exception:  # noqa: BLE001
                        log.exception("visual phrase failed")
                else:
                    phrase = backchannel_phrase(decision, line.get("text", ""))
                    if phrase and getattr(session, "twin_mode", "proactive") == "proactive":
                        session.spawn("backchannel", self._backchannel_at_gap(session, line, phrase))
            if self.listener is listener:  # a reset may have swapped it while we waited
                listener.submit(line.get("speaker", "?"), line.get("text", ""), decision=decision)
        if decision is not None and addressed_without_name(decision, line.get("text", "")):
            session.spawn("addressed_wait", self._answer_unnamed(session, line))

    async def _answer_unnamed(self, session, line: dict) -> None:
        """PC-19: "what do you think about Redis here?" gets an answer after a natural pause."""
        if getattr(session, "twin_mode", "proactive") != "proactive":
            return  # on_request: only answer when named or push-to-talk
        if session.muted or _now_ms() < self.state.hold_until_ms:
            return
        if self._turn_running():
            return  # already answering (named in the same line, or busy)
        deadline = _now_ms() + 6000
        while _now_ms() < deadline:
            last = max(session.last_human_speech_ts or 0, line.get("ts") or 0)
            if _now_ms() - last >= UNNAMED_PAUSE_MS:
                break
            await asyncio.sleep(0.1)
        else:
            return  # the room never went quiet; let it go
        if session.history and session.history[-1].get("text") != line.get("text"):
            return  # someone said something newer; the moment passed
        if self._turn_running() or session.muted or _now_ms() < self.state.hold_until_ms:
            return
        log.info("[%s] answering unnamed question: %r", session.room, line.get("text", "")[:80])
        self.speak("asked a question without saying your name", trigger_ts=_now_ms(), updates=session.pop_updates())

    async def _backchannel_at_gap(self, session, line: dict, phrase: str) -> None:
        """Proactive only: say one short reaction at the first >= 0.6 s human pause (max 4 s)."""
        import turn_taking as tt
        if tt.mentions_name(line.get("text", "")) or getattr(session, "push_to_talk_active", False):
            return  # addressed: the responder answers properly
        line_ts = int(line.get("ts") or _now_ms())
        start = _now_ms()
        while True:
            last = max(session.last_human_speech_ts or 0, line_ts)
            if _now_ms() - last >= BACKCHANNEL_GAP_MS:
                break
            if _now_ms() - start > BACKCHANNEL_MAX_WAIT_MS:
                return
            await asyncio.sleep(0.05)
        now = _now_ms()
        if (getattr(session, "twin_mode", "proactive") != "proactive" or session.muted
                or now < self.state.hold_until_ms or self._turn_running() or now < self._phrase_until_ms
                or (self._last_backchannel_ms and now - self._last_backchannel_ms < BACKCHANNEL_RATE_MS)):
            return
        self._last_backchannel_ms = now
        await self._say_cached(session, phrase)
        self._backchannel = {"line_ts": line_ts, "started_ms": now, "until_ms": max(self._phrase_until_ms, now)}
        log.info("[%s] backchannel: %r", session.room, phrase)

    def _follows_backchannel(self) -> bool:
        """A backchannel just played and no human has spoken since it started."""
        b, s = self._backchannel, self.session
        if b is None or _now_ms() - b["started_ms"] > BACKCHANNEL_FOLLOW_MS:
            return False
        return (s.last_human_speech_ts or 0) <= b["started_ms"] and _now_ms() >= b["until_ms"]

    async def _say_cached(self, session, text: str) -> None:
        """Speak a short pre-synthesized line right now, unless Twin is busy, muted or holding."""
        if session.muted or _now_ms() < self.state.hold_until_ms or self._turn_running():
            return
        if _now_ms() - (session.last_human_speech_ts or 0) < 300 and session.last_human_speech_ts:
            pass  # the line just ended; fine to acknowledge
        import base64
        from voice import tts
        wav = tts.cached(text)
        say_id = session.new_say_id()
        if wav:  # 16-bit mono 44.1 kHz after a 44-byte header
            self._phrase_until_ms = _now_ms() + int(max(0, len(wav) - 44) / 88.2)
        await session.broadcast({"type": "audio", "say_id": say_id, "seq": 0, "format": "wav", "sample_rate": 44100,
                                 "text": text, "data": base64.b64encode(wav).decode("ascii") if wav else None,
                                 "last": True})
        await session.add_line(config.TWIN_NAME, text)

    def _turn_running(self) -> bool:
        t = self.session._tasks.get("responder") if hasattr(self.session, "_tasks") else None
        return bool((t is not None and not t.done()) or self.session.speaking or self.session.current_say_id)

    async def _decide(self, session, line: dict) -> dict | None:
        try:
            from agents import fast_decide
        except Exception:  # noqa: BLE001
            return None
        recent = [f"{h.get('speaker')}: {h.get('text')}" for h in list(session.history)[-8:-1]]
        state = {
            "latest_sentence": line.get("text", ""),
            "speaker": line.get("speaker", "?"),
            "recent_transcript": recent,
            "active_topic": getattr(session, "active_topic", None),
            "twin_recent_speech": list(getattr(session, "twin_recent", []) or [])[-3:],
        }
        started = time.monotonic()
        try:
            decision = await asyncio.wait_for(fast_decide.decide(state, fast_decide.TURN_QUESTIONS), DECIDE_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - includes timeout; fail open
            log.warning("[%s] reflex decision failed (%s); listener runs normally", session.room, type(exc).__name__)
            return None
        log.info("[%s] reflex decision in %dms", session.room, (time.monotonic() - started) * 1000)
        return decision

    # ------------------------------------------------------------ ghost card (PC-20)
    async def _show_ghost(self, session, line: dict) -> None:
        """Broadcast-only "sketching" sticky; never touches BoardState or snapshots."""
        if self._ghost is not None:
            return  # at most one per room
        now = _now_ms()
        line_ts = int(line.get("ts") or now)
        gid = f"ghost_{next(self._ghost_ids)}"
        msg = {"type": "draw", "op": "add_sticky", "id": gid, "text": GHOST_TEXT, "color": "blue",
               "x": GHOST_X, "y": GHOST_Y, "by": "Twin"}
        topic = getattr(session, "active_topic", None)
        if isinstance(topic, dict) and topic.get("topic_id"):
            msg["frame"] = topic["topic_id"]
        timer = asyncio.get_running_loop().create_task(self._ghost_timeout(gid))
        self._ghost = {"id": gid, "line_ts": line_ts, "ghost_ms": max(0, now - line_ts), "timer": timer}
        await session.broadcast(msg)
        log.info("[%s] ghost %s shown %dms after line", session.room, gid, now - line_ts)

    async def _ghost_timeout(self, gid: str) -> None:
        await asyncio.sleep(GHOST_TIMEOUT_S)
        if self._ghost is not None and self._ghost["id"] == gid:
            self._ghost = None
            await self.session.broadcast({"type": "draw", "op": "remove", "id": gid})

    def _on_broadcast(self, data) -> None:
        """Session broadcast observer: contributions get spoken, shape labels are kept
        for narration, and the first real draw replaces the ghost."""
        if not isinstance(data, dict):
            return
        if data.get("type") == "contribution":
            self._on_contribution(data)
            return
        if data.get("type") != "draw":
            return
        self._collect_label(data)
        g = self._ghost
        if g is None:
            return
        op, item_id = data.get("op"), str(data.get("id") or "")
        if op in _NON_SHAPE_OPS or item_id.startswith("ghost_"):
            return
        self._ghost = None
        g["timer"].cancel()
        if op != "clear":
            _log_first_shape(max(0, _now_ms() - g["line_ts"]), g["ghost_ms"])
        task = asyncio.get_running_loop().create_task(
            self.session.broadcast({"type": "draw", "op": "remove", "id": g["id"]}))
        self._ghost_tasks.add(task)
        task.add_done_callback(self._ghost_tasks.discard)

    async def _clear_ghost(self) -> None:
        g, self._ghost = self._ghost, None
        if g is not None:
            g["timer"].cancel()
            await self.session.broadcast({"type": "draw", "op": "remove", "id": g["id"]})

    # ------------------------------------------------------------ contribution (15:17)
    def _mark_visual_turn(self, session, line: dict, decision) -> None:
        import turn_taking as tt
        text = line.get("text", "")
        addressed = bool(tt.mentions_name(text) or getattr(session, "push_to_talk_active", False)
                         or addressed_without_name(decision, text))
        self._visual_turn = {"ts": _now_ms(), "addressed": addressed}
        self._shape_labels = []

    def _collect_label(self, data: dict) -> None:
        if data.get("op") not in _SHAPE_OPS or str(data.get("id") or "").startswith("ghost_"):
            return
        if data.get("by") not in (None, "Twin", config.TWIN_NAME):
            return  # a human drew it
        label = next((str(data[k]).strip() for k in ("label", "title", "text")
                      if isinstance(data.get(k), str) and data[k].strip()), "")
        if not label:
            return
        label = " ".join(label.split()[:4])
        if any(l == label for _, _, l in self._shape_labels):
            return
        x = data.get("x")
        self._shape_labels.append((float(x) if isinstance(x, (int, float)) else float("inf"),
                                   len(self._shape_labels), label))

    def narration_labels(self) -> list[str]:
        return [l for _, _, l in sorted(self._shape_labels)][:NARRATE_MAX_LABELS]

    def _on_contribution(self, msg: dict) -> None:
        cid = str(msg.get("id") or "")
        if not cid or cid in self._spoken_contribs:
            return
        try:
            self.session.spawn("contribution", self._deliver_contribution(msg))
        except RuntimeError:  # no running loop
            log.warning("contribution arrived without a running loop")

    def _contribution_addressed(self) -> bool:
        t = self._visual_turn
        if t is not None and _now_ms() - t["ts"] <= CONTRIB_TRIGGER_TTL_MS:
            return bool(t["addressed"])
        import turn_taking as tt
        hist = [h for h in list(self.session.history) if h.get("speaker") != config.TWIN_NAME]
        return bool(hist and tt.mentions_name(hist[-1].get("text", "")))

    async def _deliver_contribution(self, msg: dict) -> None:
        """Timing + mode for the spoken contribution. Card-only when it should stay quiet."""
        s = self.session
        cid = str(msg.get("id"))
        mode = getattr(s, "twin_mode", "proactive")
        if mode == "off" or s.muted:
            return
        addressed = self._contribution_addressed()
        if not addressed and (mode != "proactive" or _now_ms() < self.state.hold_until_ms):
            return  # on_request and not asked: the "Twin's take" card carries it
        arrived = _now_ms()
        if addressed:
            while _now_ms() - arrived < CONTRIB_PHRASE_WAIT_MS and (
                    _now_ms() < self._phrase_until_ms or s.speaking):
                await asyncio.sleep(0.05)
            while s.speaking and _now_ms() - arrived < CONTRIB_BUSY_WAIT_MS:
                await asyncio.sleep(0.05)  # an answer is already playing: follow it, don't cut it
            if s.speaking:
                return
        else:
            while True:
                quiet = _now_ms() - (s.last_human_speech_ts or 0)
                free = not s.speaking and not s.current_say_id and _now_ms() >= self._phrase_until_ms
                if free and (quiet >= CONTRIB_GAP_MS or self._follows_backchannel()):
                    break
                if _now_ms() - arrived > CONTRIB_GAP_WAIT_MS:
                    return
                await asyncio.sleep(0.1)
        if getattr(s, "twin_mode", "proactive") == "off" or s.muted or cid in self._spoken_contribs:
            return
        if not addressed and _now_ms() < self.state.hold_until_ms:
            return  # a "not now" arrived while we waited
        labels = self.narration_labels() if not board_is_empty(s) else []
        text = compose_contribution(msg, labels)
        sentences = drop_false_draw_claims(s, _SENT_SPLIT.split(text)) if text else []
        text = " ".join(sentences).strip()
        if not text:
            return
        self._spoken_contribs.add(cid)
        self._turn = None
        self._updates_since = None
        s.spawn("responder", self._speak_contribution(text))  # barge-in cancels "responder"

    async def _speak_contribution(self, text: str) -> str:
        from voice import tts
        s = self.session
        say_id = s.new_say_id()
        s.current_say_id = say_id
        started = {"v": False}

        async def emit(msg: dict) -> None:
            if s.current_say_id != say_id:
                return  # barge-in or a newer turn took over
            if not started["v"]:
                started["v"] = True
                s.speaking = True
                await s.set_status("speaking")
            await s.broadcast(msg)

        spoken = ""
        try:
            spoken = (await tts.speak_text(text, say_id, emit) or "").strip()
            if spoken and s.current_say_id == say_id:
                await s.add_line(config.TWIN_NAME, spoken)
        finally:
            if s.current_say_id == say_id:
                s.current_say_id = None
                s.speaking = False
                await s.set_status("listening")
        return spoken

    async def on_reset(self, session) -> None:
        self._visual_turn = None
        self._shape_labels = []
        self._backchannel = None
        try:
            await self._clear_ghost()
        except Exception:  # noqa: BLE001
            log.exception("ghost clear failed")
        if self.listener is not None:
            try:
                await self.listener.close()
            except Exception:  # noqa: BLE001
                log.exception("listener close failed")
        # Topic reset: the next Listener starts with no active topic or frames.
        session.active_topic = None
        if hasattr(session, "topic_catalog"):
            session.topic_catalog = {}
        self.listener = _make_listener(session)
        self._updates_since = None
        self.state.hold_until_ms = 0

    # ------------------------------------------------------------ mouth path
    async def on_final(self, session, line: dict) -> None:
        self._sync()
        now = _now_ms()
        d = turn_taking.on_final_text(self.state, line.get("text", ""), now, session.has_updates())
        log.info("[%s] turn: %s (%s)", session.room, d.action, d.reason)
        if d.action == "hold":
            if self.state.twin_speaking:
                await session.barge_in()
            await session.set_status("hand_raised")
        elif d.action == "respond":
            updates = self._take_unstarted_turn() + session.pop_updates()
            self.speak(d.reason, trigger_ts=line.get("ts") or now, updates=updates)
        elif self._unstarted_turn_running():
            # Newest wins: a human kept talking before Twin said anything, so restart
            # the turn with the fresh transcript instead of answering stale input.
            pending = self._turn
            updates = self._take_unstarted_turn()
            log.info("[%s] newest wins: restarting unstarted turn", session.room)
            self.speak(pending["reason"], trigger_ts=line.get("ts") or now, updates=updates)

    def _unstarted_turn_running(self) -> bool:
        s = self.session
        t = s._tasks.get("responder") if hasattr(s, "_tasks") else None
        return bool(t is not None and not t.done() and self._turn and not s.speaking)

    def _take_unstarted_turn(self) -> list[str]:
        """Cancel a responder turn that has not produced audio yet; return its updates."""
        if not self._unstarted_turn_running():
            return []
        updates = list(self._turn.get("updates") or [])
        self.session.cancel("responder")
        self._turn = None
        return updates

    def speak(self, reason: str, *, trigger_ts: int, updates: list[str]) -> None:
        self._updates_since = None
        self._turn = {"reason": reason, "updates": list(updates)}
        self.session.spawn("responder", self._speak_when_clear(reason, trigger_ts, updates))

    async def _speak_when_clear(self, reason: str, trigger_ts: int, updates: list[str]) -> None:
        """Never start talking over a human: if someone is still mid-sentence (fresh interim
        captions after the triggering line), wait for a short gap first (max 4 s)."""
        s = self.session
        deadline = _now_ms() + 4000
        while _now_ms() < deadline:
            last = s.last_human_speech_ts or 0
            if last <= trigger_ts + 50 or _now_ms() - last >= MID_SENTENCE_GAP_MS:
                break
            await asyncio.sleep(0.08)
        await responder.respond(s, reason, trigger_ts=trigger_ts, updates=updates)

    def greet(self, meeting: str, predictions: list[str]) -> None:
        """calendar_join: Twin says hi, mentioning the brief."""
        preds = "; ".join(predictions[:3]) if predictions else "nothing specific"
        self.speak("calendar join: greet the team in one short sentence and mention the first prediction",
                   trigger_ts=_now_ms(),
                   updates=[f"You just joined {meeting}. Your brief predicts: {preds}"])

    async def _idle_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(IDLE_TICK_S)
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("idle tick failed")

    async def _tick(self) -> None:
        s = self.session
        if hasattr(s, "broadcast_participants"):
            await s.broadcast_participants()  # diffs; sends only when someone starts/stops speaking
        self._sync()
        now = _now_ms()
        has = s.has_updates()
        if has and self._updates_since is None:
            self._updates_since = now
        if not has:
            self._updates_since = None
        age = now - self._updates_since if self._updates_since else 0
        if has and now < self.state.hold_until_ms:
            hold_start = self.state.hold_until_ms - int(turn_taking.RULES.get("not_now_hold_s", 30) * 1000)
            quiet = now - (self.state.last_human_speech_ms or 0)
            if now - hold_start >= QUIET_AFTER_HOLD_MS and quiet >= QUIET_AFTER_HOLD_MS:
                self.state.hold_until_ms = now  # room went quiet: the held thought may come out now
        d = turn_taking.check_idle(self.state, now, has, age, urgent=bool(getattr(s, "urgent_updates", False)))
        if d.action == "deliver_update" and getattr(s, "twin_mode", "proactive") == "on_request" \
                and not getattr(s, "urgent_updates", False):
            d = turn_taking.Decision("none", "on_request_mode")  # updates wait until someone asks Twin
        if d.action == "deliver_update":
            updates = drop_false_draw_claims(s, s.pop_updates())
            if updates:
                turn_taking.mark_interjection(self.state, now)
                self.speak("work update", trigger_ts=now, updates=updates)
        elif d.action == "none" and d.reason == "stale":
            dropped = s.pop_updates()
            self._updates_since = None
            log.info("[%s] dropped stale updates: %s", s.room, dropped)
        elif d.action == "hold":
            if s.status not in ("hand_raised", "speaking", "thinking"):
                await s.set_status("hand_raised")
        if (s.status == "listening" and now < self.state.hold_until_ms and not self.state.twin_speaking
                and not s.current_say_id):
            await s.set_status("hand_raised")  # a turn ended inside a "not now" hold: keep the hand up
        if s.status == "hand_raised" and now >= self.state.hold_until_ms and not self.state.twin_speaking:
            await s.set_status("listening")


async def detach(session) -> None:
    """Last socket left: stop the idle loop, the Listener and any running turn.

    History and board stay on the Session so a reconnect picks up where it was.
    """
    mouth = getattr(session, "mouth", None)
    if mouth is None:
        return
    session.mouth = None
    session.decide_and_respond = None
    if mouth.on_final_mind in session._final_text_hooks:
        session._final_text_hooks.remove(mouth.on_final_mind)
    if mouth.on_reset in session._reset_hooks:
        session._reset_hooks.remove(mouth.on_reset)
    observers = getattr(session, "_broadcast_observers", None)
    if observers is not None and mouth._on_broadcast in observers:
        observers.remove(mouth._on_broadcast)
    if mouth._ghost is not None:
        mouth._ghost["timer"].cancel()
        mouth._ghost = None
    if mouth._idle_task is not None:
        mouth._idle_task.cancel()
    if mouth.listener is not None:
        try:
            await mouth.listener.close()
        except Exception:  # noqa: BLE001
            log.exception("listener close failed")
    session.cancel_all()
    session.current_say_id = None
    session.speaking = False


def attach(session) -> Mouth:
    mouth = getattr(session, "mouth", None)
    if mouth is None:
        mouth = Mouth(session)
        session.mouth = mouth
        mouth.attach()
    else:
        mouth.ensure_idle_loop()
    return mouth
