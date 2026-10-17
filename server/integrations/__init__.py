"""External integrations (PC-23) plus the ticket lookup/update path they share.

Tickets are PREET-CODEX's drafts: live ones live in `session.ticket_drafts`
(agents/task_planner.py), saved ones inside meeting payloads
(brain/meeting_store.py). We read those stores and write status/links back to
the same place; nothing here fakes a push.
"""
from __future__ import annotations

import json
import logging
import re

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


# ------------------------------------------------------------ Jira description

_WORD = re.compile(r"[a-z0-9]+")
_PATH_LINES = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]+\.[A-Za-z0-9]+):(\d+)(?:-(\d+))?")
_SOURCE_TS = re.compile(r"@\s*(\d+)\s*$")
_FILES_LINE = re.compile(r"^Relevant indexed files:\s*(.+)$", re.M)
_STOPWORDS = {"the", "and", "for", "that", "this", "with", "will", "should", "we", "to",
              "a", "an", "of", "in", "on", "is", "it", "be", "let", "lets", "decision"}


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(str(text or "").casefold())
            if len(w) > 2 and w not in _STOPWORDS}


def _decision_text(ticket: dict) -> str:
    body = str(ticket.get("body") or "")
    first = body.splitlines()[0] if body else ""
    return first.removeprefix("Decision: ") if first.startswith("Decision: ") else \
        str(ticket.get("title") or "")


def _context_lines(ticket: dict, lines: list[dict]) -> tuple[list[dict], str | None]:
    """3-6 transcript lines around the decision, plus the human who said it."""
    lines = [l for l in lines if isinstance(l, dict) and str(l.get("text") or "").strip()]
    if not lines:
        return [], None
    idx = None
    match = _SOURCE_TS.search(str(ticket.get("source") or ""))
    if match:
        ts = int(match.group(1))
        idx = next((i for i in range(len(lines) - 1, -1, -1)
                    if str(lines[i].get("ts")) == str(ts)), None)
    if idx is None:
        target = _words(_decision_text(ticket)) | _words(ticket.get("title"))
        best = 0
        for i, line in enumerate(lines):
            score = len(target & _words(line.get("text")))
            if score > best or (score == best and score and i > (idx or 0)):
                best, idx = score, i
        if not best:
            return [], None
    start = max(0, min(idx - 3, len(lines) - 3))
    window = lines[start:min(len(lines), max(idx + 3, start + 3))]
    speaker = str(lines[idx].get("speaker") or "").strip()
    if speaker.casefold() in ("twin", "participant", ""):
        speaker = None
    return window, speaker


def _github_base(repo_url: str | None) -> str | None:
    url = str(repo_url or "").strip().rstrip("/")
    if not re.match(r"^https?://(www\.)?github\.com/[\w.-]+/[\w.-]+$", url):
        return None
    return url.removesuffix(".git")


def _file_refs(ticket: dict, contribution: dict | None) -> list[tuple[str, int | None, int | None]]:
    refs: list[tuple[str, int | None, int | None]] = []
    texts = [str(s) for s in (contribution or {}).get("sources") or [] if s]
    texts.append(str(ticket.get("body") or ""))
    for text in texts:
        for path, a, b in _PATH_LINES.findall(text):
            refs.append((path, int(a), int(b or a)))
    files = _FILES_LINE.search(str(ticket.get("body") or ""))
    if files:
        for path in files.group(1).split(","):
            path = path.strip()
            if path and not any(r[0] == path for r in refs):
                refs.append((path, None, None))
    return list(dict.fromkeys(refs))[:12]


def _mermaid_codes(ticket: dict, session) -> list[str]:
    board = getattr(session, "board", None) if session is not None else None
    if board is None:
        return []
    try:
        snap = board.snapshot()
    except Exception:  # noqa: BLE001 - description is best effort
        return []
    shapes = [s for s in snap.get("shapes") or [] if isinstance(s, dict)
              and s.get("op") == "add_mermaid" and isinstance(s.get("code"), str)]
    frame = ticket.get("topic") or snap.get("active_frame")
    scoped = [s for s in shapes if frame and s.get("frame") == frame]
    return [s["code"] for s in (scoped or ([] if frame else shapes))][:3]


def _contribution(ticket: dict, session, meeting: dict | None = None) -> dict | None:
    contrib = getattr(session, "latest_contribution", None) if session is not None else None
    if not isinstance(contrib, dict):
        contrib = (meeting or {}).get("contribution")
    if not isinstance(contrib, dict):
        return None
    topic, ctopic = ticket.get("topic"), contrib.get("topic_id")
    return contrib if not topic or not ctopic or topic == ctopic else None


