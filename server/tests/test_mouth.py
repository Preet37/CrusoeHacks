import asyncio

import pytest

import mouth
from session import Session


@pytest.mark.asyncio
async def test_attach_is_idempotent_and_detach_stops_everything(monkeypatch):
    class FakeListener:
        closed = False

        def __init__(self, session):
            pass

        def submit(self, speaker, text):
            pass

        async def close(self):
            FakeListener.closed = True

    monkeypatch.setattr(mouth, "_make_listener", lambda s: FakeListener(s))
    s = Session("t")
    m1 = mouth.attach(s)
    assert mouth.attach(s) is m1
    assert s.decide_and_respond is not None
    idle = m1._idle_task
    await mouth.detach(s)
    await asyncio.sleep(0)
    assert idle.cancelled() or idle.done()
    assert FakeListener.closed
    assert s.decide_and_respond is None and getattr(s, "mouth", None) is None
    m2 = mouth.attach(s)  # reconnect re-wires once
    assert m2 is not m1
    await mouth.detach(s)


@pytest.mark.asyncio
async def test_not_now_raises_hand(monkeypatch):
    monkeypatch.setattr(mouth, "_make_listener", lambda s: None)
    s = Session("t2")
    m = mouth.attach(s)
    await m.on_final(s, {"speaker": "Preet", "text": "hold on, not now", "ts": 0})
    assert s.status == "hand_raised"
    await mouth.detach(s)


@pytest.mark.asyncio
async def test_newest_wins_restarts_unstarted_turn(monkeypatch):
    monkeypatch.setattr(mouth, "_make_listener", lambda s: None)
    calls = []
    gate = asyncio.Event()

    async def fake_respond(session, reason, *, trigger_ts=None, updates=None):
        calls.append((reason, list(updates or [])))
        await gate.wait()  # never produces audio until released

    monkeypatch.setattr(mouth.responder, "respond", fake_respond)
    s = Session("nw")
    m = mouth.attach(s)
    s.push_update("I drew redis")
    await m.on_final(s, {"speaker": "Preet", "text": "Twin, what do you think?", "ts": 1})
    await asyncio.sleep(0)
    first = s._tasks["responder"]
    await m.on_final(s, {"speaker": "Preet", "text": "and also about billing", "ts": 2})
    await asyncio.sleep(0)
    assert first.cancelled() or first.done()
    assert [c[0] for c in calls] == ["name", "name"]
    assert calls[1][1] == ["I drew redis"]  # updates carried into the restarted turn
    gate.set()
    await mouth.detach(s)


@pytest.mark.asyncio
async def test_started_turn_is_not_restarted(monkeypatch):
    monkeypatch.setattr(mouth, "_make_listener", lambda s: None)
    calls = []

    async def fake_respond(session, reason, *, trigger_ts=None, updates=None):
        calls.append(reason)
        session.speaking = True
        await asyncio.sleep(10)

    monkeypatch.setattr(mouth.responder, "respond", fake_respond)
    s = Session("nw2")
    m = mouth.attach(s)
    await m.on_final(s, {"speaker": "Preet", "text": "Twin, go ahead", "ts": 1})
    await asyncio.sleep(0)
    await m.on_final(s, {"speaker": "Preet", "text": "mm interesting", "ts": 2})
    await asyncio.sleep(0)
    assert calls == ["name"]
    await mouth.detach(s)


@pytest.mark.asyncio
async def test_mind_gets_one_decision_in_order_and_fails_open(monkeypatch):
    submitted = []

    class L:
        def __init__(self, s):
            pass

        def submit(self, speaker, text, *, decision=None):
            submitted.append((text, decision))

        async def close(self):
            pass

    monkeypatch.setattr(mouth, "_make_listener", lambda s: L(s))
    from agents import fast_decide

    async def fake_decide(state, questions):
        if "slow" in state["latest_sentence"]:
            await asyncio.sleep(0.05)
        if "boom" in state["latest_sentence"]:
            raise RuntimeError("down")
        return {"kind": {"choice": "system_fact", "confidence": 0.9}}

    monkeypatch.setattr(fast_decide, "decide", fake_decide)
    s = Session("jev")
    m = mouth.attach(s)
    t1 = asyncio.create_task(m.on_final_mind(s, {"speaker": "P", "text": "slow one"}))
    await asyncio.sleep(0)
    t2 = asyncio.create_task(m.on_final_mind(s, {"speaker": "P", "text": "boom two"}))
    await asyncio.gather(t1, t2)
    assert [t for t, _ in submitted] == ["slow one", "boom two"]
    assert submitted[0][1]["kind"]["choice"] == "system_fact"
    assert submitted[1][1] is None
    await mouth.detach(s)
