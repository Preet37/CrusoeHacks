"""End-of-meeting Slack recap to #standup (Block Kit).

Env (server/.env), either:
  SLACK_WEBHOOK_URL                 Incoming Webhook bound to #standup, or
  SLACK_BOT_TOKEN + SLACK_CHANNEL   chat.postMessage (channel default "standup").
PUBLIC_WEB_URL  dashboard link in the footer (default https://crusoehacks.vercel.app).

When the room empties and the meeting is saved, app.py calls `schedule_meeting_recap`.
A background task waits (up to WAIT_S, every POLL_S) for the auto Jira push to fill
ticket links, then posts ONE message per meeting id. The webhook URL and token are
only handed to httpx; they never appear in logs, errors or return values.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime

import httpx

import config  # noqa: F401 - loads server/.env

log = logging.getLogger("twin.slack")

WAIT_S = 45.0
POLL_S = 3.0
SUMMARY_TIMEOUT_S = 10.0
HTTP_TIMEOUT_S = 10.0
DEFAULT_WEB_URL = "https://crusoehacks.vercel.app"
CHAT_POST_URL = "https://slack.com/api/chat.postMessage"
AGENT_RUNNING = {"running", "started", "starting", "queued", "in_progress", "in progress", "working"}

# Tests swap in an httpx.MockTransport; production uses the default transport.
_TRANSPORT: httpx.AsyncBaseTransport | None = None
_POSTED: set[str] = set()       # meeting ids already posted (or being posted)
_TASKS: set[asyncio.Task] = set()
_RECENT: dict[str, dict] = {}   # meeting id -> payload (for manual repost of non-workspace rooms)


# ------------------------------------------------------------------ config

def _webhook() -> str:
    return os.getenv("SLACK_WEBHOOK_URL", "").strip()


def _bot() -> tuple[str, str]:
    channel = os.getenv("SLACK_CHANNEL", "").strip() or "standup"
    return os.getenv("SLACK_BOT_TOKEN", "").strip(), channel


def configured() -> bool:
    return bool(_webhook() or _bot()[0])


def _offline_test() -> bool:
    """Under pytest, never reach real Slack unless a test installed a mock transport."""
    return "pytest" in sys.modules and _TRANSPORT is None


def web_url() -> str:
    return (os.getenv("PUBLIC_WEB_URL", "").strip() or DEFAULT_WEB_URL).rstrip("/")


# ------------------------------------------------------------------ helpers

def _esc(text) -> str:
    return str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _jira_url(ticket: dict) -> str | None:
    links = ticket.get("links") if isinstance(ticket.get("links"), dict) else {}
    return links.get("jira") or ticket.get("jira_url") or None


def _agent_status(ticket: dict) -> str:
    agent = ticket.get("agent")
    value = ticket.get("agent_status")
    if not value and isinstance(agent, dict):
        value = agent.get("status")
    if not value and isinstance(agent, str):
        value = agent
    return str(value or "").strip().casefold()


def _status(ticket: dict) -> str:
    return str(ticket.get("jira_status") or ticket.get("status") or "draft")


def _active(tickets: list[dict]) -> list[dict]:
    return [t for t in tickets if isinstance(t, dict) and t.get("status") != "dismissed"]


def _when(meeting: dict) -> str:
    raw = meeting.get("ended_at")
    try:
        dt = datetime.fromisoformat(str(raw)) if raw else datetime.now().astimezone()
    except ValueError:
        return str(raw)
    tz = dt.tzname() or ""
    return dt.strftime("%b %d, %Y %H:%M") + (f" {tz}" if tz else "")


def _ticket_label(ticket: dict, pending_note: bool) -> str:
    title = _esc(ticket.get("title") or "Untitled ticket")
    url, key = _jira_url(ticket), ticket.get("jira_key")
    if url:
        return f"<{url}|{_esc(key or 'Jira')} {title}>"
    return title + (" _(Jira link pending)_" if pending_note else "")


def _jira_expected() -> bool:
    try:
        from integrations import jira
        return jira.configured()
    except Exception:  # noqa: BLE001
        return False


def _fallback_summary(meeting: dict) -> str:
    notes = [n for n in meeting.get("notes") or [] if isinstance(n, dict) and n.get("text")]
    if notes:
        return " ".join(f"{n.get('speaker') or 'Participant'}: {str(n['text']).strip()}"
                        for n in notes[:3])[:600]
    return str(meeting.get("summary") or "").strip()[:600] or "No discussion was captured."


async def summarize(meeting: dict) -> str:
    """2-4 sentence summary. Uses an LLM summary when the meeting only has raw notes."""
    for key in ("ai_summary", "llm_summary", "recap"):
        if str(meeting.get(key) or "").strip():
            return str(meeting[key]).strip()[:900]
    notes = [n for n in meeting.get("notes") or [] if isinstance(n, dict) and n.get("text")]
    if not notes:
        return _fallback_summary(meeting)
    transcript = "\n".join(f"{n.get('speaker') or 'Participant'}: {str(n['text']).strip()}"
                           for n in notes[:80])[:8000]
    try:
        import llm
        from config import MODEL_FAST
        text = await asyncio.wait_for(llm.chat(MODEL_FAST, [
            {"role": "system", "content": "You write crisp standup recaps. Reply with exactly 3 plain "
                                          "sentences summarizing what the meeting discussed and decided. "
                                          "No preamble, no bullets, no markdown."},
            {"role": "user", "content": f"Meeting: {meeting.get('title') or meeting.get('room')}\n"
                                        f"Transcript:\n{transcript}"},
        ], max_tokens=220, temperature=0.2), timeout=SUMMARY_TIMEOUT_S)
        text = " ".join(str(text or "").split())
        if text:
            return text[:900]
    except Exception:  # noqa: BLE001 - timeout / no key / model error -> extractive fallback
        log.info("slack recap: LLM summary unavailable, using first lines")
    return _fallback_summary(meeting)


# ------------------------------------------------------------------ message

def build_blocks(meeting: dict, summary: str, *, pending_note: bool = True) -> tuple[list[dict], str]:
    """(Block Kit blocks, plain-text fallback) for one meeting."""
    name = meeting.get("title") or meeting.get("room") or "meeting"
    header = f"Twin recap: {name} · {_when(meeting)}"
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": header[:150], "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn", "text": _esc(summary)[:2900]}},
    ]
    decisions = [str(d).strip() for d in meeting.get("decisions") or [] if str(d).strip()]
    if decisions:
        text = "*Decisions*\n" + "\n".join(f"• {_esc(d)[:300]}" for d in decisions[:10])
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}})

    tickets = _active(meeting.get("tickets") or [])
    if tickets:
        owners: dict[str, list[dict]] = {}
        for t in tickets:
            owners.setdefault(str(t.get("owner") or "").strip() or "Unassigned", []).append(t)
        lines = ["*Who owns what*"]
        for owner, items in owners.items():
            lines.append(f"*{_esc(owner)}*")
            lines += [f"• {_ticket_label(t, pending_note)} — {_esc(_status(t))}" for t in items[:10]]
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)[:2900]}})

        tlines = ["*Tickets*"]
        for t in tickets[:20]:
            tlines.append(f"• {_ticket_label(t, pending_note)}")
        for t in tickets:
            if _agent_status(t) in AGENT_RUNNING:
                tag = _esc(t.get("jira_key") or t.get("title") or t.get("id"))
                url = _jira_url(t)
                ref = f"<{url}|{tag}>" if url else tag
                tlines.append(f":robot_face: Sync in progress by Twin's AI agent on {ref}")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(tlines)[:2900]}})

    blocks.append({"type": "divider"})
    blocks.append({"type": "context", "elements": [
        {"type": "mrkdwn", "text": f"<{web_url()}|Open the Twin dashboard> · posted by Twin"}]})
    return blocks, header


# ------------------------------------------------------------------ posting

async def post_blocks(blocks: list[dict], text: str) -> dict:
    """Post one message. Returns {"ok": bool, "error"?: str}; never leaks credentials."""
    webhook = _webhook()
    token, channel = _bot()
    if not webhook and not token:
        return {"ok": False, "error": "Slack is not connected"}
    if _offline_test():
        return {"ok": False, "error": "Slack disabled in tests"}
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S, transport=_TRANSPORT) as client:
            if webhook:
                resp = await client.post(webhook, json={"text": text, "blocks": blocks})
                if resp.status_code >= 400:
                    return {"ok": False, "error": f"Slack webhook HTTP {resp.status_code}"}
                return {"ok": True}
            resp = await client.post(CHAT_POST_URL, json={"channel": channel, "text": text,
                                                         "blocks": blocks},
                                     headers={"Authorization": f"Bearer {token}"})
            try:
                data = resp.json()
            except ValueError:
                data = {}
            if resp.status_code >= 400 or not data.get("ok"):
                return {"ok": False, "error": f"Slack error: {str(data.get('error') or resp.status_code)[:80]}"}
            return {"ok": True}
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"Slack request failed ({type(e).__name__})"}


def _merge_fresh(meeting: dict, session) -> dict:
    """Overlay the latest ticket state (live drafts + stored payload); a Jira link wins."""
    by_id: dict[str, dict] = {}
    for t in meeting.get("tickets") or []:
        if isinstance(t, dict) and t.get("id"):
            by_id[t["id"]] = t
    sources: list[list] = []
    drafts = getattr(session, "ticket_drafts", None) if session is not None else None
    if isinstance(drafts, dict):
        sources.append(list(drafts.values()))
    if meeting.get("workspace") and meeting.get("id"):
        try:
            from brain.meeting_store import list_meetings
            stored = next((m for m in list_meetings(meeting["workspace"]) if m.get("id") == meeting["id"]), None)
            if stored:
                sources.append(stored.get("tickets") or [])
                for key in ("summary", "decisions", "title", "ai_summary"):
                    if stored.get(key) and not meeting.get(key):
                        meeting[key] = stored[key]
        except Exception:  # noqa: BLE001
            log.exception("slack recap: reading stored meeting failed")
    for source in sources:
        for t in source:
            if not isinstance(t, dict) or not t.get("id"):
                continue
            old = by_id.get(t["id"])
            if old is None or (_jira_url(t) and not _jira_url(old)) or \
                    (_jira_url(t) and _agent_status(t) and not _agent_status(old)):
                by_id[t["id"]] = dict(t)
    return {**meeting, "tickets": list(by_id.values())}


def _links_ready(meeting: dict) -> bool:
    return all(_jira_url(t) for t in _active(meeting.get("tickets") or []))


async def post_meeting_recap(meeting: dict, session=None, *, wait: bool = True,
                             force: bool = False) -> dict:
    """Wait for Jira links (if Jira is connected), then post the recap once per meeting id."""
    if not configured():
        return {"ok": False, "error": "Slack is not connected"}
    mid = str(meeting.get("id") or "")
    if not force and mid in _POSTED:
        return {"ok": False, "error": "Already posted"}
    if mid:
        _POSTED.add(mid)
    try:
        meeting = _merge_fresh(meeting, session)
        jira_on = _jira_expected()
        if wait and jira_on:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + WAIT_S
            while not _links_ready(meeting) and loop.time() < deadline:
                await asyncio.sleep(POLL_S)
                meeting = _merge_fresh(meeting, session)
        summary = await summarize(meeting)
        blocks, text = build_blocks(meeting, summary, pending_note=jira_on)
        result = await post_blocks(blocks, text)
    except Exception:  # noqa: BLE001
        log.exception("slack recap failed for %s", mid or "meeting")
        result = {"ok": False, "error": "Slack recap failed"}
    if not result.get("ok") and mid and not force:
        _POSTED.discard(mid)  # allow a manual retry
    log.info("slack recap for %s: %s", mid or "meeting", "posted" if result.get("ok") else result.get("error"))
    return result


def _session_payload(session) -> dict | None:
    """Minimal payload for rooms that are not saved (non-workspace demo rooms)."""
    room = str(getattr(session, "room", "") or "")
    if not room or room.casefold().endswith("-lobby"):
        return None
    from brain.meeting_store import _lines, _tickets
    lines, tickets = _lines(session), _tickets(session)
    if not lines and not tickets:
        return None
    mid = getattr(session, "meeting_id", None) or f"room:{room}:{id(session)}"
    decisions = [str(t.get("body")).splitlines()[0].removeprefix("Decision: ")
                 for t in tickets if str(t.get("body") or "").startswith("Decision: ")]
    topic = getattr(session, "active_topic", None)
    title = str(topic.get("title") or "") if isinstance(topic, dict) else ""
    return {"id": mid, "workspace": None, "room": room, "title": title or room,
            "ended_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "summary": " ".join(l["text"] for l in lines[:3])[:600],
            "notes": lines[:100], "decisions": decisions[:30], "tickets": tickets}


def schedule_meeting_recap(session, payload: dict | None) -> asyncio.Task | None:
    """Fire-and-forget from the ws `finally`; never raises."""
    try:
        if not configured() or _offline_test():
            return None
        meeting = payload if isinstance(payload, dict) else _session_payload(session)
        if not meeting:
            return None
        mid = str(meeting.get("id") or "")
        if mid:
            _RECENT[mid] = meeting
            while len(_RECENT) > 50:
                _RECENT.pop(next(iter(_RECENT)))
            if mid in _POSTED:
                return None
        task = asyncio.get_running_loop().create_task(post_meeting_recap(dict(meeting), session))
        _TASKS.add(task)
        task.add_done_callback(_TASKS.discard)
        return task
    except Exception:  # noqa: BLE001
        log.exception("could not schedule slack recap")
        return None


def find_meeting(meeting_id: str, workspace: str | None, room: str | None) -> dict | None:
    if workspace:
        from brain.meeting_store import list_meetings
        return next((m for m in list_meetings(workspace) if m.get("id") == meeting_id), None)
    meeting = _RECENT.get(meeting_id)
    if meeting and not meeting.get("workspace") and (not room or meeting.get("room") == room):
        return meeting
    return None
