"""Smoke test Crusoe Managed Inference (OpenAI-compatible).

Usage (from server/):
  .venv/bin/python scripts/smoke_crusoe.py          # full checks
  .venv/bin/python scripts/smoke_crusoe.py --quick  # list models only
Never prints the key.
"""
import asyncio
import json
import sys
import time
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_DIR))

import config  # noqa: E402

try:
    from openai import AsyncOpenAI
except ImportError:  # pragma: no cover
    print("ERROR: openai package missing. Run: .venv/bin/pip install -r requirements.txt")
    sys.exit(2)

ROLES = {
    "MODEL_RESPONDER": config.MODEL_RESPONDER,
    "MODEL_FAST": config.MODEL_FAST,
    "MODEL_SMART": config.MODEL_SMART,
    "MODEL_COACH": config.MODEL_COACH,
}
NO_THINK = {"chat_template_kwargs": {"enable_thinking": False}}
PROMPT = [{"role": "user", "content": "Say hello in five words or fewer."}]
summary: list[tuple[str, str, str]] = []  # (check, result, detail)


def record(check: str, ok: bool | None, detail: str = "") -> None:
    res = "OK" if ok else ("SKIP" if ok is None else "FAIL")
    summary.append((check, res, detail))


def short(e: Exception) -> str:
    return f"{type(e).__name__}: {str(e)[:300]}"


async def list_models(client: AsyncOpenAI) -> set[str]:
    print("== GET /models")
    try:
        t0 = time.perf_counter()
        page = await client.models.list()
        ids = sorted(m.id for m in page.data)
        ms = (time.perf_counter() - t0) * 1000
    except Exception as e:
        print(f"  FAILED: {short(e)}")
        record("list models", False, short(e))
        return set()
    print(f"  {len(ids)} models in {ms:.0f} ms:")
    for mid in ids:
        mark = "  <== NEMOTRON" if "nemotron" in mid.lower() else ""
        print(f"    {mid}{mark}")
    record("list models", True, f"{len(ids)} models")
    lower = {i.lower(): i for i in ids}
    print("  Configured role models:")
    for role, mid in ROLES.items():
        if mid in ids:
            print(f"    {role:16} {mid}: present")
            record(f"{role} present", True, mid)
        elif mid.lower() in lower:
            print(f"    {role:16} {mid}: present with different case -> {lower[mid.lower()]}")
            record(f"{role} present", False, f"case differs: use {lower[mid.lower()]}")
        else:
            print(f"    {role:16} {mid}: MISSING")
            record(f"{role} present", False, f"missing: {mid}")
    return set(ids)


async def stream_chat(client: AsyncOpenAI, model: str, extra_body: dict | None):
    t0 = time.perf_counter()
    kw = {"extra_body": extra_body} if extra_body else {}
    stream = await client.chat.completions.create(
        model=model, messages=PROMPT, max_tokens=64, stream=True, **kw
    )
    ttft = None
    parts: list[str] = []
    async for chunk in stream:
        if not chunk.choices:
            continue
        d = chunk.choices[0].delta
        txt = getattr(d, "content", None)
        if txt:
            if ttft is None:
                ttft = (time.perf_counter() - t0) * 1000
            parts.append(txt)
    total = (time.perf_counter() - t0) * 1000
    return "".join(parts).strip(), ttft, total


async def test_responder(client: AsyncOpenAI) -> None:
    model = config.MODEL_RESPONDER
    print(f"\n== Responder (streaming) {model}")
    for label, eb in (("with enable_thinking=False", NO_THINK), ("without extra_body", None)):
        try:
            reply, ttft, total = await stream_chat(client, model, eb)
        except Exception as e:
            print(f"  {label}: FAILED {short(e)}")
            record(f"responder {label}", False, short(e))
            continue
        ttft_s = f"{ttft:.0f} ms" if ttft is not None else "n/a (no content)"
        print(f"  {label}: OK  ttft={ttft_s} total={total:.0f} ms reply={reply!r}")
        record(f"responder {label}", bool(reply), f"ttft={ttft_s} total={total:.0f}ms")
        if eb is not None:
            return  # flag accepted; no need to retry without
    print("  (responder: see results above)")


async def test_plain(client: AsyncOpenAI, role: str, model: str) -> None:
    print(f"\n== {role} {model}")
    try:
        t0 = time.perf_counter()
        r = await client.chat.completions.create(model=model, messages=PROMPT, max_tokens=512)
        ms = (time.perf_counter() - t0) * 1000
        msg = r.choices[0].message
        reply = (msg.content or "").strip()
        reasoning = getattr(msg, "reasoning_content", None) or getattr(msg, "reasoning", None)
        extra = f" (+reasoning {len(reasoning)} chars)" if reasoning else ""
        print(f"  OK {ms:.0f} ms reply={reply[:200]!r}{extra} finish={r.choices[0].finish_reason}")
        record(f"{role} chat", bool(reply), f"{ms:.0f}ms")
    except Exception as e:
        print(f"  FAILED {short(e)}")
        record(f"{role} chat", False, short(e))


