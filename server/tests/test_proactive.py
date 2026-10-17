"""Proactive mode: backchannel at the first gap, faster contributions (no network)."""
import asyncio

import pytest

import mouth
from agents import fast_decide
from session import Session
from voice import tts

KIND = {"value": "decision"}
WAV_100MS = b"\0" * (44 + 8820)  # 100 ms of 16-bit mono 44.1 kHz


class FakeListener:
    def __init__(self, session):
        pass

    def submit(self, *a, **k):
        pass

    async def close(self):
        pass


def _contrib(cid="c1"):
    return {"type": "contribution", "id": cid, "current": "Login lives in auth/login.py.",
            "impact": [], "risks": [], "question": "Should sessions move to Redis", "sources": []}


@pytest.fixture
def room(monkeypatch, tmp_path):
    monkeypatch.setattr(mouth, "_make_listener", lambda s: FakeListener(s))
    monkeypatch.setattr(mouth, "LATENCY_LOG", tmp_path / "latency.jsonl")
    monkeypatch.setattr(tts, "cached", lambda text: WAV_100MS)
    spoken = []

    async def fake_speak_text(text, say_id, emit, **kw):
        spoken.append(text)
        await emit({"type": "audio", "say_id": say_id, "seq": 0, "text": text, "data": None, "last": True})
        return text

    monkeypatch.setattr(tts, "speak_text", fake_speak_text)

    async def fake_decide(state, questions):
        return {"wants_visual": {"choice": "none", "confidence": 0.9},
                "kind": {"choice": KIND["value"], "confidence": KIND.get("conf", 0.9)}}

    monkeypatch.setattr(fast_decide, "decide", fake_decide)
    s = Session("proactive")
    s.twin_mode = "proactive"
    sent = []
    s._broadcast_observers.append(sent.append)
    m = mouth.Mouth(s)
    s._broadcast_observers.append(m._on_broadcast)
    yield s, m, sent, spoken
    KIND.pop("conf", None)
    s.cancel_all()


def _cached_audio(sent):
    return [x["text"] for x in sent if isinstance(x, dict) and x.get("type") == "audio" and x.get("data")]


async def _line(s, m, text="we should move sessions into Redis today"):
    now = mouth._now_ms()
    s.last_human_speech_ts = now
    line = {"speaker": "Preet", "text": text, "ts": now}
    s.history.append(line)
    await m.on_final_mind(s, line)
    return line


async def _wait_for(pred, timeout=3.0):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return pred()


def test_backchannel_phrases_prewarmed():
    assert set(mouth.BACKCHANNEL_PHRASES.values()) <= set(mouth.PREWARM_PHRASES)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(mouth.BACKCHANNEL_PHRASES))
async def test_backchannel_per_kind(room, kind):
    s, m, sent, _ = room
    KIND["value"] = kind
    # system_fact >= 0.7 already gets the "pull up the repo" show phrase (ghost path)
    KIND["conf"] = 0.65 if kind == "system_fact" else 0.9
    await _line(s, m)
    await asyncio.sleep(0.3)
    assert _cached_audio(sent) == []  # waits for the 0.6 s gap
    assert await _wait_for(lambda: _cached_audio(sent))
    assert _cached_audio(sent) == [mouth.BACKCHANNEL_PHRASES[kind]]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["on_request", "off"])
async def test_no_backchannel_outside_proactive(room, mode):
    s, m, sent, _ = room
    s.twin_mode = mode
    KIND["value"] = "decision"
    await _line(s, m)
    await asyncio.sleep(1.0)
    assert _cached_audio(sent) == []


@pytest.mark.asyncio
async def test_no_backchannel_for_short_or_visual_lines(room, monkeypatch):
    s, m, sent, _ = room
    KIND["value"] = "claim"
    await _line(s, m, "Redis is fine")
    await asyncio.sleep(0.9)
    assert _cached_audio(sent) == []

    async def visual_decide(state, questions):
        return {"wants_visual": {"choice": "architecture", "confidence": 0.9},
                "kind": {"choice": "claim", "confidence": 0.9}}

    monkeypatch.setattr(fast_decide, "decide", visual_decide)
    await _line(s, m, "the API talks to Redis and Postgres")
    await asyncio.sleep(0.9)
    assert mouth.BACKCHANNEL_PHRASES["claim"] not in _cached_audio(sent)
    assert mouth.VISUAL_PHRASES["architecture"] in _cached_audio(sent)


