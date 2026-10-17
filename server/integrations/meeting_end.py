"""End-of-meeting pipeline: crisp summary -> tickets that always exist -> Jira push -> #standup recap.

app.py's ws `finally` calls `schedule(session, saved)` once the room empties. ONE background
coroutine runs the steps in order so the recap always sees the auto-created tickets and their
Jira links:

    save_meeting (sync, in app.py) -> ensure_tickets_and_summary -> auto_push_meeting -> slack recap

`summarize` makes one Nemotron call (Lightning, then Fast, then Coach as fallbacks) and never
returns a transcript; when every model fails, a deterministic template summary is used.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from uuid import uuid4

import config

log = logging.getLogger("twin.meeting_end")

CALL_TIMEOUT_S = 12.0
MAX_ITEMS = 5
_TASKS: set[asyncio.Task] = set()
# Under pytest the background pipeline only runs when a test opts in (no stray LLM/HTTP calls).
AUTORUN_IN_TESTS = False

_NOT_HUMAN = {"twin", "participant", "assistant", "system", ""}
_LABEL_START = re.compile(r"(?m)^\s*[A-Z][\w'’.-]*(?:\s+[A-Z][\w'’.-]*){0,2}\s*:\s")

SYSTEM = (
    "You write the end-of-meeting recap for a team's Slack #standup channel. "
    "Reply with ONE JSON object: {\"summary\": string, \"decisions\": [string], "
    "\"action_items\": [{\"title\": string, \"owner\": string|null, \"why\": string, "
    "\"acceptance\": [string]}]}.\n"
    "summary: 2-4 crisp sentences in the third person about WHAT was discussed and decided. "
    "Never quote or replay the transcript, never use speaker labels like 'Name:'.\n"
    "decisions: agreements the team reached (may be empty).\n"
    "action_items: concrete follow-up work, at least one per substantive topic, at most 5. "
    "title is an imperative ticket title under 90 characters. owner is the person who asked for "
    "the work or was named for it, spelled as in the participant list, else null. why is one "
    "sentence. acceptance has 2-3 checkable criteria.\nNo markdown, no prose outside the JSON."
)


# ------------------------------------------------------------------ meeting view

def _snapshot(session) -> dict | None:
    board = getattr(session, "board", None) if session is not None else None
    if board is None:
        return None
    try:
        snap = board.snapshot()
    except Exception:  # noqa: BLE001 - best effort
        return None
    return snap if isinstance(snap, dict) else None


def view(source, session=None) -> dict:
    """Normalize a saved meeting payload (or a live session) into what summarize needs."""
    if isinstance(source, dict):
        meeting = source
    else:
        session = session or source
        from brain.meeting_store import _lines
        topic = getattr(session, "active_topic", None)
        meeting = {"room": getattr(session, "room", ""), "notes": _lines(session),
                   "title": str(topic.get("title") or "") if isinstance(topic, dict) else "",
                   "contribution": getattr(session, "latest_contribution", None),
                   "board": _snapshot(session), "tickets": [], "decisions": []}
    if not meeting.get("board") and session is not None:
        snap = _snapshot(session)
        if snap:
            meeting = {**meeting, "board": snap}
    return meeting


def _notes(meeting: dict) -> list[dict]:
    return [n for n in meeting.get("notes") or []
            if isinstance(n, dict) and str(n.get("text") or "").strip()]


def humans(meeting: dict) -> list[str]:
    out: list[str] = []
    for n in _notes(meeting):
        name = str(n.get("speaker") or "").strip()
        if name.casefold() not in _NOT_HUMAN and name not in out:
            out.append(name)
    return out


def _clean(text, limit: int = 120) -> str:
    return " ".join(str(text or "").split())[:limit].strip()


def board_labels(meeting: dict) -> list[str]:
    snap = meeting.get("board") if isinstance(meeting.get("board"), dict) else {}
    labels: list[str] = []
    for shape in snap.get("shapes") or []:
        if isinstance(shape, dict) and shape.get("op") in ("frame", "add_chart", "add_mock", "add_3d"):
            labels.append(_clean(shape.get("title"), 80))
    for box in snap.get("boxes") or []:
        if isinstance(box, dict):
            labels.append(_clean(box.get("label"), 60))
    return list(dict.fromkeys(l for l in labels if l))[:12]


def frame_titles(meeting: dict) -> list[str]:
    snap = meeting.get("board") if isinstance(meeting.get("board"), dict) else {}
    return list(dict.fromkeys(_clean(s.get("title"), 80) for s in snap.get("shapes") or []
                              if isinstance(s, dict) and s.get("op") == "frame" and s.get("title")))


def _title_is_topic(meeting: dict) -> bool:
    """save_meeting falls back to the first spoken line as the title; that is not a topic."""
    title = _clean(meeting.get("title"), 160)
    if not title or title.endswith("?") or len(title.split()) > 9:
        return False
    return not any(title == _clean(n.get("text"), 160)[:len(title)] for n in _notes(meeting))


def topics(meeting: dict) -> list[str]:
    found = [_clean(meeting.get("title"), 100)] if _title_is_topic(meeting) else []
    found += frame_titles(meeting)
    return [t for t in dict.fromkeys(found) if t][:MAX_ITEMS]


def _contribution(meeting: dict) -> dict:
    c = meeting.get("contribution")
    return c if isinstance(c, dict) else {}


def looks_like_transcript(text: str, meeting: dict | None = None) -> bool:
    text = str(text or "")
    if _LABEL_START.search(text):
        return True
    names = (humans(meeting) if meeting else []) + ["Twin", "Participant"]
    low = text.casefold()
    return any(f"{name.casefold()}:" in low for name in names)


# ------------------------------------------------------------------ summary

def _join(items: list[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _sentence(text: str) -> str:
    text = _clean(text, 220).rstrip(" .;:,")
    return (text[:1].upper() + text[1:] + ".") if text else ""


def fallback_summary(meeting: dict) -> str:
    """Template summary from topics, board and Twin's take; never transcript lines."""
    subjects = topics(meeting) or board_labels(meeting)[:4]
    people = humans(meeting)
    parts = []
    if subjects:
        parts.append(f"The team discussed {_join(subjects[:4])}.")
    elif people:
        parts.append(f"{_join(people[:3])} met with Twin for a short working session.")
    else:
        parts.append("The team held a short working session with Twin.")
    contrib = _contribution(meeting)
    if contrib.get("show") and not looks_like_transcript(contrib["show"], meeting):
        parts.append(f"Twin proposed {_clean(contrib['show'], 200).rstrip('.')}.")
    elif contrib.get("current") and not looks_like_transcript(contrib["current"], meeting):
        parts.append(f"Twin reviewed the current state: {_clean(contrib['current'], 200).rstrip('.')}.")
    elif board_labels(meeting):
        parts.append(f"Twin sketched {_join(board_labels(meeting)[:3])} on the board.")
    decisions = [d for d in meeting.get("decisions") or [] if str(d).strip()
                 and not looks_like_transcript(d, meeting)]
    if decisions:
        parts.append(f"The team agreed to {_clean(decisions[0], 160).rstrip('.')}.")
    return " ".join(parts)[:900]


