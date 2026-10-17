"""Text-only replay through Twin's real Listener, Conductor and BoardState.

No mouth or TTS is involved. Prompt candidates live in temporary files and
replace module prompt paths only while one replay owns the process lock.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import logging
import tempfile
import time
from pathlib import Path

import protocol
from agents.listener import Listener
from brain.memory import MemoryStore
from brain.runtime import demo_map
from learning.evaluate import load_cases
from session import Session

_REPLAY_LOCK = asyncio.Lock()
_PROMPTS = {
    "listener": ("agents.listener", "PROMPT"),
    "conductor": ("agents.conductor", "ROUTER_PROMPT"),
    "diagrammer": ("agents.diagrammer", "PROMPT"),
    "code_analyst": ("agents.code_analyst", "PROMPT"),
    "fact_checker": ("agents.fact_checker", "PROMPT"),
    "task_planner": ("agents.task_planner", "PROMPT"),
}
_SEED = (
    ("Current login validates credentials and returns a signed JWT token, not a Redis-backed server session.",
     "demo_project/auth/login.py:18-24", "Auth login"),
    ("The database connection uses Postgres.",
     "demo_project/db/connection.py:6-10", "Database"),
)


async def replay_prep_case(case: dict, *, prompt_override: dict[str, str] | None = None) -> dict:
    """Cheap pre-meeting prediction replay for the deadline M3 proof."""
    from learning import prep

    if not case.get("id") or not case.get("meeting"):
        raise ValueError("Prep case needs id and meeting title")
    async with _REPLAY_LOCK:
        with tempfile.TemporaryDirectory(prefix="twin-prep-") as temporary:
            session = Session(f"prep-{case['id']}")
            session.code_map = demo_map()
            session.memory = MemoryStore(Path(temporary) / "memory.json")
            for claim, source, subject in _SEED:
                session.memory.add_fact(claim, source, scope="demo:code", subject=subject)
            content = (prompt_override or {}).get("prep")
            prompt_path = None
            if content is not None:
                prompt_path = Path(temporary) / "prep.md"
                prompt_path.write_text(content)
            usage = _Usage()
            logger = logging.getLogger("llm")
            old_level = logger.level
            logger.setLevel(logging.INFO)
            logger.addHandler(usage)
            started = time.monotonic()
            try:
                predictions = await prep.predict(session, str(case["meeting"]),
                                                 prompt_path=prompt_path)
            finally:
                logger.removeHandler(usage)
                logger.setLevel(old_level)
            result = {"id": case["id"], "predictions": predictions,
                      "latency_ms": round((time.monotonic() - started) * 1000)}
            if usage.tokens:
                result["tokens"] = sum(usage.tokens)
            return result


class _Usage(logging.Handler):
    def __init__(self):
        super().__init__()
        self.tokens: list[int] = []

    def emit(self, record):
        try:
            payload = json.loads(record.getMessage())
            if payload.get("event") == "llm_usage" and isinstance(payload.get("total_tokens"), int):
                self.tokens.append(payload["total_tokens"])
        except (ValueError, TypeError):
            pass


async def replay_case(case: dict, *, prompt_override: dict[str, str] | None = None,
                      timeout_per_line: float = 60) -> dict:
    """Replay one case; never writes candidate prompts into the shared tree."""
    if not isinstance(case.get("lines"), list) or not case.get("id"):
        raise ValueError("case needs id and lines")
    override = prompt_override or {}
    async with _REPLAY_LOCK:
        with tempfile.TemporaryDirectory(prefix="twin-replay-") as temporary:
            saved = {}
            usage = _Usage()
            logger = logging.getLogger("llm")
            old_level = logger.level
            logger.setLevel(logging.INFO)
            logger.addHandler(usage)
            try:
                for key, content in override.items():
                    key = key.removesuffix(".md")
                    if key not in _PROMPTS:
                        raise ValueError(f"unsupported prompt override: {key}")
                    module_name, attr = _PROMPTS[key]
                    module = importlib.import_module(module_name)
                    saved[(module_name, attr)] = getattr(module, attr)
                    candidate = Path(temporary) / f"{key}.md"
                    candidate.write_text(str(content))
                    setattr(module, attr, candidate)

                session = Session(f"replay-{case['id']}")
                session.code_map = demo_map()
                session.memory = MemoryStore(Path(temporary) / "memory.json")
                session.code_fact_scope = "demo:code"
                session.decision_scope = f"{session.room}:decisions"
                for claim, source, subject in _SEED:
                    session.memory.add_fact(claim, source, scope="demo:code", subject=subject)
                messages: list[dict] = []
                updates: list[dict] = []
                original_broadcast = session.broadcast
                original_push = session.push_update

                async def capture(message):
                    payload = protocol.dump(message)
                    protocol.parse(payload)
                    messages.append(payload)
                    await original_broadcast(message)

                def capture_update(text, urgent=False):
                    updates.append({"text": text, "urgent": bool(urgent)})
                    original_push(text, urgent=urgent)

                session.broadcast = capture
                session.push_update = capture_update
                listener = Listener(session, debounce_seconds=0)
                line_latencies = []
                started = time.monotonic()
                try:
                    for line in case["lines"]:
                        if not isinstance(line, dict) or not str(line.get("text") or "").strip():
                            raise ValueError(f"invalid line in case {case['id']}")
                        speaker = str(line.get("speaker") or "Preet")
                        text = str(line["text"])
                        session.history.append({"speaker": speaker, "text": text})
                        line_start = time.monotonic()
                        listener.submit(speaker, text)
                        await asyncio.wait_for(listener.wait_idle(), timeout_per_line)
                        line_latencies.append(round((time.monotonic() - line_start) * 1000))
                finally:
                    await listener.close()
                draws = [item for item in messages if item.get("type") == "draw"]
                result = {
                    "id": case["id"], "messages": messages,
                    "draws": draws,
                    "tasks": [item for item in messages if item.get("type") == "task"],
                    "events": [item for item in messages if item.get("type") == "agent_event"],
                    "topics": [item for item in messages if item.get("type") == "topic"],
                    "updates": updates,
                    "file_paths": sorted({str(item.get("label")) for item in draws
                                          if item.get("op") == "add_box"
                                          and "/" in str(item.get("label") or "")}),
                    "line_latency_ms": line_latencies,
                    "latency_ms": round((time.monotonic() - started) * 1000),
                }
                if usage.tokens:
                    result["tokens"] = sum(usage.tokens)
                return result
            finally:
                for (module_name, attr), old in saved.items():
                    setattr(importlib.import_module(module_name), attr, old)
                logger.removeHandler(usage)
                logger.setLevel(old_level)


async def replay_cases(cases: list[dict], *, prompt_override: dict[str, str] | None = None,
                       timeout_per_line: float = 60) -> list[dict]:
    return [await replay_case(case, prompt_override=prompt_override,
                              timeout_per_line=timeout_per_line) for case in cases]


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay scripted meetings on Crusoe")
    parser.add_argument("--split", choices=("train", "validation", "locked_test"), required=True)
    parser.add_argument("--allow-locked", action="store_true")
    parser.add_argument("--prep-only", action="store_true",
                        help="one Prep call per meeting; no Listener/Diagrammer calls")
    parser.add_argument("--prompt", action="append", default=[], metavar="AGENT=PATH")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cases = load_cases(args.split, include_locked=args.allow_locked)
    if not cases:
        parser.error("no cases in selected split")
    overrides = {}
    for item in args.prompt:
        name, sep, path = item.partition("=")
        if not sep or name.removesuffix(".md") not in ({"prep"} if args.prep_only else _PROMPTS):
            parser.error(f"invalid --prompt {item!r}")
        overrides[name] = Path(path).read_text()
    if args.prep_only:
        observed = asyncio.run(_replay_prep_cases(cases, overrides))
    else:
        observed = asyncio.run(replay_cases(cases, prompt_override=overrides))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(observed, indent=2) + "\n")
    print(f"Replayed {len(observed)} {args.split} meetings to {args.output}")


async def _replay_prep_cases(cases: list[dict], overrides: dict[str, str]) -> list[dict]:
    return [await replay_prep_case(case, prompt_override=overrides) for case in cases]


if __name__ == "__main__":
    main()
