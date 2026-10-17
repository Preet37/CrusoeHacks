"""M1 routing from extracted moments to specialist jobs."""

import asyncio
import hashlib
import inspect
import json
import logging
import re
from pathlib import Path

import config
import llm
from agents.code_analyst import CodeAnalyst, resolved_query
from agents.diagrammer import Diagrammer, StaleBoardError, emit_event
from agents.fact_checker import FactChecker
from agents.job_budget import JobBudget
from agents.task_planner import TaskPlanner

log = logging.getLogger(__name__)
ROUTER_PROMPT = Path(__file__).parent / "prompts" / "conductor.md"
_CODE_IMPACT = re.compile(
    r"\b(impact|touch|affected|changing|change|switch|migrate|move|replace|"
    r"break|dependenc\w*|which files|what files)\b", re.I)
_VISUAL_HINTS = {"architecture", "sequence", "chart", "mockup", "table", "code", "3d"}
_VISUAL_NOUN = re.compile(
    r"\b(chart|graph|plot|table|mockup|mock-up|wireframe|screen|ui|"
    r"sequence|flow|flowchart|diagram|process|timeline|comparison|measurements?|"
    r"robot(?:ic)?\s+arm|three-joint\s+arm|3d|three-dimensional)\b", re.I)
_VISUAL_REQUEST = re.compile(
    r"\b(draw|show|create|make|render|sketch|plot|build|generate|"
    r"visuali[sz]e|mock up|walk me through|map out|put .{0,60} in a table)\b", re.I)
_CHART_VERB = re.compile(r"\bchart\s+(?:the|these|this|our|my|a)\b", re.I)
_METRIC = re.compile(r"\b(p\d{1,3}|latency|throughput|response time|duration|"
                     r"load time|error rate|milliseconds?|ms|seconds?|percent|%)\b", re.I)
_TREND = re.compile(r"\b(from|to|went|after|before|versus|vs|improved|"
                    r"reduced|increased|decreased|dropped|rose)\b", re.I)
_SCREEN_QUESTION = re.compile(
    r"\b(what|how)\b.{0,80}\b(screen|ui|interface|page)\b.{0,60}"
    r"\b(look(?: like)?|appear|design)\b", re.I)
_PRESENT_CLAIM = re.compile(
    r"\b(already|currently|still|no longer|instead of|does not|doesn't|"
    r"has switched|now uses?)\b", re.I)
_EXPLICIT_DECISION = re.compile(
    r"\b(we decided|we agreed|decision is|we will|agreed to|"
    r"let's (?:move|switch|migrate|replace|adopt))\b", re.I)
_EXPLICIT_CURRENT = re.compile(
    r"\b(already|currently|still|no longer|instead of|does not|doesn't|"
    r"has switched|now uses?)\b", re.I)


def _measured_comparison(text: str) -> bool:
    if not (_METRIC.search(text) and _TREND.search(text)):
        return False
    # p95 and v1/v2 are labels, not measurement values. Require two actual
    # numbers so a one-off latency fact does not demand a chart.
    values = re.findall(r"\b\d+(?:\.\d+)?\b",
                        re.sub(r"\b(?:p\d{1,3}|v\d+)\b", "", text, flags=re.I))
    return len(values) >= 2


def _wants_visual(moments: list[dict], source_lines: list[dict]) -> bool:
    if any(line.get("wants_visual") in _VISUAL_HINTS for line in source_lines):
        return True
    texts = [str(line.get("text") or "") for line in source_lines]
    texts.extend(str(moment.get("text") or "") for moment in moments)
    return any((_VISUAL_NOUN.search(text) and _VISUAL_REQUEST.search(text))
               or _CHART_VERB.search(text) or _measured_comparison(text)
               or _SCREEN_QUESTION.search(text) for text in texts)