def fallback_items(meeting: dict) -> list[dict]:
    room = _clean(meeting.get("room"), 60) or "meeting"
    subjects = topics(meeting) or [f"the {room} discussion"]
    items = []
    for subject in subjects[:MAX_ITEMS]:
        items.append({"title": f"Follow up on {subject}"[:140], "owner": None,
                      "why": f"{subject[:1].upper() + subject[1:]} came up in the meeting and needs an owner to drive next steps.",
                      "acceptance": [f"Next steps for {subject} are written down in this ticket",
                                     "Owner shares an update at the next standup"]})
    return items


def _messages(meeting: dict) -> list[dict]:
    lines = [f"{n.get('speaker') or 'Participant'}: {_clean(n.get('text'), 500)}" for n in _notes(meeting)]
    transcript = "\n".join(lines)
    if len(transcript) > 7000:  # keep the end of the meeting, where conclusions land
        transcript = transcript[-7000:]
    contrib = _contribution(meeting)
    context = {
        "meeting": _clean(meeting.get("title") or meeting.get("room"), 160),
        "participants": humans(meeting),
        "topics": topics(meeting),
        "board_labels": board_labels(meeting),
        "twin_latest_contribution": {k: contrib.get(k) for k in ("show", "current", "impact", "risks", "question")
                                     if contrib.get(k)},
        "existing_tickets": [_clean(t.get("title"), 140) for t in meeting.get("tickets") or []
                             if isinstance(t, dict) and t.get("status") != "dismissed"][:10],
    }
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)
             + "\n\nTranscript (for your understanding only; do not quote it):\n" + transcript}]


