"""Wires one Session to the mouth (turn-taking + Responder + TTS) and the mind (Listener).

attach(session) is idempotent. It:
  - sends every human final line to the Listener (mind path, background)
  - runs turn_taking.on_final_text for the mouth path (respond / hold)
  - runs an idle loop that delivers queued work updates at natural pauses
  - recreates the Listener on clear_board / reset (before state is cleared)
"""
from __future__ import annotations

import asyncio
import logging
import time

import turn_taking
from agents import responder

log = logging.getLogger("twin.mouth")

IDLE_TICK_S = 0.2


def _now_ms() -> int:
    return int(time.time() * 1000)


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

    # ------------------------------------------------------------ wiring
    def attach(self) -> None:
        s = self.session
        s.decide_and_respond = self.on_final
        s.on_final_text(self.on_final_mind)
        s.on_reset(self.on_reset)
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
        if self.listener is not None:
            self.listener.submit(line.get("speaker", "?"), line.get("text", ""))

    async def on_reset(self, session) -> None:
        if self.listener is not None:
            try:
                await self.listener.close()
            except Exception:  # noqa: BLE001
                log.exception("listener close failed")
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
        self.session.spawn("responder", responder.respond(
            self.session, reason, trigger_ts=trigger_ts, updates=updates))

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
