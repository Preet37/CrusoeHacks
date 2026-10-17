"""Small async Crusoe chat client shared by Twin's agents.

The SDK's own retries are disabled so a streamed answer cannot be replayed after
its first visible token. Tool arguments are checked before any handler runs.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import math
import random
import re
import time
from functools import lru_cache
from typing import Any, AsyncIterator, Callable, Mapping, Sequence

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

import config

logger = logging.getLogger(__name__)
_sleep = asyncio.sleep
_MAX_LOG_CHARS = 300


@lru_cache(maxsize=1)
def _client() -> AsyncOpenAI:
    if not config.CRUSOE_API_KEY:
        raise RuntimeError("CRUSOE_API_KEY is missing; set it in server/.env")
    return AsyncOpenAI(
        api_key=config.CRUSOE_API_KEY,
        base_url=config.CRUSOE_BASE_URL,
        timeout=config.CRUSOE_TIMEOUT,
        max_retries=0,
    )


def _retryable(exc: Exception) -> bool:
    return isinstance(exc, (APIConnectionError, APITimeoutError)) or (
        isinstance(exc, APIStatusError) and (exc.status_code == 429 or exc.status_code >= 500)
    )


def _unsupported(exc: Exception, feature: str) -> bool:
    if not isinstance(exc, APIStatusError) or exc.status_code not in (400, 422):
        return False
    detail = str(exc).lower()
    return feature in detail or "unsupported" in detail or "not supported" in detail


def _delay(attempt: int) -> float:
    return min(2.0, 0.2 * (2**attempt)) + random.uniform(0, 0.1)


def _log_usage(model: str, operation: str, response: Any, elapsed: float) -> None:
    usage = getattr(response, "usage", None)
    payload = {
        "event": "llm_usage",
        "model": model,
        "operation": operation,
        "elapsed_ms": round(elapsed * 1000),
        "prompt_tokens": getattr(usage, "prompt_tokens", None),
        "completion_tokens": getattr(usage, "completion_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
    }
    logger.info("%s", json.dumps(payload, separators=(",", ":")))


async def _create(model: str, messages: Sequence[dict[str, Any]], operation: str, **kw: Any) -> Any:
    started = time.monotonic()
    kw.setdefault("max_tokens", config.CRUSOE_MAX_OUTPUT_TOKENS)
    for attempt in range(max(0, config.CRUSOE_MAX_RETRIES) + 1):
        try:
            response = await _client().chat.completions.create(model=model, messages=list(messages), **kw)
            _log_usage(model, operation, response, time.monotonic() - started)
            return response
        except Exception as exc:
            if not _retryable(exc) or attempt >= config.CRUSOE_MAX_RETRIES:
                raise
            logger.warning("Crusoe %s retry %s after %s", operation, attempt + 1, type(exc).__name__)
            await _sleep(_delay(attempt))
    raise AssertionError("unreachable")


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") if isinstance(part, dict) else getattr(part, "text", "") or "" for part in content)
    return ""


async def chat(model: str, messages: Sequence[dict[str, Any]], **kw: Any) -> str:
    """Return the first assistant message's text from a Crusoe completion."""
    response = await _create(model, messages, "chat", **kw)
    return _text(response.choices[0].message.content)


async def stream_chat(model: str, messages: Sequence[dict[str, Any]], **kw: Any) -> AsyncIterator[str]:
    """Yield text deltas. Retry only before yielding the first delta."""
    options = dict(kw)
    options.setdefault("max_tokens", config.CRUSOE_MAX_OUTPUT_TOKENS)
    options.setdefault("stream_options", {"include_usage": True})
    started = time.monotonic()
    attempt = 0
    yielded = False
    usage_logged = False
    while True:
        stream = None
        try:
            stream = await _client().chat.completions.create(
                model=model, messages=list(messages), stream=True, **options
            )
            async for chunk in stream:
                if getattr(chunk, "usage", None) is not None:
                    _log_usage(model, "stream_chat", chunk, time.monotonic() - started)
                    usage_logged = True
                for choice in getattr(chunk, "choices", ()):
                    delta = _text(getattr(choice.delta, "content", None))
                    if delta:
                        yielded = True
                        yield delta
            if not usage_logged:
                _log_usage(model, "stream_chat", None, time.monotonic() - started)
            return
        except Exception as exc:
            if not yielded and "stream_options" in options and _unsupported(exc, "stream_options"):
                options.pop("stream_options")
                continue
            if yielded or not _retryable(exc) or attempt >= config.CRUSOE_MAX_RETRIES:
                raise
            logger.warning("Crusoe stream retry %s after %s", attempt + 1, type(exc).__name__)
            await _sleep(_delay(attempt))
            attempt += 1
        finally:
            if stream is not None and hasattr(stream, "aclose"):
                await stream.aclose()