def _parse(raw: str) -> dict | None:
    text = str(raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        data = json.loads(text)
    except ValueError:
        match = re.search(r"\{.*\}", text, re.S)
        try:
            data = json.loads(match.group(0)) if match else None
        except ValueError:
            data = None
    if isinstance(data, dict):
        return data
    if text and not text.startswith(("{", "[")):
        return {"summary": text}  # plain prose summary
    return None


def _trim_summary(text: str) -> str:
    text = " ".join(str(text or "").split())
    sentences = re.split(r"(?<=[.!?])\s+", text)
    return " ".join(sentences[:4])[:900]


def _items(raw, meeting: dict) -> list[dict]:
    out = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str):
            item = {"title": item}
        if not isinstance(item, dict) or not _clean(item.get("title")):
            continue
        acceptance = item.get("acceptance")
        if isinstance(acceptance, str):
            acceptance = [acceptance]
        acceptance = [_clean(a, 200) for a in acceptance or [] if _clean(a)][:3]
        owner = _clean(item.get("owner"), 100) or None
        out.append({"title": _clean(item["title"], 140).rstrip("."),
                    "owner": owner if owner and owner.casefold() not in _NOT_HUMAN | {"null", "none"} else None,
                    "why": _clean(item.get("why"), 300),
                    "acceptance": acceptance or [f"{_clean(item['title'], 120)} is done and demoed to the team",
                                                 "Owner posts the result in #standup"]})
        if len(out) >= MAX_ITEMS:
            break
    return out


async def summarize(source, session=None, *, timeout: float | None = None) -> dict:
    """{"summary", "decisions", "action_items", "source": "llm"|"fallback"} for a finished meeting."""
    meeting = view(source, session)
    existing = [_clean(d, 300) for d in meeting.get("decisions") or [] if _clean(d)]
    fallback = {"summary": fallback_summary(meeting), "decisions": existing,
                "action_items": fallback_items(meeting), "source": "fallback"}
    if not _notes(meeting):
        return fallback
    import llm
    messages = _messages(meeting)
    per_call = timeout or CALL_TIMEOUT_S
    for model in dict.fromkeys(m for m in (config.MODEL_RESPONDER, config.MODEL_FAST, config.MODEL_COACH) if m):
        try:
            raw = await asyncio.wait_for(llm.chat(
                model, messages, max_tokens=900, temperature=0.2,
                response_format={"type": "json_object"},
                extra_body={"chat_template_kwargs": {"enable_thinking": False}}), timeout=per_call)
        except Exception as e:  # noqa: BLE001 - timeout / no key / model error -> next model
            log.info("meeting summary: %s unavailable (%s)", model, type(e).__name__)
            continue
        data = _parse(raw)
        summary = _trim_summary((data or {}).get("summary"))
        if not summary or looks_like_transcript(summary, meeting):
            log.info("meeting summary: %s returned an unusable summary", model)
            continue
        decisions = [_clean(d, 300) for d in (data.get("decisions") or []) if isinstance(d, str) and _clean(d)]
        items = _items(data.get("action_items"), meeting)
        return {"summary": summary, "decisions": decisions[:10] or existing,
                "action_items": items or fallback["action_items"], "source": "llm"}
    return fallback


# ------------------------------------------------------------------ tickets

def resolve_owner(owner: str | None, meeting: dict) -> str | None:
    people = humans(meeting)
    if owner:
        low = owner.casefold()
        for name in people:
            if name.casefold() == low or name.casefold().split()[0] == low.split()[0]:
                return name
        spoken = " ".join(_clean(n.get("text"), 1000) for n in _notes(meeting)).casefold()
        if re.search(rf"\b{re.escape(low.split()[0])}\b", spoken):
            return owner  # named in the meeting but not a speaker
    return people[0] if people else None


def _topic_id(meeting: dict, session) -> str | None:
    active = getattr(session, "active_topic", None) if session is not None else None
    if isinstance(active, dict) and active.get("topic_id"):
        return active["topic_id"]
    return _contribution(meeting).get("topic_id")


