"""End-of-meeting Slack recap: httpx is mocked with MockTransport, no real network or LLM."""
import asyncio
import json
from collections import OrderedDict

import httpx
import pytest
from fastapi.testclient import TestClient

import app
import llm
from integrations import jira, slack
from session import SESSIONS, Session

HOOK = "https://hooks.slack.test/services/T000/B000/secret-hook"
TOKEN = "xoxb-secret-token"


@pytest.fixture
def slack_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TWIN_DB_PATH", str(tmp_path / "company.sqlite3"))
    monkeypatch.setenv("WORKSPACES", "acme:invite")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", HOOK)
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.setenv("PUBLIC_WEB_URL", "https://twin.example")
    monkeypatch.setattr(slack, "_POSTED", set())
    monkeypatch.setattr(slack, "_RECENT", {})
    monkeypatch.setattr(slack, "POLL_S", 0.01)
    monkeypatch.setattr(slack, "WAIT_S", 0.3)
    monkeypatch.setattr(jira, "configured", lambda: True)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "slack.com":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, text="ok")

    monkeypatch.setattr(slack, "_TRANSPORT", httpx.MockTransport(handler))

    async def fake_chat(model, messages, **kw):
        return "We agreed to add retries. Avery owns it. Ship by Friday."
    monkeypatch.setattr(llm, "chat", fake_chat)
    yield calls
    for room in [r for r in SESSIONS if r.startswith("acme-")]:
        SESSIONS.pop(room, None)


def _ticket(tid="T-1", owner="Avery", key=None, agent=None):
    t = {"id": tid, "title": "Add retries", "body": "Decision: add retries", "owner": owner,
         "status": "sent" if key else "draft",
         "links": {"jira": f"https://x.atlassian.net/browse/{key}" if key else None,
                   "slack": None, "agent": None}}
    if key:
        t["jira_key"] = key
    if agent:
        t["agent_status"] = agent
    return t


def _meeting(tickets, mid="m_1"):
    return {"id": mid, "workspace": None, "room": "standup", "title": "Retries plan",
            "ended_at": "2026-10-17T16:40:00+00:00", "summary": "raw",
            "notes": [{"speaker": "Avery", "text": "we decided to add retries"}],
            "decisions": ["add retries"], "tickets": tickets}


def _posted(calls):
    return [json.loads(c.content) for c in calls]


def test_blocks_have_summary_decisions_owners_links_agent(slack_env):
    meeting = _meeting([_ticket(key="SCRUM-7", agent="running"), _ticket("T-2", "Sam")])
    out = asyncio.run(slack.post_meeting_recap(meeting, wait=False))
    assert out == {"ok": True}
    [body] = _posted(slack_env)
    text = json.dumps(body)
    assert body["blocks"][0]["type"] == "header"
    assert body["blocks"][0]["text"]["text"].startswith("Twin recap: Retries plan · Oct 17, 2026 16:40")
    assert "We agreed to add retries." in text
    assert "*Decisions*" in text and "add retries" in text
    assert "*Who owns what*" in text and "*Avery*" in text and "*Sam*" in text
    assert "<https://x.atlassian.net/browse/SCRUM-7|SCRUM-7 Add retries>" in text
    assert "(Jira link pending)" in text  # Sam's ticket never got a link
    assert ":robot_face: Sync in progress by Twin's AI agent on <https://x.atlassian.net/browse/SCRUM-7|SCRUM-7>" in text
    assert "<https://twin.example|Open the Twin dashboard>" in text
    assert str(slack_env[0].url) == HOOK


def test_waits_for_jira_links_then_posts(slack_env):
    session = Session("acme-wait")
    session.ticket_drafts = OrderedDict(a=_ticket())
    meeting = _meeting([_ticket()])

    async def run():
        task = asyncio.create_task(slack.post_meeting_recap(meeting, session))
        await asyncio.sleep(0.05)
        assert not slack_env  # still waiting for the auto Jira push
        session.ticket_drafts["a"] = _ticket(key="SCRUM-9")
        return await task
    assert asyncio.run(run()) == {"ok": True}
    text = json.dumps(_posted(slack_env)[0])
    assert "<https://x.atlassian.net/browse/SCRUM-9|SCRUM-9 Add retries>" in text
    assert "pending" not in text