def _schema_for(name: str, tools: Sequence[dict[str, Any]]) -> dict[str, Any]:
    for tool in tools:
        fn = tool.get("function", {})
        if tool.get("type") == "function" and fn.get("name") == name:
            return fn.get("parameters", {"type": "object"})
    raise ValueError(f"Tool {name!r} is not declared")


def _validate(value: Any, schema: dict[str, Any], path: str = "arguments") -> None:
    if "anyOf" in schema:
        for option in schema["anyOf"]:
            try:
                _validate(value, option, path)
                break
            except ValueError:
                pass
        else:
            raise ValueError(f"{path} does not match any allowed shape")
    kind = schema.get("type")
    types = {
        "object": lambda v: isinstance(v, dict),
        "array": lambda v: isinstance(v, list),
        "string": lambda v: isinstance(v, str),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        "boolean": lambda v: isinstance(v, bool),
        "null": lambda v: v is None,
    }
    if isinstance(kind, list):
        kind = next((option for option in kind if option in types and types[option](value)), None)
        if kind is None:
            raise ValueError(f"{path} does not match an allowed type")
    if kind in types and not types[kind](value):
        raise ValueError(f"{path} must be {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} is outside its allowed values")
    if kind == "string":
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise ValueError(f"{path} is shorter than minLength")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise ValueError(f"{path} is longer than maxLength")
    if kind in ("integer", "number"):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"{path} must be finite")
        if "minimum" in schema and value < schema["minimum"]:
            raise ValueError(f"{path} is below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValueError(f"{path} is above maximum")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            raise ValueError(f"{path} must exceed exclusiveMinimum")
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            raise ValueError(f"{path} must be below exclusiveMaximum")
    if kind == "object":
        props = schema.get("properties", {})
        for key in schema.get("required", ()):
            if key not in value:
                raise ValueError(f"{path}.{key} is required")
        for key, item in value.items():
            if key in props:
                _validate(item, props[key], f"{path}.{key}")
            elif schema.get("additionalProperties") is False:
                raise ValueError(f"{path}.{key} is unknown")
    elif kind == "array":
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise ValueError(f"{path} has fewer than minItems")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ValueError(f"{path} has more than maxItems")
        for index, item in enumerate(value):
            _validate(item, schema.get("items", {}), f"{path}[{index}]")


async def _invoke(
    name: str,
    args: Any,
    tools: Sequence[dict[str, Any]],
    handlers: Mapping[str, Callable[..., Any]],
    validator: Callable[[dict[str, Any]], Any] | None,
) -> Any:
    if name not in handlers:
        raise ValueError(f"No handler for declared tool {name!r}")
    _validate(args, _schema_for(name, tools))
    if validator is not None:
        checked = validator({"op": name, **args})
        if inspect.isawaitable(checked):
            await checked
    result = handlers[name](**args)
    return await result if inspect.isawaitable(result) else result


def _parse_ops(raw: str) -> list[dict[str, Any]]:
    # Some compatible endpoints wrap JSON in a Markdown fence despite instructions.
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.I)
    data = json.loads(cleaned)
    if isinstance(data, dict):
        data = data.get("ops")
    if not isinstance(data, list):
        raise ValueError("Expected a JSON object with an ops array")
    if not all(isinstance(op, dict) for op in data):
        raise ValueError("Every op must be an object")
    return data


def _normalize_ops(
    ops: list[dict[str, Any]],
    tools: Sequence[dict[str, Any]],
    handlers: Mapping[str, Callable[..., Any]],
    max_ops: int,
) -> list[tuple[str, dict[str, Any]]]:
    if len(ops) > max_ops:
        raise ValueError(f"Too many ops: maximum {max_ops}")
    # Check all operations before applying any of them.
    normalized = []
    for op in ops:
        name = op.get("op", op.get("name"))
        args = op.get("arguments", {k: v for k, v in op.items() if k not in ("op", "name")})
        if not isinstance(name, str) or not isinstance(args, dict):
            raise ValueError("Each op needs a name and object arguments")
        if name not in handlers:
            raise ValueError(f"No handler for declared tool {name!r}")
        _validate(args, _schema_for(name, tools))
        normalized.append((name, args))
    return normalized


async def _execute_ops(
    normalized: list[tuple[str, dict[str, Any]]],
    tools: Sequence[dict[str, Any]],
    handlers: Mapping[str, Callable[..., Any]],
    validator: Callable[[dict[str, Any]], Any] | None,
) -> str:
    results = []
    for name, args in normalized:
        results.append(await _invoke(name, args, tools, handlers, validator))
    return json.dumps(results, default=str)


