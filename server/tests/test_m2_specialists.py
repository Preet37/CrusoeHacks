"""Offline M2 specialist contracts over fake code-map and memory sources."""

import asyncio
import json

import pytest

import llm
from agents.code_analyst import CodeAnalyst
from agents.conductor import Conductor, _wants_visual
from agents.fact_checker import FactChecker
from agents.job_budget import JobBudget
from board import BoardState
from brain.memory import MemoryStore


class Session:
    def __init__(self, *, code_map=None, memory=None):
        self.room = "demo"
        self.board = BoardState()
        self.code_map = code_map
        self.memory = memory
        self.messages = []
        self.updates = []

    async def broadcast(self, message):
        self.messages.append(message)

    async def apply_draw(self, op):
        draw = self.board.apply(op)
        await self.broadcast(draw)
        return True

    def push_update(self, text, urgent=False):
        self.updates.append((text, urgent))


class FakeMap:
    def __init__(self, count=3, long_source=False):
        self.count = count
        self.long_source = long_source
        self.queries = []

    def impact(self, query):
        self.queries.append(query)
        paths = ["auth/login.py", "web/routes.py", "tests/test_auth.py"]
        paths += [f"module_{index}.py" for index in range(max(0, self.count - len(paths)))]
        return [{"path": path, "relation": "source" if index == 0 else "dependent",
                 "owner": "Priya" if index == 0 else "Alex",
                 "summary": f"{path} depends on login"}
                for index, path in enumerate(paths[:self.count])]

    def read_file(self, path, max_chars=4000):
        return ("def login():\n    return authenticate()\n" * 120 if self.long_source else
                "def login():\n    return authenticate()\n")[:max_chars]


class ResolvingMap(FakeMap):
    def resolve(self, query):
        if query.casefold() == "login":
            return ["auth/login.py"]
        if query.casefold() == "auth":
            return ["auth/login.py", "auth/jwt_tokens.py", "auth/permissions.py"]
        return []

    def impact(self, query):
        records = super().impact(query)
        for record in records:
            record["owner"] = "Avery Chen"
        return records


class FakeMemory:
    def search(self, query, *, scope=None, current_only=True, limit=8):
        return [{"id": "f_jwt", "claim": "Auth currently validates JWT tokens.",
                 "source": "auth/jwt_tokens.py:verify", "status": "current",
                 "subject": "auth"}]


async def router_and_specialists(model, messages, **kw):
    system = messages[0]["content"]
    if "Conductor router" in system:
        return json.dumps({"routes": []})
    if "Code Analyst" in system:
        return json.dumps({"summary": "The login change touches the auth and web paths.",
                           "paths": ["auth/login.py", "web/routes.py"]})
    if "Fact Checker" in system:
        return json.dumps({"contradicted": True, "fact_id": "f_jwt"})
    if "Task Planner" in system:
        return json.dumps({"title": "Move login sessions to Redis", "owner": "Priya"})
    raise AssertionError(system)


def event_tree(session):
    starts = {m["event_id"]: m for m in session.messages
              if m.get("type") == "agent_event" and m["state"] == "started"}
    terminals = [m for m in session.messages
                 if m.get("type") == "agent_event" and m["state"] != "started"]
    assert all(m["event_id"] in starts for m in terminals)
    assert len(terminals) == len(starts)
    return starts


def test_robot_arm_request_and_typed_3d_hint_route_visual():
    assert _wants_visual([], [{"text": "Show me a three-joint robot arm"}])
    assert _wants_visual([{"type": "task", "text": "Build a robot arm"}],
                         [{"text": "Could you make one?", "wants_visual": "3d"}])


@pytest.mark.asyncio
async def test_code_impact_recurses_within_job_budget_and_cites_files(monkeypatch):
    monkeypatch.setattr(llm, "chat", router_and_specialists)
    session = Session(code_map=FakeMap(count=8, long_source=True))
    session.board.apply({"op": "add_box", "id": "auth", "label": "Auth",
                         "x": 200, "y": 200, "kind": "service"})
    await Conductor().handle(session, [{"type": "question",
                                        "text": "What does changing auth/login.py touch?",
                                        "entities": []}])

    starts = event_tree(session)
    analyst = next(m for m in starts.values() if m["agent"] == "Code Analyst")
    modules = [m for m in starts.values() if m["agent"] == "Code Analyst module"]
    sections = [m for m in starts.values() if m["agent"] == "Code Analyst section"]
    assert modules and sections
    assert all(m["parent_id"] == analyst["event_id"] for m in modules)
    assert all(m["parent_id"] in {module["event_id"] for module in modules}
               for m in sections)
    assert 1 + len(modules) + len(sections) <= 12
    assert len(session.updates) == 1
    assert "auth/login.py" in session.updates[0][0]
    assert session.updates[0][1] is False
    assert any(m.get("op") == "highlight" and "auth" in m["ids"] for m in session.messages)


