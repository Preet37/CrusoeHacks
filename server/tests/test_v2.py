"""v2 server features: protocol additions, echo guard, participants, topic,
draw_error routing, calendar_join brief, connect_repo. No network calls."""
import asyncio
import json
import uuid

import pytest
from fastapi.testclient import TestClient

import mouth
import protocol
import session as session_mod
from app import app
from session import TWIN_SPEAKER, Session, get_session, is_echo


@pytest.fixture(autouse=True)
def _no_metrics_feed(monkeypatch, tmp_path):
    """Coach metrics (M3) are sent on join when the file exists; keep these sequences exact."""
    import app as _app
    monkeypatch.setattr(_app, "METRICS_FILE", tmp_path / "none.json")


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    def of(self, type_):
        return [m for m in self.sent if m["type"] == type_]


def _room():
    return f"v2_{uuid.uuid4().hex[:8]}"


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


# ---------------------------------------------------------------- protocol

def test_protocol_v2_models():
    assert protocol.PROTOCOL_VERSION == 2
    snap = {"type": "board_snapshot", "boxes": [{"id": "a", "label": "A", "x": 1, "y": 2, "kind": "service",
                                                  "frame": "t1"}],
            "edges": [], "shapes": [{"op": "add_sticky", "id": "s1", "text": "hi"}], "active_frame": "t1"}
    assert protocol.dump(protocol.parse(snap)) == snap
    d = {"type": "draw", "op": "add_chart", "id": "c", "chart": "scatter", "series": [{"name": "s",
         "values": [[1, 2]]}], "extra_thing": 5}
    assert protocol.dump(protocol.parse(d)) == d  # extra fields survive
    assert isinstance(protocol.parse({"type": "draw_error", "id": "m1", "op": "add_mermaid", "error": "x"}),
                      protocol.DrawError)
    assert protocol.parse({"type": "calendar_join", "meeting": "M"}).meeting == "M"
    assert protocol.parse({"type": "connect_repo", "url": "https://x/y"}).url == "https://x/y"
    with pytest.raises(ValueError):
        protocol.parse({"type": "draw", "op": "teleport"})


# ---------------------------------------------------------------- echo guard

def test_is_echo_rules():
    twin = ["I put Redis next to auth so sessions survive restarts."]
    assert is_echo("I put Redis next to auth so sessions survive restarts", twin)
    assert is_echo("redis next to auth so", twin)                    # >= 4-word substring
    assert is_echo("I put redis next to off so sessions survive restarts", twin)  # fuzzy
    assert not is_echo("what about the billing webhooks", twin)
    assert not is_echo("yes", ["Yes."])                               # too short to judge
    assert not is_echo("anything", [])


@pytest.mark.asyncio
async def test_echo_final_is_dropped_everywhere():
    s = Session("echo")
    ws = FakeWS()
    s.add_socket(ws, "Preet")
    hooked = []

    async def hook(sess, line):
        hooked.append(line)

    s.on_final_text(hook)
    s.decide_and_respond = hook
    await s.add_line(TWIN_SPEAKER, "Sounds good. I added a Redis box next to auth.")
    ws.sent.clear()
    before = len(s.history)

    await s.handle_message(ws, "Preet", protocol.parse(
        {"type": "user_text", "speaker": "Preet", "text": "i added a redis box next to auth", "final": True}))
    await s.handle_message(ws, "Preet", protocol.parse(
        {"type": "user_text", "speaker": "Preet", "text": "added a redis box", "final": False}))
    await _settle()
    assert len(s.history) == before and hooked == [] and ws.sent == []
    assert s.last_heard.get("Preet") is None and s.last_human_speech_ts == 0

    await s.handle_message(ws, "Preet", protocol.parse(
        {"type": "user_text", "speaker": "Preet", "text": "great, now the billing service", "final": True}))
    await _settle()
    assert len(s.history) == before + 1 and len(hooked) == 2 and ws.of("transcript")


# ---------------------------------------------------------------- participants

@pytest.fixture
def _no_mouth(monkeypatch):
    monkeypatch.setattr(mouth, "attach", lambda session: None)


def _recv_type(ws, type_, limit=20):
    for _ in range(limit):
        msg = ws.receive_json()
        if msg["type"] == type_:
            return msg
    raise AssertionError(f"no {type_}")


