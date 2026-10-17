"""Twin answers the newest request: no stale contribution or old-topic updates (no network)."""
import asyncio
from types import SimpleNamespace

import pytest

import mouth
from agents import responder
from session import Session


def _sess(**kw):
    base = dict(code_map=SimpleNamespace(root="/nonexistent", files={
        "auth/login.py": {"path": "auth/login.py", "summary": "Login", "imported_by": ["x"], "is_test": False}}),
        active_topic={"topic_id": "t_auth", "title": "Auth"}, history=[])
    base.update(kw)
    return SimpleNamespace(**base)


def _take(**kw):
    return {"id": "c1", "topic_id": "t_auth", "current": "Login lives in auth/login.py.",
            "ts": responder._now_ms(), **kw}


def _has_take(s):
    return any("Latest grounded take" in l for l in responder.repo_brief(s))


def test_contribution_injected_only_for_current_fresh_topic():
    assert _has_take(_sess(latest_contribution=_take()))
    assert not _has_take(_sess(latest_contribution=_take(topic_id="t_billing")))
    assert not _has_take(_sess(latest_contribution=_take(ts=responder._now_ms() - 91_000)))
    assert not _has_take(_sess(latest_contribution={k: v for k, v in _take().items() if k != "ts"}))
    shifted = [{"speaker": "Preet", "text": "assign someone a task",
                "decision": {"topic_changed": {"type": "noul", "noul": 0.8}}}]
    assert not _has_take(_sess(latest_contribution=_take(), history=shifted))


def test_latest_line_is_last_and_explicit():
    hist = [{"speaker": "Preet", "text": "how does auth work"},
            {"speaker": "Twin", "text": "Login lives in auth/login.py."},
            {"speaker": "Parsh", "text": "assign someone the billing task"}]
    msgs = responder.build_messages(hist, "", ["old auth update"], "name")
    last = msgs[-1]["content"].strip().splitlines()[-1]
    assert last == "Answer THIS, not earlier topics: Parsh: assign someone the billing task"
    assert "Always answer the most recent request" in msgs[0]["content"]


def test_drop_stale_updates_by_topic_and_age():
    s = Session("u")
    s.active_topic = {"topic_id": "t_auth", "title": "Auth"}
    s.push_update("auth diagram done")
    s.active_topic = {"topic_id": "t_billing", "title": "Billing"}
    s.push_update("billing note")
    assert s.drop_stale_updates("t_billing") == ["auth diagram done"]
    assert s.pop_updates() == ["billing note"]


@pytest.mark.asyncio
async def test_new_line_drops_pending_other_topic_contribution(monkeypatch):
    monkeypatch.setattr(mouth, "_make_listener", lambda s: None)
    s = Session("c")
    s.twin_mode = "proactive"
    s.active_topic = {"topic_id": "t_billing", "title": "Billing"}
    m = mouth.Mouth(s)
    s._broadcast_observers.append(m._on_broadcast)
    s.last_human_speech_ts = mouth._now_ms()  # room busy: the contribution waits for a gap
    msg = {"type": "contribution", "id": "contrib_x", "topic_id": "t_auth", "current": "Auth take."}
    s.latest_contribution = msg
    await s.broadcast(msg)
    assert isinstance(msg.get("ts"), int)
    assert "contribution" in s._tasks
    line = await s.add_line("Preet", "okay assign someone the billing task")
    m._drop_stale_work(line)
    await asyncio.sleep(0)
    assert "contribution" not in s._tasks
    s.cancel_all()
