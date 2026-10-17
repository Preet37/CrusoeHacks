"""Workspace repo sharing requires an explicit trusted Session.workspace."""

import asyncio
import gc
from pathlib import Path

import pytest

from brain import repo_connector as connector
from brain import runtime
from brain import workspace_registry


class Session:
    def __init__(self, room, workspace=None):
        self.room = room
        self.workspace = workspace
        self.sent = []
        self.updates = []
        self.code_map = "demo"

    async def broadcast(self, message):
        self.sent.append(message)

    def push_update(self, text):
        self.updates.append(text)


@pytest.fixture
def registry(monkeypatch, tmp_path):
    monkeypatch.setenv('TWIN_DB_PATH', str(tmp_path / 'workspace.sqlite3'))
    value = workspace_registry.WorkspaceRegistry()
    monkeypatch.setattr(runtime, "REGISTRY", value)
    monkeypatch.setattr(connector, "REGISTRY", value)
    monkeypatch.setattr(runtime, "demo_map", lambda: "demo")
    return value


@pytest.mark.asyncio
async def test_lobby_connect_updates_existing_and_new_workspace_sessions(
        monkeypatch, tmp_path, registry):
    monkeypatch.setenv("TWIN_STATE_DIR", str(tmp_path / "state"))

    async def clone(url, target):
        target.mkdir()
        (target / "auth.py").write_text("def login(): pass\n")

    monkeypatch.setattr(connector, "_clone", clone)
    lobby = Session("acme-lobby", "acme")
    standup = Session("acme-standup", "acme")
    runtime.attach(lobby)
    runtime.attach(standup)
    await connector.connect_repo(lobby, "https://github.com/demo/auth")

    assert lobby.code_map is standup.code_map
    assert set(standup.code_map.files) == {"auth.py"}
    assert standup.repo_url == "https://github.com/demo/auth.git"
    assert standup.code_fact_scope == lobby.code_fact_scope
    late = Session("acme-planning", "acme")
    runtime.attach(late)
    assert late.code_map is lobby.code_map
    assert late.repo_url == lobby.repo_url

    # No workspace attribute means legacy room isolation, even if its room
    # string looks like the same workspace prefix.
    legacy = Session("acme-retro")
    runtime.attach(legacy)
    assert legacy.code_map == "demo"
    assert not hasattr(legacy, "repo_url")
    other_workspace = Session("acme-standup", "other")
    runtime.attach(other_workspace)
    assert other_workspace.code_map == "demo"
    assert not hasattr(other_workspace, "repo_url")


@pytest.mark.asyncio
async def test_latest_workspace_connect_wins_and_old_clone_is_removed(
        monkeypatch, registry):
    old_entered = asyncio.Event()
    release_old = asyncio.Event()
    old_dirs = []

    async def clone(url, target):
        target.mkdir()
        (target / ("old.py" if url.endswith("old.git") else "new.py")).write_text("x = 1\n")
        if url.endswith("old.git"):
            old_dirs.append(target.parent)
            old_entered.set()
            await release_old.wait()

    monkeypatch.setattr(connector, "_clone", clone)
    lobby = Session("acme-lobby", "acme")
    standup = Session("acme-standup", "acme")
    registry.register(lobby)
    registry.register(standup)
    old = asyncio.create_task(connector.connect_repo(lobby, "https://github.com/demo/old"))
    await old_entered.wait()
    await connector.connect_repo(standup, "https://github.com/demo/new")
    release_old.set()
    with pytest.raises(asyncio.CancelledError):
        await old
    assert set(lobby.code_map.files) == {"new.py"}
    assert lobby.code_map is standup.code_map
    assert all(not path.exists() for path in old_dirs)


@pytest.mark.asyncio
async def test_replaced_workspace_checkout_lives_until_last_map_reader_finishes(
        monkeypatch, registry):
    async def clone(url, target):
        target.mkdir()
        (target / ("old.py" if url.endswith("old.git") else "new.py")).write_text("x = 1\n")

    monkeypatch.setattr(connector, "_clone", clone)
    session = Session("acme-lobby", "acme")
    registry.register(session)
    await connector.connect_repo(session, "https://github.com/demo/old")
    old_map = session.code_map
    old_checkout = Path(old_map.root)
    await connector.connect_repo(session, "https://github.com/demo/new")
    assert old_checkout.exists()  # an in-flight Code Analyst may still read it
    assert old_map.read_file("old.py").strip() == "x = 1"
    del old_map
    gc.collect()
    assert not old_checkout.exists()


def test_workspace_key_rejects_untrusted_shape_and_never_parses_room():
    assert workspace_registry.workspace_key(Session("acme-lobby")) is None
    with pytest.raises(ValueError):
        workspace_registry.workspace_key(Session("room", "../acme"))


def test_registered_session_cannot_move_between_workspaces(registry, tmp_path):
    session = Session("acme-lobby", "acme")
    registry.register(session)
    registry.register(session)  # Same trusted identity remains idempotent.

    session.workspace = "other"
    with pytest.raises(ValueError, match="cannot change"):
        registry.register(session)

    class Map:
        pass

    generation = registry.claim("acme")
    registry.publish("acme", generation, Map(), "https://example.test/repo.git",
                     "acme:code", tmp_path / "checkout")
    assert session.code_map == "demo"  # Old membership cannot leak a map after mutation.

    session.workspace = None
    with pytest.raises(ValueError, match="cannot change"):
        registry.register(session)
