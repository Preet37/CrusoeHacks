import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agents.responder as responder  # noqa: E402


class FakeSession:
    def __init__(self):
        self.history = [{"speaker": "Preet", "text": "Twin, what do you think?"}]
        self.sent, self.statuses, self.lines, self.updates = [], [], [], []
        self.current_say_id = None
        self.speaking = False
        self._n = 0
        self.board = None

    def new_say_id(self):
        self._n += 1
        return f"s_{self._n}"

    async def set_status(self, s):
        self.statuses.append(s)

    async def broadcast(self, m):
        self.sent.append(m)

    async def add_line(self, speaker, text, ts=None):
        self.lines.append((speaker, text))

    def push_update(self, t):
        self.updates.append(t)


async def fake_stream(model, messages, **kw):
    for d in ["<think>hmm</think>", "Sounds **good** to me. ", "Ship it."]:
        await asyncio.sleep(0)
        yield d


async def fake_speak(deltas, say_id, emit, timings=None):
    text = ""
    async for d in deltas:
        text += d
    await emit({"type": "audio", "say_id": say_id, "seq": 0, "text": text, "data": None, "last": True})
    return text


@pytest.fixture(autouse=True)
def tmp_latency(tmp_path, monkeypatch):
    monkeypatch.setattr(responder, "LATENCY_LOG", tmp_path / "latency.jsonl")


@pytest.mark.asyncio
async def test_respond_streams_cleans_and_records():
    s = FakeSession()
    out = await responder.respond(s, "addressed by name", stream=fake_stream, speak=fake_speak)
    assert out == "Sounds good to me. Ship it."
    assert s.statuses == ["thinking", "speaking", "listening"]
    assert s.lines == [("Twin", out)]
    assert s.sent[0]["say_id"] == "s_1"
    assert responder.LATENCY_LOG.exists()


@pytest.mark.asyncio
async def test_fallback_before_first_token():
    calls = []

    async def flaky(model, messages, **kw):
        calls.append((model, bool(kw.get("extra_body"))))
        if len(calls) == 1:
            raise RuntimeError("no-think flag rejected")
        yield "Okay, got it."

    s = FakeSession()
    out = await responder.respond(s, "push to talk", stream=flaky, speak=fake_speak)
    assert out == "Okay, got it."
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_failure_requeues_updates():
    async def broken(model, messages, **kw):
        raise RuntimeError("down")
        yield ""

    s = FakeSession()
    out = await responder.respond(s, "work update", updates=["I added Redis"], stream=broken, speak=fake_speak)
    assert out == ""
    assert s.updates == ["I added Redis"]
    assert s.sent[-1]["type"] == "error"
    assert s.statuses[-1] == "listening"


@pytest.mark.asyncio
async def test_barge_in_drops_stale_audio():
    s = FakeSession()

    async def speak_then_barge(deltas, say_id, emit, timings=None):
        s.current_say_id = None  # barge-in happened
        await emit({"type": "audio", "say_id": say_id, "seq": 0, "data": None, "last": True})
        return "partial"

    await responder.respond(s, "addressed", stream=fake_stream, speak=speak_then_barge)
    assert not [m for m in s.sent if m.get("type") == "audio"]
    assert s.lines == []


def test_build_messages_has_stable_prefix():
    m = responder.build_messages([{"speaker": "A", "text": "hi"}], "web -> auth", ["drew auth"], "addressed")
    assert m[0]["role"] == "system" and "Twin" in m[0]["content"]
    assert "drew auth" in m[1]["content"] and "web -> auth" in m[1]["content"]


@pytest.mark.asyncio
async def test_strips_speaker_label():
    async def labelled(model, messages, **kw):
        for d in ["Tw", "in: I connected ", "auth to postgres."]:
            yield d

    s = FakeSession()
    out = await responder.respond(s, "addressed", stream=labelled, speak=fake_speak)
    assert out == "I connected auth to postgres."


@pytest.mark.asyncio
@pytest.mark.parametrize("chunks", [["Twin here.", " Looks right."], ["Twin ", "here. Looks right."], ["It's Twin: Looks right."]])
async def test_strips_twin_here(chunks):
    async def labelled(model, messages, **kw):
        for d in chunks:
            yield d

    out = await responder.respond(FakeSession(), "addressed", stream=labelled, speak=fake_speak)
    assert out == "Looks right."


@pytest.mark.asyncio
async def test_instant_ack_plays_first_and_shifts_seq(monkeypatch):
    monkeypatch.setattr(responder, "pick_ack", lambda: ("Yeah, one sec.", b"RIFFxx"))

    async def speak_two(deltas, say_id, emit, timings=None):
        text = ""
        async for d in deltas:
            text += d
        await emit({"type": "audio", "say_id": say_id, "seq": 0, "text": text, "data": None, "last": True})
        return text

    s = FakeSession()
    out = await responder.respond(s, "name", stream=fake_stream, speak=speak_two)
    audio = [m for m in s.sent if m["type"] == "audio"]
    assert [a["seq"] for a in audio] == [0, 1]
    assert audio[0]["text"] == "Yeah, one sec." and audio[0]["data"] and not audio[0]["last"]
    assert audio[1]["last"] is True
    assert out.startswith("Yeah, one sec.")


@pytest.mark.asyncio
async def test_no_ack_for_work_updates(monkeypatch):
    monkeypatch.setattr(responder, "pick_ack", lambda: ("Yeah, one sec.", b"RIFFxx"))
    s = FakeSession()
    out = await responder.respond(s, "work update", stream=fake_stream, speak=fake_speak)
    assert not out.startswith("Yeah")


def test_repo_brief_grounds_answers(tmp_path):
    from types import SimpleNamespace
    (tmp_path / "README.md").write_text("# Loomin\nA physics sandbox that turns prompts into 3D scenes.")
    cm = SimpleNamespace(root=str(tmp_path), files={
        "app/page.tsx": {"path": "app/page.tsx", "summary": "Landing page", "imported_by": ["x"], "is_test": False, "owner": "Preet37"},
        "lib/specSheet.ts": {"path": "lib/specSheet.ts", "summary": "Spec sheet research", "imported_by": [], "is_test": False},
    })
    s = SimpleNamespace(code_map=cm, latest_contribution={"current": "Login lives in the auth route.", "ts": responder._now_ms()})
    lines = responder.repo_brief(s)
    assert any("physics sandbox" in l for l in lines)
    assert any("app/page.tsx" in l for l in lines)
    assert any("Latest grounded take" in l for l in lines)
    assert responder.repo_brief(SimpleNamespace()) == []
