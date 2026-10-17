"""Sourced memory curation and bounded two-pass retrieval."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Callable

import config
import llm
from brain.memory import MemoryStore


log = logging.getLogger(__name__)
PROMPT = Path(__file__).parent / "prompts" / "librarian.md"
MAX_CANDIDATES = 24
NANO_TIMEOUT = 5.0
SUPER_TIMEOUT = 18.0


def _object(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("Librarian response must be a JSON object")
    return value


def _citations(facts: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [{"id": fact["id"], "claim": fact["claim"], "source": fact["source"]}
            for fact in facts]


class Librarian:
    def __init__(self, store: MemoryStore | None = None):
        self.store = store or MemoryStore()

    async def _nano_shortlist(self, query: str, candidates: list[dict], limit: int) -> list[str]:
        payload = {"query": query, "candidates": _citations(candidates), "limit": limit}
        messages = [
            {"role": "system", "content": PROMPT.read_text(encoding="utf-8")
             + "\nSelect relevant IDs only. Return {\"ids\":[\"fact_id\"]}."},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        async with asyncio.timeout(NANO_TIMEOUT):
            raw = await llm.chat(config.MODEL_FAST, messages, max_tokens=500, temperature=0,
                                 extra_body={"chat_template_kwargs": {"enable_thinking": False}})
        ids = _object(raw).get("ids")
        if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
            raise ValueError("Nano shortlist IDs missing")
        return ids

    async def _jev_shortlist(self, query: str, candidates: list[dict], limit: int) -> list[str]:
        from agents.fast_decide import decide
        subset = candidates[:16]
        questions = {f"f{index}": {"type": "noul", "instructions":
                     f"Is fact {fact['id']} directly relevant to answering the query? Fact: {fact['claim']}"}
                     for index, fact in enumerate(subset)}
        answers = await decide({"query": query}, questions)
        scored = sorted(((float(answers[f"f{index}"]["noul"]), index)
                         for index in range(len(subset))), reverse=True)
        return [subset[index]["id"] for score, index in scored if score >= 0.35][:limit]

    async def _shortlist(self, query: str, candidates: list[dict], limit: int,
                         first_pass: Callable | None) -> list[dict]:
        allowed = {fact["id"]: fact for fact in candidates}
        try:
            if first_pass is not None:
                supplied = [dict(fact) for fact in candidates]
                if inspect.iscoroutinefunction(first_pass):
                    selected = await asyncio.wait_for(first_pass(query, supplied, limit),
                                                      timeout=NANO_TIMEOUT)
                else:
                    selected = await asyncio.wait_for(
                        asyncio.to_thread(first_pass, query, supplied, limit),
                        timeout=NANO_TIMEOUT)
                    if inspect.isawaitable(selected):
                        selected = await asyncio.wait_for(selected, timeout=NANO_TIMEOUT)
            elif os.getenv("TYPESAFE_API_KEY", "").strip():
                try:
                    selected = await self._jev_shortlist(query, candidates, limit)
                except Exception as exc:
                    log.warning("JEV memory shortlist failed; trying Nano (%s)", type(exc).__name__)
                    selected = await self._nano_shortlist(query, candidates, limit)
            else:
                selected = await self._nano_shortlist(query, candidates, limit)
            if not isinstance(selected, list) or not all(isinstance(item, str) for item in selected):
                raise ValueError("first-pass filter must return fact IDs")
            ids = list(dict.fromkeys(item for item in selected if item in allowed))[:limit]
            if not ids:
                raise ValueError("first-pass filter returned no valid candidates")
            return [allowed[item] for item in ids]
        except Exception as exc:
            log.warning("memory first-pass unavailable; using lexical shortlist (%s)", type(exc).__name__)
            return candidates[:limit]

    async def retrieve(self, query: str, *, scope: str | None = None, limit: int = 8,
                       first_pass: Callable | None = None) -> dict[str, Any]:
        """Nano/JEV selects a short list; Super answers with verified citation IDs.

        A failing backend falls back to a plain list of the sourced claims. A
        citation is never accepted unless it belongs to the selected current facts.
        """
        if not 1 <= limit <= 16:
            raise ValueError("limit must be from 1 to 16")
        candidates = self.store.search(query, scope=scope, current_only=True,
                                       limit=MAX_CANDIDATES)
        if not candidates:
            return {"answer": "I don't have a sourced fact for that yet.",
                    "citations": [], "facts": []}
        selected = await self._shortlist(query, candidates, limit, first_pass)
        cited = _citations(selected)
        messages = [
            {"role": "system", "content": PROMPT.read_text(encoding="utf-8")
             + "\nAnswer using only cited facts. Return JSON: "
               "{\"answer\":\"...\",\"citation_ids\":[\"fact_id\"]}. "
               "Every factual sentence needs one or more supplied citation IDs. "
               "If the facts do not support an answer, say you do not know and return []."},
            {"role": "user", "content": json.dumps({"question": query, "facts": cited},
                                                   ensure_ascii=False)},
        ]
        try:
            async with asyncio.timeout(SUPER_TIMEOUT):
                raw = await llm.chat(config.MODEL_SMART, messages, max_tokens=900,
                                     temperature=0,
                                     extra_body={"chat_template_kwargs": {"enable_thinking": False}})
            result = _object(raw)
            answer = result.get("answer")
            ids = result.get("citation_ids")
            if not isinstance(answer, str) or not answer.strip() or len(answer) > 3000:
                raise ValueError("answer missing or too long")
            if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
                raise ValueError("citation IDs missing")
            allowed = {fact["id"]: fact for fact in cited}
            if any(item not in allowed for item in ids):
                raise ValueError("answer cited a fact outside its supplied shortlist")
            if not ids and not re.search(
                    r"\b(?:i (?:do not|don't) know|not enough information|"
                    r"cannot determine|not specified|no sourced facts?)\b",
                    answer.casefold()):
                raise ValueError("uncited answer")
            citations = list(dict.fromkeys(ids))
            return {"answer": answer.strip(), "citations": [allowed[item] for item in citations],
                    "facts": selected}
        except Exception as exc:
            log.warning("memory answer unavailable; returning sourced claims (%s)", type(exc).__name__)
            return {"answer": "Relevant sourced facts: "
                    + "; ".join(f"{fact['claim']} [{fact['id']}]" for fact in selected),
                    "citations": cited, "facts": selected}

    async def remember(self, text: str, source: str, *, scope: str = "general",
                       subject: str | None = None) -> dict[str, Any]:
        """Store a sourced fact and, on a verified same-subject conflict, link it.

        The model can propose only an ID from current candidate facts. The store
        validates the ID, scope, source, subject, and current status before linking.
        """
        # Automatic supersession needs an explicit subject on both facts.
        # Keyword overlap alone is not evidence that two claims conflict.
        candidates = ([fact for fact in self.store.search(text, scope=scope, limit=8)
                       if fact.get("subject")
                       and fact["subject"].casefold() == subject.casefold()]
                      if subject else [])
        proposal = None
        if candidates:
            messages = [
                {"role": "system", "content": PROMPT.read_text(encoding="utf-8")
                 + "\nDecide whether the NEW sourced claim directly contradicts one OLD "
                   "current fact about the same subject. Return JSON "
                   "{\"old_id\":null,\"reason\":null} if none, otherwise use one "
                   "supplied old_id and a concrete reason naming the conflicting claims."},
                {"role": "user", "content": json.dumps({"new": {"claim": text, "source": source,
                                                                   "subject": subject, "scope": scope},
                                                      "old": _citations(candidates)}, ensure_ascii=False)},
            ]
            try:
                async with asyncio.timeout(SUPER_TIMEOUT):
                    raw = await llm.chat(config.MODEL_SMART, messages, max_tokens=350,
                                         temperature=0,
                                         extra_body={"chat_template_kwargs": {"enable_thinking": False}})
                proposal = _object(raw)
            except Exception as exc:
                log.warning("memory contradiction check unavailable (%s)", type(exc).__name__)
        new = self.store.add_fact(text, source, scope=scope, subject=subject)
        if isinstance(proposal, dict):
            old_id, reason = proposal.get("old_id"), proposal.get("reason")
            allowed = {fact["id"] for fact in candidates}
            if old_id in allowed and isinstance(reason, str) and reason.strip():
                try:
                    self.store.supersede(old_id, new, reason)
                except ValueError as exc:
                    log.warning("memory supersession rejected (%s)", type(exc).__name__)
        return self.store.get(new["id"])