def test_posts_after_timeout_with_pending(slack_env):
    out = asyncio.run(slack.post_meeting_recap(_meeting([_ticket()])))
    assert out["ok"]
    assert "(Jira link pending)" in json.dumps(_posted(slack_env)[0])


def test_no_config_is_noop(slack_env, monkeypatch):
    monkeypatch.delenv("SLACK_WEBHOOK_URL")
    out = asyncio.run(slack.post_meeting_recap(_meeting([]), wait=False))
    assert out["ok"] is False and not slack_env
    session = Session("acme-none")
    assert slack.schedule_meeting_recap(session, _meeting([])) is None


def test_dedupe_per_meeting(slack_env):
    async def run():
        session = Session("acme-dupe")
        t1 = slack.schedule_meeting_recap(session, _meeting([_ticket(key="SCRUM-1")]))
        t2 = slack.schedule_meeting_recap(session, _meeting([_ticket(key="SCRUM-1")]))
        await t1
        assert t2 is None or (await t2)["ok"] is False
        return await slack.post_meeting_recap(_meeting([]), wait=False)
    assert asyncio.run(run()) == {"ok": False, "error": "Already posted"}
    assert len(slack_env) == 1


def test_bot_token_chat_post_message(slack_env, monkeypatch):
    monkeypatch.delenv("SLACK_WEBHOOK_URL")
    monkeypatch.setenv("SLACK_BOT_TOKEN", TOKEN)
    monkeypatch.delenv("SLACK_CHANNEL", raising=False)
    assert asyncio.run(slack.post_meeting_recap(_meeting([]), wait=False)) == {"ok": True}
    req = slack_env[0]
    assert str(req.url) == slack.CHAT_POST_URL and json.loads(req.content)["channel"] == "standup"
    assert req.headers["authorization"] == f"Bearer {TOKEN}"


def test_errors_never_leak_secrets(slack_env, monkeypatch):
    monkeypatch.setattr(slack, "_TRANSPORT", httpx.MockTransport(lambda r: httpx.Response(404)))
    out = asyncio.run(slack.post_meeting_recap(_meeting([]), wait=False))
    assert out["ok"] is False and "404" in out["error"] and "secret" not in json.dumps(out)


def test_llm_timeout_falls_back_to_first_lines(slack_env, monkeypatch):
    async def slow(*a, **k):
        await asyncio.sleep(5)
    monkeypatch.setattr(llm, "chat", slow)
    monkeypatch.setattr(slack, "SUMMARY_TIMEOUT_S", 0.05)
    assert asyncio.run(slack.summarize(_meeting([]))) == "Avery: we decided to add retries"


def test_routes_integrations_and_manual_repost(slack_env):
    from brain.meeting_store import save_meeting
    session = Session("acme-standup")
    session.workspace = "acme"
    session.history = [{"speaker": "Avery", "text": "we decided to add retries", "ts": 1}]
    session.ticket_drafts = OrderedDict(a=_ticket(key="SCRUM-3"))
    saved = save_meeting(session)
    client = TestClient(app.app)
    params = {"workspace": "acme", "code": "invite"}
    assert client.get("/integrations", params=params).json()["slack"] == {"connected": True}
    assert client.post(f"/meetings/{saved['id']}/post_slack",
                       params={**params, "code": "bad"}).status_code == 403
    assert client.post(f"/meetings/{saved['id']}/post_slack", params=params).json() == {"ok": True}
    # Manual repost is allowed even after an automatic post.
    assert client.post(f"/meetings/{saved['id']}/post_slack", params=params).json() == {"ok": True}
    assert len(slack_env) == 2
    assert "SCRUM-3" in json.dumps(_posted(slack_env)[0])
    missing = client.post("/meetings/nope/post_slack", params=params)
    assert missing.status_code == 404 and missing.json()["ok"] is False