async def run_json_ops(
    model: str,
    messages: Sequence[dict[str, Any]],
    tools: Sequence[dict[str, Any]],
    handlers: Mapping[str, Callable[..., Any]],
    max_ops: int = 8,
    validator: Callable[[dict[str, Any]], Any] | None = None,
    **kw: Any,
) -> str:
    """Ask for JSON ops, validate them, then call allowed handlers.

    The optional validator can apply board-specific checks before each operation.
    """
    definitions = [tool["function"] for tool in tools if tool.get("type") == "function"]
    instruction = (
        "Return ONLY a JSON object of the form {\"ops\":[{\"op\":\"tool_name\","
        "\"arguments\":{...}}]}. Use at most %d operations. Available tools: %s"
        % (max_ops, json.dumps(definitions, separators=(",", ":")))
    )
    base = [{"role": "system", "content": instruction}] + list(messages)
    for attempt in range(2):
        request = dict(kw)
        request["response_format"] = {"type": "json_object"}
        try:
            raw = await chat(model, base, **request)
        except Exception as exc:
            if not _unsupported(exc, "response_format"):
                raise
            request.pop("response_format")
            raw = await chat(model, base, **request)
        try:
            ops = _parse_ops(raw)
            normalized = _normalize_ops(ops, tools, handlers, max_ops)
        except (ValueError, json.JSONDecodeError) as exc:
            if attempt:
                raise ValueError(f"Model returned invalid JSON ops: {str(exc)[:_MAX_LOG_CHARS]}") from exc
            base = base + [{"role": "assistant", "content": raw}, {"role": "user", "content": f"Fix the JSON operations: {str(exc)[:_MAX_LOG_CHARS]}"}]
            continue
        return await _execute_ops(normalized, tools, handlers, validator)
    raise AssertionError("unreachable")


async def run_tools(
    model: str,
    messages: Sequence[dict[str, Any]],
    tools: Sequence[dict[str, Any]],
    handlers: Mapping[str, Callable[..., Any]],
    max_steps: int = 8,
    validator: Callable[[dict[str, Any]], Any] | None = None,
    **kw: Any,
) -> str | None:
    """Run a bounded tool conversation, falling back to validated JSON ops."""
    async with asyncio.timeout(config.CRUSOE_TOOL_TIMEOUT):
        return await _run_tools(model, messages, tools, handlers, max_steps, validator, **kw)


async def _run_tools(
    model: str,
    messages: Sequence[dict[str, Any]],
    tools: Sequence[dict[str, Any]],
    handlers: Mapping[str, Callable[..., Any]],
    max_steps: int,
    validator: Callable[[dict[str, Any]], Any] | None,
    **kw: Any,
) -> str | None:
    if max_steps < 1:
        return None
    history = list(messages)
    attempted = 0
    had_tool_calls = False
    for step in range(max_steps):
        try:
            response = await _create(model, history, "run_tools", tools=list(tools), tool_choice="auto", **kw)
        except Exception as exc:
            if not had_tool_calls and _unsupported(exc, "tool"):
                return await run_json_ops(model, history, tools, handlers, max_steps, validator, **kw)
            raise
        message = response.choices[0].message
        calls = getattr(message, "tool_calls", None) or []
        if not calls:
            content = _text(getattr(message, "content", None))
            if not had_tool_calls:
                try:
                    normalized = _normalize_ops(_parse_ops(content), tools, handlers, max_steps)
                except (ValueError, json.JSONDecodeError):
                    return await run_json_ops(model, history, tools, handlers, max_steps, validator, **kw)
                return await _execute_ops(normalized, tools, handlers, validator)
            return content
        if len(calls) > max_steps - attempted:
            logger.warning("Crusoe requested more than %s tool calls", max_steps)
            return None
        had_tool_calls = True
        attempted += len(calls)
        history.append({
            "role": "assistant",
            "content": _text(getattr(message, "content", None)),
            "tool_calls": [
                {"id": call.id, "type": "function", "function": {"name": call.function.name, "arguments": call.function.arguments}}
                for call in calls
            ],
        })
        for call in calls:
            try:
                args = json.loads(call.function.arguments)
                result = await _invoke(call.function.name, args, tools, handlers, validator)
                content = json.dumps(result, default=str)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                content = json.dumps({"error": str(exc)[:_MAX_LOG_CHARS]})
            history.append({"role": "tool", "tool_call_id": call.id, "content": content})
    return None
