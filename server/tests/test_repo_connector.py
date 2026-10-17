import asyncio
from pathlib import Path

import pytest

from brain import repo_connector as connector


@pytest.mark.parametrize('url', [
    'http://github.com/a/b', 'https://github.com.evil.test/a/b',
    'https://user:secret@github.com/a/b', 'https://github.com/a/b/tree/main',
    'file:///tmp/repo', 'https://github.com/a/b?token=secret',
    'https://github.com/a/..', 'https://github.com/a/%2e%2e',
])
def test_only_public_repo_urls_allowed(url):
    with pytest.raises(ValueError):
        connector.canonical_url(url)


def test_repo_url_normalized():
    assert connector.canonical_url('https://github.com/Preet37/CrusoeHacks/') == 'https://github.com/Preet37/CrusoeHacks.git'


class Session:
    def __init__(self):
        self.sent = []
        self.updates = []
        self.code_map = 'original'
    async def broadcast(self, message):
        self.sent.append(message)
    def push_update(self, text):
        self.updates.append(text)


@pytest.mark.asyncio
async def test_failed_clone_preserves_existing_map_and_cleans_directory(monkeypatch):
    paths = []
    async def fail(url, target):
        paths.append(target.parent)
        raise ValueError('Public repository unavailable.')
    monkeypatch.setattr(connector, '_clone', fail)
    session = Session()
    await connector.connect_repo(session, 'https://github.com/demo/repo')
    assert session.code_map == 'original'
    assert all(not path.exists() for path in paths)
    assert not session.updates
    assert any(m['type'] == 'error' for m in session.sent)


@pytest.mark.asyncio
async def test_connect_builds_map_without_executing_source(monkeypatch):
    async def clone(url, target):
        target.mkdir()
        (target / 'auth.py').write_text('raise RuntimeError("must never execute")\ndef login(): pass\n')
        (target / 'web.py').write_text('from auth import login\n')
    monkeypatch.setattr(connector, '_clone', clone)
    session = Session()
    try:
        await connector.connect_repo(session, 'https://github.com/demo/repo')
        assert set(session.code_map.files) == {'auth.py', 'web.py'}
        assert len(session.updates) == 1
        assert session.repo_url == 'https://github.com/demo/repo.git'
    finally:
        import shutil
        shutil.rmtree(session._repo_directory, ignore_errors=True)


@pytest.mark.asyncio
async def test_connected_repo_root_wins_over_nested_demo_project(monkeypatch):
    async def clone(url, target):
        target.mkdir()
        (target / 'app.py').write_text('def serve(): pass\n')
        nested = target / 'demo_project'
        nested.mkdir()
        (nested / 'auth.py').write_text('def demo_login(): pass\n')
    monkeypatch.setattr(connector, '_clone', clone)
    session = Session()
    try:
        await connector.connect_repo(session, 'https://github.com/demo/repo')
        assert set(session.code_map.files) == {'app.py', 'demo_project/auth.py'}
        assert session.code_map.root.name == 'checkout'
    finally:
        import shutil
        shutil.rmtree(session._repo_directory, ignore_errors=True)


@pytest.mark.asyncio
async def test_cancelled_clone_cleans_directory(monkeypatch):
    entered = asyncio.Event()
    paths = []
    async def clone(url, target):
        paths.append(target.parent)
        entered.set()
        await asyncio.Future()
    monkeypatch.setattr(connector, '_clone', clone)
    session = Session()
    task = asyncio.create_task(connector.connect_repo(session, 'https://github.com/demo/repo'))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert session.code_map == 'original'
    assert all(not path.exists() for path in paths)


def test_clone_size_budget(monkeypatch, tmp_path):
    monkeypatch.setattr(connector, 'MAX_BYTES', 10)
    (tmp_path / 'large').write_bytes(b'x' * 11)
    with pytest.raises(ValueError, match='limit'):
        connector._check_size(tmp_path)
