"""Contributor runs per substantive moment only when the meeting mode allows it."""

import asyncio

import pytest

from agents.conductor import Conductor
from board import BoardState


class Session:
    def __init__(self, mode="proactive"):
        self.board = BoardState()
        self.twin_mode = mode
        self.active_topic = {"topic_id": "topic_a"}
        self.sent = []

    async def broadcast(self, message):
        self.sent.append(message)


class Contributor:
    def __init__(self):
        self.moments = []

    async def handle(self, session, moment=None, **kwargs):
        self.moments.append(moment)
        return {"show": "Sourced context", "current": "Current implementation", "impact": [],
                "risks": [], "question": "What should we verify?", "sources": []}


class Diagrammer:
    def __init__(self):
        self.calls = []

    async def handle(self, session, moments, **kwargs):
        self.calls.append(moments)
        return []


def contributions(session):
    return [message for message in session.sent if message.get("type") == "contribution"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,text", [
    ("decision", "We decided to use Redis next quarter."),
    ("claim", "Auth currently issues JWT tokens."),
    ("question", "What does changing login touch?"),
    ("task", "Avery will own the migration."),
    ("system_fact", "Auth uses Postgres."),
])
async def test_proactive_routes_each_substantive_kind_without_visual_request(kind, text):
    session = Session()
    contributor = Contributor()
    diagrammer = Diagrammer()
    await Conductor(diagrammer=diagrammer, contributor=contributor).handle(
        session, [{"type": kind, "text": text}])
    assert [moment["type"] for moment in contributor.moments] == [kind]
    assert len(contributions(session)) == 1
    assert len(diagrammer.calls) == int(kind == "system_fact")


@pytest.mark.asyncio
async def test_proactive_routes_all_unique_substantive_moments_in_parallel():
    session = Session()
    contributor = Contributor()
    kinds = ["decision", "claim", "question", "task", "system_fact"]
    moments = [{"type": kind, "text": f"Unique {kind} context"} for kind in kinds]
    moments += [moments[0].copy(), {"type": "small_talk", "text": "Nice weather"}]
    await Conductor(diagrammer=Diagrammer(), contributor=contributor).handle(session, moments)
    assert {moment["type"] for moment in contributor.moments} == set(kinds)
    assert len(contributor.moments) == len(contributions(session)) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,text,expected", [
    ("on_request", "What does changing login touch?", 0),
    ("on_request", "Draw the login sequence diagram.", 1),
    ("off", "Draw the login sequence diagram.", 0),
])
async def test_other_modes_preserve_existing_gate(mode, text, expected):
    session = Session(mode)
    contributor = Contributor()
    await Conductor(diagrammer=Diagrammer(), contributor=contributor).handle(
        session, [{"type": "question", "text": text}], source_lines=[{"text": text}])
    assert len(contributor.moments) == len(contributions(session)) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["mode", "topic", "board"])
async def test_inflight_proactive_result_is_dropped_after_context_change(change):
    session = Session()
    started = asyncio.Event()
    release = asyncio.Event()

    class SlowContributor(Contributor):
        async def handle(self, session, moment=None, **kwargs):
            started.set()
            await release.wait()
            return await super().handle(session, moment=moment, **kwargs)

    contributor = SlowContributor()
    task = asyncio.create_task(Conductor(diagrammer=Diagrammer(), contributor=contributor).handle(
        session, [{"type": "question", "text": "What changed?"}], topic_id="topic_a"))
    await asyncio.wait_for(started.wait(), 1)
    if change == "mode":
        session.twin_mode = "on_request"
    elif change == "topic":
        session.active_topic = {"topic_id": "topic_b"}
    else:
        session.board = BoardState()
    release.set()
    await asyncio.wait_for(task, 1)
    assert contributions(session) == []
