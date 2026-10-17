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
import json
import logging
import re
from collections import deque
from collections.abc import Awaitable, Callable
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import protocol
from protocol import Brief, BoardSnapshot, Draw, Error, StopAudio, Status, Transcript, now_ms

try:
    import config as _config

    TWIN_SPEAKER = getattr(_config, "TWIN_NAME", "Twin") or "Twin"
except Exception:  # noqa: BLE001
    TWIN_SPEAKER = "Twin"

log = logging.getLogger("twin.session")

SPEAKING_WINDOW_MS = 1500          # human counts as speaking this long after a user_text
ECHO_KEEP = 6                      # last ~3 Twin sentences + whole turns, for the echo guard
ECHO_RATIO = 0.75
SCREEN_MAX_B64 = 300_000           # v2.2: cap on one screen_frame's base64 JPEG
DEMO_CALENDAR = Path(__file__).resolve().parent / "demo_calendar.json"
GENERIC_PREDICTIONS = [
    "Status updates on current work",
    "Blockers that need an owner",
    "Decisions to record as tasks",
]

_WORD_RE = re.compile(r"[a-z0-9']+")
_SENT_RE = re.compile(r"(?<=[.!?])\s+")


def _words(text: str) -> list[str]:
    return _WORD_RE.findall((text or "").lower())


def is_echo(human_text: str, twin_texts, min_words: int = 5) -> bool:
    """True if human_text looks like the mic picking up Twin's own speech."""
    hw = _words(human_text)
    if len(hw) < min_words:
        return False  # short replies ("yes", "does that look right?") are too ambiguous to drop
    try:
        import turn_taking
        if turn_taking.mentions_name(human_text):
            return False  # Twin never says its own name, so an address is always a human
    except Exception:  # noqa: BLE001
        pass
    hs = " ".join(hw)
    hset = set(hw)
    for t in twin_texts:
        tw = _words(t)
        if not tw:
            continue
        ts = " ".join(tw)
        if f" {hs} " in f" {ts} ":
            return True
        if len(tw) < 5:
            continue  # comparing against a tiny Twin sentence gives false matches
        tset = set(tw)
        if len(hset | tset) and len(hset & tset) / len(hset | tset) >= ECHO_RATIO:
            return True
        if SequenceMatcher(None, hs, ts).ratio() >= ECHO_RATIO:
            return True
    return False


