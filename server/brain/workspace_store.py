"""SQLite workspace metadata shared with future meeting persistence."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sqlite3
from contextlib import contextmanager

_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")
_DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "learning" / "runs"


def database_path() -> Path:
    explicit = os.getenv("TWIN_DB_PATH")
    if explicit:
        return Path(explicit)
    return Path(os.getenv("TWIN_STATE_DIR", str(_DEFAULT_ROOT))) / "workspaces.sqlite3"


@contextmanager
def _connect():
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=5)
    db.execute("PRAGMA busy_timeout=5000")
    try:
        db.execute("""CREATE TABLE IF NOT EXISTS workspaces (
            workspace TEXT PRIMARY KEY,
            repo_url TEXT,
            integrations_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        with db:
            yield db
    finally:
        db.close()


def _key(workspace: str) -> str:
    if not isinstance(workspace, str) or not _KEY.fullmatch(workspace):
        raise ValueError("invalid workspace key")
    return workspace


def get_workspace(workspace: str) -> dict:
    """Return repository/integration metadata, including defaults for a new key."""
    workspace = _key(workspace)
    with _connect() as db:
        row = db.execute("SELECT repo_url, integrations_json FROM workspaces "
                         "WHERE workspace=?", (workspace,)).fetchone()
    if row is None:
        return {"workspace": workspace, "repo_url": None, "integrations": {}}
    try:
        integrations = json.loads(row[1])
    except (TypeError, ValueError):
        integrations = {}
    return {"workspace": workspace, "repo_url": row[0],
            "integrations": integrations if isinstance(integrations, dict) else {}}


def save_repo(workspace: str, url: str) -> None:
    """Persist a repository only after its clone/index was successfully published."""
    workspace = _key(workspace)
    # Import lazily: the connector uses this store after a successful publish.
    from brain.repo_connector import canonical_url
    url = canonical_url(url)
    with _connect() as db:
        db.execute("""INSERT INTO workspaces(workspace, repo_url) VALUES (?, ?)
            ON CONFLICT(workspace) DO UPDATE SET repo_url=excluded.repo_url,
            updated_at=CURRENT_TIMESTAMP""", (workspace, url))