def _repo_url(session, meeting, workspace) -> str | None:
    url = getattr(session, "repo_url", None) if session is not None else None
    if url:
        return url
    key = getattr(session, "workspace", None) or (meeting or {}).get("workspace") or workspace
    if not key:
        return None
    try:
        from brain.workspace_store import get_workspace
        return get_workspace(key).get("repo_url")
    except Exception:  # noqa: BLE001
        return None


def build_jira_description(ticket: dict, session=None, meeting: dict | None = None,
                           workspace: str | None = None) -> tuple[dict, str | None]:
    """(ADF doc without footer, decision speaker) from everything Twin knows about the ticket."""
    from integrations import jira
    content: list[dict] = []
    decision = _decision_text(ticket)
    if decision:
        content += [jira.heading("Decision"), jira.paragraph(jira._text(decision))]
    history = getattr(session, "history", None) if session is not None else None
    if not isinstance(history, list):
        history = (meeting or {}).get("notes") or ticket.get("context") or []
    window, speaker = _context_lines(ticket, list(history))
    if window:
        content.append(jira.heading("Context"))
        content.append(jira.bullet_list([[jira._text(
            f"{str(l.get('speaker') or 'Participant')}: {str(l.get('text')).strip()}"[:600])]
            for l in window]))
    contrib = _contribution(ticket, session, meeting)
    if contrib:
        items = []
        if contrib.get("current"):
            items.append([jira._text(f"Current: {contrib['current']}")])
        for label, key in (("Impact", "impact"), ("Risk", "risks")):
            for value in contrib.get(key) or []:
                items.append([jira._text(f"{label}: {value}")])
        if contrib.get("question"):
            items.append([jira._text(f"Question: {contrib['question']}")])
        if items:
            content += [jira.heading("Twin's take"), jira.bullet_list(items)]
    refs = _file_refs(ticket, contrib)
    if refs:
        base = _github_base(_repo_url(session, meeting, workspace))
        items = []
        for path, a, b in refs:
            label = f"{path}:{a}-{b}" if a else path
            link = None
            if base:
                link = f"{base}/blob/HEAD/{path.lstrip('/')}" + (f"#L{a}-L{b}" if a else "")
            items.append([jira._text(label, link)])
        content += [jira.heading("Relevant files"), jira.bullet_list(items)]
    codes = _mermaid_codes(ticket, session)
    if not codes:
        from integrations import agent_brief
        snap = agent_brief.board_snapshot(session, meeting)
        frame = ticket.get("topic") or (snap or {}).get("active_frame")
        codes = [code for _, code in agent_brief.diagrams(snap, frame)]
    if codes:
        content.append(jira.heading("Diagram (Mermaid)"))
        content += [jira.code_block(code, "mermaid") for code in codes]
    if ticket.get("agent_prompt"):
        content += [jira.heading("Agent prompt"), jira.code_block(str(ticket["agent_prompt"]))]
    acceptance = ticket.get("acceptance")
    checks = acceptance if isinstance(acceptance, list) else \
        [p for p in re.split(r"\n+|(?<=[.!?])\s+(?=[A-Z])", str(acceptance or "")) if p.strip()]
    if checks:
        content += [jira.heading("Acceptance"),
                    jira.bullet_list([[jira._text(str(c).strip())] for c in checks])]
    source = ticket.get("source")
    if source:
        content.append(jira.paragraph(jira._text(f"Source: {source}")))
    content.append(jira.paragraph(jira._text(
        "Full agent brief (Markdown: goal, discussion, analysis, diagrams, files, acceptance, instructions) "
        "is attached to this issue.")))
    if not content:
        content.append(jira.paragraph(jira._text(str(ticket.get("body") or ticket.get("title") or ""))))
    return jira.doc(content), speaker


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
            description, speaker = build_jira_description(ticket, session, meeting, workspace)
        except Exception:  # noqa: BLE001 - fall back to the plain body
            log.exception("rich Jira description failed; using plain body")
            description, speaker = None, None
        owner = ticket.get("owner")
        fallback = speaker if speaker and speaker != owner else None
        try:
            issue = await jira.create_issue(str(ticket.get("title") or ""), body,
                                            owner=owner, meeting=name,
                                            description=description,
                                            fallback_assignee=fallback)
        except jira.JiraError as e:
            raise TicketError(str(e), 502) from None
        ticket["links"] = {"jira": None, "slack": None, "agent": None, **links,
                           "jira": issue["url"]}
        ticket["jira_key"] = issue["key"]
        try:
            ticket["attachments"] = await attach_agent_brief(ticket, session, meeting, workspace, issue)
        except Exception:  # noqa: BLE001 - the issue exists; a missing attachment must not fail the push
            log.exception("attaching agent brief to %s failed", issue.get("key"))
        try:  # who it went to, for the dashboard Tickets board
            info = await jira.get_issue(issue["key"])
            ticket["jira"] = {k: info.get(k) for k in ("assignee", "reporter", "status", "url")}
        except Exception:  # noqa: BLE001 - best effort
            log.info("could not read back %s", issue.get("key"))
        ticket["status"] = "sent"
    _persist(session, meeting)
    if action == "push_jira" and ticket.get("jira_key"):
        _maybe_start_agent(ticket, session, meeting, workspace, room)
    return ticket, session


