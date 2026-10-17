"""Turn explicit commitments into task messages and local, unsent ticket drafts."""

import asyncio
import json
import logging
import re
from collections import OrderedDict
from pathlib import Path
from uuid import uuid4

import config
import llm
from agents.code_analyst import resolved_query
from agents.diagrammer import emit_event
from agents.job_budget import JobBudget

log = logging.getLogger(__name__)
PROMPT = Path(__file__).parent / "prompts" / "task_planner.md"
_NAMED_OWNER = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+will\s+own\b")
_SELF_OWNER = re.compile(r"\b(?:I(?:'ll| will| can)\s+(?:own|take)|assign\s+(?:it|this)\s+to\s+me)\b", re.I)
_MAX_DRAFTS = 64
_META_DECISION = re.compile(
    r"^(?:(?:please|twin)[,\s]+)?(?:(?:let['’]s|let us|we should|can we)\s+)?"
    r"(?:(?:end|stop|close|finish)\s+(?:(?:the|this|our)\s+)?meeting(?:\s+now)?"
    r"|(?:keep|continue)\s+(?:doing\s+)?(?:that|this|it)(?:\s+way)?)"
    r"[.!?\s]*$", re.I)


def _spoken_owner(decision: str, source_lines: list[dict], candidates: list[str]) -> str | None:
    def same_commitment(text: str) -> bool:
        return bool(text and decision and
                    (text.casefold() == decision.casefold()
                     or decision.casefold() in text.casefold()
                     or text.casefold() in decision.casefold()))

    texts = [decision] + [str(line.get("text") or "") for line in source_lines[-12:]
                          if same_commitment(str(line.get("text") or "").strip())]
    for text in texts:
        match = _NAMED_OWNER.search(text)
        if match:
            spoken = match.group(1)
            return next((name for name in candidates
                         if name.casefold() == spoken.casefold()
                         or name.casefold().startswith(spoken.casefold() + " ")), spoken)
    for line in reversed(source_lines[-12:]):
        speaker = str(line.get("speaker") or "").strip()[:100]
        text = str(line.get("text") or "").strip()
        if speaker and same_commitment(text) and _SELF_OWNER.search(text):
            return speaker
    return None


def _board_evidence(session) -> tuple[list[str], list[str]]:
    """Small, literal references to the active frame and its visual artifacts."""
    board = getattr(session, "board", None)
    if board is None:
        return [], []
    shapes = getattr(board, "shapes", {})
    if not isinstance(shapes, dict):
        return [], []
    active = getattr(board, "active_frame", None)
    refs, diagrams = [], []
    frame = shapes.get(active) if active else None
    if isinstance(frame, dict) and frame.get("title"):
        refs.append(f"frame {str(frame['title'])[:100]}")
    for shape_id, shape in list(shapes.items())[:80]:
        if not isinstance(shape, dict) or (active and shape.get("frame") != active):
            continue
        op = shape.get("op")
        if op not in {"add_mermaid", "add_chart", "add_table", "add_mock", "add_code"}:
            continue
        label = str(shape.get("title") or shape_id)[:100]
        refs.append(f"{op.removeprefix('add_')} {label}")
        if op == "add_mermaid" and isinstance(shape.get("code"), str):
            diagrams.append(f"Mermaid {label}:\n{shape['code'][:1200]}")
        if len(refs) >= 5:
            break
    return refs[:5], diagrams[:2]


def _draft_ticket(session, *, decision: str, title: str, owner: str | None,
                  source: str, paths: list[str]) -> dict:
    refs, diagrams = _board_evidence(session)
    active = getattr(session, "active_topic", None)
    topic = active.get("topic_id") if isinstance(active, dict) else None
    body = f"Decision: {decision}\nSource: {source}"
    if refs:
        body += "\nBoard context: " + "; ".join(refs)
    if paths:
        body += "\nRelevant indexed files: " + ", ".join(paths)
    acceptance = f"Verify the delivered change matches the recorded decision: {decision}"
    if paths:
        acceptance += " Run relevant tests for the affected files."
    agent_prompt = (f"Implement this recorded meeting decision: {decision}\n"
                    f"Ticket: {title}\nSource: {source}\n"
                    + (f"Owner: {owner}\n" if owner else "")
                    + (f"Relevant indexed files: {', '.join(paths)}\n" if paths else "")
                    + ("Board context: " + "; ".join(refs) + "\n" if refs else "")
                    + ("\n".join(diagrams) + "\n" if diagrams else "")
                    + f"Acceptance check: {acceptance}\n"
                    "Inspect the current code and tests before editing; do not assume the decision is implemented.")
    return {"type": "ticket", "id": f"T-{uuid4().hex[:10]}", "title": title,
            "body": body[:1600], "owner": owner, "acceptance": acceptance[:750],
            "agent_prompt": agent_prompt[:3500], "topic": topic,
            "source": source, "status": "draft",
            "links": {"jira": None, "slack": None, "agent": None}}