def _specialist_moments(moments: list[dict], source_lines: list[dict]) -> list[dict]:
    """Rescue explicit source-line claims/decisions misclassified by Listener.

    These are specialist-only labels over the user's own words, not fabricated
    system facts, and leave the Diagrammer's original moment list unchanged.
    """
    selected = list(moments)
    decision_lines = [str(line.get("text") or "") for line in source_lines
                      if _EXPLICIT_DECISION.search(str(line.get("text") or ""))]
    if decision_lines:
        # The source line is the durable decision record. Nano sometimes
        # extracts only its owner clause, losing the actual change proposal.
        selected = [moment for moment in selected if moment.get("type") != "decision"]
        selected.extend({"type": "decision", "text": text[:500], "entities": []}
                        for text in dict.fromkeys(decision_lines))
    if decision_lines and len(source_lines) == 1:
        # Nano sometimes returns a speculative `claim` alongside a decision
        # about a future migration. That is not a claim about live code. Keep
        # only claims with explicit present-tense assertion language.
        selected = [moment for moment in selected
                    if moment.get("type") != "claim"
                    or _EXPLICIT_CURRENT.search(str(moment.get("text") or ""))]
    kinds = {moment.get("type") for moment in selected}
    for line in source_lines[-12:]:
        text = str(line.get("text") or "").strip()[:500]
        if not text:
            continue
        if "claim" not in kinds and _PRESENT_CLAIM.search(text):
            selected.append({"type": "claim", "text": text, "entities": []})
            kinds.add("claim")
        if "decision" not in kinds and _EXPLICIT_DECISION.search(text):
            selected.append({"type": "decision", "text": text, "entities": []})
            kinds.add("decision")
    return selected


