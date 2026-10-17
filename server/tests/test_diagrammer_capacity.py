import asyncio

import httpx
from openai import APIStatusError
import pytest

import config
import llm
from agents import diagrammer
from agents.diagrammer import Diagrammer
from board import BoardState


class Session:
    def __init__(self):
        self.board = BoardState()
        self.sent = []
        self.updates = []

    async def broadcast(self, message):
        self.sent.append(message)

    def push_update(self, message):
        self.updates.append(message)


def capacity():
    return APIStatusError("capacity_unavailable", response=httpx.Response(
        503, request=httpx.Request("POST", "https://example.test")), body=None)


async def add(handlers):
    await handlers["add_box"](id="api", label="API", x=200, y=200, kind="service")


@pytest.mark.asyncio
async def test_json_503_and_timeout_try_coach_then_fast_once(caplog, monkeypatch):
    calls = []
    async def plan(model, messages, tools, handlers, **kwargs):
        calls.append(model)
        if model == config.MODEL_SMART:
            raise capacity()
        if model == config.MODEL_COACH:
            raise TimeoutError()
        await add(handlers)
    monkeypatch.setattr(llm, "run_json_ops", plan)
    session = Session()
    await Diagrammer().handle(session, [{"type": "task", "text": "Draw a diagram"}])
    assert calls == [config.MODEL_SMART, config.MODEL_COACH, config.MODEL_FAST]
    assert set(session.board.boxes) == {"api"}
    assert caplog.text.count("diagrammer_capacity_fallback") == 2


@pytest.mark.asyncio
async def test_hanging_plan_has_wall_deadline_before_switch(monkeypatch):
    monkeypatch.setattr(diagrammer, "PLAN_TIMEOUT_S", .1)
    calls = []
    async def plan(model, messages, tools, handlers, **kwargs):
        calls.append(model)
        if model == config.MODEL_SMART:
            await asyncio.Event().wait()
        await add(handlers)
    monkeypatch.setattr(llm, "run_json_ops", plan)
    session = Session()
    await Diagrammer().handle(session, [{"type": "task", "text": "Draw a diagram"}])
    assert calls == [config.MODEL_SMART, config.MODEL_COACH]


@pytest.mark.asyncio
async def test_capacity_after_applied_operation_never_replays(monkeypatch):
    calls = []
    async def plan(model, messages, tools, handlers, **kwargs):
        calls.append(model)
        await add(handlers)
        raise capacity()
    monkeypatch.setattr(llm, "run_json_ops", plan)
    session = Session()
    with pytest.raises(APIStatusError):
        await Diagrammer().handle(session, [{"type": "task", "text": "Draw a diagram"}])
    assert calls == [config.MODEL_SMART]
    assert len(session.board.boxes) == 1 and len(session.updates) == 1


@pytest.mark.asyncio
async def test_native_capacity_fallback_only_before_any_handler(monkeypatch):
    calls = []
    async def invalid(*args, **kwargs):
        raise ValueError("invalid JSON plan")
    async def native(model, messages, tools, handlers, **kwargs):
        calls.append(model)
        if model == config.MODEL_SMART:
            raise capacity()
        await add(handlers)
    monkeypatch.setattr(llm, "run_json_ops", invalid)
    monkeypatch.setattr(llm, "run_tools", native)
    session = Session()
    await Diagrammer().handle(session, [{"type": "task", "text": "Draw a diagram"}])
    assert calls == [config.MODEL_SMART, config.MODEL_COACH]


@pytest.mark.asyncio
async def test_cancellation_does_not_fallback(monkeypatch):
    calls = []
    async def plan(model, *args, **kwargs):
        calls.append(model)
        raise asyncio.CancelledError()
    monkeypatch.setattr(llm, "run_json_ops", plan)
    with pytest.raises(asyncio.CancelledError):
        await Diagrammer().handle(Session(), [{"type": "task", "text": "Draw a diagram"}])
    assert calls == [config.MODEL_SMART]


@pytest.mark.asyncio
async def test_three_capacity_failures_are_bounded_and_draw_nothing(monkeypatch):
    calls = []
    async def plan(model, *args, **kwargs):
        calls.append(model)
        raise capacity()
    monkeypatch.setattr(llm, "run_json_ops", plan)
    session = Session()
    with pytest.raises(APIStatusError):
        await Diagrammer().handle(session, [{"type": "task", "text": "Draw a diagram"}])
    assert calls == [config.MODEL_SMART, config.MODEL_COACH, config.MODEL_FAST]
    assert not session.board.boxes


@pytest.mark.asyncio
async def test_native_does_not_revisit_capacity_failed_json_models(monkeypatch):
    calls = []
    async def plan(model, *args, **kwargs):
        calls.append(("json", model))
        if model != config.MODEL_FAST:
            raise capacity()
        raise ValueError("malformed fast plan")
    async def native(model, messages, tools, handlers, **kwargs):
        calls.append(("native", model))
        raise capacity()
    monkeypatch.setattr(llm, "run_json_ops", plan)
    monkeypatch.setattr(llm, "run_tools", native)
    with pytest.raises(APIStatusError):
        await Diagrammer().handle(Session(), [{"type": "task", "text": "Draw a diagram"}])
    assert calls == [("json", config.MODEL_SMART), ("json", config.MODEL_COACH),
                     ("json", config.MODEL_FAST), ("native", config.MODEL_FAST)]
