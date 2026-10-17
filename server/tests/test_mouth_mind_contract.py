"""Exercise the server's real mouth/mind wiring with deterministic model replies."""

import asyncio
import json

import pytest

import config
import llm
import protocol
from agents import responder
from session import Session

mouth = pytest.importorskip("mouth", reason="Server runtime integration has not landed yet")


@pytest.mark.asyncio
async def test_wired_session_draws_speaks_and_replaces_listener_on_clear(monkeypatch, tmp_path):
    async def chat(*args, **kwargs):
        return json.dumps([{"type": "system_fact", "text": "The web app uses auth", "entities": ["web", "auth"]}])

    async def run_tools(model, messages, tools, handlers, **kwargs):
        await handlers["add_box"](id="auth", label="Auth", x=400, y=300, kind="service")
        return "Done"

    async def stream_chat(*args, **kwargs):
        yield "I am here to help."

    monkeypatch.setattr(llm, "chat", chat)
    monkeypatch.setattr(llm, "run_tools", run_tools)
    monkeypatch.setattr(llm, "stream_chat", stream_chat)
    monkeypatch.setattr(config, "NVIDIA_API_KEY", "")
    monkeypatch.setattr(responder, "LATENCY_LOG", tmp_path / "latency.jsonl")
    session = Session("offline-mouth-mind")
    sent = []

    class Socket:
        async def send_text(self, raw):
            sent.append(json.loads(raw))

    socket = Socket()
    session.add_socket(socket, "Preet")
    runtime = mouth.attach(session)
    runtime.listener.debounce_seconds = 0

    async def until(predicate):
        async with asyncio.timeout(3):
            while not predicate():
                await asyncio.sleep(0.005)

    async def say(text):
        await session.handle_message(socket, "Preet", protocol.parse({
            "type": "user_text", "speaker": "Preet", "text": text, "final": True,
            "ts": protocol.now_ms(),
        }))

    try:
        await say("Twin, the web app uses auth.")
        await until(lambda: any(m["type"] == "audio" and m["last"] for m in sent)
                    and any(m["type"] == "agent_event" and m["agent"] == "Listener"
                            and m["state"] == "done" for m in sent))
        assert "auth" in session.board.boxes
        assert all(m["data"] is None for m in sent if m["type"] == "audio")
        assert any(m["type"] == "transcript" and m["speaker"] == "Twin" for m in sent)

        old_listener = runtime.listener
        await session.clear_board()
        assert old_listener._closed
        assert runtime.listener is not old_listener
        assert session.board.snapshot()["boxes"] == []
        assert not session.has_updates()

        # Identical facts must draw again after the user clears the board.
        runtime.listener.debounce_seconds = 0
        await say("The web app uses auth.")
        await until(lambda: "auth" in session.board.boxes)
        await runtime.listener.wait_idle()
        await session.reset()
        assert session.history == []
        assert not session.has_updates()
        assert sent[-1] == {"type": "board_snapshot", "boxes": [], "edges": []}
    finally:
        await runtime.listener.close()
        runtime._idle_task.cancel()
        await asyncio.gather(runtime._idle_task, return_exceptions=True)
        tasks = list(session._tasks.values())
        session.cancel_all()
        await asyncio.gather(*tasks, return_exceptions=True)
