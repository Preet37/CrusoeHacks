"""Live CC relay (interim `caption` to other sockets) and task/ticket `assigned_by`."""
import asyncio
import json

import protocol
import session as session_mod
from session import Session


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))

    def of(self, kind: str) -> list[dict]:
        return [m for m in self.sent if m.get("type") == kind]


def _room():
    s = Session("cc_room")
    a, b = FakeWS(), FakeWS()
    s.add_socket(a, "Alice")
    s.add_socket(b, "Bob")
    return s, a, b


def _ut(text, final=False, speaker="Alice", source="speech", ts=None):
    return protocol.parse({"type": "user_text", "speaker": speaker, "text": text,
                           "final": final, "source": source, "ts": ts})


def test_interim_relayed_to_others_only():
    s, a, b = _room()
    asyncio.run(s.handle_message(a, "Alice", _ut("hello every")))
    caps = b.of("caption")
    assert caps and caps[0]["speaker"] == "Alice" and caps[0]["text"] == "hello every"
    assert caps[0]["final"] is False and isinstance(caps[0]["ts"], int)
    assert not a.of("caption")
    assert not s.history  # interims never become transcript lines


def test_interim_throttled_per_speaker(monkeypatch):
    s, a, b = _room()
    clock = {"t": 10_000}
    monkeypatch.setattr(session_mod, "now_ms", lambda: clock["t"])

    async def run():
        for i in range(20):  # 20 interims in 1 s
            clock["t"] += 50
            await s.handle_message(a, "Alice", _ut(f"word {i}"))
        await s.handle_message(b, "Bob", _ut("bob talks", speaker="Bob"))
    asyncio.run(run())
    assert 1 <= len(b.of("caption")) <= 8
    assert [c["speaker"] for c in a.of("caption")] == ["Bob"]  # other speakers are not throttled by Alice


def test_muted_typed_and_echo_not_relayed():
    s, a, b = _room()
    s.remember_twin_speech("the payments service writes to the orders database")

    async def run():
        await s.handle_message(a, "Alice", _ut("the payments service writes to the orders"))
        s.mic_muted_ws.add(a)
        await s.handle_message(a, "Alice", _ut("secret muted words here"))
        s.mic_muted_ws.discard(a)
        await s.handle_message(a, "Alice", _ut("typing", source="typed"))
    asyncio.run(run())
    assert not b.of("caption")


def test_final_still_broadcast_as_transcript():
    s, a, b = _room()
    asyncio.run(s.handle_message(a, "Alice", _ut("ship it friday", final=True)))
    assert [m["text"] for m in b.of("transcript")] == ["ship it friday"]
    assert not b.of("caption")


def test_task_and_ticket_get_assigned_by():
    s, a, b = _room()

    async def run():
        await s.handle_message(a, "Alice", _ut("Bob will add rate limiting", final=True, ts=111))
        await s.add_line("Twin", "Got it, I'll note that.")
        await s.broadcast({"type": "task", "id": "t_1", "title": "Add rate limiting",
                           "owner": "Bob", "source": "meeting cc_room @ 111"})
        await s.broadcast({"type": "ticket", "id": "T-1", "title": "Add rate limiting",
                           "owner": "Bob", "source": "meeting cc_room @ 111"})
        await s.handle_message(b, "Bob", _ut("ok next topic", final=True, speaker="Bob"))
        # A later re-broadcast (e.g. push_jira) keeps the original assigner.
        await s.broadcast({"type": "ticket", "id": "T-1", "title": "Add rate limiting", "owner": "Bob"})
        await s.broadcast({"type": "task", "id": "t_2", "title": "x", "assigned_by": "Carol"})
    asyncio.run(run())
    task = b.of("task")
    tickets = b.of("ticket")
    assert task[0]["assigned_by"] == "Alice" and task[0]["owner"] == "Bob"
    assert [t["assigned_by"] for t in tickets] == ["Alice", "Alice"]
    assert task[1]["assigned_by"] == "Carol"
