"""Per-workspace integration credentials (Slack / Jira / GitHub), stored server-side.

Rows live in the workspace SQLite database (brain.workspace_store.database_path, the
Railway volume in production) in table integration_credentials(workspace, provider,
data_json, updated_at). Secrets are only read by the integration clients; every public
view (`public_view`) strips them. Env vars (JIRA_*, SLACK_*, GITHUB_TOKEN) stay the
fallback when a workspace has nothing stored.

The "current workspace" for a call chain is a contextvar (`use_workspace`), so the Jira /
Slack / clone helpers keep their signatures and still resolve the right workspace.
"""
from __future__ import annotations

import contextvars
import json
import os
import sqlite3
import time
from contextlib import contextmanager

PROVIDERS = ("jira", "slack", "github")
SECRET_KEYS = {"api_token", "bot_token", "token", "access_token", "refresh_token", "webhook_url"}

_CURRENT: contextvars.ContextVar[str | None] = contextvars.ContextVar("twin_workspace", default=None)


@contextmanager
def use_workspace(workspace: str | None):
    """Scope integration calls to `workspace` (no-op for None)."""
    if not workspace:
        yield
        return
    token = _CURRENT.set(str(workspace).strip().lower())
    try:
        yield
    finally:
        _CURRENT.reset(token)


def set_workspace(workspace: str | None) -> None:
    """Set the current workspace for the rest of this task (tasks copy the context)."""
    if workspace:
        _CURRENT.set(str(workspace).strip().lower())


def current_workspace() -> str | None:
    return _CURRENT.get()


@contextmanager
def _connect():
    from brain.workspace_store import database_path
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=5)
    db.execute("PRAGMA busy_timeout=5000")
    try:
        db.execute("""CREATE TABLE IF NOT EXISTS integration_credentials (
            workspace TEXT NOT NULL,
            provider TEXT NOT NULL,
            data_json TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (workspace, provider)
        )""")
        with db:
            yield db
    finally:
        db.close()


def _check(workspace: str, provider: str) -> tuple[str, str]:
    from brain.workspace_store import _key
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider {provider!r}")
    return _key(str(workspace).strip().lower()), provider


def get(workspace: str | None, provider: str) -> dict | None:
    """Stored credentials (with secrets) or None. Never raises for a bad/missing workspace."""
    if not workspace:
        return None
    try:
        workspace, provider = _check(workspace, provider)
        with _connect() as db:
            row = db.execute("SELECT data_json FROM integration_credentials WHERE workspace=? AND provider=?",
                             (workspace, provider)).fetchone()
    except (ValueError, sqlite3.Error, OSError):
        return None
    if row is None:
        return None
    try:
        data = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def save(workspace: str, provider: str, data: dict) -> None:
    workspace, provider = _check(workspace, provider)
    payload = {**data, "saved_at": int(time.time())}
    with _connect() as db:
        db.execute("""INSERT INTO integration_credentials(workspace, provider, data_json) VALUES (?, ?, ?)
            ON CONFLICT(workspace, provider) DO UPDATE SET data_json=excluded.data_json,
            updated_at=CURRENT_TIMESTAMP""", (workspace, provider, json.dumps(payload)))


def delete(workspace: str, provider: str) -> bool:
    workspace, provider = _check(workspace, provider)
    with _connect() as db:
        cur = db.execute("DELETE FROM integration_credentials WHERE workspace=? AND provider=?",
                         (workspace, provider))
    return cur.rowcount > 0


def public_view(data: dict | None) -> dict:
    """What GET endpoints may show: never a token, key or webhook."""
    if not data:
        return {"connected": False}
    out = {"connected": True, "via": data.get("via") or "token"}
    for key in ("account", "site", "channel", "project", "team"):
        if data.get(key):
            out[key] = data[key]
    return out


def current(provider: str, workspace: str | None = None) -> dict | None:
    """Stored creds for the given (or current) workspace."""
    return get(workspace or current_workspace(), provider)


def github_token(workspace: str | None = None) -> str | None:
    stored = current("github", workspace)
    token = (stored or {}).get("token") or (stored or {}).get("access_token")
    return token or os.getenv("GITHUB_TOKEN", "").strip() or None
