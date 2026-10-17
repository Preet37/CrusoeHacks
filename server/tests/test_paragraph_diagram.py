"""A pasted architecture paragraph makes one atomic deep graph plan."""

import asyncio
import json

import pytest

import config
import llm
from agents.diagrammer import Diagrammer, StaleBoardError
from board import BoardState


SPEC = ("Our architecture has a web client and mobile client that call an API gateway. "
        "The gateway routes login to Auth service and billing to Billing service. "
        "Auth reads Postgres and Redis cache, while Billing writes Invoice database. "
        "A worker consumes queue events and notifications sends email. "
        "Each service runs independently; client requests cross gateway boundaries. " * 3)
PLAN = {"layers": [{"id": "clients", "title": "Clients"},
                   {"id": "services", "title": "Services"},
                   {"id": "data", "title": "Data"}],
        "nodes": [{"id": key, "label": label, "kind": kind, "layer": layer}
                  for key, label, kind, layer in [
                      ("web", "Web", "client", "clients"),
                      ("mobile", "Mobile", "client", "clients"),
                      ("gateway", "API Gateway", "service", "services"),
                      ("auth", "Auth", "service", "services"),
                      ("billing", "Billing", "service", "services"),
                      ("postgres", "Postgres", "datastore", "data"),
                      ("redis", "Redis", "datastore", "data"),
                      ("invoices", "Invoice DB", "datastore", "data"),
                  ]],
        "edges": [{"from": a, "to": b, "label": label} for a, b, label in [
            ("web", "gateway", "HTTPS"), ("mobile", "gateway", "HTTPS"),
            ("gateway", "auth", "login"), ("gateway", "billing", "billing"),
            ("auth", "postgres", "users"), ("auth", "redis", "sessions"),
            ("billing", "invoices", "writes"),
        ]]}


class Session:
    def __init__(self):
        self.board = BoardState()
        self.sent = []
        self.updates = []

    async def broadcast(self, message):
        self.sent.append(message)

    def push_update(self, text):
        self.updates.append(text)


def run(session):
    return Diagrammer().handle(session, [{"type": "system_fact", "text": SPEC}],
                              topic_id="t_arch", title="Architecture", topic_changed=True,
                              source_lines=[{"text": SPEC}])


@pytest.mark.asyncio
async def test_long_spec_one_call_layered_eight_nodes_labeled_edges(monkeypatch):
    calls = []

    async def chat(model, messages, **kwargs):
        calls.append(model)
        return json.dumps(PLAN)

    monkeypatch.setattr(llm, "chat", chat)
    session = Session()
    await run(session)
    assert calls == [config.MODEL_RESPONDER]
    assert session.board.active_frame == "t_arch"
    assert len(session.board.boxes) == 8
    assert len(session.board.edges) == 7
    assert len([shape for shape in session.board.shapes.values() if shape["op"] == "frame"]) == 4
    assert {edge["label"] for edge in session.board.edges.values()} >= {"HTTPS", "login", "billing"}
    assert len(session.updates) == 1


@pytest.mark.asyncio
async def test_invalid_plan_rejects_all_content_before_broadcast(monkeypatch):
    invalid = json.loads(json.dumps(PLAN))
    invalid["edges"][-1]["to"] = "missing"
    async def chat(*args, **kwargs):
        return json.dumps(invalid)
    monkeypatch.setattr(llm, "chat", chat)
    session = Session()
    with pytest.raises(ValueError, match="endpoint"):
        await run(session)
    assert session.board.boxes == {}
    assert session.board.edges == {}
    assert not any(m.get("op") == "add_box" for m in session.sent)


