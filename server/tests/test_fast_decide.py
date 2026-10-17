"""Fast decision adapter tests with local HTTP and model fakes only."""

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

import config
from agents import fast_decide


QUESTIONS = {
    "addressed": {"type": "noul", "instructions": "Is this for Twin?"},
    "kind": {"type": "choice", "instructions": "What kind?", "criteria": {
        "question": "Asks something", "none": "Nothing actionable",
    }},
    "urgency": {"type": "score", "instructions": "How urgent?", "criteria": ["later", "soon", "now"]},
}
ANSWERS = {
    "addressed": {"type": "noul", "noul": 0.9},
    "kind": {"type": "choice", "choice": "question", "confidence": 0.8,
             "probabilities": {"question": 0.8, "none": 0.2}},
    "urgency": {"type": "score", "score": 1.7, "confidence": 0.7,
                "legend": {"0": "later", "1": "soon", "2": "now"},
                "probabilities": {"0": 0.1, "1": 0.2, "2": 0.7}},
}


@pytest.fixture
def latency_file(monkeypatch, tmp_path):
    path = tmp_path / "latency.jsonl"
    monkeypatch.setattr(fast_decide, "LATENCY_LOG", path)
    monkeypatch.setattr(fast_decide, "_health", None)
    return path


def test_turn_questions_cover_all_fast_gates():
    assert set(fast_decide.TURN_QUESTIONS) == {
        "addressed", "kind", "topic_changed", "wants_visual", "is_echo",
    }
    fast_decide._check_request({"latest_sentence": "Twin, draw this"}, fast_decide.TURN_QUESTIONS)


@pytest.mark.asyncio
async def test_nano_fallback_uses_one_crusoe_call_and_logs_metadata(monkeypatch, latency_file):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    chat = AsyncMock(return_value=json.dumps({"answers": ANSWERS}))
    monkeypatch.setattr(fast_decide.llm, "chat", chat)
    result = await fast_decide.decide({"latest_sentence": "Twin, what happened?"}, QUESTIONS)
    assert result == ANSWERS
    chat.assert_awaited_once()
    assert chat.await_args.args[0] == config.MODEL_FAST
    assert chat.await_args.kwargs["max_tokens"] == fast_decide.NANO_MAX_TOKENS
    assert chat.await_args.kwargs["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    rows = [json.loads(line) for line in latency_file.read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["backend"] == "nano" and rows[0]["ok"]
    assert "latest_sentence" not in latency_file.read_text()


@pytest.mark.asyncio
async def test_jev_health_and_typed_response_skip_nano(monkeypatch, latency_file):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-secret")
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        assert request.headers["authorization"] == "Bearer test-secret"
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"models": [{"name": "jev-latest"}]})
        payload = json.loads(request.content)
        assert payload["model"] == "jev-latest"
        assert set(payload["questions"]) == set(QUESTIONS)
        return httpx.Response(200, json={"model": "jev-latest", "answers": ANSWERS,
                                         "usage": {"input_tokens": 20, "output_tokens": 8}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(fast_decide, "_client", lambda: client)
        chat = AsyncMock()
        monkeypatch.setattr(fast_decide.llm, "chat", chat)
        assert await fast_decide.decide("Twin, what happened?", QUESTIONS) == ANSWERS
        assert await fast_decide.decide("Twin, what happened?", QUESTIONS) == ANSWERS
    assert calls == [("GET", "/v1/models"), ("POST", "/v1/systemone"),
                     ("POST", "/v1/systemone")]
    chat.assert_not_awaited()
    assert "test-secret" not in latency_file.read_text()


@pytest.mark.asyncio
async def test_jev_unavailable_falls_back_to_nano(monkeypatch, latency_file):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-secret")
    requests = []

    def handler(request):
        requests.append(request.url.path)
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(fast_decide, "_client", lambda: client)
        chat = AsyncMock(return_value=json.dumps({"answers": ANSWERS}))
        monkeypatch.setattr(fast_decide.llm, "chat", chat)
        assert await fast_decide.decide("Twin?", QUESTIONS) == ANSWERS
    assert requests == ["/v1/models"]
    chat.assert_awaited_once()
    rows = [json.loads(line) for line in latency_file.read_text().splitlines()]
    assert [row["backend"] for row in rows] == ["jev", "nano"]
    assert [row["ok"] for row in rows] == [False, True]


@pytest.mark.asyncio
async def test_jev_invalid_answer_falls_back_and_cools_down(monkeypatch, latency_file):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-secret")
    requests = []

    def handler(request):
        requests.append(request.url.path)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"models": [{"name": "jev-latest"}]})
        bad = {**ANSWERS, "addressed": {"type": "noul", "noul": 3}}
        return httpx.Response(200, json={"answers": bad})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(fast_decide, "_client", lambda: client)
        chat = AsyncMock(return_value=json.dumps({"answers": ANSWERS}))
        monkeypatch.setattr(fast_decide.llm, "chat", chat)
        assert await fast_decide.decide("Twin?", QUESTIONS) == ANSWERS
        assert await fast_decide.decide("Twin?", QUESTIONS) == ANSWERS
    assert requests == ["/v1/models", "/v1/systemone"]
    assert chat.await_count == 2


@pytest.mark.parametrize("answer", [
    {"type": "noul", "noul": float("nan")},
    {"type": "noul", "noul": 1.5},
    {"type": "noul", "noul": 10**1000},
    {"type": "choice", "choice": "unknown", "confidence": 0.8,
     "probabilities": {"question": 0.8, "none": 0.2}},
    {"type": "choice", "choice": ["question"], "confidence": 0.8,
     "probabilities": {"question": 0.8, "none": 0.2}},
])
def test_typed_answer_validation_rejects_bad_values(answer):
    question = QUESTIONS["kind"] if answer["type"] == "choice" else QUESTIONS["addressed"]
    with pytest.raises(ValueError):
        fast_decide._check_answers({"x": answer}, {"x": question})


def test_request_validation_rejects_bad_schema_and_nonfinite_state():
    with pytest.raises(ValueError, match="choice criteria"):
        fast_decide._check_request("text", {"kind": {"type": "choice", "criteria": {}}})
    with pytest.raises(ValueError, match="finite JSON"):
        fast_decide._check_request({"x": float("nan")}, QUESTIONS)


@pytest.mark.asyncio
async def test_bad_nano_json_raises_so_caller_can_fail_open(monkeypatch, latency_file):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    chat = AsyncMock(return_value="not JSON")
    monkeypatch.setattr(fast_decide.llm, "chat", chat)
    with pytest.raises(ValueError):
        await fast_decide.decide("text", QUESTIONS)
    assert json.loads(latency_file.read_text().splitlines()[0])["ok"] is False


@pytest.mark.asyncio
async def test_nano_timeout_is_bounded_and_recorded(monkeypatch, latency_file):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(fast_decide, "NANO_TIMEOUT_S", 0.01)

    async def slow_chat(*_args, **_kwargs):
        await asyncio.sleep(0.1)

    monkeypatch.setattr(fast_decide.llm, "chat", slow_chat)
    with pytest.raises(TimeoutError):
        await fast_decide.decide("text", QUESTIONS)
    assert json.loads(latency_file.read_text().splitlines()[0])["ok"] is False
