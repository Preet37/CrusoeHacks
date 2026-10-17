"""Debounced transcript listener. Exactly one model request runs at a time."""

import asyncio
import inspect
import json
import logging
import math
import re
from collections import deque
from pathlib import Path
from uuid import uuid4

import config
import llm
from agents.conductor import Conductor, _wants_visual
from agents.diagrammer import StaleBoardError, emit_event

log = logging.getLogger(__name__)
PROMPT = Path(__file__).parent / "prompts" / "listener.md"
MOMENT_TYPES = {"system_fact", "decision", "question", "claim", "task"}
_RENAME = re.compile(r"\brename\s+([a-z][a-z0-9_-]*)\s+to\s+(.+?)[.!?]?\s*$", re.I)
_TOPIC_ID = re.compile(r"t_[a-z][a-z0-9]*(?:_[a-z0-9]+)*\Z")
_CONTRADICTION = re.compile(r"\b(actually|instead|scratch that|not that|change (?:it|that) to|replace (?:it|that) with)\b", re.I)
_TOPIC_SWITCH = re.compile(r"(?:different topic\s*[:,]?\s*(?:let(?: us|['’]s) talk about\s+)?|switching to\s+|back to\s+)([^.!?;:]{1,60})", re.I)
_DECISION = re.compile(r"\b(?:we (?:have )?decided|we agreed|decision is|let['’]s (?:move|migrate|switch|use|adopt)|will own (?:it|this))\b", re.I)
_VISUAL_HINTS = {"architecture", "sequence", "chart", "mockup", "table", "code", "3d"}


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


def _parse_batch(raw: str) -> tuple[list[dict], dict | None]:
    """Read the v2 topic envelope while accepting legacy moment arrays."""
    moments = _parse_moments(raw)
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        return moments, None
    if not isinstance(value, dict):
        return moments, None
    return moments, {
        "topic_id": value.get("topic_id"),
        "title": value.get("title"),
        "topic_changed": value.get("topic_changed"),
    }


def _slug(title: str) -> str:
    stem = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:40]
    return f"t_{stem or 'general'}"


