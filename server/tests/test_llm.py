"""Offline checks for the Crusoe client; no API key or network is needed."""

from __future__ import annotations

import math
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import logging

import httpx
import pytest
from openai import BadRequestError, RateLimitError

import llm


def response(content="", calls=None, usage=None):
    return NS(choices=[NS(message=NS(content=content, tool_calls=calls))], usage=usage)


def call(name, arguments, id="c1"):
    return NS(id=id, function=NS(name=name, arguments=arguments))


def status_error(cls, code, message):
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    return cls(message, response=httpx.Response(code, request=request), body=None)


@pytest.fixture
def fake_client(monkeypatch):
    create = AsyncMock()
    client = NS(chat=NS(completions=NS(create=create)))
    monkeypatch.setattr(llm, "_client", lambda: client)
    monkeypatch.setattr(llm, "_sleep", AsyncMock())
    return create


DRAW_TOOLS = [{
    "type": "function",
    "function": {
        "name": "add_box",
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "x": {"type": "integer", "minimum": 0, "maximum": 1000},
            },
            "required": ["id", "x"],
            "additionalProperties": False,
        },
    },
}]


@pytest.mark.asyncio
async def test_chat_retries_transient_error_and_logs_usage(fake_client, caplog):
    caplog.set_level(logging.INFO, logger="llm")
    fake_client.side_effect = [
        status_error(RateLimitError, 429, "rate limited"),
        response("hello", usage=NS(prompt_tokens=4, completion_tokens=2, total_tokens=6)),
    ]
    assert await llm.chat("model", [{"role": "user", "content": "hi"}]) == "hello"
    assert fake_client.await_count == 2
    assert '"total_tokens":6' in caplog.text


class FakeStream:
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    def __aiter__(self):
        self.it = iter(self.chunks)
        return self

    async def __anext__(self):
        item = next(self.it, None)
        if item is None:
            raise StopAsyncIteration
        if isinstance(item, Exception):
            raise item
        return item

    async def aclose(self):
        self.closed = True


def chunk(text=None, usage=None):
    return NS(choices=[] if text is None else [NS(delta=NS(content=text))], usage=usage)


@pytest.mark.asyncio
async def test_stream_falls_back_when_usage_option_unsupported(fake_client):
    stream = FakeStream([chunk("Hey"), chunk(" there"), chunk(usage=NS(prompt_tokens=1, completion_tokens=2, total_tokens=3))])
    fake_client.side_effect = [status_error(BadRequestError, 400, "stream_options unsupported"), stream]
    assert [part async for part in llm.stream_chat("model", [])] == ["Hey", " there"]
    assert stream.closed
    assert "stream_options" not in fake_client.await_args.kwargs


@pytest.mark.asyncio
async def test_stream_does_not_retry_after_first_delta(fake_client):
    error = status_error(RateLimitError, 429, "rate limited")
    fake_client.return_value = FakeStream([chunk("first"), error])
    received = []
    with pytest.raises(RateLimitError):
        async for part in llm.stream_chat("model", []):
            received.append(part)
    assert received == ["first"]
    assert fake_client.await_count == 1


@pytest.mark.asyncio
async def test_closing_stream_for_barge_in_closes_sdk_stream(fake_client):
    stream = FakeStream([chunk("first"), chunk("second")])
    fake_client.return_value = stream
    answer = llm.stream_chat("model", [])
    assert await anext(answer) == "first"
    await answer.aclose()
    assert stream.closed


@pytest.mark.asyncio
async def test_tool_loop_validates_and_returns_final_text(fake_client):
    handled = AsyncMock(return_value={"ok": True})
    fake_client.side_effect = [
        response(calls=[call("add_box", '{"id":"auth","x":420}')]),
        response("Done"),
    ]
    result = await llm.run_tools("model", [], DRAW_TOOLS, {"add_box": handled})
    assert result == "Done"
    handled.assert_awaited_once_with(id="auth", x=420)
    sent = fake_client.await_args.kwargs["messages"]
    assert sent[-1] == {"role": "tool", "tool_call_id": "c1", "content": '{"ok": true}'}


