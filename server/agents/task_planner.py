"""Turn explicit meeting decisions into owner-aware task wire messages."""

import asyncio
import json
import logging
import re
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


def _spoken_owner(decision: str, source_lines: list[dict], candidates: list[str]) -> str | None:
    texts = [str(line.get("text") or "") for line in source_lines[-12:]] + [decision]
    for text in texts:
        match = _NAMED_OWNER.search(text)
        if match:
            spoken = match.group(1)
            return next((name for name in candidates
                         if name.casefold() == spoken.casefold()
                         or name.casefold().startswith(spoken.casefold() + " ")), spoken)
    return None


class TaskPlanner:
    async def handle(self, session, moment: dict, *, parent_id: str | None = None,
                     budget: JobBudget | None = None,
                     source_lines: list[dict] | None = None) -> dict:
        budget = budget or JobBudget()
        if not budget.reserve_job():
            return {"summary": None, "citations": [], "tasks": []}
        decision = str(moment.get("text") or "").strip()[:500]
        event_id = await emit_event(session, "Task Planner", config.MODEL_SMART,
                                    "started", f"plan {decision}"[:160],
                                    parent_id=parent_id)
        try:
            if not decision:
                result = {"summary": None, "citations": [], "tasks": []}
            else:
                planned = getattr(session, "_planned_decisions", None)
                if planned is None:
                    planned = session._planned_decisions = set()
                key = decision.casefold()
                if key in planned:
                    result = {"summary": None, "citations": [], "tasks": []}
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
                    planned.add(key)
                    result = {"summary": f"Task created: {title}"
                                         + (f" (owner: {owner})" if owner else "")
                                         + f". Source: {source}.",
                              "citations": ([{"id": memory_fact["id"],
                                              "source": memory_fact["source"]}]
                                            if memory_fact else [])
                              + [{"path": path} for path in paths],
                              "tasks": [task]}
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
