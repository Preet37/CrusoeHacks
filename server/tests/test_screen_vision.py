"""PX-20: visual answers use one bounded multimodal call and safe overlay geometry."""

import asyncio
import base64
import json
from collections import deque
from pathlib import Path

import pytest

import config
import llm
from agents import screen_vision


SAMPLE = Path(__file__).parent / "fixtures" / "screen_sample.jpg"


@pytest.mark.asyncio
async def test_saved_screenshot_returns_box_near_visible_region(monkeypatch):
    jpeg = base64.b64encode(SAMPLE.read_bytes()).decode("ascii")
    calls = []

    async def chat(model, messages, **kwargs):
        calls.append(1)
        assert model == config.MODEL_COACH
        assert kwargs["max_tokens"] == 550
        assert kwargs["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False
        assert messages[-1]["content"][1]["image_url"]["url"] == "data:image/jpeg;base64," + jpeg
        context = json.loads(messages[-1]["content"][0]["text"])
        assert context["question"] == "Where is the red issue?"
        assert "Preet: This code has an issue" in context["recent_transcript"]
        return json.dumps({"say": "The red area is on the right side.",
                           "items": [{"kind": "box", "x": 0.62, "y": 0.25,
                                      "w": 0.25, "h": 0.1875, "text": "issue"}]})

    monkeypatch.setattr(llm, "chat", chat)
    result = await screen_vision.analyze(jpeg, "Where is the red issue?",
                                         deque([{"speaker": "Preet", "text": "This code has an issue"}]))
    assert calls == [1]
    assert result["say"] == "The red area is on the right side."
    assert result["items"][0]["x"] == pytest.approx(0.62)
    assert result["items"][0]["y"] == pytest.approx(0.25)


@pytest.mark.asyncio
@pytest.mark.parametrize("jpeg,question", [("not base64!", "Where?"), ("", "Where?"),
                                          (base64.b64encode(b"not a JPEG").decode(), "Where?"),
                                          (base64.b64encode(SAMPLE.read_bytes()).decode(), "")])
async def test_bad_input_never_calls_model(monkeypatch, jpeg, question):
    async def chat(*args, **kwargs):
        raise AssertionError("invalid input should not reach model")

    monkeypatch.setattr(llm, "chat", chat)
    with pytest.raises(ValueError):
        await screen_vision.analyze(jpeg, question, [])


@pytest.mark.asyncio
@pytest.mark.parametrize("item", [
    {"kind": "box", "x": 0.9, "y": 0.2, "w": 0.2, "h": 0.1},
    {"kind": "box", "x": float("nan"), "y": 0.2, "w": 0.2, "h": 0.1},
    {"kind": "box", "x": 10**1000, "y": 0.2, "w": 0.2, "h": 0.1},
    {"kind": "arrow", "x": 0.1, "y": 0.1, "x2": 1.2, "y2": 0.4},
    {"kind": "text", "x": 0.2, "y": 0.4},
    {"kind": "box", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2, "script": "x"},
])
async def test_invalid_annotations_rejected(monkeypatch, item):
    async def chat(*args, **kwargs):
        return json.dumps({"say": "I found it.", "items": [item]})

    monkeypatch.setattr(llm, "chat", chat)
    with pytest.raises(ValueError):
        await screen_vision.analyze(SAMPLE.read_bytes(), "Where?", "screen")


@pytest.mark.asyncio
async def test_no_visible_target_can_return_no_items(monkeypatch):
    async def chat(*args, **kwargs):
        return '{"say":"I cannot locate it on this frame.","items":[]}'

    monkeypatch.setattr(llm, "chat", chat)
    assert await screen_vision.analyze(SAMPLE.read_bytes(), "Where is it?", []) == {
        "say": "I cannot locate it on this frame.", "items": []}


@pytest.mark.asyncio
async def test_arrow_and_highlight_keep_normalized_schema(monkeypatch):
    async def chat(*args, **kwargs):
        return json.dumps({"say": "The issue is over there.", "items": [
            {"kind": "arrow", "x": 0.1, "y": 0.2, "x2": 0.7, "y2": 0.3},
            {"kind": "highlight", "x": 0.6, "y": 0.2, "w": 0.2, "h": 0.2},
        ]})

    monkeypatch.setattr(llm, "chat", chat)
    result = await screen_vision.analyze(SAMPLE.read_bytes(), "Where?", [])
    assert result["items"] == [
        {"kind": "arrow", "x": 0.1, "y": 0.2, "x2": 0.7, "y2": 0.3},
        {"kind": "highlight", "x": 0.6, "y": 0.2, "w": 0.2, "h": 0.2},
    ]


@pytest.mark.asyncio
async def test_model_call_has_bounded_timeout(monkeypatch):
    async def chat(*args, **kwargs):
        await asyncio.sleep(0.03)
        return '{"say":"late","items":[]}'

    monkeypatch.setattr(llm, "chat", chat)
    monkeypatch.setattr(screen_vision, "TIMEOUT_S", 0.005)
    with pytest.raises(TimeoutError):
        await screen_vision.analyze(SAMPLE.read_bytes(), "Where?", [])