@pytest.mark.asyncio
async def test_invalid_tool_arguments_never_reach_handler(fake_client):
    handled = AsyncMock()
    fake_client.side_effect = [
        response(calls=[call("add_box", '{"id":"auth","x":1001}')]),
        response("I could not add it"),
    ]
    assert await llm.run_tools("model", [], DRAW_TOOLS, {"add_box": handled}) == "I could not add it"
    handled.assert_not_awaited()
    assert "above maximum" in fake_client.await_args.kwargs["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_json_fallback_for_unsupported_tools(fake_client):
    handled = AsyncMock(return_value={"ok": True})
    fake_client.side_effect = [
        status_error(BadRequestError, 400, "tools unsupported"),
        response('{"ops":[{"op":"add_box","arguments":{"id":"auth","x":420}}]}'),
    ]
    result = await llm.run_tools("model", [], DRAW_TOOLS, {"add_box": handled})
    assert result == '[{"ok": true}]'
    handled.assert_awaited_once()


@pytest.mark.asyncio
async def test_json_fallback_retries_bad_ops_without_applying_them(fake_client):
    handled = AsyncMock(return_value={"ok": True})
    fake_client.side_effect = [
        response('{"ops":[{"op":"add_box","arguments":{"id":"auth","x":1001}}]}'),
        response('{"ops":[{"op":"add_box","arguments":{"id":"auth","x":420}}]}'),
    ]
    assert await llm.run_json_ops("model", [], DRAW_TOOLS, {"add_box": handled}) == '[{"ok": true}]'
    handled.assert_awaited_once_with(id="auth", x=420)


@pytest.mark.asyncio
async def test_json_fallback_removes_unsupported_response_format(fake_client):
    handled = AsyncMock(return_value={"ok": True})
    fake_client.side_effect = [
        status_error(BadRequestError, 400, "response_format unsupported"),
        response('{"ops":[{"op":"add_box","arguments":{"id":"auth","x":420}}]}'),
    ]
    await llm.run_json_ops("model", [], DRAW_TOOLS, {"add_box": handled})
    assert "response_format" not in fake_client.await_args.kwargs
    handled.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("native_content", [False, True])
async def test_execution_failure_does_not_replay_earlier_ops(fake_client, native_content):
    handled = AsyncMock(side_effect=[{"ok": True}, ValueError("board conflict")])
    ops = '{"ops":[{"op":"add_box","arguments":{"id":"auth","x":420}},{"op":"add_box","arguments":{"id":"db","x":600}}]}'
    fake_client.return_value = response(ops)
    with pytest.raises(ValueError, match="board conflict"):
        if native_content:
            await llm.run_tools("model", [], DRAW_TOOLS, {"add_box": handled})
        else:
            await llm.run_json_ops("model", [], DRAW_TOOLS, {"add_box": handled})
    assert handled.await_count == 2
    assert fake_client.await_count == 1


@pytest.mark.asyncio
async def test_nonfinite_json_number_is_repaired_before_handler(fake_client):
    handled = AsyncMock(return_value={"ok": True})
    fake_client.side_effect = [
        response('{"ops":[{"op":"add_box","arguments":{"id":"auth","x":NaN}}]}'),
        response('{"ops":[{"op":"add_box","arguments":{"id":"auth","x":420}}]}'),
    ]
    await llm.run_json_ops("model", [], DRAW_TOOLS, {"add_box": handled})
    handled.assert_awaited_once_with(id="auth", x=420)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_number_schema_rejects_nonfinite_values(value):
    with pytest.raises(ValueError, match="finite"):
        llm._validate(value, {"type": "number"})


@pytest.mark.asyncio
async def test_invalid_calls_count_toward_tool_budget(fake_client):
    handled = AsyncMock()
    fake_client.side_effect = [
        response(calls=[call("add_box", '{"id":"a","x":1001}', "c1"),
                        call("add_box", '{"id":"b","x":1001}', "c2")]),
        response(calls=[call("add_box", '{"id":"c","x":5}', "c3")]),
    ]
    assert await llm.run_tools("model", [], DRAW_TOOLS, {"add_box": handled}, max_steps=2) is None
    handled.assert_not_awaited()


@pytest.mark.asyncio
async def test_tool_loop_is_bounded(fake_client):
    fake_client.return_value = response(calls=[call("add_box", '{"id":"auth","x":420}')])
    handled = AsyncMock()
    assert await llm.run_tools("model", [], DRAW_TOOLS, {"add_box": handled}, max_steps=2) is None
    assert handled.await_count == 2
    assert fake_client.await_count == 2