def load_calendar() -> list[dict]:
    try:
        data = json.loads(DEMO_CALENDAR.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data.get("meetings", []) if isinstance(data, dict) else list(data)


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
DrawErrorHook = Callable[["Session", dict], Awaitable[Any]]
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
        self.urgent_updates = False
        self.twin_mode = "on_request"
        self.mic_muted: set[str] = set()  # names whose EVERY connection is muted (derived)
        self.mic_muted_ws: set = set()      # muted connections (per socket, not per name)
        # async decide_and_respond(session, line) -> None. Runs turn-taking and, if it
        # decides to talk, should spawn the responder via session.spawn("responder", ...).
        self.decide_and_respond: Callable[["Session", dict], Awaitable[Any]] | None = None
        # async responder_hook(session, line, reason) -> None. The actual mouth.
        self.responder_hook: Callable[["Session", dict | None, str], Awaitable[Any]] | None = None

        # v2 hooks (set by the mind / PREET-CODEX agents)
        self._draw_error_hooks: list[DrawErrorHook] = []
        # sync cb(data) for every broadcast dict (PC-20: mouth removes the ghost card on the first real draw)
        self._broadcast_observers: list[Callable[[dict], Any]] = []
        # async brief_provider(session, meeting) -> list[str]
        self.brief_provider: Callable[["Session", str], Awaitable[list[str]]] | None = None
        # async repo_connector(session, url) -> Any
        self.repo_connector: Callable[["Session", str], Awaitable[Any]] | None = None

        # echo guard: Twin's last few spoken sentences
        self.twin_recent: deque[str] = deque(maxlen=ECHO_KEEP)
        # participants / topic
        self.last_heard: dict[str, int] = {}        # human name -> last user_text ts (ms)
        self._last_participants: list[dict] | None = None
        self.active_topic: dict | None = None       # {"topic_id", "title"}
        self._last_topic_sent: str | None = None
        self.brief: dict | None = None
        # v2.2: latest shared-screen frame (wire dict) for late joiners and Twin's vision
        self.screen: dict | None = None

    # ------------------------------------------------------------ say ids / updates

    def new_say_id(self) -> str:
        return f"s_{next(self._say_counter)}"

    def push_update(self, text: str, urgent: bool = False) -> None:
        """Queue a work update for the mouth. Sync, safe to call from any task.

        urgent=True (e.g. a fact-check correction) skips the interjection cooldown;
        it still waits for a pause in human speech.
        """
        text = (text or "").strip()
        if not text:
            return
        if urgent:
            self.urgent_updates = True
        if self._updates and self._updates[-1] == text:
            return
        self._updates.append(text)

    def pop_updates(self) -> list[str]:
        self.urgent_updates = False
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
        self.mic_muted_ws.discard(ws)
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
        data = protocol.dump(msg)
        if isinstance(data, dict) and data.get("type") == "topic" and data.get("topic_id"):
            self._last_topic_sent = data["topic_id"]  # dedupes set_topic vs direct broadcasts
        for cb in list(getattr(self, "_broadcast_observers", ())):
            try:
                cb(data)
            except Exception:  # noqa: BLE001 - an observer must never break a broadcast
                log.exception("broadcast observer failed")
        if not self.sockets:
            return
        await asyncio.gather(*(self.send_to(ws, data) for ws in list(self.sockets)))

    async def broadcast_except(self, sender, msg) -> None:
        """v2.2: send to every socket in the room except `sender`."""
        data = protocol.dump(msg)
        targets = [ws for ws in list(self.sockets) if ws is not sender]
        if targets:
            await asyncio.gather(*(self.send_to(ws, data) for ws in targets))

    async def set_status(self, state: str) -> None:
        if state == self.status:
            return
        self.status = state
        await self.broadcast(Status(state=state))
        if state in ("speaking", "listening"):
            await self.broadcast_participants()  # Twin's speaking ring, without waiting for the ticker

    async def add_line(self, speaker: str, text: str, ts: int | None = None) -> dict:
        line = {"speaker": speaker, "text": text, "ts": ts if ts is not None else now_ms()}
        if speaker == TWIN_SPEAKER:
            self.remember_twin_speech(text)
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

    async def apply_human_draw(self, ws, user: str, msg) -> bool:
        """v2.2: a browser drew something. Validate + apply to the board, relay to everyone
        except the sender (who already has it locally). `by` is forced to the socket's user.
        On rejection the sender gets a recoverable error."""
        op = {k: v for k, v in protocol.dump(msg).items() if k not in ("type", "by")}
        try:
            result = self.board.apply(op)
            out = Draw.model_validate(result if isinstance(result, dict) else op)
        except ValueError as e:
            log.info("[%s] rejected human draw from %s %s: %s", self.room, user, op, e)
            await self.send_to(ws, Error(message=f"draw rejected: {e}"[:500], recoverable=True))
            return False
        out.by = user or "guest"
        await self.broadcast_except(ws, out)
        if out.op == "clear":
            await self.broadcast(self.snapshot_message())
        self._react_to_human_edit(out.by, op)
        return True

    def _react_to_human_edit(self, by: str, op: dict) -> None:
        """Human edits are events for the mind and get a short acknowledgment at the next pause."""
        name = op.get("label") or op.get("text") or op.get("id") or "a shape"
        verb = {"add_box": f"added {name}", "add_sticky": f"left a note: {name}", "add_text": f"wrote {name}",
                "connect": f"connected {op.get('from_id')} to {op.get('to_id')}", "rename": f"renamed {op.get('id')} to {name}",
                "remove": f"removed {op.get('id')}"}.get(op.get("op", ""), None)
        if not verb:
            return
        event = f"{by} {verb} on the whiteboard (human edit, keep it as is)"
        mouth = getattr(self, "mouth", None)
        listener = getattr(mouth, "listener", None) if mouth is not None else None
        if listener is not None:
            try:
                listener.submit(by, event)
            except Exception:  # noqa: BLE001
                log.exception("listener rejected human edit event")
        self.push_update(f"{by} {verb}; I'll treat that as the source of truth.")

    async def screen_frame(self, ws, user: str, msg) -> None:
        """v2.2: keep the latest frame per room and relay it to the other sockets."""
        if len(msg.jpeg or "") > SCREEN_MAX_B64:
            await self.send_to(ws, Error(message=f"screen_frame too large (max {SCREEN_MAX_B64} base64 chars)",
                                         recoverable=True))
            return
        data = protocol.dump(msg)
        data["user"] = user or data.get("user") or "guest"
        data.setdefault("ts", now_ms())
        self.screen = data
        await self.broadcast_except(ws, data)

    async def screen_stop(self, ws, user: str) -> None:
        self.screen = None
        await self.broadcast_except(ws, {"type": "screen_stop", "user": user or "guest"})

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

    # ------------------------------------------------------------ echo guard (PC-14)

    def remember_twin_speech(self, text: str) -> None:
        for sent in _SENT_RE.split((text or "").strip()):
            if sent.strip():
                self.twin_recent.append(sent.strip())
        if text and text.strip():
            # also keep the whole turn so multi-sentence echoes match
            self.twin_recent.append(text.strip())

    def is_echo(self, text: str, final: bool = True) -> bool:
        return bool(self.twin_recent) and is_echo(text, list(self.twin_recent), 5 if final else 3)

    # ------------------------------------------------------------ participants / topic (PC-15)

    def participants(self, now: int | None = None) -> list[dict]:
        now = now if now is not None else now_ms()
        people, seen = [], set()
        for name in self.sockets.values():
            if not name or name in seen or name == TWIN_SPEAKER:
                continue
            seen.add(name)
            socks = [w for w, n in self.sockets.items() if n == name]
            muted = bool(socks) and all(w in self.mic_muted_ws for w in socks)
            speaking = (not muted) and now - self.last_heard.get(name, 0) <= SPEAKING_WINDOW_MS
            people.append({"name": name, "kind": "human", "speaking": speaking, "muted": muted})
        people.append({"name": TWIN_SPEAKER, "kind": "ai",
                       "speaking": bool(self.speaking)})
        return people

    async def broadcast_participants(self, force: bool = False) -> bool:
        """Broadcast `participants` if it changed since last time (or force). True if sent."""
        if not self.sockets:
            return False
        people = self.participants()
        if not force and people == self._last_participants:
            return False
        self._last_participants = people
        await self.broadcast({"type": "participants", "people": people})
        return True

    async def set_topic(self, topic_id: str, title: str) -> bool:
        """Remember and broadcast the active topic. No-op (False) if unchanged."""
        if not topic_id:
            return False
        title = title or topic_id
        prev = self.active_topic if isinstance(self.active_topic, dict) else None
        changed = prev is None or prev.get("topic_id") != topic_id
        same_title = prev is not None and prev.get("title") == title
        self.active_topic = {"topic_id": topic_id, "title": title}
        if self._last_topic_sent == topic_id and same_title:
            return False
        await self.broadcast({"type": "topic", "topic_id": topic_id, "title": title,
                              "active": True, "changed": changed})
        return True

    def topic_message(self) -> dict | None:
        t = self.active_topic
        if not isinstance(t, dict) or not t.get("topic_id"):
            return None
        return {"type": "topic", "topic_id": t["topic_id"], "title": t.get("title") or t["topic_id"],
                "active": True, "changed": False}

    async def send_initial_state(self, ws) -> None:
        """After status + board_snapshot: participants (to everyone) and topic/brief (to ws)."""
        await self.broadcast_participants(force=True)
        topic = self.topic_message()
        if topic is not None:
            await self.send_to(ws, topic)
        if self.screen is not None:
            await self.send_to(ws, self.screen)

    # ------------------------------------------------------------ draw_error / calendar / repo (PC-17)

    def on_draw_error(self, cb: DrawErrorHook) -> DrawErrorHook:
        """Register async cb(session, msg_dict) for browser draw_error reports. Idempotent."""
        if cb not in self._draw_error_hooks:
            self._draw_error_hooks.append(cb)
        return cb

    def _run_draw_error_hooks(self, data: dict) -> None:
        if not self._draw_error_hooks:
            log.info("[%s] draw_error with no hook: %s", self.room, data)
            return
        for cb in self._draw_error_hooks:
            self.spawn(None, cb(self, data))

    async def brief_predictions(self, meeting: str) -> list[str]:
        if self.brief_provider is not None:
            try:
                preds = await self.brief_provider(self, meeting)
                preds = [str(p) for p in (preds or []) if str(p).strip()]
                if preds:
                    return preds
            except Exception:  # noqa: BLE001
                log.exception("[%s] brief_provider failed", self.room)
        for m in load_calendar():
            if str(m.get("title", "")).strip().lower() == (meeting or "").strip().lower():
                preds = [str(p) for p in m.get("predictions") or []]
                if preds:
                    return preds
        return list(GENERIC_PREDICTIONS)

    async def calendar_join(self, meeting: str, scheduled: str | None = None) -> dict:
        """Twin joins a scheduled meeting: broadcast `brief`, then greet via the mouth."""
        meeting = (meeting or "").strip() or "Meeting"
        preds = await self.brief_predictions(meeting)
        brief = Brief(title=meeting, predictions=preds, ts=now_ms())
        self.brief = protocol.dump(brief)
        await self.broadcast(brief)
        await self.broadcast_participants()
        mouth = getattr(self, "mouth", None)
        if mouth is not None and hasattr(mouth, "greet") and not self.muted:
            mouth.greet(meeting, preds)
        else:
            log.info("[%s] calendar_join: no mouth attached, skipping greeting", self.room)
        return self.brief

    async def connect_repo(self, ws, url: str) -> None:
        if self.repo_connector is None:
            await self.send_to(ws, Error(message="connect_repo not available yet", recoverable=True))
            return
        self.spawn("connect_repo", self.repo_connector(self, url))

    # ------------------------------------------------------------ actions

    async def barge_in(self) -> None:
        self.cancel("responder")
        self.cancel("tts")
        await self.broadcast(StopAudio(say_id=self.current_say_id))
        self.current_say_id = None
        self.speaking = False
        await self.set_status("listening")

    async def _run_reset_hooks(self) -> None:
        """Mind cleanup runs BEFORE state is cleared, so late work cannot leak back in."""
        for cb in self._reset_hooks:
            try:
                res = cb(self)
                if inspect.isawaitable(res):
                    await res
            except Exception:  # noqa: BLE001
                log.exception("[%s] reset hook failed", self.room)

    async def clear_board(self) -> None:
        await self._run_reset_hooks()
        self._updates.clear()
        await self.apply_draw({"op": "clear"})

    async def reset(self) -> None:
        await self._run_reset_hooks()
        self.cancel_all()
        self.history.clear()
        self._updates.clear()
        self.twin_recent.clear()
        self.board = BoardState()
        self.current_say_id = None
        self.speaking = False
        self.push_to_talk_active = False
        await self.broadcast(StopAudio())
        await self.set_status("listening")
        await self.broadcast(self.snapshot_message())

    async def handle_message(self, ws, user: str, msg) -> None:
        t = msg.type
        if t == "hello":
            if msg.user and ws in self.sockets:
                self.sockets[ws] = msg.user
                await self.broadcast_participants()
        elif t == "user_text":
            if msg.source != "typed" and ws in self.mic_muted_ws:
                return  # mute drops microphone results; explicit typed requests remain usable
            text = msg.text.strip()
            if self.is_echo(text, bool(msg.final)):
                # PC-14: the mic heard Twin. No history, hooks, transcript or speaking state.
                if msg.final:
                    log.info("[%s] echo guard dropped %r from %s", self.room, text, msg.speaker or user)
                return
            self.last_human_speech_ts = now_ms()
            self.last_heard[msg.speaker or user] = now_ms()
            if msg.final:
                if not text:
                    return
                line = await self.add_line(msg.speaker or user, text, msg.ts)
                if msg.source:
                    line["source"] = msg.source
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
            elif msg.action in ("mic_off", "mic_on"):
                name = self.sockets.get(ws, user)
                if msg.action == "mic_off":
                    self.mic_muted_ws.add(ws)
                else:
                    self.mic_muted_ws.discard(ws)
                socks = [w for w, n in self.sockets.items() if n == name]
                if socks and all(w in self.mic_muted_ws for w in socks):
                    self.mic_muted.add(name)
                    self.last_heard.pop(name, None)
                    # Muting ends that person's turn: only people still unmuted keep the floor "busy".
                    live = [t for n, t in self.last_heard.items() if n not in self.mic_muted]
                    self.last_human_speech_ts = max(live) if live else 0
                else:
                    self.mic_muted.discard(name)
                await self.broadcast_participants(force=True)
                if msg.action == "mic_off" and name in self.mic_muted:
                    hook = getattr(getattr(self, "mouth", None), "on_floor_handoff", None)
                    if hook is not None:
                        self.spawn("floor_handoff", hook(name))
            elif msg.action == "set_mode" and msg.mode:
                self.twin_mode = msg.mode  # off | on_request | proactive (honored by mouth turn-taking)
                self.muted = msg.mode == "off"
            elif msg.action == "clear_board":
                await self.clear_board()
            elif msg.action == "reset_session":
                await self.reset()
        elif t == "draw_error":
            self._run_draw_error_hooks(protocol.dump(msg))
        elif t == "calendar_join":
            await self.calendar_join(msg.meeting, msg.scheduled)
        elif t == "connect_repo":
            await self.connect_repo(ws, msg.url)
        elif t == "draw":  # v2.2: humans draw too
            await self.apply_human_draw(ws, user, msg)
        elif t == "screen_frame":
            await self.screen_frame(ws, user, msg)
        elif t == "screen_stop":
            await self.screen_stop(ws, user)
        elif t == "ticket_action":
            await self.ticket_action(ws, protocol.dump(msg))
        else:
            await self.send_to(ws, Error(message=f"unexpected message type {t!r}", recoverable=True))

    async def ticket_action(self, ws, data: dict) -> None:
        """PC-23: approve | push_jira | dismiss a draft ticket; everyone sees the update."""
        import integrations
        ticket_id, action = str(data.get("id") or ""), str(data.get("action") or "")
        try:
            ticket, _ = await integrations.apply_action(
                ticket_id, action, workspace=getattr(self, "workspace", None), room=self.room)
        except integrations.TicketError as e:
            await self.send_to(ws, Error(message=str(e)[:500], recoverable=True))
            return
        await self.broadcast(integrations.wire(ticket))

    def debug_state(self) -> dict:
        return {
            "room": self.room,
            "history_len": len(self.history),
            "status": self.status,
            "speaking": self.speaking,
            "muted": self.muted,
            "sockets": len(self.sockets),
            "users": list(self.sockets.values()),
            "participants": self.participants(),
            "active_topic": self.active_topic,
            "twin_recent": list(self.twin_recent),
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
