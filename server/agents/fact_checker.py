"""Check spoken claims against current, sourced memory facts."""

import asyncio
import json
import logging
import re
from pathlib import Path

import config
import llm
from agents.diagrammer import emit_event
from agents.job_budget import JobBudget

log = logging.getLogger(__name__)
PROMPT = Path(__file__).parent / "prompts" / "fact_checker.md"
_IMPLEMENTATION = re.compile(
    r"\b(currently|uses?|does|no longer|now|implemented|stores?|validates?|"
    r"runs?|ships?|supports?)\b", re.I)


class FactChecker:
    async def handle(self, session, moment: dict, *, parent_id: str | None = None,
                     budget: JobBudget | None = None) -> dict:
        budget = budget or JobBudget()
        if not budget.reserve_job():
            return {"summary": None, "citations": [], "urgent": False}
        claim = str(moment.get("text") or "")[:500]
        event_id = await emit_event(session, "Fact Checker", config.MODEL_SMART,
                                    "started", f"check {claim}"[:160],
                                    parent_id=parent_id)
        try:
            memory = getattr(session, "memory", None)
            if memory and _IMPLEMENTATION.search(claim):
                # A meeting decision to migrate is not evidence that the code
                # already migrated. Present-tense implementation claims are
                # checked against the code scope only.
                code_scope = getattr(session, "code_fact_scope",
                                     f"{getattr(session, 'room', 'demo')}:code")
                candidates = memory.search(claim, scope=code_scope,
                                           current_only=True, limit=8)
            else:
                candidates = memory.search(claim, current_only=True, limit=8) if memory else []
            # Unsourced facts cannot support a public correction. Keep them in
            # memory for other use, but do not present them as proof.
            candidates = [fact for fact in candidates if isinstance(fact, dict)
                          and fact.get("id") and fact.get("claim") and fact.get("source")
                          and fact.get("status", "current") == "current"][:8]
            result = {"summary": None, "citations": [], "urgent": False}
            if candidates:
                prompt_input = json.dumps({"claim": claim, "current_facts": [
                    {key: fact.get(key) for key in ("id", "claim", "source", "subject")}
                    for fact in candidates
                ]})
                tokens = budget.reserve_model(PROMPT.read_text() + prompt_input, 400)
                if tokens:
                    raw = await llm.chat(config.MODEL_SMART, [
                        {"role": "system", "content": PROMPT.read_text()},
                        {"role": "user", "content": prompt_input},
                    ], max_tokens=tokens, response_format={"type": "json_object"},
                        extra_body={"chat_template_kwargs": {"enable_thinking": False}})
                    try:
                        verdict = json.loads(raw)
                    except (ValueError, TypeError):
                        verdict = {}
                    if isinstance(verdict, dict) and verdict.get("contradicted") is True:
                        matched = next((fact for fact in candidates
                                        if fact["id"] == verdict.get("fact_id")), None)
                        if matched is not None:
                            source = matched["source"]
                            if not isinstance(source, str):
                                source = json.dumps(source, default=str)
                            source = source[:240]
                            result = {
                                "summary": f"Correction: {matched['claim']} (source: {source}).",
                                "citations": [{"id": matched["id"], "claim": matched["claim"],
                                               "source": matched["source"]}],
                                "urgent": True,
                            }
            await emit_event(session, "Fact Checker", config.MODEL_SMART,
                             "done", "contradiction" if result["urgent"] else "no sourced contradiction",
                             event_id=event_id, parent_id=parent_id)
            return result
        except asyncio.CancelledError:
            await emit_event(session, "Fact Checker", config.MODEL_SMART,
                             "cancelled", "cancelled", event_id=event_id,
                             parent_id=parent_id)
            raise
        except Exception as exc:
            await emit_event(session, "Fact Checker", config.MODEL_SMART,
                             "failed", str(exc)[:160], event_id=event_id,
                             parent_id=parent_id)
            raise
