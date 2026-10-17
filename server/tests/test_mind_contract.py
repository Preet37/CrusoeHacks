"""Offline contract checks across the real Listener, Conductor, Diagrammer and LLM.

Run from the repository root with ``PYTHONPATH=server server/.venv/bin/python
-m pytest server/tests/test_mind_contract.py``. No API key or network is used.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

import llm
import protocol
from agents.listener import Listener
from board import BoardState


def _response(content: str = "", calls: list | None = None) -> NS:
    return NS(choices=[NS(message=NS(content=content, tool_calls=calls))], usage=None)


class _Session:
    def __init__(self) -> None:
        self.board = BoardState()
        self.messages: list[dict] = []
        self.updates: list[str] = []

    async def broadcast(self, message: dict) -> None:
        # Mimic the wire boundary: callers cannot rely on mutating the sent dict.
        self.messages.append(json.loads(json.dumps(message)))

    def push_update(self, text: str) -> None:
        self.updates.append(text)


@pytest.mark.asyncio
async def test_real_mind_pipeline_uses_json_batch_and_preserves_wire_contract(monkeypatch) -> None:
    ops = [
        {"op": "add_box", "id": "web", "label": "Web app", "x": 120, "y": 300, "kind": "client"},
        {"op": "add_box", "id": "auth", "label": "Auth", "x": 420, "y": 300, "kind": "service"},
        {"op": "connect", "id": "e_web_auth", "from_id": "web", "to_id": "auth", "label": "login"},
        {"op": "highlight", "ids": ["auth"], "on": True},
    ]
    create = AsyncMock(side_effect=[
        _response('[{"type":"system_fact","text":"Web app calls Auth for login","entities":["web","auth"]}]'),
        _response(json.dumps({"ops": ops})),
    ])
    monkeypatch.setattr(llm, "_client", lambda: NS(chat=NS(completions=NS(create=create))))
    session = _Session()
    listener = Listener(session, debounce_seconds=0)

    listener.submit("Preet", "The web app calls auth for login")
    await listener.wait_idle()

    assert create.await_count == 2  # listener chat + one Diagrammer JSON batch
    assert create.await_args_list[0].kwargs["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    request = create.await_args_list[1].kwargs
    assert request["response_format"] == {"type": "json_object"}
    assert session.board.snapshot() == {
        "type": "board_snapshot",
        "boxes": [
            {"id": "auth", "label": "Auth", "x": 420, "y": 300, "kind": "service"},
            {"id": "web", "label": "Web app", "x": 120, "y": 300, "kind": "client"},
        ],
        "edges": [{"id": "e_web_auth", "from_id": "web", "to_id": "auth", "label": "login"}],
    }
    assert session.board.highlighted == {"auth"}
    draws = [m for m in session.messages if m["type"] == "draw"]
    assert [m["op"] for m in draws] == ["add_box", "add_box", "connect", "highlight"]
    assert [m.get("id") for m in draws[:3]] == ["web", "auth", "e_web_auth"]
    assert len(session.updates) == 1

    events = [m for m in session.messages if m["type"] == "agent_event"]
    starts = {m["agent"]: m for m in events if m["state"] == "started"}
    assert set(starts) == {"Listener", "Conductor", "Diagrammer"}
    assert starts["Listener"]["parent_id"] is None
    assert starts["Conductor"]["parent_id"] == starts["Listener"]["event_id"]
    assert starts["Diagrammer"]["parent_id"] == starts["Conductor"]["event_id"]
    for agent, started in starts.items():
        finished = [m for m in events if m["agent"] == agent and m["state"] == "done"]
        assert len(finished) == 1
        assert finished[0]["event_id"] == started["event_id"]
        assert finished[0]["parent_id"] == started["parent_id"]

    for message in session.messages + [session.board.snapshot()]:
        parsed = protocol.parse(message)
        assert parsed.type == message["type"]
        assert protocol.parse(protocol.dumps(parsed)).type == message["type"]


@pytest.mark.asyncio
async def test_listener_close_cancels_work_without_a_draw(monkeypatch) -> None:
    started = asyncio.Event()

    async def blocked_chat(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(llm, "chat", blocked_chat)
    session = _Session()
    listener = Listener(session, debounce_seconds=0)
    listener.submit("Preet", "The web app talks to auth")
    await started.wait()
    await listener.close()

    assert session.board.snapshot()["boxes"] == []
    assert not any(m["type"] == "draw" for m in session.messages)
    states = [m["state"] for m in session.messages if m["type"] == "agent_event"]
    assert states == ["started", "cancelled"]
