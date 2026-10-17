import asyncio
import json

import pytest

import llm
from agents.diagrammer import Diagrammer, StaleBoardError, repair_draw
from board import BoardState


class Session:
    def __init__(self):
        self.board = BoardState()
        self.sent = []
        self.updates = []

    async def broadcast(self, message):
        self.sent.append(message)

    def push_update(self, text):
        self.updates.append(text)


@pytest.mark.asyncio
async def test_rich_plan_uses_one_call_and_topic_frame(monkeypatch):
    calls = []

    async def plan(model, messages, tools, handlers, **kwargs):
        calls.append(kwargs)
        await handlers["add_mermaid"](id="login_flow", code="sequenceDiagram\n Browser->>Auth: login", x=100, y=150)
        await handlers["add_chart"](id="latency", chart="bar", title="Latency", labels=["v1", "v2"],
                                    series=[{"name": "p95", "values": [420, 180]}], x=500, y=150)
        await handlers["add_mock"](id="login_ui", title="Login", html="<div><input placeholder='Email'><button>Sign in</button></div>", x=200, y=500)

    monkeypatch.setattr(llm, "run_json_ops", plan)
    session = Session()
    await Diagrammer().handle(session, [{"type": "system_fact", "text": "Login flow and metrics"}],
                               topic_id="t_auth", title="Authentication", topic_changed=True)
    assert len(calls) == 1
    assert calls[0]["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False
    assert session.board.active_frame == "t_auth"
    assert all(session.board.shapes[name]["frame"] == "t_auth" for name in ("login_flow", "latency", "login_ui"))
    assert len(session.updates) == 1
    assert [m["op"] for m in session.sent if m["type"] == "draw"] == ["frame", "focus", "add_mermaid", "add_chart", "add_mock"]


def mermaid(session):
    session.board.apply({"op": "add_mermaid", "id": "flow", "code": "bad syntax", "x": 0, "y": 0})


@pytest.mark.asyncio
async def test_mermaid_repair_is_once_even_with_two_clients(monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def chat(*args, **kwargs):
        calls.append(1)
        started.set()
        await release.wait()
        return json.dumps({"code": "flowchart LR\n A-->B"})

    monkeypatch.setattr(llm, "chat", chat)
    session = Session()
    mermaid(session)
    message = {"id": "flow", "op": "add_mermaid", "error": "bad syntax"}
    pending = asyncio.create_task(repair_draw(session, message))
    await started.wait()
    assert await repair_draw(session, message) is False
    release.set()
    assert await pending is True
    assert len(calls) == 1
    assert session.board.shapes["flow"]["code"] == "flowchart LR\n A-->B"
    assert sum(m["type"] == "draw" and m["op"] == "update" for m in session.sent) == 1
    assert await repair_draw(session, message) is False


@pytest.mark.asyncio
async def test_clear_during_mermaid_repair_prevents_stale_update(monkeypatch):
    session = Session()
    mermaid(session)

    async def chat(*args, **kwargs):
        session.board.apply({"op": "clear"})
        return '{"code":"flowchart LR; A-->B"}'

    monkeypatch.setattr(llm, "chat", chat)
    with pytest.raises(StaleBoardError):
        await repair_draw(session, {"id": "flow", "error": "syntax"})
    assert session.board.shapes == {}
    assert not any(m["type"] == "draw" for m in session.sent)
