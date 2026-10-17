"""NVIDIA Magpie TTS + streaming sentence pipeline.

- synthesize(text) -> WAV bytes or None
- SentenceSplitter: cuts a token stream into speakable sentences (min 4 words)
- speak(deltas, say_id, emit): token stream -> ordered PROTOCOL `audio` messages
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
from collections import OrderedDict
import re
import struct
import time
from typing import AsyncIterator, Awaitable, Callable, Optional

import httpx

import config

log = logging.getLogger("twin.tts")

MAGPIE_BASE = "https://877104f7-e885-42b9-8de8-f6e4c6303969.invocation.api.nvcf.nvidia.com"
MAGPIE_SYNTH_URL = f"{MAGPIE_BASE}/v1/audio/synthesize"
MAGPIE_VOICES_URL = f"{MAGPIE_BASE}/v1/audio/list_voices"
VOICE = os.getenv("TTS_VOICE", "Magpie-Multilingual.EN-US.Aria")
# Free-tier Magpie rate-limits bursts (HTTP 429): retry briefly, then fall back to data:null.
RETRY_429_BACKOFF_S: tuple[float, ...] = (0.4, 0.9)
CACHE_SIZE = 64  # repeated phrases (acks, greetings) skip the network
_CACHE: "OrderedDict[tuple[str, str], bytes]" = OrderedDict()
LANGUAGE = "en-US"
SAMPLE_RATE = 44100
TTS_TIMEOUT = 10.0
TTS_CONCURRENCY = 2

Emit = Callable[[dict], Awaitable[None]]
Synth = Callable[[str], Awaitable[Optional[bytes]]]

_client: httpx.AsyncClient | None = None


def _now_ms() -> float:
    return time.time() * 1000.0


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(TTS_TIMEOUT, connect=5.0))
    return _client


async def aclose() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def pcm16_to_wav(pcm: bytes, sample_rate: int = SAMPLE_RATE, channels: int = 1) -> bytes:
    """Wrap raw little-endian 16-bit PCM in a minimal WAV (RIFF) header."""
    bits = 16
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, sample_rate, byte_rate, block_align, bits)
    header += b"data" + struct.pack("<I", len(pcm))
    return header + pcm


def ensure_wav(data: bytes) -> bytes:
    if data[:4] == b"RIFF":
        return data
    return pcm16_to_wav(data)


async def synthesize(text: str, client: httpx.AsyncClient | None = None) -> bytes | None:
    """One Magpie call. Returns WAV bytes (RIFF), or None on any error / missing key."""
    text = (text or "").strip()
    if not text or not config.NVIDIA_API_KEY:
        if not config.NVIDIA_API_KEY:
            log.debug("NVIDIA_API_KEY not set; skipping TTS")
        return None
    c = client or get_client()
    form = {
        "text": text,
        "language": LANGUAGE,
        "voice": VOICE,
        "encoding": "LINEAR_PCM",
        "sample_rate_hz": str(SAMPLE_RATE),
    }
    key = (VOICE, text)
    cached = _CACHE.get(key)
    if cached is not None:
        _CACHE.move_to_end(key)
        return cached
    r = None
    for attempt, backoff in enumerate(RETRY_429_BACKOFF_S + (None,)):
        try:
            # (None, value) tuples force multipart/form-data with plain fields
            r = await c.post(
                MAGPIE_SYNTH_URL,
                files={k: (None, v) for k, v in form.items()},
                headers={"Authorization": f"Bearer {config.NVIDIA_API_KEY}"},
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # network, timeout
            log.warning("Magpie TTS request failed: %s: %s", type(e).__name__, e)
            return None
        if r.status_code == 429 and backoff is not None:
            log.info("Magpie TTS 429, retry %d in %.1fs", attempt + 1, backoff)
            await asyncio.sleep(backoff)
            continue
        break
    if r.status_code != 200:
        log.warning("Magpie TTS HTTP %s: %s", r.status_code, r.text[:300])
        return None
    if not r.content:
        log.warning("Magpie TTS returned empty body")
        return None
    wav = ensure_wav(r.content)
    _CACHE[key] = wav
    while len(_CACHE) > CACHE_SIZE:
        _CACHE.popitem(last=False)
    return wav


def cached(text: str) -> bytes | None:
    """WAV for text if it was synthesized before (current voice), else None. No network."""
    return _CACHE.get((VOICE, (text or "").strip()))


async def prewarm(texts: list[str]) -> int:
    """Synthesize phrases into the cache, one at a time (gentle on the 429 limit)."""
    n = 0
    for t in texts:
        if cached(t) is None and await synthesize(t):
            n += 1
    return n


# ---------------------------------------------------------------- splitting

_ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "vs", "prof", "sr", "jr", "st", "inc", "ltd",
    "co", "corp", "approx", "dept", "est", "fig", "no", "vol",
}
_TERMINATORS = ".?!"
_CLOSERS = "\"')]”’"


class SentenceSplitter:
    """Accumulates streamed text and returns complete sentences.

    A sentence ends at . ? ! followed by whitespace, once it has >= min_words
    words. Shorter fragments carry into the next sentence. Decimals (3.5) and
    common abbreviations (e.g., i.e., Mr., Dr., vs.) never split.
    """

    def __init__(self, min_words: int = 4):
        self.min_words = min_words
        self.buf = ""

    def feed(self, delta: str) -> list[str]:
        if not delta:
            return []
        self.buf += delta
        out: list[str] = []
        i = 0
        while i < len(self.buf):
            ch = self.buf[i]
            if ch not in _TERMINATORS:
                i += 1
                continue
            # extend over runs like "?!" / "..." and closing quotes/brackets
            j = i
            while j + 1 < len(self.buf) and self.buf[j + 1] in _TERMINATORS:
                j += 1
            while j + 1 < len(self.buf) and self.buf[j + 1] in _CLOSERS:
                j += 1
            if j + 1 >= len(self.buf):
                break  # don't know what follows yet
            if not self.buf[j + 1].isspace():
                i = j + 1  # e.g. "3.5", "e.g" mid-token
                continue
            if ch == "." and i == j and self._is_abbreviation(i):
                i = j + 1
                continue
            candidate = self.buf[: j + 1].strip()
            if len(candidate.split()) >= self.min_words:
                out.append(candidate)
                self.buf = self.buf[j + 1:].lstrip()
                i = 0
            else:
                i = j + 1
        return out

    def _is_abbreviation(self, dot_idx: int) -> bool:
        m = re.search(r"(\S+)$", self.buf[:dot_idx])
        if not m:
            return False
        word = m.group(1).lstrip("(\"'“‘").lower()
        if "." in word:  # e.g / i.e / u.s
            return True
        return word in _ABBREVIATIONS

    def flush(self) -> str | None:
        rest = self.buf.strip()
        self.buf = ""
        return rest or None


# ---------------------------------------------------------------- streaming

_END = object()


async def speak(
    deltas: AsyncIterator[str],
    say_id: str,
    emit: Emit,
    synth: Synth = synthesize,
    timings: dict | None = None,
    on_sentence: Callable[[int, str], None] | None = None,
    on_first_audio: Callable[[float], None] | None = None,
    concurrency: int = TTS_CONCURRENCY,
    min_words: int = 4,
) -> str:
    """Stream text deltas -> sentences -> TTS -> ordered `audio` messages.

    TTS for later sentences overlaps earlier ones (bounded by `concurrency`),
    but messages are emitted strictly in seq order. The final chunk has
    last=True. No text -> nothing emitted. On cancellation, in-flight synth
    tasks are cancelled and nothing further is emitted.

    `timings` (if given) is filled with ms epoch timestamps: start,
    first_token, first_sentence, first_audio, done.
    Returns the full spoken text.
    """
    t = timings if timings is not None else {}
    t.setdefault("start", _now_ms())
    sem = asyncio.Semaphore(max(1, concurrency))
    queue: asyncio.Queue = asyncio.Queue()
    synth_tasks: list[asyncio.Task] = []
    spoken: list[str] = []
    producer_error: list[BaseException] = []

    async def _synth(text: str) -> bytes | None:
        async with sem:
            try:
                return await synth(text)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("synth failed for say_id=%s: %s", say_id, e)
                return None

    def _enqueue(text: str) -> None:
        seq = len(spoken)
        spoken.append(text)
        if seq == 0:
            t["first_sentence"] = _now_ms()
        if on_sentence:
            try:
                on_sentence(seq, text)
            except Exception:
                log.exception("on_sentence callback failed")
        task = asyncio.create_task(_synth(text))
        synth_tasks.append(task)
        queue.put_nowait((seq, text, task))

    async def _produce() -> None:
        splitter = SentenceSplitter(min_words=min_words)
        try:
            async for delta in deltas:
                if not delta:
                    continue
                if "first_token" not in t:
                    t["first_token"] = _now_ms()
                for s in splitter.feed(delta):
                    _enqueue(s)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("text stream failed for say_id=%s: %s", say_id, e)
            producer_error.append(e)
        rest = splitter.flush()
        if rest:
            _enqueue(rest)
        queue.put_nowait(_END)

    producer = asyncio.create_task(_produce())
    try:
        item = await queue.get()
        while item is not _END:
            seq, text, task = item
            data = await task
            nxt = await queue.get()  # need to know whether this is the last chunk
            if data is not None and "first_audio" not in t:
                t["first_audio"] = _now_ms()
                if on_first_audio:
                    try:
                        on_first_audio(t["first_audio"])
                    except Exception:
                        log.exception("on_first_audio callback failed")
            await emit({
                "type": "audio",
                "say_id": say_id,
                "seq": seq,
                "format": "wav",
                "sample_rate": SAMPLE_RATE,
                "text": text,
                "data": base64.b64encode(data).decode("ascii") if data is not None else None,
                "last": nxt is _END,
            })
            item = nxt
        await producer
    finally:
        for task in synth_tasks:
            if not task.done():
                task.cancel()
        if not producer.done():
            producer.cancel()
        pending = [x for x in [producer, *synth_tasks] if not x.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    t["done"] = _now_ms()
    _log_timings(say_id, t)
    if producer_error:
        raise producer_error[0]
    return " ".join(spoken)


def _log_timings(say_id: str, t: dict) -> None:
    start = t.get("start")
    if start is None:
        return
    parts = []
    for k in ("first_token", "first_sentence", "first_audio", "done"):
        if k in t:
            parts.append(f"{k}=+{t[k] - start:.0f}ms")
    log.info("speak %s %s", say_id, " ".join(parts))


async def speak_text(
    text: str,
    say_id: str,
    emit: Emit,
    synth: Synth = synthesize,
    timings: dict | None = None,
    **kw,
) -> str:
    """Speak a fixed string (split into sentences, same ordering guarantees)."""

    async def one() -> AsyncIterator[str]:
        if text:
            yield text

    return await speak(one(), say_id, emit, synth=synth, timings=timings, **kw)