def test_two_clients_see_each_other(_no_mouth):
    room = _room()
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws?room={room}&user=Preet") as a:
            p = _recv_type(a, "participants")
            assert p["people"] == [{"name": "Preet", "kind": "human", "speaking": False},
                                   {"name": TWIN_SPEAKER, "kind": "ai", "speaking": False}]
            with client.websocket_connect(f"/ws?room={room}&user=Parsh") as b:
                names = [x["name"] for x in _recv_type(b, "participants")["people"]]
                assert names == ["Preet", "Parsh", TWIN_SPEAKER]
                assert [x["name"] for x in _recv_type(a, "participants")["people"]] == names
                # hello with a new name updates everyone
                b.send_json({"type": "hello", "protocol": 2, "user": "Parsh K", "room": room})
                names = [x["name"] for x in _recv_type(a, "participants")["people"]]
                assert names == ["Preet", "Parsh K", TWIN_SPEAKER]
    # (TestClient cancels the server task on close, so "leave" is covered in the unit test below)


@pytest.mark.asyncio
async def test_leave_broadcasts_participants():
    s = Session("leave")
    a, b = FakeWS(), FakeWS()
    s.add_socket(a, "Preet")
    s.add_socket(b, "Parsh")
    await s.broadcast_participants()
    s.remove_socket(b)
    assert await s.broadcast_participants() is True
    assert [p["name"] for p in a.of("participants")[-1]["people"]] == ["Preet", TWIN_SPEAKER]


@pytest.mark.asyncio
async def test_participants_dedupe_and_speaking_diff():
    s = Session("p")
    ws1, ws2 = FakeWS(), FakeWS()
    s.add_socket(ws1, "Preet")
    s.add_socket(ws2, "Preet")  # same name twice (two tabs) -> one person
    assert [p["name"] for p in s.participants()] == ["Preet", TWIN_SPEAKER]
    assert await s.broadcast_participants() is True
    assert await s.broadcast_participants() is False          # unchanged -> nothing
    s.last_heard["Preet"] = protocol.now_ms()
    assert await s.broadcast_participants() is True
    assert ws1.of("participants")[-1]["people"][0]["speaking"] is True
    s.last_heard["Preet"] = protocol.now_ms() - 5000
    s.speaking = True
    assert await s.broadcast_participants() is True
    people = ws1.of("participants")[-1]["people"]
    assert people[0]["speaking"] is False and people[1] == {"name": TWIN_SPEAKER, "kind": "ai", "speaking": True}
    empty = Session("none")
    assert await empty.broadcast_participants() is False      # no sockets -> no work


@pytest.mark.asyncio
async def test_mouth_tick_broadcasts_participants(monkeypatch):
    monkeypatch.setattr(mouth, "_make_listener", lambda s: None)
    s = Session("tick")
    ws = FakeWS()
    s.add_socket(ws, "Preet")
    m = mouth.attach(s)
    await m._tick()
    assert ws.of("participants")
    await mouth.detach(s)


# ---------------------------------------------------------------- topic

@pytest.mark.asyncio
async def test_set_topic_dedupes():
    s = Session("topic")
    ws = FakeWS()
    s.add_socket(ws, "Preet")
    assert await s.set_topic("t_auth", "Auth redesign") is True
    assert await s.set_topic("t_auth", "Auth redesign") is False
    assert ws.of("topic") == [{"type": "topic", "topic_id": "t_auth", "title": "Auth redesign",
                               "active": True, "changed": True}]
    assert s.active_topic == {"topic_id": "t_auth", "title": "Auth redesign"}
    # Listener broadcasting directly is remembered too
    await s.broadcast({"type": "topic", "topic_id": "t_bill", "title": "Billing", "active": True, "changed": True})
    s.active_topic = {"topic_id": "t_bill", "title": "Billing"}
    assert await s.set_topic("t_bill", "Billing") is False
    assert await s.set_topic("t_auth", "Auth redesign") is True
    assert len(ws.of("topic")) == 3


def test_new_socket_gets_current_topic(_no_mouth):
    room = _room()
    s = get_session(room)
    s.active_topic = {"topic_id": "t_auth", "title": "Auth redesign"}
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws?room={room}&user=Preet") as a:
            types = [a.receive_json()["type"] for _ in range(4)]
            assert types == ["status", "board_snapshot", "participants", "topic"]


# ---------------------------------------------------------------- draw_error

@pytest.mark.asyncio
async def test_draw_error_routes_to_hooks(caplog):
    s = Session("de")
    ws = FakeWS()
    s.add_socket(ws, "Preet")
    msg = protocol.parse({"type": "draw_error", "id": "m1", "op": "add_mermaid", "error": "Parse error"})
    caplog.set_level("INFO")
    await s.handle_message(ws, "Preet", msg)
    assert "no hook" in caplog.text
    got = []

    async def hook(sess, data):
        got.append(data)

    async def bad(sess, data):
        raise RuntimeError("boom")

    s.on_draw_error(hook)
    s.on_draw_error(hook)  # idempotent
    s.on_draw_error(bad)
    await s.handle_message(ws, "Preet", msg)
    await _settle()
    assert got == [{"type": "draw_error", "id": "m1", "op": "add_mermaid", "error": "Parse error"}]
    assert ws.of("error") == []


