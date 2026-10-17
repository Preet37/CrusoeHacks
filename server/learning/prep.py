"""Pre-meeting brief predictions from current sourced memory and the code map."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import config
import llm


PROMPT = Path(__file__).parent / "playbooks" / "prep.md"


def _evidence(session: Any) -> dict[str, Any]:
    memory = getattr(session, "memory", None)
    facts = memory.current_facts()[:20] if memory is not None else []
    code_map = getattr(session, "code_map", None)
    files = list(getattr(code_map, "files", {}).values())[:30] if code_map is not None else []
    return {
        "facts": [{"claim": str(fact.get("claim", ""))[:300],
                   "source": str(fact.get("source", ""))[:150]} for fact in facts],
        "files": [{"path": str(item.get("path", ""))[:160],
                   "summary": str(item.get("summary", ""))[:180]} for item in files],
    }


async def predict(session: Any, meeting: str, *, prompt_path: str | Path | None = None) -> list[str]:
    """Return up to three grounded predictions; an invalid reply gives no brief."""
    if not isinstance(meeting, str) or not meeting.strip():
        raise ValueError("meeting title is required")
    if prompt_path is not None:
        prompt_text = Path(prompt_path).read_text(encoding="utf-8")
    else:
        # The offline Coach selects a version atomically in active.json. A
        # missing or invalid selection falls back to the shipped baseline.
        prompt_text = PROMPT.read_text(encoding="utf-8")
        from learning.coach import DEFAULT_STORE, active_prompts
        if (DEFAULT_STORE / "active.json").exists():
            try:
                prompt_text = active_prompts()["prep"]
            except (OSError, ValueError, KeyError, TypeError):
                pass
    raw = await llm.chat(config.MODEL_SMART, [
        {"role": "system", "content": prompt_text},
        {"role": "user", "content": json.dumps({"meeting": meeting[:120],
                                                 "evidence": _evidence(session)})},
    ], max_tokens=350, response_format={"type": "json_object"},
        extra_body={"chat_template_kwargs": {"enable_thinking": False}})
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    values = data.get("predictions") if isinstance(data, dict) else None
    if not isinstance(values, list):
        return []
    return [value.strip()[:200] for value in values[:3]
            if isinstance(value, str) and value.strip()]


async def brief_provider(session: Any, meeting: str) -> list[str]:
    """Plug directly into ``Session.brief_provider``."""
    return await predict(session, meeting)
