"""Sourced code impact analysis over a prebuilt CodeMap."""

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
PROMPT = Path(__file__).parent / "prompts" / "code_analyst.md"
_PATH = re.compile(r"\b[\w./-]+\.py\b")
_SUBJECT = re.compile(r"\b(?:changing|change|switch|move|migrate|adopt|replace|impact of)\s+"
                      r"(?:the\s+)?([a-z][\w/-]*)", re.I)


def _query(moment: dict) -> str:
    text = str(moment.get("text") or "")
    path = _PATH.search(text)
    if path:
        return path.group(0)
    entities = moment.get("entities") or []
    if isinstance(entities, list) and entities:
        return str(entities[0])[:100]
    subject = _SUBJECT.search(text)
    return subject.group(1) if subject else text[:200]


def resolved_query(code_map, moment: dict, source_lines: list[dict] | None = None) -> tuple[str, tuple[str, ...]]:
    """Choose a CodeMap target, preferring the narrowest sourced resolution.

    Listener may extract a broad entity (``auth``) from a line explicitly
    about ``login``. Resolve both the moment and its source before spawning
    jobs, so those two forms share one impact analysis.
    """
    candidates = [_query(moment)]
    candidates.extend(_query({"text": str(line.get("text") or ""), "entities": []})
                      for line in (source_lines or [])[-12:])
    candidates = list(dict.fromkeys(candidate for candidate in candidates if candidate))
    resolve = getattr(code_map, "resolve", None)
    if callable(resolve):
        choices = []
        for index, candidate in enumerate(candidates):
            paths = tuple(sorted(set(resolve(candidate))))
            if paths:
                choices.append((len(paths), index, candidate, paths))
        if choices:
            _, _, query, paths = min(choices)
            return query, paths
    return (candidates[0], ()) if candidates else ("", ())


class CodeAnalyst:
    async def _section(self, session, path: str, section: str, parent_id: str,
                       budget: JobBudget) -> str:
        if not budget.reserve_job():
            return ""
        event_id = await emit_event(session, "Code Analyst section", "code_map",
                                    "started", f"read {path} section", parent_id=parent_id)
        try:
            # Evidence collection is bounded; a single giant file cannot fill
            # the reasoning prompt or recursively spawn unbounded children.
            excerpt = section[:1000]
            await emit_event(session, "Code Analyst section", "code_map",
                             "done", f"read {len(excerpt)} characters", event_id=event_id,
                             parent_id=parent_id)
            return excerpt
        except asyncio.CancelledError:
            await emit_event(session, "Code Analyst section", "code_map",
                             "cancelled", "cancelled", event_id=event_id,
                             parent_id=parent_id)
            raise

    async def _module(self, session, code_map, record: dict, parent_id: str,
                      budget: JobBudget) -> dict | None:
        if not budget.reserve_job():
            return None
        path = str(record.get("path") or "")
        event_id = await emit_event(session, "Code Analyst module", "code_map",
                                    "started", f"inspect {path}", parent_id=parent_id)
        try:
            source = ""
            read_file = getattr(code_map, "read_file", None)
            if callable(read_file) and path:
                source = read_file(path, max_chars=4000) or ""
            if len(source) > 2400:
                # Depth 3: Code Analyst -> module -> section. The shared
                # budget caps total jobs for this moment at 12.
                sections = await asyncio.gather(*(
                    self._section(session, path, chunk, event_id, budget)
                    for chunk in (source[:2000], source[2000:4000])
                ))
                source = "\n".join(section for section in sections if section)
            evidence = {
                "path": path,
                "relation": record.get("relation", "source"),
                "owner": record.get("owner"),
                "summary": str(record.get("summary") or "")[:220],
                "source": source[:1200],
            }
            await emit_event(session, "Code Analyst module", "code_map",
                             "done", f"inspected {path}", event_id=event_id,
                             parent_id=parent_id)
            return evidence
        except asyncio.CancelledError:
            await emit_event(session, "Code Analyst module", "code_map",
                             "cancelled", "cancelled", event_id=event_id,
                             parent_id=parent_id)
            raise
        except Exception as exc:
            await emit_event(session, "Code Analyst module", "code_map",
                             "failed", str(exc)[:160], event_id=event_id,
                             parent_id=parent_id)
            log.exception("module inspection failed: %s", path)
            return None

    async def handle(self, session, moment: dict, *, parent_id: str | None = None,
                     budget: JobBudget | None = None,
                     query_override: str | None = None) -> dict:
        budget = budget or JobBudget()
        if not budget.reserve_job():
            return {"summary": None, "citations": [], "highlights": []}
        query = query_override or _query(moment)
        event_id = await emit_event(session, "Code Analyst", config.MODEL_SMART,
                                    "started", f"impact of {query}"[:160],
                                    parent_id=parent_id)
        try:
            code_map = getattr(session, "code_map", None)
            if code_map is None:
                result = {"summary": None, "citations": [], "highlights": []}
            else:
                records = code_map.impact(query)[:8]
                evidence = [item for item in await asyncio.gather(*(
                    self._module(session, code_map, record, event_id, budget)
                    for record in records
                )) if item]
                paths = [item["path"] for item in evidence if item["path"]]
                citations = [{"path": item["path"], "owner": item["owner"],
                              "relation": item["relation"]} for item in evidence if item["path"]]
                summary = None
                if paths:
                    fallback = f"Changing {query} may affect {', '.join(paths[:6])}."
                    prompt_input = json.dumps({"question": moment.get("text", ""),
                                               "evidence": evidence[:8]})
                    tokens = budget.reserve_model(PROMPT.read_text() + prompt_input, 450)
                    if tokens:
                        try:
                            raw = await llm.chat(config.MODEL_SMART, [
                                {"role": "system", "content": PROMPT.read_text()},
                                {"role": "user", "content": prompt_input},
                            ], max_tokens=tokens, response_format={"type": "json_object"},
                                extra_body={"chat_template_kwargs": {"enable_thinking": False}})
                            parsed = json.loads(raw)
                            selected = parsed.get("paths", []) if isinstance(parsed, dict) else []
                            if (isinstance(selected, list) and selected
                                    and all(isinstance(path, str) and path in paths for path in selected)
                                    and isinstance(parsed.get("summary"), str)):
                                summary = parsed["summary"].strip()[:450]
                        except (ValueError, TypeError, KeyError):
                            log.warning("Code Analyst returned unusable summary")
                        except Exception:
                            log.exception("Code Analyst model failed; using mapped paths")
                    summary = summary or fallback
                    summary += " Sources: " + ", ".join(paths[:6]) + "."
                result = {"summary": summary, "citations": citations,
                          "highlights": paths}
            await emit_event(session, "Code Analyst", config.MODEL_SMART, "done",
                             f"{len(result['citations'])} sourced file(s)", event_id=event_id,
                             parent_id=parent_id)
            return result
        except asyncio.CancelledError:
            await emit_event(session, "Code Analyst", config.MODEL_SMART,
                             "cancelled", "cancelled", event_id=event_id,
                             parent_id=parent_id)
            raise
        except Exception as exc:
            await emit_event(session, "Code Analyst", config.MODEL_SMART,
                             "failed", str(exc)[:160], event_id=event_id,
                             parent_id=parent_id)
            raise
