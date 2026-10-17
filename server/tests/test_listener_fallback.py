"""Bounded Listener provider fallback and model-independent repository routing."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from openai import APIStatusError

import config
from agents import listener as listener_module
from agents.listener import Listener
from board import BoardState


def _capacity_error():
    response = httpx.Response(503, request=httpx.Request("POST", "https://example.test/chat"))
    return APIStatusError("capacity", response=response, body={})


class FakeSession:
    def __init__(self, *, code_map=None):
        self.board = BoardState()
        self.code_map = code_map
        self.messages = []
        self.twin_mode = "off"

    async def broadcast(self, message):
        self.messages.append(message)

    def push_update(self, text):
        pass


@pytest.mark.asyncio
async def test_capacity_switches_once_to_distinct_coach(monkeypatch, caplog):
    calls = []

    async def chat(model, messages, **kw):
        calls.append(model)
        if model == config.MODEL_FAST:
            raise _capacity_error()
        return json.dumps({"moments": [{"type": "question", "text": "Where is auth?"}]})

    monkeypatch.setattr(listener_module.llm, "chat", chat)
    result, _ = await listener_module._extract_batch([{"role": "user", "content": "test"}], 1)
    assert calls == [config.MODEL_FAST, config.MODEL_COACH]
    assert result[0]["text"] == "Where is auth?"
    assert "Listener model fallback" in caplog.text


@pytest.mark.asyncio
async def test_timeout_switches_to_coach_and_is_bounded(monkeypatch):
    calls = []

    async def chat(model, messages, **kw):
        calls.append(model)
        if model == config.MODEL_FAST:
            await asyncio.sleep(0.1)
        return '{"moments":[]}'

    monkeypatch.setattr(listener_module.llm, "chat", chat)
    monkeypatch.setattr(listener_module, "_MODEL_TIMEOUTS", (0.01, 0.05))
    moments, _ = await listener_module._extract_batch([], 1)
    assert moments == []
    assert calls == [config.MODEL_FAST, config.MODEL_COACH]


@pytest.mark.asyncio
async def test_both_providers_fail_without_unbounded_retries(monkeypatch):
    calls = []

    async def chat(model, messages, **kw):
        calls.append(model)
        raise _capacity_error()

    monkeypatch.setattr(listener_module.llm, "chat", chat)
    with pytest.raises(APIStatusError):
        await listener_module._extract_batch([], 1)
    assert calls == [config.MODEL_FAST, config.MODEL_COACH]


@pytest.mark.asyncio
async def test_repo_overview_draws_from_index_without_listener_model(monkeypatch):
    code_map = SimpleNamespace(files={
        "src/main.ts": {"imports": ["src/auth.ts"], "imported_by": []},
        "src/auth.ts": {"imports": [], "imported_by": ["src/main.ts"]},
    })
    session = FakeSession(code_map=code_map)

    async def forbidden_chat(*args, **kwargs):
        raise AssertionError("explicit repository overview must not wait on extraction")

    monkeypatch.setattr(listener_module.llm, "chat", forbidden_chat)
    listener = Listener(session, debounce_seconds=0)
    listener.submit("Preet", "Walk me through the repository and how it works")
    await listener.wait_idle()
    assert len(session.board.boxes) == 2
    assert len(session.board.edges) == 1
    assert any(message.get("type") == "draw" for message in session.messages)
    assert not any(message.get("type") == "agent_event" and message.get("status") == "failed"
                   for message in session.messages)


@pytest.mark.asyncio
async def test_reflex_smalltalk_cannot_discard_explicit_repo_overview(monkeypatch):
    code_map = SimpleNamespace(files={"src/main.ts": {"imports": [], "imported_by": []}})
    session = FakeSession(code_map=code_map)

    async def forbidden_chat(*args, **kwargs):
        raise AssertionError("should use indexed code map")

    monkeypatch.setattr(listener_module.llm, "chat", forbidden_chat)
    listener = Listener(session, debounce_seconds=0)
    listener.submit("Preet", "Show the project architecture", decision={
        "kind": {"type": "choice", "choice": "small_talk", "confidence": 0.99},
        "topic_changed": {"type": "noul", "noul": 0.01},
        "wants_visual": {"type": "choice", "choice": "none", "confidence": 0.99},
    })
    await listener.wait_idle()
    assert len(session.board.boxes) == 1
