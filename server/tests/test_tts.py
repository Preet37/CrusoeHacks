import asyncio
import base64
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from voice import tts  # noqa: E402
from voice.tts import SentenceSplitter, speak, speak_text  # noqa: E402


# ---------------------------------------------------------------- splitter

def split_all(text: str, chunk: int = 3) -> list[str]:
    s = SentenceSplitter()
    out: list[str] = []
    for i in range(0, len(text), chunk):
        out += s.feed(text[i:i + chunk])
    rest = s.flush()
    if rest:
        out.append(rest)
    return out


def test_basic_split():
    assert split_all("I put Redis next to auth. Does that look right to you? Great work here!") == [
        "I put Redis next to auth.",
        "Does that look right to you?",
        "Great work here!",
    ]


def test_min_four_words_carries_short_fragment():
    assert split_all("Yeah, one sec. I am adding the cache now. Done.") == [
        "Yeah, one sec. I am adding the cache now.",
        "Done.",
    ]


def test_decimals_not_split():
    assert split_all("The p99 is about 3.5 seconds right now. We can fix it.") == [
        "The p99 is about 3.5 seconds right now.",
        "We can fix it.",
    ]


def test_abbreviations_not_split():
    out = split_all(
        "Use a cache, e.g. Redis or Memcached for this. Ask Dr. Smith about it soon. "
        "Postgres vs. Mongo is the question here. That is i.e. the real issue today."
    )
    assert out == [
        "Use a cache, e.g. Redis or Memcached for this.",
        "Ask Dr. Smith about it soon.",
        "Postgres vs. Mongo is the question here.",
        "That is i.e. the real issue today.",
    ]


def test_mr_abbreviation():
    assert split_all("Mr. Jones wrote the auth service last year. Ok.") == [
        "Mr. Jones wrote the auth service last year.",
        "Ok.",
    ]


def test_no_split_until_whitespace_follows():
    s = SentenceSplitter()
    assert s.feed("This is a full sentence.") == []
    assert s.feed(" Next") == ["This is a full sentence."]
    assert s.flush() == "Next"
    assert s.flush() is None


def test_flush_empty():
    s = SentenceSplitter()
    assert s.flush() is None
    s.feed("   ")
    assert s.flush() is None


def test_multi_punctuation():
    assert split_all("Wait, is that really true?! Yes it is.") == ["Wait, is that really true?!", "Yes it is."]


# ---------------------------------------------------------------- speak

async def token_stream(text: str, delay: float = 0.001):
    for i in range(0, len(text), 4):
        await asyncio.sleep(delay)
        yield text[i:i + 4]


def make_synth(fail_on: set[str] | None = None, max_delay: float = 0.03):
    calls: list[str] = []

    async def synth(text: str):
        calls.append(text)
        await asyncio.sleep(random.uniform(0, max_delay))
        if fail_on and text in fail_on:
            return None
        return tts.pcm16_to_wav(text.encode())

    synth.calls = calls
    return synth


TEXT = (
    "I put Redis next to the auth service. It caches the session tokens now. "
    "The web app talks to auth over HTTPS. Does that match what you described? "
    "Let me know if anything is off."
)


@pytest.mark.asyncio
async def test_speak_ordered_and_last():
    random.seed(1)
    out: list[dict] = []

    async def emit(m):
        out.append(m)

    timings: dict = {}
    full = await speak(token_stream(TEXT), "s_1", emit, synth=make_synth(), timings=timings)
    assert [m["seq"] for m in out] == list(range(len(out)))
    assert len(out) == 5
    assert [m["last"] for m in out] == [False] * 4 + [True]
    assert " ".join(m["text"] for m in out) == TEXT
    assert full == TEXT
    for m in out:
        assert m["type"] == "audio" and m["say_id"] == "s_1"
        assert m["format"] == "wav" and m["sample_rate"] == 44100
        wav = base64.b64decode(m["data"])
        assert wav[:4] == b"RIFF"
        assert wav[44:].decode() == m["text"]
    for k in ("start", "first_token", "first_sentence", "first_audio", "done"):
        assert k in timings
    assert timings["first_token"] <= timings["first_sentence"] <= timings["first_audio"]