ADD_BOX_TOOL = {
    "type": "function",
    "function": {
        "name": "add_box",
        "description": "Add a box to the whiteboard.",
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "label": {"type": "string"},
                "x": {"type": "integer", "minimum": 0, "maximum": 1000},
                "y": {"type": "integer", "minimum": 0, "maximum": 1000},
                "kind": {"type": "string", "enum": ["service", "datastore", "client", "external", "note"]},
            },
            "required": ["id", "label", "x", "y", "kind"],
        },
    },
}


async def test_tools(client: AsyncOpenAI) -> None:
    model = config.MODEL_SMART
    print(f"\n== Tool calling on {model}")
    msgs = [
        {"role": "system", "content": "You draw architecture diagrams by calling tools."},
        {"role": "user", "content": "Add a datastore box for Redis (id 'redis') at x=500, y=300."},
    ]
    try:
        t0 = time.perf_counter()
        r = await client.chat.completions.create(
            model=model, messages=msgs, tools=[ADD_BOX_TOOL], tool_choice="auto", max_tokens=1024
        )
        ms = (time.perf_counter() - t0) * 1000
        msg = r.choices[0].message
        calls = msg.tool_calls or []
        if not calls:
            print(f"  tool calling: NOT OK ({ms:.0f} ms) no tool_calls; content={(msg.content or '')[:300]!r}")
            record("tool calling", False, "no tool_calls returned")
            return
        details = []
        ok = True
        for c in calls:
            try:
                args = json.loads(c.function.arguments)
                details.append(f"{c.function.name}({args})")
                ok = ok and c.function.name == "add_box" and args.get("id") == "redis"
            except Exception as e:
                ok = False
                details.append(f"{c.function.name} bad args {c.function.arguments!r}: {e}")
        print(f"  tool calling: {'OK' if ok else 'NOT OK'} ({ms:.0f} ms) {'; '.join(details)}")
        record("tool calling", ok, f"{ms:.0f}ms {'; '.join(details)[:120]}")
    except Exception as e:
        print(f"  tool calling: NOT OK {short(e)}")
        record("tool calling", False, short(e))


async def test_json_mode(client: AsyncOpenAI) -> None:
    model = config.MODEL_SMART
    print(f"\n== response_format=json_object on {model}")
    msgs = [{"role": "user", "content": 'Return a JSON object {"ops": [...]} with one op '
             '{"op":"add_box","id":"redis","label":"Redis","x":500,"y":300,"kind":"datastore"}. JSON only.'}]
    try:
        t0 = time.perf_counter()
        r = await client.chat.completions.create(
            model=model, messages=msgs, response_format={"type": "json_object"}, max_tokens=1024
        )
        ms = (time.perf_counter() - t0) * 1000
        content = r.choices[0].message.content or ""
        try:
            obj = json.loads(content)
            print(f"  json_object: OK ({ms:.0f} ms) {json.dumps(obj)[:200]}")
            record("json_object", True, f"{ms:.0f}ms")
        except ValueError:
            print(f"  json_object: accepted but output not valid JSON ({ms:.0f} ms): {content[:300]!r}")
            record("json_object", False, "invalid JSON output")
    except Exception as e:
        print(f"  json_object: NOT SUPPORTED {short(e)}")
        record("json_object", False, short(e))


def print_summary() -> None:
    print("\n== Summary")
    w = max((len(c) for c, _, _ in summary), default=10)
    for check, res, detail in summary:
        print(f"  {check:<{w}}  {res:<4}  {detail}")


async def main() -> int:
    if not config.CRUSOE_API_KEY:
        print("ERROR: CRUSOE_API_KEY is not set. Add it to server/.env (see server/.env.example).")
        return 2
    print(f"base_url: {config.CRUSOE_BASE_URL}")
    client = AsyncOpenAI(base_url=config.CRUSOE_BASE_URL, api_key=config.CRUSOE_API_KEY,
                         timeout=config.CRUSOE_TIMEOUT, max_retries=1)
    try:
        await list_models(client)
        if "--quick" not in sys.argv[1:]:
            await test_responder(client)
            for role in ("MODEL_FAST", "MODEL_SMART", "MODEL_COACH"):
                await test_plain(client, role, ROLES[role])
            await test_tools(client)
            await test_json_mode(client)
    finally:
        await client.close()
    print_summary()
    return 0 if all(r != "FAIL" for _, r, _ in summary) else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
