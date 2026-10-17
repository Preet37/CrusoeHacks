"""Session behaviour over real FastAPI WebSockets plus Session unit tests."""
import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

import session as session_mod
from app import app
from session import Session, get_session


def _room() -> str:
    return f"t_{uuid.uuid4().hex[:8]}"


def _connect(client, room, user):
    ws = client.websocket_connect(f"/ws?room={room}&user={user}").__enter__()
    first, second = ws.receive_json(), ws.receive_json()
    return ws, first, second


def _recv_until(ws, type_, limit=10):
    for _ in range(limit):
        msg = ws.receive_json()
        if msg["type"] == type_:
            return msg
    raise AssertionError(f"no {type_} message")


@pytest.fixture(autouse=True)
def _no_mouth(monkeypatch):
    """These tests exercise the bare Session; the mouth (live model calls) is tested separately."""
    import mouth
    monkeypatch.setattr(mouth, "attach", lambda session: None)


def test_two_clients_get_initial_state_and_shared_transcript():
    room = _room()
    with TestClient(app) as client:
        a, a1, a2 = _connect(client, room, "Preet")
        b, b1, b2 = _connect(client, room, "Parsh")
        for first, second in ((a1, a2), (b1, b2)):
            assert first == {"type": "status", "state": "listening"}
            assert second == {"type": "board_snapshot", "boxes": [], "edges": []}

        a.send_json({"type": "hello", "protocol": 1, "user": "Preet", "room": room})
        a.send_json({"type": "user_text", "speaker": "Preet", "text": "interim", "final": False})
        a.send_json({"type": "user_text", "speaker": "Preet", "text": "the app talks to auth",
                     "final": True, "ts": 123})
        expected = {"type": "transcript", "speaker": "Preet", "text": "the app talks to auth", "ts": 123}
        assert a.receive_json() == expected
        assert b.receive_json() == expected

        s = get_session(room)
        assert len(s.history) == 1 and s.history[0]["text"] == "the app talks to auth"
        assert s.last_human_speech_ts > 0

        state = client.get(f"/state?room={room}").json()
        assert state["history_len"] == 1 and state["sockets"] == 2
        assert client.get("/health").json() == {"ok": True}
        a.__exit__(None, None, None)
        b.__exit__(None, None, None)


def test_barge_in_sends_stop_audio_and_listening():
    room = _room()
    with TestClient(app) as client:
        a, _, _ = _connect(client, room, "Preet")
        s = get_session(room)
        s.status, s.speaking, s.current_say_id = "speaking", True, "s_9"
        a.send_json({"type": "barge_in", "speaker": "Preet", "ts": 1})
        assert a.receive_json() == {"type": "stop_audio", "say_id": "s_9"}
        assert a.receive_json() == {"type": "status", "state": "listening"}
        assert s.current_say_id is None and s.speaking is False
        a.__exit__(None, None, None)


def test_invalid_json_yields_recoverable_error_and_socket_survives():
    room = _room()
    with TestClient(app) as client:
        a, _, _ = _connect(client, room, "Preet")
        a.send_text("{not json")
        err = a.receive_json()
        assert err["type"] == "error" and err["recoverable"] is True
        a.send_text('{"type":"nope"}')
        assert a.receive_json()["type"] == "error"
        a.send_json({"type": "user_text", "speaker": "Preet", "text": "still alive", "final": True})
        assert a.receive_json()["type"] == "transcript"
        a.__exit__(None, None, None)


def test_controls_clear_board_and_reset():
    room = _room()
    with TestClient(app) as client:
        a, _, _ = _connect(client, room, "Preet")
        s = get_session(room)
        s.board.apply({"op": "add_box", "id": "web", "label": "Web", "x": 1, "y": 2, "kind": "client"})
        a.send_json({"type": "control", "action": "clear_board"})
        assert a.receive_json() == {"type": "draw", "op": "clear"}
        assert a.receive_json() == {"type": "board_snapshot", "boxes": [], "edges": []}
        a.send_json({"type": "control", "action": "mute_twin"})
        a.send_json({"type": "push_to_talk", "active": True})
        a.send_json({"type": "user_text", "speaker": "Preet", "text": "x", "final": True})
        _recv_until(a, "transcript")
        assert s.muted and s.push_to_talk_active
        s.push_update("hello")
        a.send_json({"type": "control", "action": "reset_session"})
        snap = _recv_until(a, "board_snapshot")
        assert snap["boxes"] == []
        assert s.history == [] and not s.has_updates()
        a.__exit__(None, None, None)