@pytest.mark.asyncio
async def test_clear_during_one_model_call_blocks_stale_plan(monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    async def chat(*args, **kwargs):
        started.set()
        await release.wait()
        return json.dumps(PLAN)
    monkeypatch.setattr(llm, "chat", chat)
    session = Session()
    task = asyncio.create_task(run(session))
    await asyncio.wait_for(started.wait(), 1)
    session.board.apply({"op": "clear"})
    release.set()
    with pytest.raises(StaleBoardError):
        await asyncio.wait_for(task, 1)
    assert session.board.boxes == {}
    assert session.board.edges == {}


@pytest.mark.asyncio
async def test_second_paragraph_keeps_existing_human_ids_and_reports_lightning(monkeypatch):
    async def chat(*args, **kwargs):
        return json.dumps(PLAN)
    monkeypatch.setattr(llm, "chat", chat)
    session = Session()
    session.board.apply({"op": "add_box", "id": "human_box", "label": "Human",
                         "kind": "note", "x": 12, "y": 12})
    await run(session)
    first_ids = set(session.board.boxes)
    await run(session)
    assert session.board.boxes["human_box"]["label"] == "Human"
    assert first_ids < set(session.board.boxes)
    assert len(session.board.boxes) == 17
    events = [message for message in session.sent if message.get("type") == "agent_event"
              and message.get("agent") == "Diagrammer"]
    assert events
    assert all(event["model"] == config.MODEL_RESPONDER.rsplit("/", 1)[-1] for event in events)


@pytest.mark.asyncio
async def test_unique_label_aliases_resolve_without_inventing_nodes(monkeypatch):
    aliased = json.loads(json.dumps(PLAN))
    aliased["edges"][0]["from"] = "Web"
    aliased["edges"][0]["to"] = "API Gateway"
    async def chat(*args, **kwargs):
        return json.dumps(aliased)
    monkeypatch.setattr(llm, "chat", chat)
    session = Session()
    await run(session)
    assert len(session.board.edges) == 7


@pytest.mark.asyncio
async def test_natural_language_model_ids_stay_internal_and_nodes_use_layer_coordinates(monkeypatch):
    plan = json.loads(json.dumps(PLAN))
    layer_names = {"clients": "Client Applications", "services": "Application Services",
                   "data": "Data and Infrastructure"}
    for layer in plan["layers"]:
        layer["id"] = layer_names[layer["id"]]
    labels = {node["id"]: node["label"] for node in plan["nodes"]}
    for node in plan["nodes"]:
        node["layer"] = layer_names[node["layer"]]
        node["id"] = node["label"]
    for edge in plan["edges"]:
        edge["from"] = labels[edge["from"]]
        edge["to"] = labels[edge["to"]]

    async def chat(*args, **kwargs):
        return json.dumps(plan)

    monkeypatch.setattr(llm, "chat", chat)
    session = Session()
    await run(session)
    assert len(session.board.boxes) == 8
    assert len(session.board.edges) == 7
    assert all(box["id"].startswith("arch_") and box["y"] == 500
               for box in session.board.boxes.values())


def indexed_plan():
    plan = json.loads(json.dumps(PLAN))
    layer_index = {layer["id"]: index for index, layer in enumerate(plan["layers"])}
    node_index = {node["id"]: index for index, node in enumerate(plan["nodes"])}
    return {
        "layers": [{"title": layer["title"]} for layer in plan["layers"]],
        "nodes": [{"label": node["label"], "kind": node["kind"],
                   "layer": layer_index[node["layer"]]} for node in plan["nodes"]],
        "edges": [{"from": node_index[edge["from"]], "to": node_index[edge["to"]],
                   "label": edge["label"]} for edge in plan["edges"]],
    }


@pytest.mark.asyncio
async def test_indexed_one_call_plan_is_complete(monkeypatch):
    calls = []
    async def chat(model, messages, **kwargs):
        calls.append(messages[0]["content"])
        return json.dumps(indexed_plan())
    monkeypatch.setattr(llm, "chat", chat)
    session = Session()
    await run(session)
    assert len(calls) == 1
    assert "ZERO-BASED INTEGER" in calls[0]
    assert len(session.board.boxes) == 8
    assert len(session.board.edges) == 7
    assert {edge["label"] for edge in session.board.edges.values()} == {
        "HTTPS", "login", "billing", "users", "sessions", "writes"}


@pytest.mark.parametrize("bad_endpoint", [8, -1, True])
@pytest.mark.asyncio
async def test_indexed_dangling_or_boolean_endpoint_rejects_atomically(monkeypatch, bad_endpoint):
    plan = indexed_plan()
    plan["edges"][-1]["to"] = bad_endpoint
    async def chat(*args, **kwargs):
        return json.dumps(plan)
    monkeypatch.setattr(llm, "chat", chat)
    session = Session()
    with pytest.raises(ValueError, match="endpoint"):
        await run(session)
    assert session.board.boxes == {}
    assert session.board.edges == {}
