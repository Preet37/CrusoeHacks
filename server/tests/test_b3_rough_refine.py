"""A physical sketch appears before the slower refinement and remains safe."""

import asyncio
import json

import pytest

import config
import llm
from agents.diagrammer import Diagrammer, StaleBoardError
from board import BoardState


REQUEST = "Let's build an airplane"
ROUGH = [[[100, 500], [500, 350], [900, 500]]]
REFINED = [[[100, 500], [500, 350], [900, 500]],
           [[500, 350], [500, 650], [100, 500]]]


class Session:
    def __init__(self):
        self.board = BoardState()
        self.sent = []
        self.updates = []

    async def broadcast(self, message):
        self.sent.append(message)

    def push_update(self, text):
        self.updates.append(text)


def _sketches(session):
    return [message for message in session.sent
            if message.get("type") == "draw" and message.get("op") in {"sketch", "update"}]


def _request(session):
    return Diagrammer().handle(session, [{"type": "task", "text": REQUEST}],
                              source_lines=[{"text": REQUEST, "speaker": "Preet"}])


def _fail_full_planner(monkeypatch):
    async def unexpected(*args, **kwargs):
        raise AssertionError("physical sketch should use the rough-then-refine path")
    monkeypatch.setattr(llm, "run_json_ops", unexpected)
    monkeypatch.setattr(llm, "run_tools", unexpected)


@pytest.mark.asyncio
@pytest.mark.parametrize('hint', [None, '3d'])
async def test_rough_is_visible_before_super_and_refinement_updates_same_id(monkeypatch, hint):
    session = Session()
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def chat(model, messages, **kwargs):
        calls.append(model)
        if model == config.MODEL_FAST:
            return json.dumps({"strokes": ROUGH})
        assert model == config.MODEL_SMART
        started.set()
        await release.wait()
        return json.dumps({"strokes": REFINED})

    monkeypatch.setattr(llm, "chat", chat)
    _fail_full_planner(monkeypatch)
    task = asyncio.create_task(Diagrammer().handle(
        session, [{"type": "task", "text": REQUEST}],
        source_lines=[{"text": REQUEST, "speaker": "Preet", "wants_visual": hint}]))
    try:
        await asyncio.wait_for(started.wait(), 2)
        rough = _sketches(session)
        assert [message["op"] for message in rough] == ["sketch"]
        shape_id = rough[0]["id"]
        assert session.board.shapes[shape_id]["strokes"] == ROUGH
        release.set()
        await asyncio.wait_for(task, 2)
    finally:
        release.set()
    assert calls == [config.MODEL_FAST, config.MODEL_SMART]
    assert [message["op"] for message in _sketches(session)] == ["sketch", "update"]
    assert _sketches(session)[1]["id"] == shape_id
    assert set(session.board.shapes) == {shape_id}
    assert session.board.shapes[shape_id]["strokes"] == REFINED


@pytest.mark.asyncio
async def test_refinement_failure_preserves_broadcast_rough_sketch(monkeypatch):
    session = Session()
    calls = []

    async def chat(model, messages, **kwargs):
        calls.append(model)
        if model == config.MODEL_FAST:
            return json.dumps({"strokes": ROUGH})
        raise RuntimeError("Super unavailable")

    monkeypatch.setattr(llm, "chat", chat)
    _fail_full_planner(monkeypatch)
    await _request(session)
    assert calls == [config.MODEL_FAST, config.MODEL_SMART]
    assert [message["op"] for message in _sketches(session)] == ["sketch"]
    shape = next(iter(session.board.shapes.values()))
    assert shape["strokes"] == ROUGH


@pytest.mark.asyncio
async def test_airplane_still_has_visible_rough_when_fast_model_fails(monkeypatch):
    session = Session()

    async def chat(model, messages, **kwargs):
        if model == config.MODEL_FAST:
            raise RuntimeError("Nano unavailable")
        raise RuntimeError("Super unavailable")

    monkeypatch.setattr(llm, "chat", chat)
    _fail_full_planner(monkeypatch)
    await _request(session)
    assert [message["op"] for message in _sketches(session)] == ["sketch"]
    assert len(next(iter(session.board.shapes.values()))["strokes"]) >= 2


@pytest.mark.asyncio
async def test_super_echo_still_refines_same_airplane_shape(monkeypatch):
    session = Session()

    async def chat(model, messages, **kwargs):
        return json.dumps({"strokes": ROUGH})

    monkeypatch.setattr(llm, "chat", chat)
    _fail_full_planner(monkeypatch)
    await _request(session)
    draws = _sketches(session)
    assert [message["op"] for message in draws] == ["sketch", "update"]
    assert draws[0]["id"] == draws[1]["id"]
    assert len(session.board.shapes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["clear", "reset"])
async def test_clear_or_reset_while_fast_model_pending_blocks_rough(monkeypatch, change):
    session = Session()
    started, release = asyncio.Event(), asyncio.Event()

    async def chat(model, messages, **kwargs):
        assert model == config.MODEL_FAST
        started.set()
        await release.wait()
        return json.dumps({"strokes": ROUGH})

    monkeypatch.setattr(llm, "chat", chat)
    _fail_full_planner(monkeypatch)
    task = asyncio.create_task(_request(session))
    try:
        await asyncio.wait_for(started.wait(), 2)
        if change == "clear":
            session.board.apply({"op": "clear"})
        else:
            session.board = BoardState()
        release.set()
        with pytest.raises(StaleBoardError):
            await asyncio.wait_for(task, 2)
    finally:
        release.set()
    assert session.board.shapes == {}
    assert _sketches(session) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["clear", "reset"])
async def test_clear_or_reset_during_super_blocks_late_update(monkeypatch, change):
    session = Session()
    started, release = asyncio.Event(), asyncio.Event()

    async def chat(model, messages, **kwargs):
        if model == config.MODEL_FAST:
            return json.dumps({"strokes": ROUGH})
        started.set()
        await release.wait()
        return json.dumps({"strokes": REFINED})

    monkeypatch.setattr(llm, "chat", chat)
    _fail_full_planner(monkeypatch)
    task = asyncio.create_task(_request(session))
    try:
        await asyncio.wait_for(started.wait(), 2)
        assert [message["op"] for message in _sketches(session)] == ["sketch"]
        if change == "clear":
            session.board.apply({"op": "clear"})
        else:
            session.board = BoardState()
        release.set()
        try:
            await asyncio.wait_for(task, 2)
        except StaleBoardError:
            pass
    finally:
        release.set()
    assert session.board.shapes == {}
    assert [message["op"] for message in _sketches(session)] == ["sketch"]


@pytest.mark.asyncio
async def test_cancellation_during_super_never_applies_refinement(monkeypatch):
    session = Session()
    started = asyncio.Event()

    async def chat(model, messages, **kwargs):
        if model == config.MODEL_FAST:
            return json.dumps({"strokes": ROUGH})
        started.set()
        await asyncio.Future()

    monkeypatch.setattr(llm, "chat", chat)
    _fail_full_planner(monkeypatch)
    task = asyncio.create_task(_request(session))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert [message["op"] for message in _sketches(session)] == ["sketch"]
    assert next(iter(session.board.shapes.values()))["strokes"] == ROUGH