def ticket_from_item(item: dict, meeting: dict, session=None) -> dict:
    """A draft in the same shape agents/task_planner.py produces."""
    title = _clean(item.get("title"), 140) or "Follow up on the meeting"
    owner = resolve_owner(item.get("owner"), meeting)
    why = _clean(item.get("why"), 300)
    acceptance = [a for a in item.get("acceptance") or [] if a][:3]
    source = f"meeting {meeting.get('room') or 'session'} (end-of-meeting summary)"
    labels = board_labels(meeting)
    body = f"Action item: {title}" + (f"\nWhy: {why}" if why else "") + f"\nSource: {source}"
    if labels:
        body += "\nBoard context: " + "; ".join(labels[:5])
    acceptance_text = "\n".join(acceptance)
    agent_prompt = (f"Implement this follow-up from the meeting: {title}\n"
                    + (f"Why: {why}\n" if why else "")
                    + f"Source: {source}\n"
                    + (f"Owner: {owner}\n" if owner else "")
                    + ("Board context: " + "; ".join(labels[:5]) + "\n" if labels else "")
                    + (f"Acceptance check:\n{acceptance_text}\n" if acceptance_text else "")
                    + "Inspect the current code and tests before editing; do not assume the work is done.")
    return {"type": "ticket", "id": f"T-{uuid4().hex[:10]}", "title": title,
            "body": body[:1600], "owner": owner, "acceptance": acceptance_text[:750],
            "agent_prompt": agent_prompt[:3500], "topic": _topic_id(meeting, session),
            "source": source, "status": "draft",
            "links": {"jira": None, "slack": None, "agent": None}, "auto": True}


def _active(meeting: dict) -> list[dict]:
    return [t for t in meeting.get("tickets") or [] if isinstance(t, dict) and t.get("status") != "dismissed"]


async def ensure_tickets_and_summary(saved: dict, session=None) -> dict:
    """Add the crisp summary + action items; create tickets when the meeting drafted none; persist."""
    import integrations
    meeting = json.loads(json.dumps(saved, default=str))
    result = await summarize(meeting, session)
    meeting["summary"] = meeting["ai_summary"] = result["summary"]
    meeting["decisions"] = result["decisions"]
    meeting["action_items"] = [{**i, "owner": resolve_owner(i.get("owner"), meeting)}
                               for i in result["action_items"]]
    workspace = meeting.get("workspace")
    allowed = integrations.ticket_permission(None, workspace) != "off"
    if workspace and allowed and not _active(meeting):
        tickets = [ticket_from_item(i, meeting, session) for i in result["action_items"][:MAX_ITEMS]]
        meeting["tickets"] = list(meeting.get("tickets") or []) + tickets
        meeting["todos"] = list(meeting.get("todos") or []) + [
            {"title": t["title"], "owner": t["owner"], "status": "draft", "ticket_id": t["id"]} for t in tickets]
        log.info("[%s] meeting end: created %d ticket(s) from action items", meeting.get("room"), len(tickets))
    if workspace:
        integrations._save_meeting_payload(meeting)
    return meeting


# ------------------------------------------------------------------ pipeline

async def run(session, saved: dict | None) -> dict:
    """ensure tickets + summary -> auto Jira push -> #standup recap (waits for links). Never raises."""
    out: dict = {"pushed": [], "recap": None}
    meeting = saved
    try:
        if isinstance(saved, dict):
            meeting = await ensure_tickets_and_summary(saved, session)
        else:
            from integrations import slack
            payload = slack._session_payload(session)
            if payload:
                result = await summarize(payload, session)
                meeting = {**payload, "summary": result["summary"], "ai_summary": result["summary"],
                           "decisions": result["decisions"], "action_items": result["action_items"]}
    except Exception:  # noqa: BLE001
        log.exception("meeting end: summary/tickets failed")
    try:
        if isinstance(meeting, dict) and meeting.get("workspace") and _active(meeting):
            import integrations
            out["pushed"] = await integrations.auto_push_meeting(meeting)
    except Exception:  # noqa: BLE001
        log.exception("meeting end: auto Jira push failed")
    try:
        from integrations import slack
        out["recap"] = await slack.recap_now(session, meeting if isinstance(meeting, dict) else None)
    except Exception:  # noqa: BLE001
        log.exception("meeting end: slack recap failed")
    out["meeting"] = meeting
    return out


def schedule(session, saved: dict | None) -> asyncio.Task | None:
    """Fire-and-forget from the ws `finally`; never blocks or raises."""
    try:
        if "pytest" in sys.modules and not AUTORUN_IN_TESTS:
            return None
        task = asyncio.get_running_loop().create_task(run(session, saved))
        _TASKS.add(task)
        task.add_done_callback(_TASKS.discard)
        return task
    except Exception:  # noqa: BLE001
        log.exception("could not schedule the end-of-meeting pipeline")
        return None
