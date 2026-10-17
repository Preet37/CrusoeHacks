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
