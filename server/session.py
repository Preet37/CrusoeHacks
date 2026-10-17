"""Per-room Session: sockets, history, board, speaking state, work-update queue, tasks.

One Session per room (get_session). The WebSocket endpoint in app.py feeds parsed
messages into Session.handle_message. Agents get the Session object and use:
    session.board, await session.broadcast(msg), session.push_update(text),
    session.spawn(name, coro), await session.apply_draw(op_dict), session.history
"""
from __future__ import annotations

import asyncio
import inspect
import itertools
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import protocol
from protocol import BoardSnapshot, Draw, Error, StopAudio, Status, Transcript, now_ms

log = logging.getLogger("twin.session")

TWIN_SPEAKER = "Twin"


# ---------------------------------------------------------------- board

class _FallbackBoard:
    """Minimal stand-in used only if server/board.py is missing or fails to import."""

    def __init__(self) -> None:
        self.boxes: dict[str, dict] = {}
        self.edges: dict[str, dict] = {}
        self.highlighted: set[str] = set()

    def apply(self, op: dict) -> dict:
        name = op.get("op")
        item_id = op.get("id")
        if name == "add_box":
            if not item_id:
                raise ValueError("add_box needs id")
            self.boxes[item_id] = {k: op.get(k) for k in ("id", "label", "x", "y", "kind")}
        elif name == "connect":
            if not item_id or op.get("from_id") not in self.boxes or op.get("to_id") not in self.boxes:
                raise ValueError("connect needs id and existing endpoints")
            self.edges[item_id] = {"id": item_id, "from_id": op["from_id"], "to_id": op["to_id"],
                                   "label": op.get("label", "")}
        elif name == "rename":
            if item_id not in self.boxes:
                raise ValueError(f"no box {item_id}")
            self.boxes[item_id]["label"] = op.get("label", "")
        elif name == "remove":
            self.boxes.pop(item_id, None)
            self.edges.pop(item_id, None)
            for eid in [e for e, edge in self.edges.items() if item_id in (edge["from_id"], edge["to_id"])]:
                del self.edges[eid]
        elif name == "highlight":
            ids = op.get("ids") or []
            (self.highlighted.update if op.get("on") else self.highlighted.difference_update)(ids)
        elif name == "clear":
            self.boxes.clear()
            self.edges.clear()
            self.highlighted.clear()
        else:
            raise ValueError(f"unknown draw op {name!r}")
        return {"type": "draw", **{k: v for k, v in op.items() if k != "type"}}

    def snapshot(self) -> dict:
        return {"type": "board_snapshot", "boxes": [dict(b) for b in self.boxes.values()],
                "edges": [dict(e) for e in self.edges.values()]}

    def summary(self) -> str:
        if not self.boxes:
            return "Board empty."
        boxes = ", ".join(f"{b['id']}={b['label']}" for b in self.boxes.values())
        edges = ", ".join(f"{e['from_id']}->{e['to_id']}" for e in self.edges.values())
        return f"Boxes: {boxes}; Edges: {edges or 'none'}."


try:
    import board as _board_module

    BoardState = _board_module.BoardState
except Exception as e:  # noqa: BLE001 - any import problem falls back
    log.warning("board.py unavailable (%s); using fallback board", e)
    BoardState = _FallbackBoard


# ---------------------------------------------------------------- session

FinalTextHook = Callable[["Session", dict], Awaitable[Any]]
ResetHook = Callable[["Session"], Awaitable[Any] | Any]


