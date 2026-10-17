"""External integrations (PC-23) plus the ticket lookup/update path they share.

Tickets are PREET-CODEX's drafts: live ones live in `session.ticket_drafts`
(agents/task_planner.py), saved ones inside meeting payloads
(brain/meeting_store.py). We read those stores and write status/links back to
the same place; nothing here fakes a push.
"""
from __future__ import annotations

import json
import logging

log = logging.getLogger("twin.integrations")

ACTIONS = {"approve", "push_jira", "dismiss"}
TICKETS_OFF = "Ticket creation is turned off for this workspace"


class TicketError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _live_sessions(workspace: str | None, room: str | None):
    from session import SESSIONS
    for name, session in list(SESSIONS.items()):
        if room and name != room:
            continue
        if workspace and getattr(session, "workspace", None) != workspace:
            continue
        if not workspace and getattr(session, "workspace", None):
            continue  # workspace rooms need the invite code
        yield session


def _drafts(session) -> list[dict]:
    drafts = getattr(session, "ticket_drafts", None)
    values = drafts.values() if isinstance(drafts, dict) else []
    return [t for t in list(values) if isinstance(t, dict) and t.get("id")]


def list_tickets(workspace: str | None, room: str | None = None) -> list[dict]:
    """Saved tickets for the workspace overlaid by live drafts (live wins), newest first."""
    out: dict[str, dict] = {}
    if workspace:
        from brain.meeting_store import list_meetings
        for meeting in list_meetings(workspace):
            if room and meeting.get("room") != room:
                continue
            for ticket in meeting.get("tickets") or []:
                if isinstance(ticket, dict) and ticket.get("id") and ticket["id"] not in out:
                    out[ticket["id"]] = {**ticket, "meeting_id": meeting["id"],
                                         "ended_at": meeting.get("ended_at"),
                                         "room": meeting.get("room")}
    for session in _live_sessions(workspace, room):
        for ticket in _drafts(session):
            out[ticket["id"]] = {**ticket, "room": session.room,
                                 "meeting_id": getattr(session, "meeting_id", None), "live": True}
    return list(out.values())


def find_ticket(ticket_id: str, workspace: str | None, room: str | None = None):
    """(ticket dict to mutate, live session or None, saved meeting payload or None)."""
    for session in _live_sessions(workspace, room):
        for ticket in _drafts(session):
            if ticket["id"] == ticket_id:
                return ticket, session, None
    if workspace:
        from brain.meeting_store import list_meetings
        for meeting in list_meetings(workspace):
            if room and meeting.get("room") != room:
                continue
            for ticket in meeting.get("tickets") or []:
                if isinstance(ticket, dict) and ticket.get("id") == ticket_id:
                    return ticket, None, meeting
    raise TicketError(f"Unknown ticket {ticket_id}", 404)


def _save_meeting_payload(meeting: dict) -> None:
    from brain.meeting_store import _connect
    by_id = {t.get("id"): t for t in meeting.get("tickets") or [] if isinstance(t, dict)}
    for todo in meeting.get("todos") or []:
        ticket = by_id.get(todo.get("ticket_id"))
        if ticket:
            todo["status"] = ticket.get("status", todo.get("status"))
    with _connect() as db:
        db.execute("UPDATE meetings SET payload_json=? WHERE id=? AND workspace=?",
                   (json.dumps(meeting, ensure_ascii=False), meeting["id"], meeting["workspace"]))
        db.commit()


def _persist(session, meeting) -> None:
    if meeting is not None:
        _save_meeting_payload(meeting)
    elif session is not None and getattr(session, "workspace", None):
        try:
            from brain.meeting_store import save_meeting
            save_meeting(session)
        except Exception:  # noqa: BLE001 - live state already updated
            log.exception("could not persist ticket update")


def ticket_permission(session, workspace: str | None) -> str:
    """'on' | 'ask' | 'off' for Create tickets. Defaults to 'ask' (explicit click = approval)."""
    perms = getattr(session, "permissions", None) if session is not None else None
    if not isinstance(perms, dict) and workspace:
        try:
            from brain.workspace_store import get_workspace
            perms = (get_workspace(workspace).get("integrations") or {}).get("permissions")
        except Exception:  # noqa: BLE001
            perms = None
    value = perms.get("create_tickets") if isinstance(perms, dict) else None
    return value if value in ("on", "ask", "off") else "ask"


def wire(ticket: dict) -> dict:
    """A protocol `ticket` message for broadcast."""
    keep = ("id", "title", "body", "owner", "topic", "source", "status", "links",
            "acceptance", "agent_prompt")
    return {"type": "ticket", **{k: ticket.get(k) for k in keep if k in ticket}}


async def apply_action(ticket_id: str, action: str, *, workspace: str | None,
                       room: str | None = None) -> tuple[dict, object | None]:
    """Run one ticket action; returns (updated ticket, live session or None)."""
    from integrations import jira
    if action not in ACTIONS:
        raise TicketError(f"Unsupported ticket action {action!r}")
    ticket, session, meeting = find_ticket(ticket_id, workspace, room)
    if action != "dismiss" and ticket_permission(session, workspace) == "off":
        raise TicketError(TICKETS_OFF, 403)
    if action == "dismiss":
        ticket["status"] = "dismissed"
    elif action == "approve":
        if ticket.get("status") in (None, "draft"):
            ticket["status"] = "approved"
    else:
        links = ticket.get("links") if isinstance(ticket.get("links"), dict) else {}
        if links.get("jira"):
            return ticket, session  # already pushed; never create a duplicate
        if not jira.configured():
            raise TicketError("connect Jira", 409)
        name = (meeting or {}).get("title") or (getattr(session, "room", None)
                                                 or (meeting or {}).get("room") or "session")
        body = str(ticket.get("body") or "")
        if ticket.get("acceptance"):
            body += f"\nAcceptance: {ticket['acceptance']}"
        try:
            issue = await jira.create_issue(str(ticket.get("title") or ""), body,
                                            owner=ticket.get("owner"), meeting=name)
        except jira.JiraError as e:
            raise TicketError(str(e), 502) from None
        ticket["links"] = {"jira": None, "slack": None, "agent": None, **links,
                           "jira": issue["url"]}
        ticket["jira_key"] = issue["key"]
        ticket["status"] = "sent"
    _persist(session, meeting)
    return ticket, session
