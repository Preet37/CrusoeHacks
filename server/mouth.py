"""Wires one Session to the mouth (turn-taking + Responder + TTS) and the mind (Listener).

attach(session) is idempotent. It:
  - sends every human final line to the Listener (mind path, background)
  - runs turn_taking.on_final_text for the mouth path (respond / hold)
  - runs an idle loop that delivers queued work updates at natural pauses
  - recreates the Listener on clear_board / reset (before state is cleared)
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
from pathlib import Path

import turn_taking
from agents import responder

log = logging.getLogger("twin.mouth")

IDLE_TICK_S = 0.2
DECIDE_TIMEOUT_S = 4.5
UNNAMED_PAUSE_MS = 1200      # answer an unnamed question after this much silence
MID_SENTENCE_GAP_MS = 600    # a human counts as mid-sentence until this gap
QUIET_AFTER_HOLD_MS = 2000   # after "not now", a held update may come out once quiet this long
_QUESTION_START = ("what", "how", "why", "should", "could", "can", "do ", "does", "is ", "are ", "would", "which", "who", "any thoughts")


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
                    try:
                        await self._show_ghost(session, line)
                    except Exception:  # noqa: BLE001 - the ghost is cosmetic
                        log.exception("ghost card failed")
            if self.listener is listener:  # a reset may have swapped it while we waited
                listener.submit(line.get("speaker", "?"), line.get("text", ""), decision=decision)
        if decision is not None and addressed_without_name(decision, line.get("text", "")):
            session.spawn("addressed_wait", self._answer_unnamed(session, line))

    async def _answer_unnamed(self, session, line: dict) -> None:
        """PC-19: "what do you think about Redis here?" gets an answer after a natural pause."""
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
        """Session broadcast observer: the first real draw replaces the ghost."""
        g = self._ghost
        if g is None or not isinstance(data, dict) or data.get("type") != "draw":
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

    async def on_reset(self, session) -> None:
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
        if d.action == "deliver_update":
            turn_taking.mark_interjection(self.state, now)
            self.speak("work update", trigger_ts=now, updates=s.pop_updates())
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
