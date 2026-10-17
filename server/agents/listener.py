"""Debounced transcript listener. Exactly one model request runs at a time."""

import asyncio
import json
import logging
from collections import deque
from pathlib import Path

import config
import llm
from agents.conductor import Conductor
from agents.diagrammer import StaleBoardError, emit_event

log = logging.getLogger(__name__)
PROMPT = Path(__file__).parent / "prompts" / "listener.md"
MOMENT_TYPES = {"system_fact", "decision", "question", "claim", "task"}


def _parse_moments(raw: str) -> list[dict]:
    """Accept strict JSON plus the occasional fenced/model-wrapped JSON array."""
    if not raw:
        return []
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("["), text.rfind("]")
        if start < 0 or end < start:
            raise ValueError("Listener returned no JSON moment array")
        value = json.loads(text[start : end + 1])
    if isinstance(value, dict):
        value = value.get("moments", [])
    if not isinstance(value, list):
        raise ValueError("Listener moments must be a JSON list")
    moments = []
    for item in value[:12]:
        if not isinstance(item, dict) or item.get("type") not in MOMENT_TYPES:
            continue
        body = str(item.get("text", "")).strip()
        if not body:
            continue
        entities = item.get("entities", [])
        if not isinstance(entities, list):
            entities = []
        moments.append({"type": item["type"], "text": body[:500],
                        "entities": [str(e)[:80] for e in entities[:12]]})
    return moments


class Listener:
    def __init__(self, session, conductor=None, debounce_seconds: float = 0.25):
        self.session = session
        self.conductor = conductor or Conductor()
        self.debounce_seconds = debounce_seconds
        self._pending: deque[dict] = deque()
        self._recent: deque[dict] = deque(maxlen=16)
        self._previous: deque[dict] = deque(maxlen=24)
        self._pending_changed = asyncio.Event()
        self._worker: asyncio.Task | None = None
        self._closed = False

    def submit(self, speaker: str, text: str) -> None:
        """Queue a final transcript line immediately; never start concurrent calls."""
        if self._closed:
            return
        text = text.strip()
        if not text:
            return
        line = {"speaker": speaker, "text": text}
        self._pending.append(line)
        self._recent.append(line)
        self._pending_changed.set()
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run())

    async def wait_idle(self) -> None:
        """Wait until all submitted lines have been processed (useful for replay/tests)."""
        while self._worker is not None:
            worker = self._worker
            await worker
            if self._worker is worker:
                break

    async def close(self) -> None:
        """Cancel work and discard all buffered lines; create a new Listener after reset."""
        self._closed = True
        worker = self._worker
        if worker and not worker.done():
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
        self._pending.clear()
        self._recent.clear()
        self._previous.clear()
        self._pending_changed.clear()

    async def _run(self) -> None:
        try:
            while self._pending:
                # Wait for a quiet window after the most recent final line.
                # New lines during a model call remain queued for the next pass.
                self._pending_changed.clear()
                while self.debounce_seconds > 0:
                    try:
                        await asyncio.wait_for(self._pending_changed.wait(),
                                               timeout=self.debounce_seconds)
                        self._pending_changed.clear()
                    except TimeoutError:
                        break
                lines = list(self._pending)
                self._pending.clear()
                event_id = await emit_event(self.session, "Listener", config.MODEL_FAST,
                                            "started", f"{len(lines)} transcript line(s)")
                try:
                    messages = [
                        {"role": "system", "content": PROMPT.read_text()},
                        {"role": "user", "content": json.dumps({
                            "new_lines": lines,
                            "recent_lines": list(self._recent),
                            "previous_moments": list(self._previous),
                        })},
                    ]
                    moments = _parse_moments(await llm.chat(config.MODEL_FAST, messages,
                                                            max_tokens=800))
                    seen = {(m["type"], m["text"].casefold()) for m in self._previous}
                    moments = [m for m in moments if (m["type"], m["text"].casefold()) not in seen]
                    if moments:
                        await self.conductor.handle(self.session, moments, parent_id=event_id)
                        self._previous.extend(moments)
                    await emit_event(self.session, "Listener", config.MODEL_FAST, "done",
                                     f"{len(moments)} moment(s)", event_id=event_id)
                except StaleBoardError:
                    await emit_event(self.session, "Listener", config.MODEL_FAST,
                                     "cancelled", "board changed during drawing",
                                     event_id=event_id)
                    # This batch belongs to the old board; newer final lines in
                    # _pending still need a fresh Listener pass.
                except asyncio.CancelledError:
                    await emit_event(self.session, "Listener", config.MODEL_FAST,
                                     "cancelled", "cancelled", event_id=event_id)
                    raise
                except Exception as exc:
                    log.exception("Listener failed")
                    await emit_event(self.session, "Listener", config.MODEL_FAST,
                                     "failed", str(exc)[:160], event_id=event_id)
        finally:
            self._worker = None