def _maybe_start_agent(ticket: dict, session, meeting: dict | None, workspace: str | None, room: str | None) -> None:
    """Spawn the Ticket Agent once the issue is in Jira (TICKET_AGENT=auto, the default)."""
    from integrations import ticket_agent
    if not ticket_agent.auto_enabled() or (ticket.get("agent") or {}).get("status") in ("running", "queued", "done"):
        return
    scope_room = room or getattr(session, "room", None) or (meeting or {}).get("room")
    scope_ws = workspace or getattr(session, "workspace", None) or (meeting or {}).get("workspace")
    try:
        ticket_agent.start(str(ticket["id"]), scope_ws, scope_room)
    except RuntimeError:  # no running event loop (sync callers/tests)
        log.info("ticket agent not started for %s (no event loop)", ticket.get("id"))


async def auto_push_meeting(meeting: dict | None) -> list[str]:
    """Meeting just ended: push every open draft ticket to Jira (assigned, brief attached) without a click.

    Skips dismissed/already-pushed tickets and workspaces whose 'Create tickets' permission is off."""
    from integrations import jira
    if not isinstance(meeting, dict) or not jira.configured():
        return []
    workspace, room = meeting.get("workspace"), meeting.get("room")
    if ticket_permission(None, workspace) == "off":
        log.info("[%s] auto Jira skipped: Create tickets is off", room)
        return []
    pushed = []
    for t in list(meeting.get("tickets") or []):
        if not isinstance(t, dict) or not t.get("id") or t.get("status") == "dismissed":
            continue
        if (t.get("links") or {}).get("jira"):
            continue
        try:
            ticket, _ = await apply_action(t["id"], "push_jira", workspace=workspace, room=room)
            pushed.append(str(ticket.get("jira_key")))
        except TicketError as e:
            log.warning("[%s] auto Jira failed for %s: %s", room, t.get("id"), e)
    if pushed:
        log.info("[%s] meeting ended: auto-created Jira %s", room, ", ".join(pushed))
    return pushed


def agent_brief_markdown(ticket: dict, session=None, meeting: dict | None = None,
                         workspace: str | None = None, issue: dict | None = None) -> str:
    """Markdown brief for a coding agent (see integrations/agent_brief.py)."""
    from integrations import agent_brief
    history = getattr(session, "history", None) if session is not None else None
    if not isinstance(history, list):
        history = (meeting or {}).get("notes") or ticket.get("context") or []
    window, _speaker = _context_lines(ticket, list(history))
    contrib = _contribution(ticket, session, meeting)
    return agent_brief.build_brief(
        ticket, session=session, meeting=meeting, repo_url=_repo_url(session, meeting, workspace),
        contribution=contrib, context=window, files=_file_refs(ticket, contrib),
        jira_key=(issue or {}).get("key"), jira_url=(issue or {}).get("url"))


async def attach_agent_brief(ticket: dict, session, meeting: dict | None, workspace: str | None,
                             issue: dict) -> list[str]:
    """Attach TWIN-<KEY>-brief.md (+ one .mmd per diagram) to the Jira issue."""
    from integrations import agent_brief, jira
    key = str(issue.get("key") or ticket.get("id") or "ticket")
    files = [(f"{key}-agent-brief.md",
              agent_brief_markdown(ticket, session, meeting, workspace, issue).encode("utf-8"),
              "text/markdown")]
    snap = agent_brief.board_snapshot(session, meeting)
    frame = ticket.get("topic") or (snap or {}).get("active_frame")
    for i, (_name, code) in enumerate(agent_brief.diagrams(snap, frame), start=1):
        files.append((f"{key}-diagram-{i}.mmd", code.encode("utf-8"), "text/plain"))
    return await jira.attach_files(key, files)
