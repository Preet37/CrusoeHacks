"""v2.2: humans draw (browser draw ops with `by`) and screen_frame/screen_stop relay. No network."""
import asyncio

import protocol
from session import SCREEN_MAX_B64, Session


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_text(self, text: str) -> None:
        import json
        self.sent.append(json.loads(text))

    def of(self, type_: str) -> list[dict]:
        return [m for m in self.sent if m["type"] == type_]


def _room():
    s = Session("t_human_draw")
    a, b = FakeWS(), FakeWS()
    s.add_socket(a, "Preet")
    s.add_socket(b, "Parsh")
    return s, a, b


def _run(coro):
    return asyncio.run(coro)


def _msg(d: dict):
    return protocol.parse(d)


def test_add_box_relayed_to_others_not_sender_and_on_board():
    s, a, b = _room()

    async def go():
        await s.handle_message(a, "Preet", _msg({"type": "draw", "op": "add_box", "id": "h_preet_1",
                                                 "label": "Payments", "x": 300, "y": 200,
                                                 "kind": "service", "by": "Mallory"}))
    _run(go())
    assert a.sent == []
    draws = b.of("draw")
    assert len(draws) == 1
    d = draws[0]
    assert d["op"] == "add_box" and d["id"] == "h_preet_1" and d["label"] == "Payments"
    assert d["by"] == "Preet"  # forced to the socket's user
    assert "h_preet_1" in s.board.boxes
    assert "Payments" in s.board.summary()


def test_connect_and_rename_from_human():
    s, a, b = _room()

    async def go():
        for op in (
            {"op": "add_box", "id": "h_p_1", "label": "API", "x": 100, "y": 100, "kind": "service"},
            {"op": "add_box", "id": "h_p_2", "label": "DB", "x": 400, "y": 100, "kind": "service"},
            {"op": "connect", "id": "h_p_e1", "from_id": "h_p_1", "to_id": "h_p_2"},
            {"op": "rename", "id": "h_p_2", "label": "Postgres"},
        ):
            await s.handle_message(a, "Preet", _msg({"type": "draw", **op}))
    _run(go())
    assert [m["op"] for m in b.of("draw")] == ["add_box", "add_box", "connect", "rename"]
    assert a.of("draw") == [] and a.of("error") == []
    summary = s.board.summary()
    assert "Postgres" in summary and "h_p_1->h_p_2" in summary


def test_invalid_op_errors_sender_only():
    s, a, b = _room()

    async def go():
        await s.handle_message(a, "Preet", _msg({"type": "draw", "op": "connect", "id": "e1",
                                                 "from_id": "nope", "to_id": "nada"}))
    _run(go())
    errs = a.of("error")
    assert len(errs) == 1 and errs[0]["recoverable"] is True
    assert b.sent == []
    assert not s.board.edges


def test_screen_frame_relayed_and_latest_kept():
    s, a, b = _room()

    async def go():
        for i in (1, 2):
            await s.handle_message(a, "Preet", _msg({"type": "screen_frame", "user": "x",
                                                     "jpeg": f"AAA{i}", "w": 1280, "h": 800, "ts": i}))
    _run(go())
    assert a.sent == []
    frames = b.of("screen_frame")
    assert [f["jpeg"] for f in frames] == ["AAA1", "AAA2"]
    assert frames[-1]["user"] == "Preet"
    assert s.screen["jpeg"] == "AAA2"

    # a late joiner gets the latest frame
    c = FakeWS()
    s.add_socket(c, "Late")
    _run(s.send_initial_state(c))
    assert [f["jpeg"] for f in c.of("screen_frame")] == ["AAA2"]

    _run(s.handle_message(a, "Preet", _msg({"type": "screen_stop", "user": "Preet"})))
    assert s.screen is None
    assert b.of("screen_stop") == [{"type": "screen_stop", "user": "Preet"}]
    assert a.of("screen_stop") == []


def test_screen_frame_too_large_rejected():
    s, a, b = _room()
    _run(s.handle_message(a, "Preet", _msg({"type": "screen_frame", "jpeg": "A" * (SCREEN_MAX_B64 + 1)})))
    assert len(a.of("error")) == 1
    assert b.sent == [] and s.screen is None


def test_twin_apply_draw_still_broadcasts_to_everyone():
    s, a, b = _room()
    ok = _run(s.apply_draw({"op": "add_box", "id": "auth", "label": "Auth", "x": 1, "y": 2, "kind": "service"}))
    assert ok
    assert len(a.of("draw")) == 1 and len(b.of("draw")) == 1
    assert "by" not in a.of("draw")[0]
