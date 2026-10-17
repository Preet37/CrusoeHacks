"""Turn-taking commands must not become meeting decisions or tickets."""

import json

import pytest

from agents import listener as listener_module
from agents import task_planner as planner_module
from agents.listener import Listener
from agents.task_planner import TaskPlanner
from board import BoardState


class Session:
    def __init__(self):
        self.room = "standup"
        self.board = BoardState()
        self.messages = []

    async def broadcast(self, message):
        self.messages.append(message)


class Recorder:
    def __init__(self):
        self.calls = []

    async def handle(self, session, moments, **kwargs):
        self.calls.append(moments)


@pytest.mark.asyncio
async def test_listener_drops_meta_commands_before_extraction(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("meta line must not reach extraction")

    monkeypatch.setattr(listener_module.llm, "chat", forbidden)
    recorder = Recorder()
    listener = Listener(Session(), conductor=recorder, debounce_seconds=0)
    for text in ("end meeting", "Let's keep doing that.", "Twin, finish this meeting now"):
        listener.submit("Preet", text)
    await listener.wait_idle()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_listener_filters_meta_decision_from_mixed_model_output(monkeypatch):
    async def chat(*args, **kwargs):
        return json.dumps({"moments": [
            {"type": "decision", "text": "Let's keep doing that."},
            {"type": "decision", "text": "We decided to use Redis for sessions."},
        ]})

    monkeypatch.setattr(listener_module.llm, "chat", chat)
    recorder = Recorder()
    listener = Listener(Session(), conductor=recorder, debounce_seconds=0)
    listener.submit("Preet", "We decided to use Redis for sessions.")
    await listener.wait_idle()
    assert recorder.calls == [[{"type": "decision", "text": "We decided to use Redis for sessions.",
                                "entities": []}]]


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["end meeting", "Let's keep doing that.",
                                         "We should end this meeting now."])
async def test_planner_refuses_meta_ticket_even_if_called_directly(monkeypatch, text):
    async def forbidden(*args, **kwargs):
        raise AssertionError("meta line must not reach title model")

    monkeypatch.setattr(planner_module.llm, "chat", forbidden)
    session = Session()
    result = await TaskPlanner().handle(session, {"type": "decision", "text": text},
                                        source_lines=[{"speaker": "Preet", "text": text}])
    assert result == {"summary": None, "citations": [], "tasks": [], "tickets": []}
    assert session.messages == []
    assert not hasattr(session, "ticket_drafts")