@pytest.mark.asyncio
async def test_backchannel_rate_limited(room):
    s, m, sent, _ = room
    KIND["value"] = "decision"
    await _line(s, m)
    assert await _wait_for(lambda: len(_cached_audio(sent)) == 1)
    KIND["value"] = "question"
    await _line(s, m, "what happens to logout after that change")
    await asyncio.sleep(0.9)
    assert len(_cached_audio(sent)) == 1
    m._last_backchannel_ms -= mouth.BACKCHANNEL_RATE_MS  # 10 s later
    await _line(s, m, "what happens to logout after that change")
    assert await _wait_for(lambda: len(_cached_audio(sent)) == 2)
    assert _cached_audio(sent)[-1] == mouth.BACKCHANNEL_PHRASES["question"]


@pytest.mark.asyncio
async def test_no_backchannel_while_twin_speaking(room):
    s, m, sent, _ = room
    KIND["value"] = "task"
    s.speaking = True
    await _line(s, m)
    await asyncio.sleep(0.9)
    assert _cached_audio(sent) == []


@pytest.mark.asyncio
async def test_no_backchannel_while_responder_runs(room):
    s, m, sent, _ = room
    KIND["value"] = "task"
    s.spawn("responder", asyncio.sleep(5))
    await _line(s, m)
    await asyncio.sleep(0.9)
    assert _cached_audio(sent) == []


@pytest.mark.asyncio
async def test_no_backchannel_in_hold(room):
    s, m, sent, _ = room
    KIND["value"] = "system_fact"
    m.state.hold_until_ms = mouth._now_ms() + 30000
    await _line(s, m)
    await asyncio.sleep(0.9)
    assert _cached_audio(sent) == []


@pytest.mark.asyncio
async def test_contribution_speaks_after_short_gap(room):
    s, m, _, spoken = room
    assert mouth.CONTRIB_GAP_MS == 700
    s.last_human_speech_ts = mouth._now_ms()
    await s.broadcast(_contrib())
    await asyncio.sleep(0.5)
    assert spoken == []
    assert await _wait_for(lambda: spoken, timeout=1.0)
    assert "auth/login.py" in spoken[0]


@pytest.mark.asyncio
async def test_contribution_waits_while_human_talks(room):
    s, m, _, spoken = room
    for _ in range(10):  # human keeps talking for ~1.5 s
        s.last_human_speech_ts = mouth._now_ms()
        if _ == 0:
            await s.broadcast(_contrib("c2"))
        await asyncio.sleep(0.15)
    assert spoken == []
    assert await _wait_for(lambda: spoken, timeout=1.5)


@pytest.mark.asyncio
async def test_contribution_follows_backchannel(room, monkeypatch):
    s, m, sent, spoken = room
    monkeypatch.setattr(mouth, "CONTRIB_GAP_MS", 5000)  # only the backchannel chain can release it
    KIND["value"] = "decision"
    await _line(s, m)
    await s.broadcast(_contrib("c3"))
    assert await _wait_for(lambda: _cached_audio(sent))
    bc_at = mouth._now_ms()
    assert await _wait_for(lambda: spoken, timeout=1.0)
    assert mouth._now_ms() - bc_at < 600


@pytest.mark.asyncio
async def test_contribution_does_not_chain_if_human_resumes(room, monkeypatch):
    s, m, sent, spoken = room
    monkeypatch.setattr(mouth, "CONTRIB_GAP_MS", 5000)
    KIND["value"] = "decision"
    await _line(s, m)
    await s.broadcast(_contrib("c4"))
    assert await _wait_for(lambda: _cached_audio(sent))
    s.last_human_speech_ts = mouth._now_ms() + 1  # someone started talking
    await asyncio.sleep(0.6)
    assert spoken == []
