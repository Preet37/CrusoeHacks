"""15:17 Twin contributes: step-1 phrase per visual, spoken contribution, narration (no network)."""
import asyncio

import pytest

import mouth
from agents import fast_decide
from session import Session
from voice import tts


class FakeListener:
    def __init__(self, session):
        pass

    def submit(self, *a, **k):
        pass

    async def close(self):
        pass


CONTRIB = {
    "type": "contribution", "id": "contrib_1", "topic_id": None, "show": "Auth architecture",
    "current": "Login lives in auth/login.py and reads users from Postgres via models/user.py. It is simple.",
    "impact": ["Moving sessions to Redis changes the token check in auth/login.py:18-24.", "Second impact."],
    "risks": ["Logout must invalidate the Redis key or old tokens keep working.", "Another risk."],
    "question": "What TTL should we pick for sessions",
    "sources": ["auth/login.py:18-24"],
}


def _sentences(text):
    return [x for x in mouth._SENT_SPLIT.split(text) if x]


@pytest.fixture
def room(monkeypatch, tmp_path):
    monkeypatch.setattr(mouth, "_make_listener", lambda s: FakeListener(s))
    monkeypatch.setattr(mouth, "LATENCY_LOG", tmp_path / "latency.jsonl")
    monkeypatch.setattr(mouth, "CONTRIB_GAP_MS", 50)
    spoken = []
    gate = {"event": None}

    async def fake_speak_text(text, say_id, emit, **kw):
        spoken.append(text)
        await emit({"type": "audio", "say_id": say_id, "seq": 0, "text": text, "data": None, "last": True})
        if gate["event"] is not None:
            await gate["event"].wait()
        return text

    monkeypatch.setattr(tts, "speak_text", fake_speak_text)

    async def fake_decide(state, questions):
        return {"wants_visual": {"choice": "architecture", "confidence": 0.9},
                "kind": {"choice": "claim", "confidence": 0.9}}

    monkeypatch.setattr(fast_decide, "decide", fake_decide)
    s = Session("contrib")
    sent = []
    s._broadcast_observers.append(sent.append)
    m = mouth.Mouth(s)  # no idle loop / brain runtime in tests
    s._broadcast_observers.append(m._on_broadcast)
    yield s, m, sent, spoken, gate
    s.cancel_all()


async def _settle(s, n=60):
    for _ in range(n):
        await asyncio.sleep(0.02)
        if not any(not t.done() for t in s._tasks.values()):
            return


def test_phrase_per_visual_type():
    seen = set()
    for visual in ("architecture", "sequence", "chart", "mockup", "table", "code", "3d"):
        p = mouth.visual_phrase({"wants_visual": {"choice": visual, "confidence": 0.9}})
        assert p == mouth.VISUAL_PHRASES[visual] and "sketching" not in p.lower()
        seen.add(p)
    assert len(seen) == 7
    assert set(mouth.VISUAL_PHRASES.values()) <= set(mouth.PREWARM_PHRASES)
    fact = mouth.visual_phrase({"wants_visual": {"choice": "none", "confidence": 0.9},
                                "kind": {"choice": "system_fact", "confidence": 0.9}})
    assert fact == mouth.FACT_PHRASE


@pytest.mark.asyncio
async def test_visual_line_plays_typed_phrase(room):
    s, m, sent, _, _ = room
    await m.on_final_mind(s, {"speaker": "Preet", "text": "how do the services connect", "ts": mouth._now_ms()})
    audio = [x for x in sent if x.get("type") == "audio"]
    assert audio and audio[0]["text"] == "Here's how those pieces connect."


def test_compose_max_four_sentences_keeps_file_names():
    text = mouth.compose_contribution(CONTRIB)
    sents = _sentences(text)
    assert len(sents) <= 4
    assert "auth/login.py" in text and "models/user.py" in text
    assert sents[-1].endswith("?")
    assert "Second impact" not in text and "Another risk" not in text
    with_narr = mouth.compose_contribution(CONTRIB, ["Browser", "Auth", "Postgres"])
    sents = _sentences(with_narr)
    assert len(sents) == 4 and sents[0] == "Left is Browser, then Auth, then Postgres."
    assert sents[-1].endswith("?")


def test_never_claims_code_was_changed():
    c = {**CONTRIB, "current": "I've updated auth/login.py to use Redis.", "impact": ["We changed the logout."]}
    text = mouth.compose_contribution(c)
    assert "updated" not in text and "changed the logout" not in text
    assert text.endswith("?")


