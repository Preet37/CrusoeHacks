"""Persist only successful workspace repository choices and restore once."""

import asyncio

import pytest

from brain import repo_connector, runtime, workspace_registry, workspace_store


class Session:
    def __init__(self, room, workspace):
        self.room = room
        self.workspace = workspace
        self.code_map = "demo"
        self.messages = []

    async def broadcast(self, message):
        self.messages.append(message)

    def push_update(self, text):
        pass


def test_workspace_store_roundtrip_and_isolation(monkeypatch, tmp_path):
    monkeypatch.setenv("TWIN_DB_PATH", str(tmp_path / "shared.sqlite3"))
    assert workspace_store.get_workspace("acme") == {
        "workspace": "acme", "repo_url": None, "integrations": {}}
    workspace_store.save_repo("acme", "https://github.com/demo/one")
    assert workspace_store.get_workspace("acme")["repo_url"] == "https://github.com/demo/one.git"
    assert workspace_store.get_workspace("other")["repo_url"] is None
    with pytest.raises(ValueError):
        workspace_store.save_repo("../acme", "https://github.com/demo/one")
    with pytest.raises(ValueError):
        workspace_store.save_repo("acme", "https://example.com/elsewhere")


@pytest.mark.asyncio
async def test_successful_publish_persists_and_new_runtime_restores_once(monkeypatch, tmp_path):
    monkeypatch.setenv("TWIN_DB_PATH", str(tmp_path / "workspaces.sqlite3"))
    monkeypatch.setenv("TWIN_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(runtime, "demo_map", lambda: "demo")
    first_registry = workspace_registry.WorkspaceRegistry()
    monkeypatch.setattr(runtime, "REGISTRY", first_registry)
    monkeypatch.setattr(repo_connector, "REGISTRY", first_registry)

    async def clone(url, target):
        target.mkdir()
        (target / "auth.py").write_text("def login(): pass\n")

    monkeypatch.setattr(repo_connector, "_clone", clone)
    original = Session("acme-lobby", "acme")
    runtime.attach(original)
    await repo_connector.connect_repo(original, "https://github.com/demo/auth")
    saved = workspace_store.get_workspace("acme")
    assert saved["repo_url"] == "https://github.com/demo/auth.git"

    # Simulate a process restart: registry is empty but SQLite remains.
    second_registry = workspace_registry.WorkspaceRegistry()
    monkeypatch.setattr(runtime, "REGISTRY", second_registry)
    monkeypatch.setattr(repo_connector, "REGISTRY", second_registry)
    calls = []
    restored = asyncio.Event()

    async def restore(session, url, *, restore_generation):
        calls.append((session.room, url, restore_generation))
        restored.set()

    monkeypatch.setattr(runtime, "connect_repo", restore)
    runtime.attach(Session("acme-standup", "acme"))
    restore_generation = second_registry._generation["acme"]
    runtime.attach(Session("acme-retro", "acme"))
    await asyncio.wait_for(restored.wait(), 1)
    await asyncio.sleep(0)
    assert calls == [("acme-standup", "https://github.com/demo/auth.git", restore_generation)]
    assert second_registry.claim("acme") is not restore_generation
    assert not second_registry.publish("acme", restore_generation, object(),
                                       "https://github.com/demo/auth.git", "repo:old:code",
                                       tmp_path / "stale")


@pytest.mark.asyncio
async def test_saved_repo_reindexes_once_and_shares_map_after_restart(monkeypatch, tmp_path):
    monkeypatch.setenv("TWIN_DB_PATH", str(tmp_path / "workspaces.sqlite3"))
    monkeypatch.setenv("TWIN_STATE_DIR", str(tmp_path / "state"))
    workspace_store.save_repo("acme", "https://github.com/demo/auth")
    registry = workspace_registry.WorkspaceRegistry()
    monkeypatch.setattr(runtime, "REGISTRY", registry)
    monkeypatch.setattr(repo_connector, "REGISTRY", registry)
    monkeypatch.setattr(runtime, "demo_map", lambda: "demo")
    clones = []

    async def clone(url, target):
        clones.append(url)
        target.mkdir()
        (target / "auth.py").write_text("def login(): pass\n")

    monkeypatch.setattr(repo_connector, "_clone", clone)
    lobby = Session("acme-lobby", "acme")
    standup = Session("acme-standup", "acme")
    runtime.attach(lobby)
    runtime.attach(standup)
    async with asyncio.timeout(2):
        while registry.current("acme") is None:
            await asyncio.sleep(0.01)
    assert clones == ["https://github.com/demo/auth.git"]
    assert lobby.code_map is standup.code_map
    assert set(standup.code_map.files) == {"auth.py"}


@pytest.mark.asyncio
async def test_failed_restore_retries_on_later_join_not_in_a_loop(monkeypatch, tmp_path):
    monkeypatch.setenv("TWIN_DB_PATH", str(tmp_path / "workspaces.sqlite3"))
    monkeypatch.setenv("TWIN_STATE_DIR", str(tmp_path / "state"))
    workspace_store.save_repo("acme", "https://github.com/demo/auth")
    registry = workspace_registry.WorkspaceRegistry()
    monkeypatch.setattr(runtime, "REGISTRY", registry)
    monkeypatch.setattr(runtime, "demo_map", lambda: "demo")
    calls = []

    async def failed_restore(session, url, *, restore_generation):
        calls.append(session.room)
        # The connector reports provider errors to the session and returns.

    monkeypatch.setattr(runtime, "connect_repo", failed_restore)
    runtime.attach(Session("acme-lobby", "acme"))
    async with asyncio.timeout(1):
        while len(calls) < 1 or registry._restore_started:
            await asyncio.sleep(0.01)
    assert calls == ["acme-lobby"]
    runtime.attach(Session("acme-standup", "acme"))
    async with asyncio.timeout(1):
        while len(calls) < 2:
            await asyncio.sleep(0.01)
    assert calls == ["acme-lobby", "acme-standup"]