class TaskPlanner:
    async def handle(self, session, moment: dict, *, parent_id: str | None = None,
                     budget: JobBudget | None = None,
                     source_lines: list[dict] | None = None) -> dict:
        decision = str(moment.get("text") or "").strip()[:500]
        # A turn-taking command or deictic acknowledgement is not a product
        # decision, even when extraction labels it as one.
        if (_META_DECISION.fullmatch(decision) or
                (source_lines and all(_META_DECISION.fullmatch(str(line.get("text") or "").strip())
                                      for line in source_lines))):
            return {"summary": None, "citations": [], "tasks": [], "tickets": []}
        budget = budget or JobBudget()
        if not budget.reserve_job():
            return {"summary": None, "citations": [], "tasks": [], "tickets": []}
        event_id = await emit_event(session, "Task Planner", config.MODEL_SMART,
                                    "started", f"plan {decision}"[:160],
                                    parent_id=parent_id)
        try:
            if not decision:
                result = {"summary": None, "citations": [], "tasks": [], "tickets": []}
            else:
                planned = getattr(session, "_planned_decisions", None)
                if not isinstance(planned, OrderedDict):
                    planned = session._planned_decisions = OrderedDict(
                        (item, None) for item in list(planned or ())[-_MAX_DRAFTS:])
                key = decision.casefold()
                if key in planned:
                    result = {"summary": None, "citations": [], "tasks": [], "tickets": []}
                else:
                    code_map = getattr(session, "code_map", None)
                    query, _ = resolved_query(code_map, moment, source_lines) if code_map else ("", ())
                    records = code_map.impact(query)[:8] if code_map and query else []
                    owners = list(dict.fromkeys(str(record["owner"]) for record in records
                                                if record.get("owner")))[:6]
                    paths = [str(record["path"]) for record in records if record.get("path")][:6]
                    title = decision.rstrip(".!? ")[:140]
                    spoken_owner = _spoken_owner(decision, source_lines or [], owners)
                    owner = spoken_owner or (owners[0] if owners else None)
                    prompt_input = json.dumps({"decision": decision, "candidate_owners": owners,
                                               "affected_paths": paths})
                    tokens = budget.reserve_model(PROMPT.read_text() + prompt_input, 280)
                    if tokens:
                        try:
                            raw = await llm.chat(config.MODEL_SMART, [
                                {"role": "system", "content": PROMPT.read_text()},
                                {"role": "user", "content": prompt_input},
                            ], max_tokens=tokens, response_format={"type": "json_object"},
                                extra_body={"chat_template_kwargs": {"enable_thinking": False}})
                            parsed = json.loads(raw)
                            if isinstance(parsed, dict):
                                proposed_title = parsed.get("title")
                                proposed_owner = parsed.get("owner")
                                if isinstance(proposed_title, str) and proposed_title.strip():
                                    title = proposed_title.strip()[:140]
                                if proposed_owner in owners and spoken_owner is None:
                                    owner = proposed_owner
                        except (ValueError, TypeError):
                            log.warning("Task Planner returned unusable JSON; using decision")
                        except Exception:
                            log.exception("Task Planner model failed; using decision")
                    source = f"meeting {getattr(session, 'room', 'session')}"
                    line = (source_lines or [])[-1:] or [{}]
                    if line[0].get("ts") is not None:
                        source += f" @ {line[0]['ts']}"
                    task = {"type": "task", "id": f"t_{uuid4().hex[:10]}",
                            "title": title, "owner": owner, "source": source}
                    # A code file's last author is not a meeting assignment.
                    # Tickets carry an owner only when the spoken line commits
                    # that person; legacy task messages keep their M2 behavior.
                    draft = _draft_ticket(session, decision=decision, title=title,
                                          owner=spoken_owner, source=source, paths=paths)
                    await session.broadcast(task)
                    memory_fact = None
                    memory = getattr(session, "memory", None)
                    if memory is not None:
                        scope = getattr(session, "decision_scope",
                                        f"{getattr(session, 'room', 'demo')}:decisions")
                        active = getattr(session, "active_topic", None)
                        subject = getattr(session, "decision_subject", None)
                        if not subject and isinstance(active, dict):
                            subject = active.get("title")
                        if not subject:
                            entities = moment.get("entities") or []
                            subject = str(entities[0]) if isinstance(entities, list) and entities else None
                        try:
                            # The Librarian may compare only same-subject facts
                            # in the decision scope. Its model call is reserved
                            # against this moment's shared token budget.
                            if budget.reserve_model(PROMPT.read_text() + decision + source
                                                    + str(subject), 450):
                                from agents.librarian import Librarian
                                memory_fact = await Librarian(memory).remember(
                                    decision, source, scope=scope, subject=subject)
                            else:
                                memory_fact = memory.add_fact(decision, source,
                                                              scope=scope, subject=subject)
                        except Exception:
                            log.exception("Could not store meeting decision")
                    drafts = getattr(session, "ticket_drafts", None)
                    if not isinstance(drafts, OrderedDict):
                        drafts = session.ticket_drafts = OrderedDict()
                    drafts[key] = draft
                    drafts.move_to_end(key)
                    while len(drafts) > _MAX_DRAFTS:
                        drafts.popitem(last=False)
                    planned[key] = None
                    while len(planned) > _MAX_DRAFTS:
                        planned.popitem(last=False)
                    result = {"summary": f"Task created: {title}"
                                         + (f" (owner: {owner})" if owner else "")
                                         + f". Source: {source}.",
                              "citations": ([{"id": memory_fact["id"],
                                              "source": memory_fact["source"]}]
                                            if memory_fact else [])
                              + [{"path": path} for path in paths],
                              "tasks": [task], "tickets": [draft]}
            await emit_event(session, "Task Planner", config.MODEL_SMART,
                             "done", f"{len(result['tasks'])} task(s)",
                             event_id=event_id, parent_id=parent_id)
            return result
        except asyncio.CancelledError:
            await emit_event(session, "Task Planner", config.MODEL_SMART,
                             "cancelled", "cancelled", event_id=event_id,
                             parent_id=parent_id)
            raise
        except Exception as exc:
            await emit_event(session, "Task Planner", config.MODEL_SMART,
                             "failed", str(exc)[:160], event_id=event_id,
                             parent_id=parent_id)
            raise