def test_final_text_hook_and_decide_hook_are_called():
    room = _room()
    seen = []
    s = get_session(room)

    async def hook(sess, line):
        seen.append(("mind", line["text"]))

    async def decide(sess, line):
        seen.append(("mouth", line["text"]))

    s.on_final_text(hook)
    s.decide_and_respond = decide
    with TestClient(app) as client:
        a, _, _ = _connect(client, room, "Preet")
        a.send_json({"type": "user_text", "speaker": "Preet", "text": "Twin, hi", "final": True})
        a.receive_json()
        # Hooks ran as background tasks on the server loop; give them a moment.
        a.send_json({"type": "hello", "protocol": 1, "user": "Preet", "room": room})
        for _ in range(50):
            if len(seen) == 2:
                break
            client.get("/health")
        assert sorted(seen) == [("mind", "Twin, hi"), ("mouth", "Twin, hi")]
        a.__exit__(None, None, None)


# ---------------------------------------------------------------- unit tests

def test_push_and_pop_updates_dedupes_consecutive():
    s = Session("u")
    assert not s.has_updates()
    s.push_update("added redis")
    s.push_update("added redis")
    s.push_update("")
    s.push_update("renamed auth")
    s.push_update("added redis")
    assert s.has_updates()
    assert s.pop_updates() == ["added redis", "renamed auth", "added redis"]
    assert s.pop_updates() == [] and not s.has_updates()


def test_new_say_id_increments():
    s = Session("u")
    assert [s.new_say_id(), s.new_say_id(), s.new_say_id()] == ["s_1", "s_2", "s_3"]


@pytest.mark.asyncio
async def test_spawn_replaces_same_name_and_cancel():
    s = Session("u")
    started = asyncio.Event()

    async def sleeper():
        started.set()
        await asyncio.sleep(10)

    t1 = s.spawn("responder", sleeper())
    await started.wait()
    t2 = s.spawn("responder", sleeper())
    await asyncio.sleep(0)
    assert t1.cancelled() or t1.cancelling()
    assert s.task_names() == ["responder"]
    assert s.cancel("responder") is True
    await asyncio.sleep(0)
    assert t2.cancelled()
    assert s.cancel("responder") is False
    s.spawn("a", sleeper())
    s.spawn("b", sleeper())
    s.cancel_all()
    await asyncio.sleep(0)
    assert s.task_names() == []


@pytest.mark.asyncio
async def test_spawn_logs_exceptions(caplog):
    s = Session("u")

    async def boom():
        raise RuntimeError("kaboom")

    t = s.spawn("x", boom())
    with pytest.raises(RuntimeError):
        await t
    await asyncio.sleep(0)
    assert "kaboom" in caplog.text or any("failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_apply_draw_broadcasts_and_rejects():
    s = Session("u")
    sent = []

    class FakeWS:
        async def send_text(self, text):
            sent.append(text)

    class DeadWS:
        async def send_text(self, text):
            raise RuntimeError("closed")

    s.add_socket(FakeWS(), "a")
    s.add_socket(DeadWS(), "b")
    ok = await s.apply_draw({"op": "add_box", "id": "auth", "label": "Auth", "x": 10, "y": 20, "kind": "service"})
    assert ok and len(sent) == 1 and '"add_box"' in sent[0]
    assert len(s.sockets) == 1  # dead socket dropped
    assert await s.apply_draw({"op": "connect", "id": "e1", "from_id": "auth", "to_id": "ghost"}) is False
    assert s.snapshot_message().boxes[0].id == "auth"
    assert "auth" in s.board.summary().lower()


def test_fallback_board_has_same_surface():
    b = session_mod._FallbackBoard()
    b.apply({"op": "add_box", "id": "a", "label": "A", "x": 1, "y": 1, "kind": "service"})
    b.apply({"op": "add_box", "id": "b", "label": "B", "x": 1, "y": 1, "kind": "service"})
    b.apply({"op": "connect", "id": "e", "from_id": "a", "to_id": "b"})
    b.apply({"op": "remove", "id": "a"})
    assert b.snapshot()["edges"] == [] and len(b.snapshot()["boxes"]) == 1
    with pytest.raises(ValueError):
        b.apply({"op": "zap"})
