"""PC-20: ghost "sketching" card on a visual reflex decision (no network)."""
import asyncio
import json

import pytest

import mouth
from agents import fast_decide
from session import Session


class FakeListener:
    def __init__(self, session):
        self.lines = []

    def submit(self, speaker, text, decision=None):
        self.lines.append((speaker, text, decision))

    async def close(self):
        pass


def _decision(visual="architecture", vconf=0.9, kind="claim", kconf=0.9):
    return {"wants_visual": {"choice": visual, "confidence": vconf},
            "kind": {"choice": kind, "confidence": kconf}}


@pytest.fixture
def room(monkeypatch, tmp_path):
    monkeypatch.setattr(mouth, "_make_listener", lambda s: FakeListener(s))
    monkeypatch.setattr(mouth, "LATENCY_LOG", tmp_path / "latency.jsonl")
    holder = {"decision": _decision()}

    async def fake_decide(state, questions):
        return holder["decision"]

    monkeypatch.setattr(fast_decide, "decide", fake_decide)
    s = Session("ghost")
    sent = []
    s._broadcast_observers.append(sent.append)
    return s, sent, holder, tmp_path / "latency.jsonl"


def _ghosts(sent, op="add_sticky"):
    return [m for m in sent if m.get("type") == "draw" and m.get("op") == op
            and str(m.get("id", "")).startswith("ghost_")]


@pytest.mark.asyncio
async def test_ghost_on_visual_not_on_none(room):
    s, sent, holder, _ = room
    s.active_topic = {"topic_id": "t_auth", "title": "Auth"}
    m = mouth.attach(s)
    holder["decision"] = _decision(visual="none", kind="question")
    await m.on_final_mind(s, {"speaker": "Preet", "text": "how are you", "ts": mouth._now_ms()})
    assert _ghosts(sent) == []
    holder["decision"] = _decision(visual="architecture", vconf=0.5, kind="claim")
    await m.on_final_mind(s, {"speaker": "Preet", "text": "maybe", "ts": mouth._now_ms()})
    assert _ghosts(sent) == []
    holder["decision"] = _decision()
    await m.on_final_mind(s, {"speaker": "Preet", "text": "API talks to Redis", "ts": mouth._now_ms()})
    g = _ghosts(sent)
    assert len(g) == 1 and g[0]["frame"] == "t_auth" and g[0]["by"] == "Twin" and g[0]["color"] == "blue"
    await m.on_final_mind(s, {"speaker": "Preet", "text": "and Postgres", "ts": mouth._now_ms()})
    assert len(_ghosts(sent)) == 1  # at most one at a time
    snap = json.dumps(s.board.snapshot())
    assert "ghost_" not in snap  # never lands on the board
    await mouth.detach(s)


@pytest.mark.asyncio
async def test_system_fact_shows_ghost(room):
    s, sent, holder, _ = room
    holder["decision"] = _decision(visual="none", kind="system_fact", kconf=0.8)
    m = mouth.attach(s)
    await m.on_final_mind(s, {"speaker": "Preet", "text": "API calls Redis", "ts": mouth._now_ms()})
    assert len(_ghosts(sent)) == 1 and "frame" not in _ghosts(sent)[0]
    await mouth.detach(s)


@pytest.mark.asyncio
async def test_ghost_removed_on_first_real_draw_and_logged(room):
    s, sent, _, log_path = room
    m = mouth.attach(s)
    await m.on_final_mind(s, {"speaker": "Preet", "text": "API talks to Redis", "ts": mouth._now_ms()})
    gid = _ghosts(sent)[0]["id"]
    assert await s.apply_draw({"op": "add_box", "id": "b1", "label": "API", "x": 100, "y": 100, "kind": "service"})
    await asyncio.sleep(0.01)
    assert [r["id"] for r in _ghosts(sent, "remove")] == [gid]
    assert m._ghost is None
    row = json.loads(log_path.read_text().strip().splitlines()[-1])
    assert row["stage"] == "first_shape" and row["first_shape_ms"] >= 0 and "ghost_ms" in row
    assert "ghost_" not in json.dumps(s.board.snapshot())
    await mouth.detach(s)


@pytest.mark.asyncio
async def test_ghost_removed_on_timeout(room, monkeypatch):
    monkeypatch.setattr(mouth, "GHOST_TIMEOUT_S", 0.05)
    s, sent, _, log_path = room
    m = mouth.attach(s)
    await m.on_final_mind(s, {"speaker": "Preet", "text": "API talks to Redis", "ts": mouth._now_ms()})
    gid = _ghosts(sent)[0]["id"]
    await asyncio.sleep(0.15)
    assert [r["id"] for r in _ghosts(sent, "remove")] == [gid]
    assert m._ghost is None and not log_path.exists()
    await mouth.detach(s)


@pytest.mark.asyncio
async def test_ghost_removed_on_reset(room):
    s, sent, _, _ = room
    m = mouth.attach(s)
    await m.on_final_mind(s, {"speaker": "Preet", "text": "API talks to Redis", "ts": mouth._now_ms()})
    gid = _ghosts(sent)[0]["id"]
    await s.clear_board()
    assert gid in [r["id"] for r in _ghosts(sent, "remove")]
    assert m._ghost is None
    await mouth.detach(s)


@pytest.mark.asyncio
async def test_sketch_ack_spoken_once_when_idle(monkeypatch):
    import mouth
    from session import Session
    s = Session("b3")
    sent = []

    async def bc(m):
        sent.append(m if isinstance(m, dict) else m.model_dump())

    monkeypatch.setattr(s, "broadcast", bc)
    m = mouth.Mouth(s)
    await m._say_cached(s, mouth.SKETCH_ACK)
    audio = [x for x in sent if x.get("type") == "audio"]
    assert len(audio) == 1 and audio[0]["text"] == mouth.SKETCH_ACK and audio[0]["last"] is True
    s.speaking = True
    await m._say_cached(s, mouth.SKETCH_ACK)
    assert len([x for x in sent if x.get("type") == "audio"]) == 1  # busy: stays quiet
