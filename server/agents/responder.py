"""The mouth: short spoken replies streamed from Nemotron into TTS.

The responder never waits on the mind. It reads recent transcript, the board
summary and pending work updates, streams a reply, and hands the token stream
to voice.tts which emits `audio` chunks sentence by sentence.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

import config
import llm

log = logging.getLogger(__name__)

PROMPT = Path(__file__).parent / "prompts" / "responder.md"
HISTORY_LINES = 20
MAX_TOKENS = 120
LATENCY_LOG = Path(__file__).resolve().parent.parent / "learning" / "runs" / "latency.jsonl"
NO_THINK = {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}

_think_flag_ok: bool | None = None

# Instant acknowledgment (ARCH 1E.5 rule 5): pre-synthesized at startup, played the
# moment Twin is addressed so the call never sits in silence while TTS works.
ACKS = ["Yeah, one sec.", "Mm-hm, good question.", "Sure, let me check."]
ACK_REASONS = {"name", "push_to_talk"}
_ack_i = 0


def pick_ack() -> tuple[str, bytes] | None:
    global _ack_i
    try:
        from voice import tts
    except Exception:  # noqa: BLE001
        return None
    for _ in range(len(ACKS)):
        text = ACKS[_ack_i % len(ACKS)]
        _ack_i += 1
        wav = tts.cached(text)
        if wav:
            return text, wav
    return None  # learned at runtime: does Crusoe accept the no-think flag?


def _now_ms() -> int:
    return int(time.time() * 1000)


def load_prompt() -> str:
    """Loaded at call time so the Coach can rewrite the file between meetings."""
    return PROMPT.read_text(encoding="utf-8").strip()


def build_messages(history: list[dict], board_summary: str, updates: list[str],
                   reason: str, memory: list[str] | None = None) -> list[dict]:
    """Stable system prefix first (prompt caching on Crusoe), changing context after."""
    lines = [f"{h.get('speaker', '?')}: {h.get('text', '')}" for h in history[-HISTORY_LINES:]]
    context = [
        "Transcript (most recent last):",
        "\n".join(lines) if lines else "(nothing yet)",
        "",
        "Whiteboard: " + (board_summary.strip() or "empty"),
    ]
    if updates:
        context += ["", "Work updates to mention (things you just did):"] + [f"- {u}" for u in updates]
    if memory:
        context += ["", "Cited memory:"] + [f"- {m}" for m in memory]
    context += ["", f"Why you are speaking now: {reason}. Reply as Twin, out loud, now."]
    return [
        {"role": "system", "content": load_prompt()},
        {"role": "user", "content": "\n".join(context)},
    ]


_THINK_RE = re.compile(r"<think>.*?</think>", re.S)
_MD_RE = re.compile(r"[*_#`]+")


async def clean_stream(deltas: AsyncIterator[str]) -> AsyncIterator[str]:
    """Drop <think> blocks and markdown symbols so TTS never reads them."""
    buf = ""
    in_think = False
    async for d in deltas:
        buf += d
        out = ""
        while buf:
            if in_think:
                end = buf.find("</think>")
                if end < 0:
                    buf = buf[-8:]  # keep a tail in case the closing tag is split
                    break
                buf = buf[end + 8:]
                in_think = False
                continue
            start = buf.find("<think>")
            if start >= 0:
                out += buf[:start]
                buf = buf[start + 7:]
                in_think = True
                continue
            lt = buf.rfind("<")
            if lt >= 0 and len(buf) - lt < 8 and "<think>".startswith(buf[lt:]):
                out += buf[:lt]  # possible partial tag, wait for more
                buf = buf[lt:]
            else:
                out += buf
                buf = ""
            break
        out = _MD_RE.sub("", out)
        if out:
            yield out
    if buf and not in_think:
        tail = _MD_RE.sub("", _THINK_RE.sub("", buf))
        if tail:
            yield tail


_PREFIX_RE = re.compile(r"^\s*(?:(?:hey,?\s+|hi,?\s+)?(?:it's\s+)?twin(?:\s+here)?\s*[:.,!\-]\s*)+", re.I)


async def strip_prefix(deltas: AsyncIterator[str]) -> AsyncIterator[str]:
    """Models like to start with a speaker label ("Twin: ..."); never speak it."""
    head = ""
    done = False
    async for d in deltas:
        if done:
            yield d
            continue
        head += d
        if len(head) < 14 and any(p.startswith(head.strip().lower()) for p in ("twin here", "it's twin", "hey, twin", "hi, twin")):
            continue  # could still be the start of a label
        done = True
        head = _PREFIX_RE.sub("", head)
        if head:
            yield head
    if not done and head:
        head = _PREFIX_RE.sub("", head)
        if head:
            yield head


async def model_stream(messages: list[dict], stream: Callable[..., AsyncIterator[str]] | None = None
                       ) -> AsyncIterator[str]:
    """Responder model with thinking off; fall back to the plain call, then to MODEL_FAST.

    Fallbacks only happen before the first token, so nothing is ever spoken twice.
    """
    global _think_flag_ok
    stream = stream or llm.stream_chat
    attempts: list[tuple[str, dict]] = []
    if _think_flag_ok is not False:
        attempts.append((config.MODEL_RESPONDER, NO_THINK))
    attempts.append((config.MODEL_RESPONDER, {}))
    attempts.append((config.MODEL_FAST, NO_THINK))
    last_exc: Exception | None = None
    for model, extra in attempts:
        started = False
        try:
            async for d in stream(model, messages, max_tokens=MAX_TOKENS, temperature=0.6, **extra):
                if not started and extra is NO_THINK and model == config.MODEL_RESPONDER:
                    _think_flag_ok = True
                started = True
                yield d
            if started:
                return
        except Exception as exc:  # noqa: BLE001 - we log and fall through
            if started:
                raise
            last_exc = exc
            if extra is NO_THINK and model == config.MODEL_RESPONDER:
                _think_flag_ok = False
            log.warning("responder: %s %s failed before first token: %s", model,
                        "no-think" if extra else "plain", type(exc).__name__)
    if last_exc:
        raise last_exc


def log_latency(row: dict) -> None:
    try:
        LATENCY_LOG.parent.mkdir(parents=True, exist_ok=True)
        with LATENCY_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except OSError as exc:
        log.warning("latency log failed: %s", exc)
    log.info("latency %s", json.dumps(row))


async def respond(session, reason: str, *, trigger_ts: int | None = None,
                  updates: list[str] | None = None,
                  stream: Callable[..., AsyncIterator[str]] | None = None,
                  speak: Callable[..., Awaitable[str]] | None = None) -> str:
    """Speak one Twin turn. Run as the session's cancellable "responder" task.

    `updates` are work updates already popped from the session queue.
    Returns the spoken text ("" if nothing was said or the turn failed).
    """
    if speak is None:
        from voice.tts import speak as speak  # late import keeps tests light
    trigger_ts = trigger_ts or _now_ms()
    updates = updates or []
    say_id = session.new_say_id()
    session.current_say_id = say_id
    timings: dict[str, Any] = {}
    await session.set_status("thinking")

    board = getattr(session, "board", None)
    summary = board.summary() if board is not None and hasattr(board, "summary") else ""
    messages = build_messages(list(session.history), summary, updates, reason)

    first_audio = {"sent": False}

    async def emit(msg: dict) -> None:
        if session.current_say_id != say_id:
            return  # barge-in or a newer turn took over
        if not first_audio["sent"]:
            first_audio["sent"] = True
            timings["first_audio_sent_ms"] = _now_ms()
            session.speaking = True
            await session.set_status("speaking")
        await session.broadcast(msg)

    # Instant ack as seq 0; the real answer follows as seq 1..n.
    ack = pick_ack() if reason in ACK_REASONS else None
    seq_offset = 0
    if ack is not None:
        import base64
        await emit({"type": "audio", "say_id": say_id, "seq": 0, "format": "wav", "sample_rate": 44100,
                    "text": ack[0], "data": base64.b64encode(ack[1]).decode("ascii"), "last": False})
        seq_offset = 1

    async def emit_after_ack(msg: dict) -> None:
        if seq_offset and msg.get("type") == "audio":
            msg = {**msg, "seq": msg.get("seq", 0) + seq_offset}
        await emit(msg)

    spoken = ""
    try:
        spoken = await speak(strip_prefix(clean_stream(model_stream(messages, stream))), say_id, emit_after_ack, timings=timings)
        spoken = (spoken or "").strip()
        if ack is not None and session.current_say_id == say_id:
            if not spoken:  # close the turn so the player is not left waiting
                await emit({"type": "audio", "say_id": say_id, "seq": 1, "format": "wav", "sample_rate": 44100,
                            "text": "", "data": None, "last": True})
            spoken = f"{ack[0]} {spoken}".strip()
        if spoken and session.current_say_id == say_id:
            await session.add_line(config.TWIN_NAME, spoken)
    except Exception as exc:  # noqa: BLE001
        log.exception("responder failed")
        await session.broadcast({"type": "error", "message": f"Responder failed: {type(exc).__name__}",
                                 "recoverable": True})
        # put undelivered updates back so they are not lost
        for u in updates:
            session.push_update(u)
    finally:
        if session.current_say_id == say_id:
            session.current_say_id = None
            session.speaking = False
            await session.set_status("listening")
        row = {"ts": trigger_ts, "say_id": say_id, "reason": reason, "chars": len(spoken)}
        ft = timings.get("first_token")
        fs = timings.get("first_sentence")
        fa = timings.get("first_audio")
        if ft:
            row["final_to_first_token_ms"] = ft - trigger_ts
        if ft and fs:
            row["first_token_to_sentence_ms"] = fs - ft
        if fs and fa:
            row["sentence_to_audio_ms"] = fa - fs
        if timings.get("first_audio_sent_ms"):
            row["final_to_audio_sent_ms"] = timings["first_audio_sent_ms"] - trigger_ts
        log_latency(row)
    return spoken
