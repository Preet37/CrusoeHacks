"""Small sourced fact store. Facts and lineage events are never deleted.

The JSON snapshot keeps current statuses for quick reads and an append-only event
trail so a supersession can always be traced back to its source and reason.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import threading
import tempfile
import fcntl
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4


DEFAULT_PATH = Path(__file__).resolve().parent / "memory.json"
_WORDS = re.compile(r"[a-z0-9_]+")
_STOP = {"a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "is", "it", "of", "on", "or", "the", "to", "we", "with"}
_IO_LOCK = threading.RLock()


def _text(value: Any, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise ValueError(f"{name} must be non-empty text of at most {maximum} characters")
    return value.strip()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _terms(value: str) -> set[str]:
    return {word for word in _WORDS.findall(value.casefold()) if word not in _STOP}


class MemoryStore:
    """Persist current facts plus every creation, supersession, and retirement event."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else DEFAULT_PATH
        self._facts: dict[str, dict[str, Any]] = {}
        self._events: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        self._facts = {}
        self._events = []
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("facts"), list) or not isinstance(data.get("events"), list):
                raise ValueError("invalid memory file")
            for fact in data["facts"]:
                if not isinstance(fact, dict) or not isinstance(fact.get("id"), str) or fact["id"] in self._facts:
                    raise ValueError("invalid or duplicate memory fact id")
                self._facts[fact["id"]] = fact
            self._events = data["events"]

    @contextmanager
    def _transaction(self):
        # The thread lock protects multiple store objects in one process; flock
        # protects workers in separate server processes. The JSON replace below
        # means readers never see a partially written file.
        digest = hashlib.sha256(str(self.path.resolve()).encode()).hexdigest()
        lock_dir = Path(tempfile.gettempdir()) / "twin-memory-locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        with _IO_LOCK, (lock_dir / f"{digest}.lock").open("a+") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                self._load()
                try:
                    yield
                    self._save()
                except BaseException:
                    self._load()
                    raise
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"facts": list(self._facts.values()), "events": self._events}
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as output:
                json.dump(data, output, ensure_ascii=False, indent=2, allow_nan=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def add_fact(self, text: str, source: str, *, scope: str = "general",
                 subject: str | None = None, seen_at: str | None = None) -> dict[str, Any]:
        claim = _text(text, "claim", 2000)
        citation = _text(source, "source", 500)
        scope = _text(scope, "scope", 120)
        if subject is not None:
            subject = _text(subject, "subject", 120)
        stamp = _text(seen_at, "seen_at", 80) if seen_at is not None else _now()
        with self._transaction():
            fact = {"id": f"fact_{uuid4().hex[:16]}", "claim": claim, "source": citation,
                    "status": "current", "seen_at": stamp, "scope": scope,
                    "subject": subject, "superseded_by": None, "reason": None}
            self._facts[fact["id"]] = fact
            self._events.append({"type": "added", "fact_id": fact["id"], "at": stamp})
            return dict(fact)

    def supersede(self, old_id: str, new_fact: str | dict[str, Any], reason: str, *,
                  source: str | None = None, scope: str | None = None,
                  subject: str | None = None) -> dict[str, Any]:
        """Link an existing current fact to a separately sourced replacement.

        `new_fact` may be an already-stored current fact or new claim text. A
        model-supplied ID is never enough: it must resolve to this store.
        """
        reason = _text(reason, "reason", 500)
        with self._transaction():
            old = self._facts.get(old_id)
            if old is None or old["status"] != "current":
                raise ValueError("old fact must be a current stored fact")
            if isinstance(new_fact, dict):
                replacement = self._facts.get(new_fact.get("id"))
                if replacement is None or replacement["status"] != "current":
                    raise ValueError("replacement must be a current stored fact")
                if (new_fact.get("claim") != replacement["claim"]
                        or new_fact.get("source") != replacement["source"]):
                    raise ValueError("replacement does not match stored fact")
            else:
                if source is None:
                    raise ValueError("a new replacement needs a source")
                if scope is not None and scope != old["scope"]:
                    raise ValueError("replacement must be in the same scope")
                if (old["subject"] and subject is not None
                        and old["subject"].casefold() != subject.casefold()):
                    raise ValueError("replacement must concern the same subject")
                replacement = {"id": f"fact_{uuid4().hex[:16]}",
                               "claim": _text(new_fact, "claim", 2000),
                               "source": _text(source, "source", 500),
                               "status": "current", "seen_at": _now(),
                               "scope": old["scope"],
                               "subject": (_text(subject, "subject", 120)
                                           if subject is not None else old["subject"]),
                               "superseded_by": None, "reason": None}
                self._facts[replacement["id"]] = replacement
                self._events.append({"type": "added", "fact_id": replacement["id"],
                                     "at": replacement["seen_at"]})
            if replacement["id"] == old_id:
                raise ValueError("a fact cannot supersede itself")
            if replacement["scope"] != old["scope"]:
                raise ValueError("replacement must be in the same scope")
            if old["subject"] and replacement["subject"] and old["subject"].casefold() != replacement["subject"].casefold():
                raise ValueError("replacement must concern the same subject")
            old["status"] = "superseded"
            old["superseded_by"] = replacement["id"]
            old["reason"] = reason
            self._events.append({"type": "superseded", "fact_id": old_id,
                                 "by": replacement["id"], "reason": reason, "at": _now()})
            return dict(replacement)

    def retire(self, fact_id: str, reason: str) -> dict[str, Any]:
        reason = _text(reason, "reason", 500)
        with self._transaction():
            fact = self._facts.get(fact_id)
            if fact is None or fact["status"] != "current":
                raise ValueError("fact must be current")
            fact["status"] = "retired"
            fact["reason"] = reason
            self._events.append({"type": "retired", "fact_id": fact_id, "reason": reason, "at": _now()})
            return dict(fact)

    def get(self, fact_id: str) -> dict[str, Any] | None:
        with _IO_LOCK:
            self._load()
            fact = self._facts.get(fact_id)
            return dict(fact) if fact is not None else None

    def facts(self, *, scope: str | None = None, current_only: bool = False) -> list[dict[str, Any]]:
        with _IO_LOCK:
            self._load()
            return [dict(fact) for fact in self._facts.values()
                    if (scope is None or fact["scope"] == scope)
                    and (not current_only or fact["status"] == "current")]

    def current_facts(self, *, scope: str | None = None) -> list[dict[str, Any]]:
        return self.facts(scope=scope, current_only=True)

    def events(self) -> list[dict[str, Any]]:
        with _IO_LOCK:
            self._load()
            return [dict(event) for event in self._events]

    def chain(self, fact_id: str) -> list[dict[str, Any]]:
        with _IO_LOCK:
            self._load()
            result: list[dict[str, Any]] = []
            seen: set[str] = set()
            while fact_id and fact_id not in seen:
                seen.add(fact_id)
                fact = self._facts.get(fact_id)
                if fact is None:
                    break
                result.append(dict(fact))
                fact_id = fact.get("superseded_by")
            return result

    def search(self, query: str, *, scope: str | None = None, current_only: bool = True,
               limit: int = 20) -> list[dict[str, Any]]:
        query = _text(query, "query", 2000)
        if not 1 <= limit <= 100:
            raise ValueError("limit must be from 1 to 100")
        terms = _terms(query)
        facts = self.facts(scope=scope, current_only=current_only)
        ranked = sorted(facts, key=lambda fact: (
            -len(terms & (_terms(fact["claim"]) | _terms(fact.get("subject") or ""))),
            -len(terms & _terms(fact.get("subject") or "")),
            fact["seen_at"], fact["id"]), reverse=False)
        return ranked[:limit]

    async def retrieve(self, query: str, *, scope: str | None = None, limit: int = 8,
                       first_pass: Callable | None = None) -> dict[str, Any]:
        from agents.librarian import Librarian
        return await Librarian(self).retrieve(query, scope=scope, limit=limit,
                                              first_pass=first_pass)