def _probability(value) -> float | None:
    """Keep missing or malformed scores distinct from an explicit zero."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        score = float(value)
    except (OverflowError, ValueError):
        return None
    return score if math.isfinite(score) and 0 <= score <= 1 else None


def _confidence(value) -> float:
    return _probability(value) or 0.0


def _noul(decision: dict | None, key: str) -> float | None:
    answer = decision.get(key) if isinstance(decision, dict) else None
    if not isinstance(answer, dict) or answer.get("type") != "noul":
        return None
    return _probability(answer.get("noul"))


def _choice(decision: dict | None, key: str) -> tuple[str | None, float]:
    answer = decision.get(key) if isinstance(decision, dict) else None
    if not isinstance(answer, dict) or answer.get("type") != "choice":
        return None, 0.0
    value = answer.get("choice")
    if not isinstance(value, str):
        return None, 0.0
    return value, _confidence(answer.get("confidence"))


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
        self._jobs: dict[asyncio.Task, tuple[str | None, list[dict]]] = {}
        self._terminals: set[asyncio.Task] = set()
        self._topics: dict[str, str] = dict(getattr(session, "topic_catalog", {}) or {})
        active = getattr(session, "active_topic", None)
        self._active_topic: str | None = active.get("topic_id") if isinstance(active, dict) else None
        self._closed = False

    def submit(self, speaker: str, text: str, *, decision: dict | None = None) -> None:
        """Queue a final line; optional reflex answers may skip it or preempt stale jobs."""
        if self._closed:
            return
        text = text.strip()
        if not text:
            return
        # Echoes need both a confident reflex answer and the Session's lexical
        # guard. A mistaken single model score must not discard human input.
        echo_score = _noul(decision, "is_echo")
        if echo_score is not None and echo_score >= 0.9:
            is_echo = getattr(self.session, "is_echo", None)
            if callable(is_echo):
                try:
                    if is_echo(text):
                        return
                except Exception:
                    log.exception("echo check failed; keeping transcript line")
        topic_score = _noul(decision, "topic_changed")
        topic_switch = topic_score is not None and topic_score >= 0.7
        visual, visual_confidence = _choice(decision, "wants_visual")
        visual_hint = visual if visual in _VISUAL_HINTS and visual_confidence >= 0.7 else None
        kind, kind_confidence = _choice(decision, "kind")
        if (kind in {"none", "small_talk"} and kind_confidence >= 0.8
                and topic_score is not None and topic_score < 0.7
                and visual == "none" and visual_confidence >= 0.7
                and not _CONTRADICTION.search(text)):
            return
        line = {"speaker": speaker, "text": text, "line_id": f"ln_{uuid4().hex[:12]}"}
        if visual_hint is not None:
            line["wants_visual"] = visual_hint
        self._pending.append(line)
        self._recent.append(line)
        if topic_switch or _TOPIC_SWITCH.search(text) or _CONTRADICTION.search(text):
            self._cancel_jobs(contradiction=True)
        self._pending_changed.set()
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run())

    async def wait_idle(self) -> None:
        """Wait until all submitted lines have been processed (useful for replay/tests)."""
        while self._worker is not None or self._jobs or self._terminals:
            worker = self._worker
            if worker is not None:
                await worker
            jobs = list(self._jobs)
            if jobs:
                await asyncio.gather(*jobs, return_exceptions=True)
                for job in jobs:
                    self._job_done(job)
            terminal_tasks = list(self._terminals)
            if terminal_tasks:
                await asyncio.gather(*terminal_tasks, return_exceptions=True)
                self._terminals.difference_update(terminal_tasks)

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
        jobs = list(self._jobs)
        for job in jobs:
            job.cancel()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
        self._jobs.clear()
        terminal_tasks = list(self._terminals)
        if terminal_tasks:
            await asyncio.gather(*terminal_tasks, return_exceptions=True)
        self._terminals.clear()
        self._pending.clear()
        self._recent.clear()
        self._previous.clear()
        self._pending_changed.clear()

    def _cancel_jobs(self, *, topic_id: str | None = None,
                     contradiction: bool = False) -> None:
        for job, (job_topic, _) in list(self._jobs.items()):
            if not job.done() and (contradiction or (topic_id is not None and job_topic != topic_id)):
                job.cancel()

    def _resolve_topic(self, meta: dict | None) -> tuple[str | None, str | None, bool]:
        if meta is None:
            return None, None, False
        title = str(meta.get("title") or "General").strip()[:80] or "General"
        proposed = str(meta.get("topic_id") or "").strip().lower()
        if not _TOPIC_ID.fullmatch(proposed):
            proposed = _slug(title)
        if (proposed in self._topics
                and self._topics[proposed].casefold() != title.casefold()
                and meta.get("topic_changed") is True):
            proposed = _slug(title)
        for known_id, known_title in self._topics.items():
            if known_title.casefold() == title.casefold():
                proposed = known_id
                break
        if proposed not in self._topics:
            self._topics[proposed] = title
        else:
            title = self._topics[proposed]
        changed = proposed != self._active_topic
        self._active_topic = proposed
        self.session.topic_catalog = self._topics
        return proposed, title, changed

    def _job_done(self, job: asyncio.Task) -> None:
        metadata = self._jobs.pop(job, None)
        if metadata is None:
            return
        _, moments = metadata
        failed = job.cancelled()
        if not failed:
            failed = job.exception() is not None
        if failed:
            for moment in moments:
                try:
                    self._previous.remove(moment)
                except ValueError:
                    pass

    async def _finish_job(self, job: asyncio.Task, event_id: str,
                          moment_count: int) -> None:
        try:
            await job
        except (StaleBoardError, asyncio.CancelledError):
            await emit_event(self.session, "Listener", config.MODEL_FAST,
                             "cancelled", "downstream job cancelled", event_id=event_id)
        except Exception as exc:
            await emit_event(self.session, "Listener", config.MODEL_FAST,
                             "failed", str(exc)[:160], event_id=event_id)
        else:
            await emit_event(self.session, "Listener", config.MODEL_FAST, "done",
                             f"{moment_count} moment(s)", event_id=event_id)

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
                    visible_lines = [{"speaker": line["speaker"], "text": line["text"]}
                                     for line in lines]
                    messages = [
                        {"role": "system", "content": PROMPT.read_text()},
                        {"role": "user", "content": json.dumps({
                            "new_lines": visible_lines,
                            "recent_lines": [{"speaker": line["speaker"], "text": line["text"]}
                                             for line in self._recent],
                            "previous_moments": list(self._previous),
                            "active_topic": getattr(self.session, "active_topic", None),
                            "known_topics": [{"topic_id": topic_id, "title": title}
                                             for topic_id, title in self._topics.items()],
                        })},
                    ]
                    raw = await llm.chat(config.MODEL_FAST, messages, max_tokens=1600,
                                         extra_body={"chat_template_kwargs": {"enable_thinking": False}})
                    try:
                        moments, topic_meta = _parse_batch(raw)
                    except (ValueError, json.JSONDecodeError):
                        # A reasoning-heavy Nano reply can hit its output cap
                        # mid-JSON. Retry once only when parsing actually fails.
                        repaired = await llm.chat(config.MODEL_FAST, messages + [
                            {"role": "assistant", "content": raw},
                            {"role": "user", "content": "Return the complete JSON moment array only."},
                        ], max_tokens=2400)
                        moments, topic_meta = _parse_batch(repaired)
                    # Short imperative edits are often classified as tasks by
                    # Nano. If the named component is already on this board,
                    # make the requested diagram change a routed system fact.
                    for line in lines:
                        match = _RENAME.search(line["text"].strip())
                        if match is None or match.group(1).lower() not in self.session.board.boxes:
                            continue
                        source, target = match.group(1).lower(), match.group(2).strip()
                        if not any(m["type"] == "system_fact" and source in m["text"].lower()
                                   and target.lower() in m["text"].lower() for m in moments):
                            moments.append({"type": "system_fact",
                                            "text": f"Rename {source} to {target}",
                                            "entities": [source]})
                    # Explicit human decisions must survive a zero-moment or
                    # misclassified Nano reply. Preserve the exact source text.
                    if not any(m["type"] == "decision" for m in moments):
                        for line in lines:
                            if _DECISION.search(line["text"]):
                                moments.append({"type": "decision", "text": line["text"][:500],
                                                "entities": []})
                    seen = {(m["type"], m["text"].casefold()) for m in self._previous}
                    moments = [m for m in moments if (m["type"], m["text"].casefold()) not in seen]
                    for line in lines:
                        switch = _TOPIC_SWITCH.search(line["text"])
                        if switch:
                            title = switch.group(1).strip(" ,")
                            known = [(key, value) for key, value in self._topics.items()
                                     if title.casefold() == value.casefold()
                                     or re.search(r"\b" + re.escape(title.casefold()) + r"\b", value.casefold())]
                            topic_meta = {"topic_id": known[0][0] if len(known) == 1 else _slug(title),
                                          "title": known[0][1] if len(known) == 1 else title,
                                          "topic_changed": True}
                    topic_id, title, topic_changed = self._resolve_topic(topic_meta)
                    if topic_id is not None:
                        if topic_changed:
                            self._cancel_jobs(topic_id=topic_id)
                        set_topic = getattr(self.session, "set_topic", None)
                        if callable(set_topic):
                            result = set_topic(topic_id, title)
                            if inspect.isawaitable(result):
                                await result
                        else:
                            self.session.active_topic = {"topic_id": topic_id, "title": title}
                            await self.session.broadcast({
                                "type": "topic", "topic_id": topic_id, "title": title,
                                "active": True, "changed": topic_changed,
                            })
                    if moments or (topic_id is not None and topic_changed) or _wants_visual(moments, lines):
                        job = asyncio.create_task(self.conductor.handle(
                            self.session, moments, parent_id=event_id,
                            topic_id=topic_id, title=title,
                            topic_changed=topic_changed, source_lines=lines[-12:],
                        ))
                        self._jobs[job] = (topic_id, moments)
                        job.add_done_callback(self._job_done)
                        terminal = asyncio.create_task(self._finish_job(job, event_id,
                                                                        len(moments)))
                        self._terminals.add(terminal)
                        terminal.add_done_callback(self._terminals.discard)
                        self._previous.extend(moments)
                    else:
                        await emit_event(self.session, "Listener", config.MODEL_FAST, "done",
                                         "0 moment(s)", event_id=event_id)
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