@pytest.mark.asyncio
async def test_on_request_not_addressed_is_silent(room):
    s, m, sent, spoken, _ = room
    s.twin_mode = "on_request"
    await m.on_final_mind(s, {"speaker": "Preet", "text": "how do the services connect", "ts": mouth._now_ms()})
    await s.broadcast(dict(CONTRIB))
    await _settle(s)
    assert spoken == []


@pytest.mark.asyncio
async def test_off_mode_never_speaks(room):
    s, m, sent, spoken, _ = room
    s.twin_mode = "off"
    await m.on_final_mind(s, {"speaker": "Preet", "text": "Twin, draw the auth flow", "ts": mouth._now_ms()})
    await s.broadcast(dict(CONTRIB))
    await _settle(s)
    assert spoken == []


@pytest.mark.asyncio
async def test_addressed_spoken_once_with_transcript(room):
    s, m, sent, spoken, _ = room
    s.twin_mode = "on_request"
    await m.on_final_mind(s, {"speaker": "Preet", "text": "Twin, draw the auth architecture", "ts": mouth._now_ms()})
    m._phrase_until_ms = 0  # the phrase had no wav in tests
    await s.broadcast(dict(CONTRIB))
    await _settle(s)
    await s.broadcast(dict(CONTRIB))  # same id again: not repeated
    await _settle(s)
    assert len(spoken) == 1 and "auth/login.py" in spoken[0]
    assert len(_sentences(spoken[0])) <= 4
    twin_lines = [h for h in s.history if h["speaker"] == mouth.config.TWIN_NAME and "auth/login.py" in h["text"]]
    assert len(twin_lines) == 1
    assert not s.speaking and s.current_say_id is None


@pytest.mark.asyncio
async def test_proactive_speaks_after_gap(room):
    s, m, sent, spoken, _ = room
    s.twin_mode = "proactive"
    await m.on_final_mind(s, {"speaker": "Preet", "text": "how do the services connect", "ts": mouth._now_ms()})
    m._phrase_until_ms = 0
    await s.broadcast(dict(CONTRIB))
    await _settle(s)
    assert len(spoken) == 1


@pytest.mark.asyncio
async def test_barge_in_cancels_contribution(room):
    s, m, sent, spoken, gate = room
    gate["event"] = asyncio.Event()  # speech never finishes on its own
    s.twin_mode = "on_request"
    await m.on_final_mind(s, {"speaker": "Preet", "text": "Twin, show the auth architecture", "ts": mouth._now_ms()})
    m._phrase_until_ms = 0
    await s.broadcast(dict(CONTRIB))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if s.speaking:
            break
    assert s.speaking and spoken
    task = s._tasks.get("responder")
    assert task is not None and not task.done()
    await s.barge_in()
    await asyncio.sleep(0.02)
    assert task.cancelled() or task.done()
    assert not s.speaking and s.current_say_id is None
    assert not [h for h in s.history if "auth/login.py" in h["text"] and h["speaker"] == mouth.config.TWIN_NAME]


@pytest.mark.asyncio
async def test_narration_uses_real_labels_only(room):
    s, m, sent, spoken, _ = room
    s.twin_mode = "on_request"
    await m.on_final_mind(s, {"speaker": "Preet", "text": "Twin, draw the auth architecture", "ts": mouth._now_ms()})
    m._phrase_until_ms = 0
    for op in ({"op": "add_box", "id": "b2", "label": "Auth", "x": 300, "y": 0, "kind": "service"},
               {"op": "add_box", "id": "b1", "label": "Browser", "x": 0, "y": 0, "kind": "client"},
               {"op": "connect", "id": "e1", "from_id": "b1", "to_id": "b2", "label": "HTTPS"},
               {"op": "add_box", "id": "b3", "label": "Postgres", "x": 600, "y": 0, "kind": "datastore"}):
        await s.apply_draw({"type": "draw", **op})
    await s.broadcast(dict(CONTRIB))
    await _settle(s)
    assert len(spoken) == 1
    assert spoken[0].startswith("Left is Browser, then Auth, then Postgres.")
    assert "HTTPS" not in spoken[0]


@pytest.mark.asyncio
async def test_no_narration_when_nothing_drawn(room):
    s, m, sent, spoken, _ = room
    s.twin_mode = "on_request"
    await m.on_final_mind(s, {"speaker": "Preet", "text": "Twin, draw the auth architecture", "ts": mouth._now_ms()})
    m._phrase_until_ms = 0
    await s.broadcast(dict(CONTRIB))
    await _settle(s)
    assert len(spoken) == 1 and "Left is" not in spoken[0]
