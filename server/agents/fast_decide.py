"""Typed, non-verbal decisions from JEV, with a Crusoe Nemotron fallback.

Both backends return the same TypeSafe-style answer mapping. Callers choose
their own probability thresholds; this module never drops a meeting sentence.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import time
from pathlib import Path
from typing import Any, Mapping

import httpx

import config
import llm

log = logging.getLogger(__name__)

JEV_URL = "https://api.typesafe.ai/v1"
JEV_TIMEOUT_S = 0.9
NANO_TIMEOUT_S = 4.0
NANO_MAX_TOKENS = 900
HEALTH_TTL_S = 60.0
HEALTH_FAILURE_TTL_S = 10.0
MAX_QUESTIONS = 128
MAX_REQUEST_BYTES = 120_000
LATENCY_LOG = Path(__file__).resolve().parents[1] / "learning" / "runs" / "latency.jsonl"

TURN_QUESTIONS: dict[str, dict[str, Any]] = {
    "addressed": {"type": "noul", "instructions": "Is the latest human sentence directed at Twin, the AI participant?"},
    "kind": {"type": "choice", "instructions": "What is the primary role of the latest human sentence?", "criteria": {
        "system_fact": "A fact about system components or connections",
        "decision": "A choice the team made",
        "claim": "A claim that could be checked",
        "question": "A question seeking an answer",
        "task": "A request to do future work",
        "small_talk": "Social talk with no project information",
        "none": "No actionable content",
    }},
    "topic_changed": {"type": "noul", "instructions": "Did the latest human sentence introduce a different substantive work topic from the current topic? Greetings, small talk, acknowledgments, and questions about the same work topic are not topic changes. A clear switch such as 'different topic: billing' is a topic change."},
    "wants_visual": {"type": "choice", "instructions": "Which visual, if any, best fits the latest sentence?", "criteria": {
        "none": "No drawing needed",
        "architecture": "Components and connections",
        "sequence": "A process or interaction over time",
        "chart": "Numbers or trends",
        "mockup": "A screen or interface design",
        "table": "A comparison",
        "code": "A specific function or code snippet",
        "3d": "A physical object, robot, device, rack or room layout, or an explicit 3D view",
    }},
    "is_echo": {"type": "noul", "instructions": "Is the latest human sentence just Twin's recent speech picked up by the microphone?"},
}

_http_client: httpx.AsyncClient | None = None
_health: tuple[str, bool, float] | None = None  # key, available, expires_at


def _client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=httpx.Timeout(JEV_TIMEOUT_S, connect=0.4))
    return _http_client


async def aclose() -> None:
    """Release the shared HTTP pool on server shutdown."""
    global _http_client
    if _http_client is not None and not _http_client.is_closed:
        await _http_client.aclose()
    _http_client = None


def _log_latency(backend: str, started: float, count: int, ok: bool) -> None:
    row = {
        "ts": int(time.time() * 1000),
        "stage": "fast_decide",
        "backend": backend,
        "latency_ms": round((time.monotonic() - started) * 1000),
        "questions": count,
        "ok": ok,
    }
    # Never record state, prompts, API keys, or model output in latency logs.
    try:
        LATENCY_LOG.parent.mkdir(parents=True, exist_ok=True)
        with LATENCY_LOG.open("a", encoding="utf-8") as out:
            out.write(json.dumps(row, separators=(",", ":")) + "\n")
    except OSError as exc:
        log.warning("fast_decide latency log failed: %s", type(exc).__name__)
    log.info("fast_decide %s", json.dumps(row, separators=(",", ":")))


def _finite_probability(value: Any, path: str) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or (isinstance(value, float) and not math.isfinite(value)) or not 0 <= value <= 1):
        raise ValueError(f"{path} must be a finite probability")
    return float(value)


def _check_questions(questions: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    if not isinstance(questions, Mapping) or not 1 <= len(questions) <= MAX_QUESTIONS:
        raise ValueError(f"questions must contain 1 to {MAX_QUESTIONS} items")
    checked: dict[str, dict[str, Any]] = {}
    for name, question in questions.items():
        if not isinstance(name, str) or re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", name) is None:
            raise ValueError("question ids must be short alphanumeric names")
        if not isinstance(question, Mapping):
            raise ValueError(f"question {name} must be an object")
        q = dict(question)
        kind = q.get("type")
        if kind not in {"noul", "choice", "score"}:
            raise ValueError(f"question {name} has unsupported type")
        if kind == "choice":
            criteria = q.get("criteria")
            if not isinstance(criteria, dict) or not 1 <= len(criteria) <= 255 or not all(
                isinstance(key, str) and key for key in criteria
            ):
                raise ValueError(f"question {name} needs named choice criteria")
        elif kind == "score":
            criteria = q.get("criteria")
            if not isinstance(criteria, list) or not 1 <= len(criteria) <= 10:
                raise ValueError(f"question {name} needs 1 to 10 score levels")
        elif "criteria" in q and q["criteria"] is not None and not isinstance(q["criteria"], dict):
            raise ValueError(f"question {name} noul criteria must be an object")
        checked[name] = q
    return checked


def _check_request(state: Any, questions: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(state, (str, dict, list)):
        raise ValueError("state must be text, an object, or an array")
    checked = _check_questions(questions)
    request = {"model": config.MODEL_JEV, "state": state, "questions": checked}
    try:
        encoded = json.dumps(request, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("state and questions must be finite JSON") from exc
    if len(encoded) > MAX_REQUEST_BYTES:
        raise ValueError("fast_decide request is too large")
    return request


def _check_answers(data: Any, questions: Mapping[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    if not isinstance(data, dict) or set(data) != set(questions):
        raise ValueError("answer ids do not match question ids")
    checked: dict[str, dict[str, Any]] = {}
    for name, question in questions.items():
        answer = data[name]
        kind = question["type"]
        if not isinstance(answer, dict) or answer.get("type") != kind:
            raise ValueError(f"answer {name} has the wrong type")
        if kind == "noul":
            _finite_probability(answer.get("noul"), f"answers.{name}.noul")
        elif kind == "choice":
            choices = set(question["criteria"])
            if not isinstance(answer.get("choice"), str) or answer["choice"] not in choices:
                raise ValueError(f"answer {name} chose an unknown option")
            _finite_probability(answer.get("confidence"), f"answers.{name}.confidence")
            probabilities = answer.get("probabilities")
            if not isinstance(probabilities, dict) or set(probabilities) != choices:
                raise ValueError(f"answer {name} has incomplete probabilities")
            for option, probability in probabilities.items():
                _finite_probability(probability, f"answers.{name}.probabilities.{option}")
        else:
            levels = len(question["criteria"])
            score = answer.get("score")
            if (isinstance(score, bool) or not isinstance(score, (int, float))
                    or (isinstance(score, float) and not math.isfinite(score))
                    or not 0 <= score <= levels - 1):
                raise ValueError(f"answer {name} has an invalid score")
            _finite_probability(answer.get("confidence"), f"answers.{name}.confidence")
            expected = {str(i) for i in range(levels)}
            probabilities = answer.get("probabilities")
            legend = answer.get("legend")
            if not isinstance(probabilities, dict) or set(probabilities) != expected:
                raise ValueError(f"answer {name} has incomplete probabilities")
            if not isinstance(legend, dict) or set(legend) != expected:
                raise ValueError(f"answer {name} has an incomplete legend")
            for level, probability in probabilities.items():
                _finite_probability(probability, f"answers.{name}.probabilities.{level}")
        checked[name] = answer
    return checked


async def _jev_healthy(key: str) -> bool:
    global _health
    now = time.monotonic()
    if _health is not None and _health[0] == key and now < _health[2]:
        return _health[1]
    try:
        async with asyncio.timeout(JEV_TIMEOUT_S):
            response = await _client().get(f"{JEV_URL}/models", headers={"Authorization": f"Bearer {key}"})
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("JEV models response must be an object")
            models = payload.get("models", [])
            if not isinstance(models, list):
                raise ValueError("JEV models must be an array")
        healthy = any(isinstance(item, dict) and item.get("name") == config.MODEL_JEV for item in models)
    except (httpx.HTTPError, ValueError, asyncio.TimeoutError):
        healthy = False
    _health = (key, healthy, now + (HEALTH_TTL_S if healthy else HEALTH_FAILURE_TTL_S))
    return healthy


async def _jev_decide(key: str, request: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if not await _jev_healthy(key):
        raise RuntimeError("JEV health check unavailable")
    async with asyncio.timeout(JEV_TIMEOUT_S):
        response = await _client().post(
            f"{JEV_URL}/systemone",
            headers={"Authorization": f"Bearer {key}"},
            json=request,
        )
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("JEV response must be an object")
    return _check_answers(payload.get("answers"), request["questions"])


_NANO_PROMPT = """You are a typed decision function. Do not chat. Return ONLY one JSON object:
{"answers":{"question_id":{"type":"noul","noul":0.0}}}.
For each supplied question, answer under its exact id and type. Noul is the probability of yes, from 0 to 1.
For choice, return type, choice (one criteria key), confidence (0..1), and probabilities for EVERY criteria key.
For score, return type, score (0 to number of criteria minus 1), confidence (0..1), legend mapping each index string to its criterion, and probabilities for EVERY index string.
Use the state only. Do not add prose or extra questions. Probabilities must be finite numbers from 0 to 1."""


def _json_object(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("Nano decision must be a JSON object")
    return value


async def _nano_decide(request: dict[str, Any]) -> dict[str, dict[str, Any]]:
    messages = [
        {"role": "system", "content": _NANO_PROMPT},
        {"role": "user", "content": json.dumps({"state": request["state"], "questions": request["questions"]}, separators=(",", ":"))},
    ]
    async with asyncio.timeout(NANO_TIMEOUT_S):
        raw = await llm.chat(
            config.MODEL_FAST, messages, max_tokens=NANO_MAX_TOKENS, temperature=0,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
    return _check_answers(_json_object(raw).get("answers"), request["questions"])


async def decide(state: Any, questions: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Answer typed questions; use JEV when healthy, otherwise Crusoe Nano.

    Raises ValueError for bad inputs or unusable Nano output. Callers should
    fail open to the normal Listener rather than silently dropping a line.
    """
    global _health
    request = _check_request(state, questions)
    key = os.getenv("TYPESAFE_API_KEY", "").strip()
    if key:
        started = time.monotonic()
        try:
            result = await _jev_decide(key, request)
        except (httpx.HTTPError, ValueError, RuntimeError, asyncio.TimeoutError) as exc:
            _health = (key, False, time.monotonic() + HEALTH_FAILURE_TTL_S)
            _log_latency("jev", started, len(questions), False)
            log.warning("JEV unavailable; using Nano fallback (%s)", type(exc).__name__)
        else:
            _log_latency("jev", started, len(questions), True)
            return result
    started = time.monotonic()
    try:
        result = await _nano_decide(request)
    except BaseException:
        _log_latency("nano", started, len(questions), False)
        raise
    _log_latency("nano", started, len(questions), True)
    return result
