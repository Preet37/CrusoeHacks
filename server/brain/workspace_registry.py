"""Process-local repository maps keyed only by trusted Session.workspace.

The WebSocket layer must authenticate an invite code and set ``workspace``
before attaching the mind. Room names are never parsed for identity here.
"""

from __future__ import annotations

import re
import shutil
import threading
import weakref
from dataclasses import dataclass
from pathlib import Path

_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")


def workspace_key(session) -> str | None:
    value = getattr(session, "workspace", None)
    if value is None:
        return None
    if not isinstance(value, str) or not _KEY.fullmatch(value):
        raise ValueError("workspace must be a trusted, validated key")
    return value


@dataclass(frozen=True)
class Repository:
    code_map: object
    url: str
    code_fact_scope: str


class WorkspaceRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._generation: dict[str, object] = {}
        self._current: dict[str, Repository] = {}
        self._sessions: dict[str, weakref.WeakSet] = {}

    def register(self, session) -> Repository | None:
        key = workspace_key(session)
        if key is None:
            return None
        with self._lock:
            self._sessions.setdefault(key, weakref.WeakSet()).add(session)
            current = self._current.get(key)
            if current is not None:
                self._apply(session, current)
            return current

    def claim(self, key: str) -> object:
        if not isinstance(key, str) or not _KEY.fullmatch(key):
            raise ValueError("invalid workspace key")
        with self._lock:
            generation = object()
            self._generation[key] = generation
            return generation

    def publish(self, key: str, generation: object, code_map, url: str,
                code_fact_scope: str, directory: Path) -> bool:
        """Atomically install a current map; stale clones remain unpublished."""
        with self._lock:
            if self._generation.get(key) is not generation:
                return False
            # CodeMap owns its checkout lifetime. Older maps stay readable
            # while an in-flight analyst holds a reference after replacement.
            weakref.finalize(code_map, shutil.rmtree, directory, ignore_errors=True)
            current = Repository(code_map, url, code_fact_scope)
            self._current[key] = current
            for session in tuple(self._sessions.get(key, ())):
                self._apply(session, current)
            return True

    def current(self, key: str) -> Repository | None:
        with self._lock:
            return self._current.get(key)

    @staticmethod
    def _apply(session, current: Repository) -> None:
        session.code_map = current.code_map
        session.repo_url = current.url
        session.code_fact_scope = current.code_fact_scope


REGISTRY = WorkspaceRegistry()
