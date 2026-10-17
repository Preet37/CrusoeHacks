"""Persistent first-use workspace invites when no operator allowlist is set."""
import hashlib
import hmac
import os
import re
import sqlite3
from contextlib import contextmanager

from brain.workspace_store import database_path


@contextmanager
def _db():
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=5)
    db.execute('CREATE TABLE IF NOT EXISTS workspace_invites (workspace TEXT PRIMARY KEY, salt BLOB NOT NULL, digest BLOB NOT NULL)')
    try:
        with db:
            yield db
    finally:
        db.close()


def authenticate(workspace: str, code: str) -> bool:
    if not re.fullmatch(r'[a-z0-9][a-z0-9_.-]{0,79}', workspace) or not 1 <= len(code) <= 256:
        return False
    with _db() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT salt,digest FROM workspace_invites WHERE workspace=?', (workspace,)).fetchone()
        salt = row[0] if row else os.urandom(16)
        digest = hashlib.pbkdf2_hmac('sha256', code.encode(), salt, 100_000)
        if row:
            return hmac.compare_digest(row[1], digest)
        db.execute('INSERT INTO workspace_invites VALUES (?,?,?)', (workspace, salt, digest))
        return True


def workspace_names() -> list[str]:
    with _db() as db:
        return [row[0] for row in db.execute('SELECT workspace FROM workspace_invites')]
