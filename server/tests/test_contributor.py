"""Contributor speaks from indexed code only and fails closed on weak evidence."""

import json

import pytest

import config
import llm
from agents.contributor import Contributor


class Map:
    files = {"auth/login.py": {"path": "auth/login.py"}}

    def resolve(self, query):
        return ["auth/login.py"] if "login" in query.lower() else []

    def read_file(self, path, max_chars=2400):
        assert path == "auth/login.py"
        return "def login():\n    user = postgres.get_user()\n    return jwt.sign(user)\n"


class MapNoPostgres(Map):
    def read_file(self, path, max_chars=2400):
        assert path == "auth/login.py"
        return "def login():\n    user = get_user()\n    return jwt.sign(user)\n"


class Board:
    def summary(self):
        return "Boxes: browser, auth, Postgres."


class Session:
    def __init__(self, code_map=None):
        self.code_map = code_map
        self.board = Board()
        self.active_topic = {"topic_id": "t_auth", "title": "Login"}
        self.history = [{"speaker": "Preet", "text": "Show the login flow and Redis migration."}]
        self.messages = []

    async def broadcast(self, message):
        self.messages.append(message)


def answer(**changes):
    value = {"show": "I'll show the login flow.",
             "current": "Login reads a Postgres user and returns a JWT.",
             "impact": ["A Redis migration would change session handling."],
             "risks": ["Logout may need key invalidation."],
             "question": "What session TTL should we choose?",
             "sources": ["auth/login.py:1-3"]}
    value.update(changes)
    return json.dumps(value)


@pytest.mark.asyncio
async def test_sourced_contribution_one_call_and_parented_events(monkeypatch):
    calls = []

    async def fake(model, messages, **kwargs):
        calls.append((model, messages, kwargs))
        return answer()

    monkeypatch.setattr(llm, "chat", fake)
    session = Session(Map())
    result = await Contributor().handle(session, {"text": "Draw login flow"},
                                        parent_id="conductor-1", source_lines=session.history)
    assert len(calls) == 1 and calls[0][0] == config.MODEL_COACH
    assert calls[0][2]["max_tokens"] <= 500
    assert "auth/login.py" in calls[0][1][1]["content"]
    assert result["sources"] == ["auth/login.py:1-3"]
    assert "Postgres" in result["current"]
    events = [m for m in session.messages if m.get("type") == "agent_event"]
    assert [m["state"] for m in events] == ["started", "done"]
    assert events[0]["parent_id"] == "conductor-1"
    assert events[1]["event_id"] == events[0]["event_id"]


@pytest.mark.asyncio
async def test_invented_source_and_unsupported_current_fail_closed(monkeypatch):
    async def invented(model, messages, **kwargs):
        return answer(sources=["auth/redis.py:1-3"])

    monkeypatch.setattr(llm, "chat", invented)
    result = await Contributor().handle(Session(Map()), {"text": "login"})
    assert result["sources"] == ["auth/login.py:1-3"]
    assert result["current"] == "Current repo entry points include auth/login.py:1-3."

    async def unsupported(model, messages, **kwargs):
        return answer(current="Redis stores all production sessions already.")

    monkeypatch.setattr(llm, "chat", unsupported)
    result = await Contributor().handle(Session(Map()), {"text": "login"})
    assert result["sources"] == ["auth/login.py:1-3"] and result["impact"] == []

    async def unsupported_postgres(model, messages, **kwargs):
        return answer(current="Login reads PostgreSQL users and returns a JWT.")

    monkeypatch.setattr(llm, "chat", unsupported_postgres)
    result = await Contributor().handle(Session(MapNoPostgres()), {"text": "login"})
    assert result["sources"] == ["auth/login.py:1-3"]
    assert result["current"] == "Current repo entry points include auth/login.py:1-3."


@pytest.mark.asyncio
async def test_embedded_unindexed_path_is_removed(monkeypatch):
    async def forged_path(model, messages, **kwargs):
        return answer(impact=["Change auth/redis.py to store sessions."],
                      risks=["Check tests/test_auth.py after migration."])

    monkeypatch.setattr(llm, "chat", forged_path)
    result = await Contributor().handle(Session(Map()), {"text": "login"})
    assert result["sources"] == ["auth/login.py:1-3"]
    assert result["impact"] == [] and result["risks"] == []


@pytest.mark.asyncio
async def test_provider_failure_uses_one_nano_fallback(monkeypatch):
    models = []

    async def fake(model, messages, **kwargs):
        models.append(model)
        if model == config.MODEL_COACH:
            raise RuntimeError("provider unavailable")
        return answer()

    monkeypatch.setattr(llm, "chat", fake)
    session = Session(Map())
    result = await Contributor().handle(session, {"text": "login"})
    assert models == [config.MODEL_COACH, config.MODEL_FAST]
    assert result["sources"] == ["auth/login.py:1-3"]
    assert session.messages[-1]["model"] == config.MODEL_FAST.split("/")[-1]


@pytest.mark.asyncio
async def test_missing_code_map_does_not_call_model(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("model should not be called without evidence")

    monkeypatch.setattr(llm, "chat", forbidden)
    result = await Contributor().handle(Session(), {"text": "login"})
    assert result["sources"] == []
    assert "don't have verified repo context" in result["current"]