@pytest.mark.asyncio
async def test_speak_ordering_with_slow_first_synth():
    out: list[dict] = []

    async def emit(m):
        out.append(m)

    async def synth(text):
        await asyncio.sleep(0.1 if text.startswith("I put") else 0.0)
        return b"RIFFxxxx"

    await speak(token_stream(TEXT, 0), "s_2", emit, synth=synth)
    assert [m["seq"] for m in out] == [0, 1, 2, 3, 4]


@pytest.mark.asyncio
async def test_bounded_concurrency():
    active = 0
    peak = 0

    async def synth(text):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return b"RIFF"

    async def emit(m):
        pass

    async def instant():
        yield TEXT

    await speak(instant(), "s_c", emit, synth=synth, concurrency=2)
    assert peak == 2


@pytest.mark.asyncio
async def test_synth_none_gives_null_data():
    out: list[dict] = []

    async def emit(m):
        out.append(m)

    synth = make_synth(fail_on={"It caches the session tokens now."})
    await speak(token_stream(TEXT), "s_3", emit, synth=synth)
    assert out[1]["text"] == "It caches the session tokens now."
    assert out[1]["data"] is None
    assert all(m["data"] is not None for i, m in enumerate(out) if i != 1)


@pytest.mark.asyncio
async def test_synth_exception_gives_null_data():
    out: list[dict] = []

    async def emit(m):
        out.append(m)

    async def synth(text):
        raise RuntimeError("boom")

    await speak_text("This sentence will fail to synth.", "s_4", emit, synth=synth)
    assert len(out) == 1 and out[0]["data"] is None and out[0]["last"] is True


@pytest.mark.asyncio
async def test_empty_stream_emits_nothing():
    out: list[dict] = []

    async def emit(m):
        out.append(m)

    async def empty():
        if False:
            yield ""

    synth = make_synth()
    assert await speak(empty(), "s_5", emit, synth=synth) == ""
    assert out == []
    assert synth.calls == []
    assert await speak_text("", "s_5b", emit, synth=synth) == ""
    assert out == []


@pytest.mark.asyncio
async def test_short_text_single_last_chunk():
    out: list[dict] = []

    async def emit(m):
        out.append(m)

    await speak_text("Got it.", "s_6", emit, synth=make_synth())
    assert len(out) == 1
    assert out[0]["seq"] == 0 and out[0]["last"] is True and out[0]["text"] == "Got it."


@pytest.mark.asyncio
async def test_cancellation_stops_emits():
    out: list[dict] = []
    synth_cancelled = []

    async def emit(m):
        out.append(m)

    async def slow_synth(text):
        try:
            await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            synth_cancelled.append(text)
            raise
        return b"RIFF"

    async def endless():
        n = 0
        while True:
            await asyncio.sleep(0.005)
            n += 1
            yield f"This is sentence number {n}. "

    task = asyncio.create_task(speak(endless(), "s_7", emit, synth=slow_synth))
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    count = len(out)
    assert count >= 1
    assert all(m["last"] is False for m in out)
    await asyncio.sleep(0.2)
    assert len(out) == count
    assert synth_cancelled  # in-flight synth was cancelled


@pytest.mark.asyncio
async def test_synthesize_no_key_returns_none(monkeypatch):
    monkeypatch.setattr(tts.config, "NVIDIA_API_KEY", "")
    assert await tts.synthesize("Hello there friend.") is None


def test_ensure_wav_wraps_pcm():
    pcm = b"\x00\x01" * 100
    wav = tts.ensure_wav(pcm)
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE" and wav[44:] == pcm
    assert tts.ensure_wav(wav) == wav


@pytest.mark.asyncio
async def test_synthesize_retries_429_then_caches(monkeypatch):
    import voice.tts as t
    calls = []

    class R:
        def __init__(self, code):
            self.status_code, self.content, self.text = code, (b"RIFF" + b"\0" * 40 if code == 200 else b""), ""

    class C:
        async def post(self, *a, **k):
            calls.append(1)
            return R(429 if len(calls) == 1 else 200)

    monkeypatch.setattr(t.config, "NVIDIA_API_KEY", "x")
    monkeypatch.setattr(t, "RETRY_429_BACKOFF_S", (0.0, 0.0))
    t._CACHE.clear()
    b1 = await t.synthesize("hello there", client=C())
    b2 = await t.synthesize("hello there", client=C())
    assert b1 and b1 == b2 and len(calls) == 2
