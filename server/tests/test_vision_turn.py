"""Vision turn: a fresh shared screen + a deictic line runs screen_vision, annotates, and speaks (no network)."""
import asyncio

import pytest

import mouth
from agents import screen_vision
from session import Session
from voice import tts


class FakeListener:
    def __init__(self, session):
        pass

    def submit(self, *a, **k):
        pass

    async def close(self):
        pass


@pytest.fixture
def room(monkeypatch, tmp_path):
    monkeypatch.setattr(mouth, "_make_listener", lambda s: FakeListener(s))
    monkeypatch.setattr(mouth, "LATENCY_LOG", tmp_path / "latency.jsonl")
    spoken, calls = [], []

    async def fake_speak_text(text, say_id, emit, **kw):
        spoken.append(text)
        await emit({"type": "audio", "say_id": say_id, "seq": 0, "text": text, "data": None, "last": True})
        return text

    async def fake_analyze(jpeg, question, transcript):
        calls.append((jpeg, question, list(transcript)))
        return {"say": "That's the PROJ-12 ticket, assigned to nobody.",
                "items": [{"kind": "box", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.1, "text": "PROJ-12"}]}

    monkeypatch.setattr(tts, "speak_text", fake_speak_text)
    monkeypatch.setattr(tts, "cached", lambda text: None)
    monkeypatch.setattr(screen_vision, "analyze", fake_analyze)
    s = Session("vision")
    s.twin_mode = "proactive"
    sent = []
    s._broadcast_observers.append(sent.append)
    m = mouth.Mouth(s)
    yield s, m, sent, spoken, calls
    s.cancel_all()


async def _settle(s, n=100):
    for _ in range(n):
        await asyncio.sleep(0.02)
        if not any(not t.done() for t in s._tasks.values()):
            return


def _share(s, age_ms=0):
    s.screen = {"type": "screen_frame", "user": "Preet", "jpeg": "abc", "w": 1280, "h": 800,
                "ts": mouth._now_ms() - age_ms}


def test_trigger_needs_fresh_frame_and_deictic_line():
    s = Session("t")
    s.twin_mode = "proactive"
    assert not mouth.wants_vision_turn(s, "what does it look like?")  # nobody sharing
    _share(s, age_ms=25_000)
    assert not mouth.wants_vision_turn(s, "what does it look like?")  # stale frame
    _share(s)
    assert mouth.wants_vision_turn(s, "what does it look like? how does it look?")
    assert mouth.wants_vision_turn(s, "what is this ticket")
    assert not mouth.wants_vision_turn(s, "let's assign Parsh the billing task")
    assert mouth.wants_vision_turn(s, "anything", {"kind": {"choice": "question", "confidence": 0.9}})
    s.twin_mode = "off"
    assert not mouth.wants_vision_turn(s, "what does it look like?")
    s.twin_mode = "on_request"
    assert mouth.wants_vision_turn(s, "what does it look like?")       # direct question
    assert not mouth.wants_vision_turn(s, "look at this screen")        # not asked
    assert mouth.wants_vision_turn(s, "Twin, look at this screen")      # named
    assert mouth.VISION_ACK in mouth.PREWARM_PHRASES


@pytest.mark.asyncio
async def test_vision_turn_annotates_and_speaks(room):
    s, m, sent, spoken, calls = room
    _share(s)
    await s.add_line("Preet", "we fixed the auth flow earlier")
    line = await s.add_line("Preet", "what does it look like? how does it look?")
    await m.on_final(s, line)
    await _settle(s)
    assert len(calls) == 1
    jpeg, question, transcript = calls[0]
    assert jpeg == "abc" and question == "what does it look like? how does it look?"
    assert transcript and len(transcript) <= mouth.VISION_HISTORY_LINES
    ann = [x for x in sent if x.get("type") == "annotate"]
    assert len(ann) == 1
    a = ann[0]
    assert a["target"] == "screen" and a["user"] == "Preet" and a["ttl_s"] == 20
    assert a["id"].startswith("twin_") and a["by"] == "Twin"
    assert a["items"][0]["kind"] == "box"
    assert spoken == ["That's the PROJ-12 ticket, assigned to nobody."]
    texts = [h["text"] for h in s.history if h["speaker"] == "Twin"]
    assert mouth.VISION_ACK in texts and spoken[0] in texts
    assert s.status == "listening" and not s.speaking


@pytest.mark.asyncio
async def test_no_vision_without_share(room):
    s, m, sent, spoken, calls = room
    line = await s.add_line("Preet", "what does it look like?")
    await m.on_final(s, line)
    await _settle(s)
    assert calls == [] and not [x for x in sent if x.get("type") == "annotate"]


@pytest.mark.asyncio
async def test_vision_failure_says_honest_line(room, monkeypatch):
    s, m, sent, spoken, calls = room

    async def broken(*a, **k):
        raise ValueError("bad json")

    monkeypatch.setattr(screen_vision, "analyze", broken)
    _share(s)
    line = await s.add_line("Preet", "what is this on my screen?")
    await m.on_final(s, line)
    await _settle(s)
    assert spoken == [mouth.VISION_FAIL]
    assert not [x for x in sent if x.get("type") == "annotate"]
