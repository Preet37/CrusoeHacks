"""Persist extractive meeting notes and draft tickets per trusted workspace."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4

from brain.workspace_registry import workspace_key
from brain.workspace_store import database_path


@contextmanager
def _connect():
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=5)
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("""CREATE TABLE IF NOT EXISTS meetings (
        id TEXT PRIMARY KEY, workspace TEXT NOT NULL, ended_at TEXT NOT NULL,
        payload_json TEXT NOT NULL
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS meetings_workspace_ended ON meetings(workspace, ended_at DESC)")
    try:
        with db:
            yield db
    finally:
        db.close()


def _lines(session) -> list[dict]:
    lines = []
    for line in getattr(session, "history", [])[-500:]:
        if not isinstance(line, dict) or not isinstance(line.get("text"), str):
            continue
        text = line["text"].strip()
        if text:
            lines.append({"speaker": str(line.get("speaker") or "Participant")[:100],
                          "text": text[:1000], "ts": line.get("ts")})
    return lines


def _tickets(session) -> list[dict]:
    drafts = getattr(session, "ticket_drafts", None)
    values = drafts.values() if isinstance(drafts, dict) else []
    tickets = []
    for item in list(values)[-64:]:
        if isinstance(item, dict) and isinstance(item.get("title"), str) and item["title"].strip():
            tickets.append(json.loads(json.dumps(item, default=str)))
    return tickets


def save_meeting(session) -> dict | None:
    """Upsert the current non-lobby meeting; return None for unauthenticated/empty rooms."""
    workspace = workspace_key(session)
    room = str(getattr(session, "room", ""))
    if workspace is None or room.casefold().endswith("-lobby"):
        return None
    lines, tickets = _lines(session), _tickets(session)
    if not lines and not tickets:
        return None
    meeting_id = getattr(session, "meeting_id", None)
    if not isinstance(meeting_id, str) or not meeting_id:
        meeting_id = session.meeting_id = f"m_{uuid4().hex}"
    ended_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    topic = getattr(session, "active_topic", None)
    title = str(topic.get("title") or "")[:160] if isinstance(topic, dict) else ""
    if not title:
        title = lines[0]["text"][:100] if lines else tickets[0]["title"][:100]
    summary = " ".join(line["text"] for line in lines[:3])[:600]
    if not summary:
        summary = "; ".join(ticket["title"] for ticket in tickets[:3])[:600]
    decisions = []
    for ticket in tickets:
        body = str(ticket.get("body") or "")
        if body.startswith("Decision: "):
            decisions.append(body.splitlines()[0].removeprefix("Decision: ")[:300])
    for line in lines:
        if line["speaker"].casefold() == "twin":
            continue
        if any(phrase in line["text"].casefold() for phrase in
               ("we decided", "we agreed", "let's", "we will")):
            if line["text"] not in decisions:
                decisions.append(line["text"][:300])
    todos = [{"title": ticket["title"], "owner": ticket.get("owner"),
              "status": ticket.get("status", "draft"), "ticket_id": ticket.get("id")}
             for ticket in tickets]
    payload = {"id": meeting_id, "workspace": workspace, "room": room,
               "title": title, "ended_at": ended_at, "summary": summary,
               "notes": lines[:100], "decisions": decisions[:30],
               "todos": todos, "tickets": tickets}
    # Board + Twin's take, so tickets pushed after the meeting still carry the diagram and analysis.
    board = getattr(session, "board", None)
    try:
        snap = board.snapshot() if board is not None else None
    except Exception:  # noqa: BLE001 - best effort
        snap = None
    if isinstance(snap, dict) and (snap.get("boxes") or snap.get("shapes")):
        payload["board"] = snap
    contribution = getattr(session, "latest_contribution", None)
    if isinstance(contribution, dict):
        payload["contribution"] = contribution
    with _connect() as db:
        result = db.execute("""INSERT INTO meetings(id, workspace, ended_at, payload_json) VALUES(?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET ended_at=excluded.ended_at,
            payload_json=excluded.payload_json WHERE meetings.workspace=excluded.workspace""",
                   (meeting_id, workspace, ended_at, json.dumps(payload, ensure_ascii=False)))
        if result.rowcount != 1:
            raise ValueError("meeting id belongs to another workspace")
        db.commit()
    return payload


def list_meetings(workspace: str) -> list[dict]:
    """Newest saved meetings in this authenticated workspace."""
    from brain.workspace_store import _key
    workspace = _key(workspace)
    with _connect() as db:
        rows = db.execute("SELECT payload_json FROM meetings WHERE workspace=? "
                          "ORDER BY ended_at DESC, id DESC LIMIT 100", (workspace,)).fetchall()
    return [json.loads(row[0]) for row in rows]


def list_tickets(workspace: str) -> list[dict]:
    """Draft tickets with their meeting identity; owner and links stay unchanged."""
    return [{**ticket, "meeting_id": meeting["id"], "ended_at": meeting["ended_at"]}
            for meeting in list_meetings(workspace)
            for ticket in meeting["tickets"]]