@pytest.mark.asyncio
async def test_empty_board_shows_sourced_file_boxes_and_reuses_them(monkeypatch):
    monkeypatch.setattr(llm, "chat", router_and_specialists)
    session = Session(code_map=ResolvingMap())
    conductor = Conductor()
    moment = {"type": "question", "text": "Twin, what does changing login touch?",
              "entities": []}
    await conductor.handle(session, [moment])
    files = {box["label"]: box_id for box_id, box in session.board.boxes.items()}
    assert set(files) == {"auth/login.py", "web/routes.py", "tests/test_auth.py"}
    assert set(files.values()) <= session.board.highlighted
    assert len([m for m in session.messages if m.get("op") == "add_box"]) == 3

    await conductor.handle(session, [moment])
    assert len(session.board.boxes) == 3
    assert len([m for m in session.messages if m.get("op") == "add_box"]) == 3


@pytest.mark.asyncio
async def test_user_clear_during_impact_model_does_not_revive_file_boxes(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_analyst(model, messages, **kw):
        if "Code Analyst" in messages[0]["content"]:
            entered.set()
            await release.wait()
        return await router_and_specialists(model, messages, **kw)

    monkeypatch.setattr(llm, "chat", slow_analyst)
    session = Session(code_map=ResolvingMap())
    job = asyncio.create_task(Conductor().handle(session, [
        {"type": "question", "text": "What does changing login touch?", "entities": []}]))
    await asyncio.wait_for(entered.wait(), 1)
    session.board.apply({"op": "clear"})
    release.set()
    await asyncio.wait_for(job, 1)
    assert session.board.boxes == {}
    assert not any(m.get("op") == "add_box" for m in session.messages)


@pytest.mark.asyncio
async def test_decision_uses_narrow_code_target_and_spoken_owner(monkeypatch):
    monkeypatch.setattr(llm, "chat", router_and_specialists)
    code_map = ResolvingMap(count=1)
    session = Session(code_map=code_map)
    line = "We decided to migrate login to Redis sessions; Avery will own it."
    await Conductor().handle(session, [
        {"type": "question", "text": "What does changing login touch?", "entities": []},
        {"type": "decision", "text": line, "entities": ["auth"]},
    ], source_lines=[{"text": line}])
    starts = [m for m in session.messages
              if m.get("type") == "agent_event" and m["state"] == "started"]
    assert sum(m["agent"] == "Code Analyst" for m in starts) == 1
    task = next(m for m in session.messages if m.get("type") == "task")
    assert task["owner"] == "Avery Chen"
    assert code_map.queries.count("login") == 2  # analyst and planner, same target


@pytest.mark.asyncio
async def test_spoken_decision_replaces_truncated_extraction_without_drawing_future_fact(
        monkeypatch, tmp_path):
    monkeypatch.setattr(llm, "chat", router_and_specialists)
    session = Session(code_map=ResolvingMap(count=1),
                      memory=MemoryStore(tmp_path / "memory.json"))
    drawn = []

    class SpyDiagrammer:
        async def handle(self, *args, **kw):
            drawn.append(args[1])
            return []

    line = "We decided to migrate login to Redis sessions; Avery will own it."
    await Conductor(diagrammer=SpyDiagrammer()).handle(session, [
        {"type": "system_fact", "text": "Auth uses Redis sessions.", "entities": []},
        {"type": "decision", "text": "Avery will own the migration", "entities": []},
    ], source_lines=[{"text": line}])
    assert drawn == []
    assert next(m for m in session.messages if m.get("type") == "task")["owner"] == "Avery Chen"
    decisions = session.memory.current_facts(scope="demo:decisions")
    assert len(decisions) == 1
    assert decisions[0]["claim"] == line


@pytest.mark.asyncio
async def test_false_claim_gets_only_sourced_urgent_correction(monkeypatch):
    monkeypatch.setattr(llm, "chat", router_and_specialists)
    session = Session(memory=FakeMemory())
    await Conductor().handle(session, [{"type": "claim",
                                        "text": "Auth no longer validates JWT tokens.",
                                        "entities": ["auth"]}])
    starts = event_tree(session)
    assert any(m["agent"] == "Fact Checker" for m in starts.values())
    assert session.updates == [(
        "Correction: Auth currently validates JWT tokens. (source: auth/jwt_tokens.py:verify).",
        True)]
    assert not any(m.get("type") == "draw" for m in session.messages)


@pytest.mark.asyncio
async def test_source_line_claim_rescues_misclassified_system_fact(monkeypatch):
    monkeypatch.setattr(llm, "chat", router_and_specialists)
    session = Session(memory=FakeMemory())

    class NoopDiagrammer:
        async def handle(self, *args, **kw):
            return []

    line = "Auth already uses Redis sessions instead of JWT."
    await Conductor(diagrammer=NoopDiagrammer()).handle(
        session, [{"type": "system_fact", "text": line, "entities": ["auth"]}],
        source_lines=[{"line_id": "ln_claim", "text": line}])
    assert len(session.updates) == 1
    assert session.updates[0][1] is True
    assert "auth/jwt_tokens.py:verify" in session.updates[0][0]
    assert any(m.get("agent") == "Fact Checker" for m in session.messages)


@pytest.mark.asyncio
async def test_source_line_decision_routes_when_listener_extracts_nothing(monkeypatch):
    monkeypatch.setattr(llm, "chat", router_and_specialists)
    session = Session(code_map=FakeMap(count=1))
    line = "We decided to migrate login to Redis sessions; Avery will own it."
    await Conductor().handle(session, [], source_lines=[{"line_id": "ln_decision",
                                                         "text": line, "ts": 12345}])
    assert len([m for m in session.messages if m.get("type") == "task"]) == 1
    assert len(session.updates) == 1
    assert "meeting demo @ 12345" in session.updates[0][0]


@pytest.mark.asyncio
async def test_planned_migration_does_not_trigger_code_correction_or_duplicate_analyst(
        monkeypatch, tmp_path):
    monkeypatch.setattr(llm, "chat", router_and_specialists)
    store = MemoryStore(tmp_path / "memory.json")
    store.add_fact("Auth currently validates JWT tokens, not Redis sessions.",
                   "demo_project/auth/login.py:18-24", scope="demo:code",
                   subject="Auth login")
    session = Session(code_map=FakeMap(count=1), memory=store)
    session.active_topic = {"topic_id": "t_auth", "title": "Auth login"}
    line = "We decided to migrate login to Redis sessions; Avery will own it."
    await Conductor().handle(session, [
        {"type": "question", "text": "What does changing login touch?", "entities": []},
        {"type": "claim", "text": "Auth uses Redis sessions.", "entities": []},
        {"type": "decision", "text": line, "entities": []},
    ], source_lines=[{"text": line}])
    starts = [m for m in session.messages
              if m.get("type") == "agent_event" and m["state"] == "started"]
    assert sum(m["agent"] == "Code Analyst" for m in starts) == 1
    assert not any(m["agent"] == "Fact Checker" for m in starts)
    assert len([m for m in session.messages if m.get("type") == "task"]) == 1
    assert len(session.updates) == 1
    assert session.updates[0][1] is False
    assert len(store.current_facts(scope="demo:code")) == 1
    assert len(store.current_facts(scope="demo:decisions")) == 1


@pytest.mark.asyncio
async def test_decision_emits_owned_task_once_and_one_sourced_update(monkeypatch):
    monkeypatch.setattr(llm, "chat", router_and_specialists)
    session = Session(code_map=FakeMap(count=1))
    moment = {"type": "decision", "text": "We will move login sessions to Redis.",
              "entities": ["auth/login.py"]}
    conductor = Conductor()
    await conductor.handle(session, [moment], source_lines=[{"ts": 12345}])
    await conductor.handle(session, [moment], source_lines=[{"ts": 12345}])

    tasks = [m for m in session.messages if m.get("type") == "task"]
    assert len(tasks) == 1
    assert tasks[0]["owner"] == "Priya"
    assert tasks[0]["source"] == "meeting demo @ 12345"
    assert "Move login sessions to Redis" in tasks[0]["title"]
    assert len(session.updates) == 2
    assert "meeting demo @ 12345" in session.updates[0][0]
    assert "Task created" not in session.updates[1][0]
    event_tree(session)


@pytest.mark.asyncio
async def test_code_claim_never_uses_planned_decision_as_implementation_proof(
        monkeypatch, tmp_path):
    store = MemoryStore(tmp_path / "memory.json")
    code = store.add_fact("Auth currently validates JWT tokens.",
                          "demo_project/auth/login.py:18-24",
                          scope="demo:code", subject="Auth login")
    decision = store.add_fact("We plan to move login sessions to Redis.",
                              "meeting demo @ 12345", scope="demo:decisions",
                              subject="Auth login")
    session = Session(memory=store)

    async def malicious_verdict(model, messages, **kw):
        return json.dumps({"contradicted": True, "fact_id": decision["id"]})

    monkeypatch.setattr(llm, "chat", malicious_verdict)
    result = await FactChecker().handle(session, {
        "type": "claim", "text": "Auth no longer validates JWT tokens."})
    assert result["urgent"] is False
    assert result["summary"] is None

    async def valid_verdict(model, messages, **kw):
        return json.dumps({"contradicted": True, "fact_id": code["id"]})

    monkeypatch.setattr(llm, "chat", valid_verdict)
    result = await FactChecker().handle(session, {
        "type": "claim", "text": "Auth no longer validates JWT tokens."})
    assert result["urgent"] is True
    assert result["citations"][0]["id"] == code["id"]
    assert "demo_project/auth/login.py:18-24" in result["summary"]


@pytest.mark.asyncio
async def test_decision_is_stored_in_separate_scope_from_code(monkeypatch, tmp_path):
    monkeypatch.setattr(llm, "chat", router_and_specialists)
    store = MemoryStore(tmp_path / "memory.json")
    code = store.add_fact("Auth currently validates JWT tokens.",
                          "demo_project/auth/login.py:18-24",
                          scope="demo:code", subject="Auth login")
    session = Session(memory=store)
    session.active_topic = {"topic_id": "t_auth", "title": "Auth login"}
    await Conductor().handle(session, [{"type": "decision",
                                        "text": "We will move login sessions to Redis.",
                                        "entities": []}],
                             source_lines=[{"ts": 12345}])
    assert len(store.current_facts(scope="demo:code")) == 1
    assert store.current_facts(scope="demo:code")[0]["id"] == code["id"]
    decisions = store.current_facts(scope="demo:decisions")
    assert len(decisions) == 1
    assert decisions[0]["subject"] == "Auth login"
    assert decisions[0]["source"] == "meeting demo @ 12345"


@pytest.mark.asyncio
async def test_parallel_specialists_merge_one_update(monkeypatch):
    monkeypatch.setattr(llm, "chat", router_and_specialists)
    session = Session(code_map=FakeMap(count=1), memory=FakeMemory())
    started = {"analyst": asyncio.Event(), "checker": asyncio.Event()}
    release = asyncio.Event()

    class Analyst:
        async def handle(self, *args, **kw):
            started["analyst"].set()
            await release.wait()
            return {"summary": "auth/login.py is impacted. Source: auth/login.py.",
                    "highlights": []}

    class Checker:
        async def handle(self, *args, **kw):
            started["checker"].set()
            await release.wait()
            return {"summary": "Correction: JWT remains active (source: auth/jwt_tokens.py).",
                    "urgent": True}

    conductor = Conductor(code_analyst=Analyst(), fact_checker=Checker())
    run = asyncio.create_task(conductor.handle(session, [
        {"type": "question", "text": "What does changing login touch?"},
        {"type": "claim", "text": "Auth no longer validates JWT."},
    ]))
    await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started.values())), 1)
    assert session.updates == []
    release.set()
    await asyncio.wait_for(run, 1)
    assert len(session.updates) == 1
    assert session.updates[0][1] is True
    assert "auth/login.py" in session.updates[0][0]
    assert "auth/jwt_tokens.py" in session.updates[0][0]


def test_job_budget_caps_jobs_and_reserved_model_tokens():
    budget = JobBudget(max_jobs=12, max_tokens=4000)
    assert sum(budget.reserve_job() for _ in range(20)) == 12
    assert budget.reserve_model("x" * 3000, 450) == 450
    assert budget.reserve_model("x" * 10000, 450) == 0
    assert budget.tokens_reserved <= 4000
