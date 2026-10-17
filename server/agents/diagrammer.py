"""Bounded Nemotron drawing agent over the authoritative BoardState."""

import asyncio
import inspect
import json
import logging
import time
from pathlib import Path
from uuid import uuid4

import config
import llm

log = logging.getLogger(__name__)
PROMPT = Path(__file__).parent / "prompts" / "diagrammer.md"
MAX_OPS = 8


class StaleBoardError(RuntimeError):
    """The user cleared or reset the board while a model job was in flight."""


async def emit_event(session, agent: str, model: str, state: str, detail: str,
                     *, event_id: str | None = None, parent_id: str | None = None) -> str:
    """Use one id across a job's lifecycle so the browser can update its tree node."""
    event_id = event_id or f"ev_{uuid4().hex[:12]}"
    await session.broadcast({"type": "agent_event", "event_id": event_id,
                             "parent_id": parent_id, "agent": agent,
                             "model": model.rsplit("/", 1)[-1], "state": state,
                             "detail": detail, "ts": int(time.time() * 1000)})
    return event_id


def _tool(name: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": f"Apply the {name} operation to the architecture board.",
        "parameters": {"type": "object", "properties": properties,
                       "required": required, "additionalProperties": False},
    }}


_ID = {"type": "string", "description": "Stable lowercase slug"}
_LABEL = {"type": "string"}
DRAW_TOOLS = [
    _tool("add_box", {"id": _ID, "label": _LABEL,
                      "x": {"type": "integer", "minimum": 0, "maximum": 1000},
                      "y": {"type": "integer", "minimum": 0, "maximum": 1000},
                      "kind": {"type": "string", "enum": ["service", "datastore", "client", "external", "note"]}},
          ["id", "label", "x", "y", "kind"]),
    _tool("connect", {"id": _ID, "from_id": _ID, "to_id": _ID, "label": _LABEL},
          ["id", "from_id", "to_id"]),
    _tool("rename", {"id": _ID, "label": _LABEL}, ["id", "label"]),
    _tool("remove", {"id": _ID}, ["id"]),
    _tool("highlight", {"ids": {"type": "array", "items": _ID},
                        "on": {"type": "boolean"}}, ["ids", "on"]),
    _tool("clear", {}, []),
]


def _describe_changes(draws: list[dict]) -> str:
    for draw in reversed(draws):
        match draw["op"]:
            case "add_box":
                return f"I added {draw['label']} to the diagram."
            case "connect":
                return f"I connected {draw['from_id']} to {draw['to_id']} on the diagram."
            case "rename":
                return f"I updated {draw['label']} on the diagram."
            case "remove":
                return f"I removed {draw['id']} from the diagram."
            case "clear":
                return "I cleared the diagram."
    return "I updated the diagram."


class Diagrammer:
    @staticmethod
    async def _publish_draw(session, board, draw: dict, expected_revision: int) -> None:
        # BoardState.apply is synchronous. Once it has changed state, finish the
        # matching broadcast even if the enclosing job is cancelled.
        sending = asyncio.create_task(session.broadcast(draw))

        async def reconcile() -> None:
            try:
                await session.broadcast(session.board.snapshot())
            except Exception:
                log.exception("Board snapshot reconciliation failed")

        try:
            await asyncio.shield(sending)
        except asyncio.CancelledError:
            try:
                await sending
            except BaseException:
                await reconcile()
                raise
            if session.board is not board or board.revision != expected_revision:
                await reconcile()
            raise
        except Exception:
            # A send may have reached only some sockets. Reconcile all live
            # clients with the authoritative state before reporting failure.
            await reconcile()
            raise

    async def handle(self, session, moments: list[dict], parent_id: str | None = None) -> list[dict]:
        event_id = await emit_event(session, "Diagrammer", config.MODEL_SMART, "started",
                                    f"draw {len(moments)} system fact(s)", parent_id=parent_id)
        try:
            # A Session owns one board. Serialize diagram jobs for that board.
            lock = getattr(session, "_diagrammer_lock", None)
            if lock is None:
                lock = asyncio.Lock()
                session._diagrammer_lock = lock
            async with lock:
                draws = await self._draw(session, moments)
            await emit_event(session, "Diagrammer", config.MODEL_SMART, "done",
                             f"{len(draws)} draw operation(s)", event_id=event_id,
                             parent_id=parent_id)
            return draws
        except StaleBoardError:
            await emit_event(session, "Diagrammer", config.MODEL_SMART, "cancelled",
                             "board changed during drawing", event_id=event_id,
                             parent_id=parent_id)
            raise
        except asyncio.CancelledError:
            await emit_event(session, "Diagrammer", config.MODEL_SMART, "cancelled",
                             "cancelled", event_id=event_id, parent_id=parent_id)
            raise
        except Exception as exc:
            log.exception("Diagrammer failed")
            await emit_event(session, "Diagrammer", config.MODEL_SMART, "failed",
                             str(exc)[:160], event_id=event_id, parent_id=parent_id)
            raise

    async def _draw(self, session, moments: list[dict]) -> list[dict]:
        board = session.board
        expected_revision = board.revision
        draws: list[dict] = []
        changed_draws: list[dict] = []
        attempts = 0

        async def apply(op: dict) -> dict:
            nonlocal attempts, expected_revision
            if session.board is not board or board.revision != expected_revision:
                raise StaleBoardError("board changed during drawing")
            if attempts >= MAX_OPS:
                return {"ok": False, "error": f"limit of {MAX_OPS} draw calls reached"}
            attempts += 1
            before = (board.snapshot(), set(board.highlighted))
            try:
                draw = board.apply(op)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            expected_revision = board.revision
            changed = (board.snapshot(), set(board.highlighted)) != before
            if changed or draw["op"] == "clear":
                draws.append(draw)
                if changed:
                    changed_draws.append(draw)
                try:
                    await self._publish_draw(session, board, draw, expected_revision)
                finally:
                    if draw["op"] == "clear":
                        current = session.board
                        await self._publish_draw(session, current, current.snapshot(), current.revision)
                if session.board is not board or board.revision != expected_revision:
                    await session.broadcast(session.board.snapshot())
                    raise StaleBoardError("board changed during draw broadcast")
            return {"ok": True, "changed": len(draws), "board": board.summary()}

        handlers = {}
        for tool in DRAW_TOOLS:
            name = tool["function"]["name"]

            async def handler(*, _name=name, **kwargs):
                return await apply({"op": _name, **kwargs})

            handlers[name] = handler

        messages = [
            {"role": "system", "content": PROMPT.read_text()},
            {"role": "user", "content": json.dumps({"board": board.snapshot(),
                                                      "facts": moments,
                                                      "max_draw_calls": MAX_OPS})},
        ]
        try:
            # run_tools executes both native tool calls and its JSON-ops fallback
            # through these handlers; its return value is only a completion/result.
            await llm.run_tools(config.MODEL_SMART, messages, DRAW_TOOLS,
                                handlers, max_steps=MAX_OPS, max_tokens=1200)
        finally:
            # Native tools may have changed the board before a later model step
            # fails or gets cancelled. The update still needs to be spoken once.
            if changed_draws and session.board is board and board.revision == expected_revision:
                update = session.push_update(_describe_changes(changed_draws))
                if inspect.isawaitable(update):
                    await update
        return draws