class Conductor:
    def __init__(self, diagrammer=None, code_analyst=None,
                 fact_checker=None, task_planner=None):
        self.diagrammer = diagrammer or Diagrammer()
        self.code_analyst = code_analyst or CodeAnalyst()
        self.fact_checker = fact_checker or FactChecker()
        self.task_planner = task_planner or TaskPlanner()

    async def _route_m2(self, session, moments: list[dict], budgets: list[JobBudget],
                        parent_id: str) -> dict[int, set[str]]:
        """Use Super for M2 routing; deterministic required routes survive bad JSON."""
        code_map = getattr(session, "code_map", None)
        memory = getattr(session, "memory", None)
        routes: dict[int, set[str]] = {}
        for index, moment in enumerate(moments):
            kind = moment.get("type")
            text = str(moment.get("text") or "")
            selected = set()
            if code_map is not None and kind in {"question", "task", "decision"} and _CODE_IMPACT.search(text):
                selected.add("code_analyst")
            if memory is not None and kind == "claim":
                selected.add("fact_checker")
            if (code_map is not None or memory is not None) and kind == "decision":
                selected.add("task_planner")
            if selected:
                routes[index] = selected
        if not routes:
            return routes

        budget = budgets[min(routes)]
        prompt_input = json.dumps({"moments": moments[:12],
                                   "available": {"code_map": code_map is not None,
                                                 "memory": memory is not None}})
        if not budget.reserve_job():
            return routes
        tokens = budget.reserve_model(ROUTER_PROMPT.read_text() + prompt_input, 420)
        if not tokens:
            return routes
        event_id = await emit_event(session, "Conductor router", config.MODEL_SMART,
                                    "started", f"route {len(routes)} M2 moment(s)",
                                    parent_id=parent_id)
        try:
            raw = await llm.chat(config.MODEL_SMART, [
                {"role": "system", "content": ROUTER_PROMPT.read_text()},
                {"role": "user", "content": prompt_input},
            ], max_tokens=tokens, response_format={"type": "json_object"},
                extra_body={"chat_template_kwargs": {"enable_thinking": False}})
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and isinstance(parsed.get("routes"), list):
                for item in parsed["routes"][:12]:
                    if not isinstance(item, dict) or not isinstance(item.get("index"), int):
                        continue
                    index = item["index"]
                    if index < 0 or index >= len(moments) or not isinstance(item.get("agents"), list):
                        continue
                    kind = moments[index].get("type")
                    allowed = set()
                    if code_map is not None and kind in {"question", "task", "decision"}:
                        allowed.add("code_analyst")
                    if memory is not None and kind == "claim":
                        allowed.add("fact_checker")
                    if (code_map is not None or memory is not None) and kind == "decision":
                        allowed.add("task_planner")
                    routes.setdefault(index, set()).update(
                        agent for agent in item["agents"] if isinstance(agent, str) and agent in allowed)
            await emit_event(session, "Conductor router", config.MODEL_SMART,
                             "done", f"{sum(map(len, routes.values()))} route(s)",
                             event_id=event_id, parent_id=parent_id)
        except asyncio.CancelledError:
            await emit_event(session, "Conductor router", config.MODEL_SMART,
                             "cancelled", "cancelled", event_id=event_id,
                             parent_id=parent_id)
            raise
        except Exception as exc:
            log.warning("M2 router failed; using required routes: %s", exc)
            await emit_event(session, "Conductor router", config.MODEL_SMART,
                             "failed", str(exc)[:160], event_id=event_id,
                             parent_id=parent_id)
        return routes

    async def _run_m2(self, session, moments: list[dict], source_lines: list[dict],
                      parent_id: str) -> list[dict]:
        budgets = [JobBudget() for _ in moments]
        routes = await self._route_m2(session, moments, budgets, parent_id)
        if not routes:
            return []
        calls = []
        seen_jobs = set()
        for index, agents in routes.items():
            moment, budget = moments[index], budgets[index]
            if "code_analyst" in agents:
                query, resolved_paths = resolved_query(
                    getattr(session, "code_map", None), moment, source_lines)
                key = ("code_analyst", resolved_paths or query.casefold())
                if key not in seen_jobs:
                    seen_jobs.add(key)
                    calls.append(self.code_analyst.handle(session, moment,
                                                           parent_id=parent_id, budget=budget,
                                                           query_override=query))
            if "fact_checker" in agents:
                key = ("fact_checker", str(moment.get("text") or "").casefold())
                if key not in seen_jobs:
                    seen_jobs.add(key)
                    calls.append(self.fact_checker.handle(session, moment,
                                                           parent_id=parent_id, budget=budget))
            if "task_planner" in agents:
                key = ("task_planner", str(moment.get("text") or "").casefold())
                if key not in seen_jobs:
                    seen_jobs.add(key)
                    calls.append(self.task_planner.handle(session, moment,
                                                           parent_id=parent_id, budget=budget,
                                                           source_lines=source_lines))
        results = await asyncio.gather(*calls, return_exceptions=True)
        good = []
        for result in results:
            if isinstance(result, BaseException):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                log.error("M2 specialist failed: %s", result)
            elif isinstance(result, dict):
                good.append(result)
        # All specialist findings become one sourced update. The Fact Checker
        # marks it urgent, so the mouth can deliver the correction promptly.
        summaries = [str(result["summary"]) for result in good if result.get("summary")]
        if summaries:
            text = " ".join(summaries)[:1200]
            urgent = any(result.get("urgent") for result in good)
            push_update = getattr(session, "push_update", None)
            if callable(push_update):
                try:
                    emitted = push_update(text, urgent=urgent)
                except TypeError:
                    emitted = push_update(text)
                if inspect.isawaitable(emitted):
                    await emitted
        return good

    async def _highlight_impact(self, session, findings: list[dict], *,
                                expected_board=None, expected_revision=None) -> None:
        paths = list(dict.fromkeys(str(path) for result in findings
                                   for path in result.get("highlights", [])
                                   if isinstance(path, str)))[:6]
        board = getattr(session, "board", None)
        if not paths or board is None:
            return
        lock = getattr(session, "_diagrammer_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            session._diagrammer_lock = lock
        async with lock:
            # A user clear/reset during Code Analyst's model call invalidates
            # that answer's board context. Do not revive its file boxes.
            if (expected_board is not None and session.board is not expected_board) or (
                expected_revision is not None and board.revision != expected_revision
            ):
                return
            expected_revision = board.revision
            ids = []

            async def publish(op):
                nonlocal expected_revision
                if session.board is not board or board.revision != expected_revision:
                    raise StaleBoardError("board changed during code impact drawing")
                draw = board.apply(op)
                expected_revision = board.revision
                await Diagrammer._publish_draw(session, board, draw, expected_revision)
                if session.board is not board or board.revision != expected_revision:
                    raise StaleBoardError("board changed during code impact broadcast")

            for index, path in enumerate(paths):
                file_id = "file_" + hashlib.sha1(path.encode()).hexdigest()[:12]
                existing = next((box_id for box_id, box in board.boxes.items()
                                 if box.get("label") == path), None)
                if existing is None and file_id not in board.boxes:
                    op = {"op": "add_box", "id": file_id, "label": path,
                          "x": 30 + (index % 3) * 320,
                          "y": 650 + (index // 3) * 150, "kind": "note"}
                    if board.active_frame in board.shapes:
                        op["frame"] = board.active_frame
                    await publish(op)
                ids.append(existing or file_id)
            # Keep the existing architecture component lit alongside the
            # concrete file boxes when it clearly names an affected path.
            ids.extend(box_id for box_id, box in board.boxes.items()
                       if box_id not in ids and any(
                           len(str(box.get("label") or "")) >= 3
                           and str(box["label"]).casefold() in path.casefold()
                           for path in paths))
            ids = list(dict.fromkeys(ids))[:12]
            if ids and not set(ids) <= board.highlighted:
                await publish({"op": "highlight", "ids": ids, "on": True})

    async def handle(self, session, moments: list[dict], parent_id: str | None = None,
                     *, topic_id: str | None = None, title: str | None = None,
                     topic_changed: bool = False,
                     source_lines: list[dict] | None = None) -> None:
        source_lines = (source_lines or [])[-12:]
        source_ids = ",".join(str(line.get("line_id", "")) for line in source_lines)
        detail = f"route {len(moments)} moment(s)"
        if topic_id:
            detail += f" topic={topic_id}"
        if source_ids:
            detail += f" lines={source_ids}"
        event_id = await emit_event(session, "Conductor", config.MODEL_SMART, "started",
                                    detail[:200], parent_id=parent_id)
        source_board = getattr(session, "board", None)
        board_revision = source_board.revision if source_board is not None else None
        try:
            facts = [m for m in moments if m.get("type") == "system_fact"]
            if (len(source_lines) == 1
                    and _EXPLICIT_DECISION.search(str(source_lines[0].get("text") or ""))
                    and not _EXPLICIT_CURRENT.search(str(source_lines[0].get("text") or ""))):
                # A migration plan is not evidence that the live system has
                # migrated; extracted system facts from this line are unsafe.
                facts = []
            visual_request = _wants_visual(moments, source_lines)
            # Preserve the Listener's classifications. A requested chart or
            # sequence is actionable even if the line was tagged task/question;
            # a minimal task lets Diagrammer read source_lines when no moment
            # survived extraction, without inventing a system fact.
            draw_moments = (moments or [{"type": "task",
                                         "text": "Create the visual requested in source_lines.",
                                         "entities": []}]) if visual_request else facts
            specialist_moments = _specialist_moments(moments, source_lines)
            # A present-tense implementation assertion may be false even if
            # Listener called it system_fact. Check sourced code memory before
            # drawing that assertion onto the authoritative board.
            defer_claim_draw = bool(
                facts and getattr(session, "memory", None) is not None
                and any(_PRESENT_CLAIM.search(str(line.get("text") or ""))
                        for line in source_lines)
            )

            async def draw_job():
                nonlocal board_revision
                if topic_id is None:
                    if visual_request:
                        result = await self.diagrammer.handle(session, draw_moments,
                                                               parent_id=event_id,
                                                               source_lines=source_lines)
                    else:
                        result = await self.diagrammer.handle(session, draw_moments,
                                                               parent_id=event_id)
                else:
                    result = await self.diagrammer.handle(session, draw_moments,
                                                           parent_id=event_id,
                                                           topic_id=topic_id, title=title,
                                                           topic_changed=topic_changed,
                                                           source_lines=source_lines)
                if source_board is not None and session.board is source_board:
                    board_revision = source_board.revision
                return result

            pending = []
            should_draw = bool(draw_moments or (topic_id is not None and topic_changed))
            if should_draw and not defer_claim_draw:
                pending.append(asyncio.create_task(draw_job()))
            m2_task = None
            if specialist_moments and (getattr(session, "code_map", None) is not None
                            or getattr(session, "memory", None) is not None):
                m2_task = asyncio.create_task(self._run_m2(session, specialist_moments,
                                                           source_lines, event_id))
                pending.append(m2_task)
            try:
                if pending:
                    await asyncio.gather(*pending)
            except BaseException:
                for task in pending:
                    if not task.done():
                        task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                raise
            findings = m2_task.result() if m2_task else []
            contradicted = any(result.get("urgent") for result in findings)
            if defer_claim_draw and should_draw:
                if contradicted:
                    # Keep topic navigation but never publish the rejected
                    # system fact as a box, arrow, or rich shape.
                    if topic_id is not None and topic_changed:
                        await self.diagrammer.handle(session, [], parent_id=event_id,
                                                      topic_id=topic_id, title=title,
                                                      topic_changed=True,
                                                      source_lines=source_lines)
                        if source_board is not None and session.board is source_board:
                            board_revision = source_board.revision
                else:
                    await draw_job()
            if findings:
                await self._highlight_impact(session, findings,
                                             expected_board=source_board,
                                             expected_revision=board_revision)
            await emit_event(session, "Conductor", config.MODEL_SMART, "done",
                             f"routed {len(facts)} system fact(s)"
                             + (" and visual request" if visual_request else ""), event_id=event_id,
                             parent_id=parent_id)
        except StaleBoardError:
            await emit_event(session, "Conductor", config.MODEL_SMART, "cancelled",
                             "board changed during drawing", event_id=event_id,
                             parent_id=parent_id)
            raise
        except asyncio.CancelledError:
            await emit_event(session, "Conductor", config.MODEL_SMART, "cancelled",
                             "cancelled", event_id=event_id, parent_id=parent_id)
            raise
        except Exception as exc:
            log.exception("Conductor failed")
            await emit_event(session, "Conductor", config.MODEL_SMART, "failed",
                             str(exc)[:160], event_id=event_id, parent_id=parent_id)
            raise
