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


def test_addressed_without_name_rules():
    yes = {"addressed": {"noul": 0.9}, "kind": {"choice": "question", "confidence": 0.8}}
    assert mouth.addressed_without_name(yes, "what do you think about Redis here?")
    assert not mouth.addressed_without_name(yes, "Twin, what do you think?")  # lexical path handles it
    assert not mouth.addressed_without_name({"addressed": {"noul": 0.95}, "kind": {"choice": "small_talk", "confidence": 0.9}}, "haha nice weekend")
    assert not mouth.addressed_without_name({"addressed": {"noul": 0.4}}, "what do you think about Redis here?")


@pytest.mark.asyncio
async def test_unnamed_question_answered_after_pause(monkeypatch):
    monkeypatch.setattr(mouth, "_make_listener", lambda s: None)
    monkeypatch.setattr(mouth, "UNNAMED_PAUSE_MS", 50)
    calls = []

    async def fake_respond(session, reason, *, trigger_ts=None, updates=None):
        calls.append(reason)

    monkeypatch.setattr(mouth.responder, "respond", fake_respond)
    s = Session("unnamed")
    s.twin_mode = "proactive"
    m = mouth.attach(s)
    line = {"speaker": "Preet", "text": "what do you think about Redis here?", "ts": mouth._now_ms()}
    s.history.append(line)
    await m._answer_unnamed(s, line)
    await asyncio.sleep(0.05)
    assert calls and "without saying your name" in calls[0]
    await mouth.detach(s)


@pytest.mark.asyncio
async def test_waits_while_human_mid_sentence(monkeypatch):
    monkeypatch.setattr(mouth, "_make_listener", lambda s: None)
    monkeypatch.setattr(mouth, "MID_SENTENCE_GAP_MS", 150)
    started = []

    async def fake_respond(session, reason, *, trigger_ts=None, updates=None):
        started.append(mouth._now_ms())

    monkeypatch.setattr(mouth.responder, "respond", fake_respond)
    s = Session("mid")
    m = mouth.attach(s)
    t0 = mouth._now_ms()
    s.last_human_speech_ts = t0 + 100  # an interim arrived after the triggering final
    m.speak("name", trigger_ts=t0, updates=[])
    await asyncio.sleep(0.05)
    assert not started  # still waiting for the human to finish
    await asyncio.sleep(0.4)
    assert started
    await mouth.detach(s)


def test_draw_claims_dropped_on_empty_board():
    s = Session("b2")
    assert mouth.drop_false_draw_claims(s, ["Here's what I built: an airplane."]) == []
    assert mouth.drop_false_draw_claims(s, ["Avery owns auth/login.py."]) == ["Avery owns auth/login.py."]
    s.board.apply({"op": "add_box", "id": "web", "label": "Web", "x": 100, "y": 100, "kind": "client"})
    assert mouth.drop_false_draw_claims(s, ["I drew the web box."]) == ["I drew the web box."]