class Session:
    def __init__(self, room: str) -> None:
        self.room = room
        self.sockets: dict[Any, str] = {}          # ws -> user name
        self._send_locks: dict[Any, asyncio.Lock] = {}
        self.history: list[dict] = []               # {speaker, text, ts}; final lines only
        self.status: str = "listening"
        self.speaking: bool = False
        self.current_say_id: str | None = None
        self._say_counter = itertools.count(1)
        self.last_human_speech_ts: int = 0
        self.push_to_talk_active: bool = False
        self.muted: bool = False
        self.board = BoardState()

        self._updates: list[str] = []
        self._tasks: dict[str, asyncio.Task] = {}
        self._anon = itertools.count(1)

        # Hooks. Mind: called for every human final line. Mouth: pluggable decision.
        self._final_text_hooks: list[FinalTextHook] = []
        self._reset_hooks: list[ResetHook] = []
        # async decide_and_respond(session, line) -> None. Runs turn-taking and, if it
        # decides to talk, should spawn the responder via session.spawn("responder", ...).
        self.decide_and_respond: Callable[["Session", dict], Awaitable[Any]] | None = None
        # async responder_hook(session, line, reason) -> None. The actual mouth.
        self.responder_hook: Callable[["Session", dict | None, str], Awaitable[Any]] | None = None

    # ------------------------------------------------------------ say ids / updates

    def new_say_id(self) -> str:
        return f"s_{next(self._say_counter)}"

    def push_update(self, text: str) -> None:
        """Queue a work update for the mouth. Sync, safe to call from any task."""
        text = (text or "").strip()
        if not text:
            return
        if self._updates and self._updates[-1] == text:
            return
        self._updates.append(text)

    def pop_updates(self) -> list[str]:
        out, self._updates = self._updates, []
        return out

    def has_updates(self) -> bool:
        return bool(self._updates)

    # ------------------------------------------------------------ background tasks

    def spawn(self, name: str | None, coro: Awaitable[Any]) -> asyncio.Task:
        """Run coro in the background under `name`, cancelling any task with that name."""
        if name is None:
            name = f"anon:{next(self._anon)}"
        self.cancel(name)
        task = asyncio.ensure_future(coro)
        self._tasks[name] = task

        def _done(t: asyncio.Task, name: str = name) -> None:
            if self._tasks.get(name) is t:
                del self._tasks[name]
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                log.error("[%s] background task %r failed", self.room, name, exc_info=exc)

        task.add_done_callback(_done)
        return task

    def cancel(self, name: str) -> bool:
        task = self._tasks.pop(name, None)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    def cancel_all(self) -> None:
        for name in list(self._tasks):
            self.cancel(name)

    def task_names(self) -> list[str]:
        return [n for n, t in self._tasks.items() if not t.done()]

    # ------------------------------------------------------------ sockets / sending

    def add_socket(self, ws, user: str) -> None:
        self.sockets[ws] = user
        self._send_locks[ws] = asyncio.Lock()

    def remove_socket(self, ws) -> None:
        self.sockets.pop(ws, None)
        self._send_locks.pop(ws, None)

    async def send_to(self, ws, msg) -> bool:
        """Send one message to one socket. Returns False (and drops the socket) on failure."""
        lock = self._send_locks.get(ws)
        try:
            if lock is None:
                await protocol.send(ws, msg)
            else:
                async with lock:
                    await protocol.send(ws, msg)
            return True
        except Exception as e:  # noqa: BLE001 - dead socket
            log.info("[%s] dropping socket %s: %s", self.room, self.sockets.get(ws), e)
            self.remove_socket(ws)
            return False

    async def broadcast(self, msg) -> None:
        """Send to every socket in the room (model or plain dict)."""
        if not self.sockets:
            return
        data = protocol.dump(msg)
        await asyncio.gather(*(self.send_to(ws, data) for ws in list(self.sockets)))

    async def set_status(self, state: str) -> None:
        if state == self.status:
            return
        self.status = state
        await self.broadcast(Status(state=state))

    async def add_line(self, speaker: str, text: str, ts: int | None = None) -> dict:
        line = {"speaker": speaker, "text": text, "ts": ts if ts is not None else now_ms()}
        self.history.append(line)
        await self.broadcast(Transcript(**line))
        return line

    # ------------------------------------------------------------ board

    def snapshot_message(self) -> BoardSnapshot:
        return BoardSnapshot.model_validate(self.board.snapshot())

    async def apply_draw(self, op: dict) -> bool:
        """Validate+apply one draw op, broadcast it. False if the board rejected it."""
        try:
            result = self.board.apply(op)
            msg = Draw.model_validate(result if isinstance(result, dict) else op)
        except ValueError as e:  # includes pydantic ValidationError
            log.warning("[%s] rejected draw op %s: %s", self.room, op, e)
            return False
        await self.broadcast(msg)
        if msg.op == "clear":
            await self.broadcast(self.snapshot_message())
        return True

    # ------------------------------------------------------------ hooks

    def on_final_text(self, cb: FinalTextHook) -> FinalTextHook:
        """Register async cb(session, line) for every human final line. Usable as decorator."""
        self._final_text_hooks.append(cb)
        return cb

    def on_reset(self, cb: ResetHook) -> ResetHook:
        """Register cb(session) (sync or async) called on reset_session."""
        self._reset_hooks.append(cb)
        return cb

    def _run_final_text_hooks(self, line: dict) -> None:
        for cb in self._final_text_hooks:
            self.spawn(None, cb(self, line))

    def respond_hook_check(self, line: dict) -> None:
        """Mouth path. No-op unless self.decide_and_respond is set."""
        if self.decide_and_respond is not None:
            self.spawn(None, self.decide_and_respond(self, line))

    # ------------------------------------------------------------ actions

    async def barge_in(self) -> None:
        self.cancel("responder")
        self.cancel("tts")
        await self.broadcast(StopAudio(say_id=self.current_say_id))
        self.current_say_id = None
        self.speaking = False
        await self.set_status("listening")

    async def reset(self) -> None:
        self.cancel_all()
        self.history.clear()
        self._updates.clear()
        self.board = BoardState()
        self.current_say_id = None
        self.speaking = False
        self.push_to_talk_active = False
        for cb in self._reset_hooks:
            try:
                res = cb(self)
                if inspect.isawaitable(res):
                    await res
            except Exception:  # noqa: BLE001
                log.exception("[%s] reset hook failed", self.room)
        await self.broadcast(StopAudio())
        await self.set_status("listening")
        await self.broadcast(self.snapshot_message())

    async def handle_message(self, ws, user: str, msg) -> None:
        t = msg.type
        if t == "hello":
            if msg.user and ws in self.sockets:
                self.sockets[ws] = msg.user
        elif t == "user_text":
            self.last_human_speech_ts = now_ms()
            if msg.final:
                text = msg.text.strip()
                if not text:
                    return
                line = await self.add_line(msg.speaker or user, text, msg.ts)
                self._run_final_text_hooks(line)
                self.respond_hook_check(line)
        elif t == "barge_in":
            self.last_human_speech_ts = now_ms()
            await self.barge_in()
        elif t == "push_to_talk":
            self.push_to_talk_active = msg.active
            if msg.active:
                self.last_human_speech_ts = now_ms()
        elif t == "control":
            if msg.action == "mute_twin":
                self.muted = True
                if self.speaking or self.current_say_id:
                    await self.barge_in()
            elif msg.action == "unmute_twin":
                self.muted = False
            elif msg.action == "clear_board":
                await self.apply_draw({"op": "clear"})
            elif msg.action == "reset_session":
                await self.reset()
        else:
            await self.send_to(ws, Error(message=f"unexpected message type {t!r}", recoverable=True))

    def debug_state(self) -> dict:
        return {
            "room": self.room,
            "history_len": len(self.history),
            "status": self.status,
            "speaking": self.speaking,
            "muted": self.muted,
            "sockets": len(self.sockets),
            "users": list(self.sockets.values()),
            "pending_updates": list(self._updates),
            "tasks": self.task_names(),
            "board": self.board.snapshot(),
        }


# ---------------------------------------------------------------- registry

SESSIONS: dict[str, Session] = {}


def get_session(room: str) -> Session:
    session = SESSIONS.get(room)
    if session is None:
        session = SESSIONS[room] = Session(room)
    return session
