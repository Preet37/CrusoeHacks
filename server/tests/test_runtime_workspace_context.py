"""Only the demo room gets demo facts; trusted workspaces restore their own repo."""

import asyncio

import pytest

from brain import repo_connector, runtime, workspace_registry
from brain.workspace_store import save_repo


class Session:
    def __init__(self, room, workspace=None):
        self.room = room
        self.workspace = workspace
        self.sent = []
        self.updates = []

    async def broadcast(self, message):
        self.sent.append(message)

    def push_update(self, text):
        self.updates.append(text)


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("TWIN_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("TWIN_DB_PATH", str(tmp_path / "workspaces.sqlite3"))
    monkeypatch.delenv("TWIN_REPO_ROOT", raising=False)
    registry = workspace_registry.WorkspaceRegistry()
    monkeypatch.setattr(runtime, "REGISTRY", registry)
    monkeypatch.setattr(repo_connector, "REGISTRY", registry)
    monkeypatch.setattr(runtime, "demo_map", lambda: "DEMO_MAP")
    return registry


def test_demo_is_only_seeded_in_unconnected_demo_room(isolated):
    demo = Session("demo")
    runtime.attach(demo)
    assert demo.code_map == "DEMO_MAP"
    assert len(demo.memory.current_facts(scope="demo:code")) == 2

    arbitrary = Session("crusoehacks-standup")
    runtime.attach(arbitrary)
    assert arbitrary.code_map is None
    assert arbitrary.memory.current_facts() == []
    assert arbitrary.code_fact_scope != "demo:code"

    workspace_demo = Session("demo", "crusoehacks")
    runtime.attach(workspace_demo)
    assert workspace_demo.code_map is None
    assert workspace_demo.memory.current_facts() == []


def test_late_workspace_authorization_discards_legacy_demo_context(isolated):
    session = Session("demo")
    runtime.attach(session)
    assert session.code_map == "DEMO_MAP"
    session.workspace = "crusoehacks"
    runtime.attach(session)
    assert session.code_map is None
    assert session.code_fact_scope != "demo:code"
    assert session.memory.current_facts() == []


def test_existing_legacy_repo_is_not_replaced_by_demo(isolated):
    session = Session("demo")
    session.code_map = "CONNECTED_MAP"
    session.repo_url = "https://github.com/example/real.git"
    session.code_fact_scope = "repo:verified:code"
    runtime.attach(session)
    assert session.code_map == "CONNECTED_MAP"
    assert session.code_fact_scope == "repo:verified:code"
    assert session.memory.current_facts(scope="demo:code") == []


@pytest.mark.asyncio
async def test_saved_repo_restores_into_every_workspace_room_and_memory_is_shared(
        isolated, monkeypatch):
    save_repo("crusoehacks", "https://github.com/Preet37/LoominPhysicsSandbox.git")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def clone(url, target):
        entered.set()
        await release.wait()
        target.mkdir()
        (target / "geometry.py").write_text("def build(): pass\n")

    monkeypatch.setattr(repo_connector, "_clone", clone)
    first = Session("crusoehacks-standup", "crusoehacks")
    second = Session("crusoehacks-planning", "crusoehacks")
    runtime.attach(first)
    await asyncio.wait_for(entered.wait(), 1)
    runtime.attach(second)
    assert first.code_map is second.code_map is None
    assert first.memory.path == second.memory.path
    assert first.memory.current_facts() == []
    first.memory.add_fact("The design includes geometry", "geometry.py:1",
                          scope="meeting:design")
    assert len(second.memory.current_facts(scope="meeting:design")) == 1

    release.set()
    for _ in range(100):
        if isolated.current("crusoehacks") is not None:
            break
        await asyncio.sleep(.01)
    else:
        pytest.fail("saved repository did not restore")
    assert first.code_map is second.code_map
    assert set(first.code_map.files) == {"geometry.py"}
    assert first.repo_url == second.repo_url == "https://github.com/Preet37/LoominPhysicsSandbox.git"
    assert first.code_fact_scope == second.code_fact_scope
    third = Session("crusoehacks-retro", "crusoehacks")
    runtime.attach(third)
    assert third.code_map is first.code_map
    assert third.memory.path == first.memory.path
