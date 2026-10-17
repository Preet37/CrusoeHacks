"""PX-21 drafts are grounded meeting artifacts, not external ticket sends."""

import json

import pytest

import llm
from agents.conductor import Conductor
from agents.task_planner import TaskPlanner
from board import BoardState


class Session:
    def __init__(self, code_map=None):
        self.room = "standup"
        self.board = BoardState()
        self.code_map = code_map
        self.active_topic = {"topic_id": "t_auth", "title": "Authentication"}
        self.messages = []

    async def broadcast(self, message):
        self.messages.append(message)


class Map:
    def resolve(self, query):
        return ["auth/login.py"] if "login" in query.lower() else []

    def impact(self, query):
        return [{"path": "auth/login.py", "owner": "Avery Chen"},
                {"path": "tests/test_auth.py", "owner": "Morgan"}]


@pytest.mark.asyncio
async def test_draft_ticket_uses_spoken_owner_board_frame_and_sourced_paths(monkeypatch):
    async def title_only(model, messages, **kwargs):
        return json.dumps({"title": "Migrate login sessions to Redis", "owner": "Morgan"})

    monkeypatch.setattr(llm, "chat", title_only)
    session = Session(Map())
    session.board.apply({"op": "frame", "id": "f_auth", "title": "Authentication", "x": 0, "y": 0})
    session.board.apply({"op": "add_mermaid", "id": "login_flow", "frame": "f_auth",
                         "code": "sequenceDiagram\nApp->>Auth: login", "x": 10, "y": 10})
    session.board.apply({"op": "focus", "id": "f_auth"})
    decision = "We decided to migrate login to Redis sessions; Avery will own it."
    result = await TaskPlanner().handle(session, {"type": "decision", "text": decision},
                                        source_lines=[{"speaker": "Preet", "text": decision,
                                                       "ts": 123}])

    assert len(result["tickets"]) == 1
    draft = result["tickets"][0]
    assert draft["title"] == "Migrate login sessions to Redis"
    assert draft["owner"] == "Avery Chen"  # Spoken commitment overrides model's guess.
    assert draft["topic"] == "t_auth"
    assert draft["status"] == "draft" and all(v is None for v in draft["links"].values())
    assert decision in draft["body"] and "meeting standup @ 123" in draft["body"]
    assert "frame Authentication" in draft["body"] and "mermaid login_flow" in draft["body"]
    assert "auth/login.py" in draft["agent_prompt"]
    assert "sequenceDiagram" in draft["agent_prompt"]
    assert decision in draft["acceptance"]
    assert not any(m.get("type") == "ticket" for m in session.messages)
    assert len([m for m in session.messages if m.get("type") == "task"]) == 1

    again = await TaskPlanner().handle(session, {"type": "decision", "text": decision},
                                       source_lines=[{"speaker": "Preet", "text": decision}])
    assert again["tickets"] == [] and len(session.ticket_drafts) == 1


@pytest.mark.asyncio
async def test_first_person_owner_and_model_failure_still_draft_without_invented_paths(monkeypatch):
    async def failed_model(model, messages, **kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr(llm, "chat", failed_model)
    session = Session()
    decision = "I'll own the signup copy update."
    result = await TaskPlanner().handle(session, {"type": "decision", "text": decision},
                                        source_lines=[{"speaker": "Parsh", "text": decision}])
    draft = result["tickets"][0]
    assert draft["owner"] == "Parsh"
    assert draft["title"] == "I'll own the signup copy update"
    assert "Relevant indexed files" not in draft["body"]
    assert "Relevant indexed files" not in draft["agent_prompt"]


@pytest.mark.asyncio
async def test_explicit_action_item_routes_once_without_visual_or_extra_ticket_send(monkeypatch):
    async def fake_model(model, messages, **kwargs):
        if "Conductor router" in messages[0]["content"]:
            return '{"routes": []}'
        return '{"title":"Update signup copy","owner":null}'

    monkeypatch.setattr(llm, "chat", fake_model)
    session = Session(Map())
    line = "Action item: Parsh will own the signup copy update."
    await Conductor().handle(session, [{"type": "task", "text": "Parsh will own it"}],
                             source_lines=[{"speaker": "Preet", "text": line}])
    assert len(session.ticket_drafts) == 1
    draft = next(iter(session.ticket_drafts.values()))
    assert draft["owner"] == "Parsh"
    assert line in draft["body"]
    assert not any(message.get("type") == "ticket" for message in session.messages)


@pytest.mark.asyncio
async def test_ticket_does_not_assign_code_author_without_meeting_commitment(monkeypatch):
    async def suggestion(model, messages, **kwargs):
        return '{"title":"Migrate login sessions","owner":"Avery Chen"}'

    monkeypatch.setattr(llm, "chat", suggestion)
    session = Session(Map())
    result = await TaskPlanner().handle(session, {"type": "decision",
                                                 "text": "We decided to migrate login sessions."},
                                        source_lines=[
                                            {"speaker": "Bob", "text": "I'll own billing."},
                                            {"speaker": "Morgan", "text": "Avery will own payments."},
                                            {"speaker": "Preet", "text": "We decided to migrate login sessions."},
                                        ])
    assert result["tickets"][0]["owner"] is None
    assert "auth/login.py" in result["tickets"][0]["agent_prompt"]
