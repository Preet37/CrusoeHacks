"""Attach the optional code-aware mind to a meeting without changing wire types."""
from __future__ import annotations

import atexit
import asyncio
import hashlib
import logging
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading

from brain.code_map import CodeMap
from brain.memory import MemoryStore
from brain.repo_connector import connect_repo
from brain.workspace_registry import REGISTRY
from brain.workspace_registry import workspace_key
from brain.workspace_store import get_workspace

log = logging.getLogger(__name__)
DEMO = Path(__file__).resolve().parents[2] / 'demo_project'
STATE_DIR = Path(__file__).resolve().parents[1] / 'learning' / 'runs' / 'memory'
_demo = None
_demo_lock = threading.Lock()


def _restore_saved_repo(session) -> None:
    """Schedule one nonblocking restore for a workspace with a saved URL."""
    key = workspace_key(session)
    if key is None or REGISTRY.current(key) is not None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # A later attach from the WebSocket loop can schedule it.
    try:
        url = get_workspace(key)["repo_url"]
    except Exception:
        log.exception("Could not read saved repository for workspace %s", key)
        return
    if not url or not REGISTRY.claim_restore(key):
        return
    # Reserve the generation before scheduling: a later manual connect must
    # supersede this restore even if the event loop starts the jobs out of order.
    generation = REGISTRY.claim(key)
    task = loop.create_task(connect_repo(session, url, restore_generation=generation),
                            name=f"restore-repo-{key}")

    def finished(done: asyncio.Task) -> None:
        if REGISTRY.current(key) is None:
            REGISTRY.release_failed_restore(key, generation)
        if not done.cancelled() and done.exception() is not None:
            log.warning("Saved repository restore failed for %s", key)

    task.add_done_callback(finished)


def demo_map() -> CodeMap:
    global _demo
    with _demo_lock:
        if _demo is None:
            configured = os.getenv('TWIN_REPO_ROOT')
            if configured:
                _demo = CodeMap(Path(configured)).build()
            else:
                temporary = Path(tempfile.mkdtemp(prefix='twin-demo-'))
                try:
                    # Only this repository's own, fixed demo script is executed.
                    # Connected repositories are AST-parsed and never executed.
                    subprocess.run(['bash', str(DEMO / 'fake_history.sh'), str(temporary)],
                                   check=True, capture_output=True, timeout=15)
                    _demo = CodeMap(temporary).build()
                except BaseException:
                    shutil.rmtree(temporary, ignore_errors=True)
                    raise
                atexit.register(shutil.rmtree, temporary, ignore_errors=True)
        return _demo


def attach(session) -> None:
    """Idempotent hook for mouth.attach; no model call and no network request."""
    session.repo_connector = connect_repo
    if getattr(session, "brief_provider", None) is None:
        from learning.prep import brief_provider
        session.brief_provider = brief_provider
    if getattr(session, '_mind_runtime_ready', False):
        REGISTRY.register(session)
        _restore_saved_repo(session)
        return
    try:
        session.code_map = demo_map()
        room = str(getattr(session, 'room', 'demo'))
        room_key = hashlib.sha256(room.encode()).hexdigest()[:24]
        state_root = Path(os.getenv('TWIN_STATE_DIR', str(STATE_DIR)))
        session.memory = MemoryStore(state_root / f'{room_key}.json')
        session.code_fact_scope = 'demo:code'
        session.decision_scope = f'{room}:decisions'
        # Seed only verified demo implementation facts; meeting decisions live
        # in a separate scope and never imply that code has already changed.
        if not os.getenv('TWIN_REPO_ROOT'):
            seed = [
                ('Current login validates credentials and returns a signed JWT token, not a Redis-backed server session.',
                 'demo_project/auth/login.py:18-24', 'Auth login'),
                ('The database connection uses Postgres.', 'demo_project/db/connection.py:6-10', 'Database'),
            ]
            existing = {(f['claim'], f['source']) for f in session.memory.facts()}
            for claim, source, subject in seed:
                if (claim, source) not in existing:
                    session.memory.add_fact(claim, source, scope=session.code_fact_scope, subject=subject)
        REGISTRY.register(session)
        session._mind_runtime_ready = True
        _restore_saved_repo(session)
    except Exception:
        # Keep the conversation usable if a local code map or state file fails.
        log.exception('Could not initialize code-aware meeting context')