def test_mouth_registers_diagrammer_repair(monkeypatch):
    from agents import diagrammer
    if not hasattr(diagrammer, "repair_draw"):
        pytest.skip("diagrammer.repair_draw not present")
    monkeypatch.setattr(mouth, "_make_listener", lambda s: None)
    s = Session("rep")

    async def run():
        mouth.attach(s)
        await mouth.detach(s)

    asyncio.run(run())
    assert diagrammer.repair_draw in s._draw_error_hooks


# ---------------------------------------------------------------- calendar_join

class FakeMouth:
    def __init__(self):
        self.greets = []

    def greet(self, meeting, predictions):
        self.greets.append((meeting, predictions))


@pytest.mark.asyncio
async def test_calendar_join_uses_demo_calendar_then_greets():
    s = Session("cal")
    ws = FakeWS()
    s.add_socket(ws, "Preet")
    s.mouth = FakeMouth()
    await s.handle_message(ws, "Preet", protocol.parse({"type": "calendar_join", "meeting": "Daily standup",
                                                         "scheduled": "13:30"}))
    brief = ws.of("brief")[0]
    assert brief["title"] == "Daily standup" and len(brief["predictions"]) == 3
    assert any("Redis" in p for p in brief["predictions"]) and brief["ts"] > 0
    assert s.mouth.greets == [("Daily standup", brief["predictions"])]


@pytest.mark.asyncio
async def test_calendar_join_provider_and_generic_fallback():
    s = Session("cal2")
    ws = FakeWS()
    s.add_socket(ws, "Preet")
    s.mouth = FakeMouth()
    assert await s.brief_predictions("Unknown meeting") == session_mod.GENERIC_PREDICTIONS

    async def provider(sess, meeting):
        return [f"{meeting}: custom"]

    s.brief_provider = provider
    await s.calendar_join("Retro")
    assert ws.of("brief")[0]["predictions"] == ["Retro: custom"]

    async def broken(sess, meeting):
        raise RuntimeError("nope")

    s.brief_provider = broken
    assert len(await s.brief_predictions("Daily standup")) == 3


@pytest.mark.asyncio
async def test_mouth_greet_passes_brief_as_update(monkeypatch):
    monkeypatch.setattr(mouth, "_make_listener", lambda s: None)
    calls = []

    async def fake_respond(session, reason, *, trigger_ts=None, updates=None, **kw):
        calls.append((reason, updates))
        return "hi"

    monkeypatch.setattr(mouth.responder, "respond", fake_respond)
    s = Session("greet")
    s.add_socket(FakeWS(), "Preet")
    m = mouth.attach(s)
    m.greet("Daily standup", ["Auth migration", "Billing retries"])
    await _settle()
    assert calls and "calendar join" in calls[0][0]
    assert "Daily standup" in calls[0][1][0] and "Auth migration" in calls[0][1][0]
    await mouth.detach(s)


# ---------------------------------------------------------------- connect_repo + HTTP

@pytest.mark.asyncio
async def test_connect_repo_hook_or_error():
    s = Session("repo")
    ws = FakeWS()
    s.add_socket(ws, "Preet")
    msg = protocol.parse({"type": "connect_repo", "url": "https://github.com/a/b"})
    await s.handle_message(ws, "Preet", msg)
    assert ws.of("error") == [{"type": "error", "message": "connect_repo not available yet", "recoverable": True}]
    got = []

    async def connector(sess, url):
        got.append(url)

    s.repo_connector = connector
    await s.handle_message(ws, "Preet", msg)
    await _settle()
    assert got == ["https://github.com/a/b"]


def test_calendar_endpoints(_no_mouth):
    with TestClient(app) as client:
        cal = client.get("/calendar").json()
        assert any(m["title"] == "Daily standup" for m in cal["meetings"])
        r = client.post(f"/simulate/calendar_join?room={_room()}&meeting=Daily%20standup").json()
        assert r["ok"] is False  # nobody connected
        room = _room()
        with client.websocket_connect(f"/ws?room={room}&user=Preet") as a:
            _recv_type(a, "participants")
            r = client.post(f"/simulate/calendar_join?room={room}&meeting=Daily%20standup").json()
            assert r["ok"] is True and r["brief"]["title"] == "Daily standup"
            assert _recv_type(a, "brief")["predictions"] == r["brief"]["predictions"]


def test_echo_guard_never_drops_addressed_or_short_lines():
    from session import is_echo
    twin = ["I put the postgres connection next to auth.", "Does that look right?"]
    assert not is_echo("Twin, does that look right?", twin)
    assert not is_echo("does that look right", twin)
    assert is_echo("I put the postgres connection next to auth", twin)
