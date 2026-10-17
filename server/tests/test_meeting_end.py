"""End-of-meeting pipeline: summary -> tickets -> Jira -> #standup recap. LLM, Jira and Slack are mocked."""
import asyncio
import json

import httpx
import pytest

import config
import integrations
import llm
from brain.meeting_store import _connect, list_meetings
from integrations import jira, meeting_end, slack

HOOK = "https://hooks.slack.test/services/T000/B000/secret-hook"
NOTES = [
    {"speaker": "Preet Karia", "text": "so could you walk me through the checkout flow", "ts": 1},
    {"speaker": "Twin", "text": "Here's a rough screen of the checkout and where payments fail", "ts": 2},
    {"speaker": "Preet Karia", "text": "Sam, can you look into the payment retries", "ts": 3},
]
GOOD = {"summary": "The team walked through the checkout flow and where payments fail. "
                   "Twin sketched the checkout screen. Sam will look into payment retries.",
        "decisions": ["Investigate payment retries before the next release"],
        "action_items": [{"title": "Add retries to payment capture", "owner": "Sam",
                          "why": "Payments fail intermittently at checkout.",
                          "acceptance": ["Capture retries 3 times", "Failures are logged"]}]}


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("TWIN_DB_PATH", str(tmp_path / "company.sqlite3"))
    monkeypatch.setenv("WORKSPACES", "acme:invite")
    monkeypatch.setenv("JIRA_BASE_URL", "https://example.atlassian.net/")
    monkeypatch.setenv("JIRA_EMAIL", "bot@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    monkeypatch.setenv("JIRA_PROJECT_KEY", "SCRUM")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", HOOK)
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.setattr(jira, "_status_cache", {"key": None, "at": 0.0, "value": None})
    monkeypatch.setattr(slack, "_POSTED", set())
    monkeypatch.setattr(slack, "_RECENT", {})
    monkeypatch.setattr(slack, "POLL_S", 0.01)
    monkeypatch.setattr(slack, "WAIT_S", 0.3)
    monkeypatch.setattr(integrations, "_repo_url", lambda *a, **k: None)
    state = {"issues": 0, "slack": []}

    def jira_handler(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p == "/rest/api/3/user/search":
            return httpx.Response(200, json=[{"accountId": "acc-sam", "accountType": "atlassian", "active": True}])
        if p == "/rest/api/3/issue" and req.method == "POST":
            state["issues"] += 1
            return httpx.Response(201, json={"key": f"SCRUM-{10 + state['issues']}"})
        if p.endswith("/attachments"):
            return httpx.Response(200, json=[{"filename": "x"}])
        if p.startswith("/rest/api/3/issue/SCRUM-") and req.method == "GET":
            return httpx.Response(200, json={"fields": {"summary": "x", "assignee": {"displayName": "Sam"},
                                                        "reporter": {"displayName": "Preet"},
                                                        "status": {"name": "To Do"}}})
        return httpx.Response(404, json={"errorMessages": ["nope"]})

    def slack_handler(req: httpx.Request) -> httpx.Response:
        state["slack"].append(json.loads(req.content))
        return httpx.Response(200, text="ok")

    monkeypatch.setattr(jira, "_TRANSPORT", httpx.MockTransport(jira_handler))
    monkeypatch.setattr(slack, "_TRANSPORT", httpx.MockTransport(slack_handler))
    return state


def _fake_llm(monkeypatch, replies):
    calls = []

    async def fake_chat(model, messages, **kw):
        calls.append((model, kw))
        reply = replies[min(len(calls) - 1, len(replies) - 1)]
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, str) else json.dumps(reply)
    monkeypatch.setattr(llm, "chat", fake_chat)
    return calls


def _save(tickets=(), mid="m1"):
    payload = {"id": mid, "workspace": "acme", "room": "acme-standup",
               "title": NOTES[0]["text"], "ended_at": "2026-10-17T23:00:00+00:00",
               "summary": " ".join(n["text"] for n in NOTES), "notes": NOTES, "decisions": [],
               "todos": [], "tickets": list(tickets),
               "board": {"boxes": [], "edges": [], "shapes": [
                   {"id": "f1", "op": "frame", "title": "Checkout flow", "x": 0, "y": 0}]}}
    with _connect() as db:
        db.execute("INSERT INTO meetings(id, workspace, ended_at, payload_json) VALUES(?,?,?,?)",
                   (mid, "acme", payload["ended_at"], json.dumps(payload)))
        db.commit()
    return payload


def _stored(mid="m1"):
    return next(m for m in list_meetings("acme") if m["id"] == mid)


def test_summary_is_crisp_not_transcript(env, monkeypatch):
    transcript_like = {"summary": "Preet Karia: so could you walk me through... Twin: Here's a rough screen"}
    calls = _fake_llm(monkeypatch, [transcript_like, GOOD])
    out = asyncio.run(meeting_end.summarize(_save()))
    assert out["source"] == "llm" and out["summary"] == GOOD["summary"]
    assert "Preet Karia:" not in out["summary"] and "Twin:" not in out["summary"]
    assert out["action_items"][0]["acceptance"] == ["Capture retries 3 times", "Failures are logged"]
    # Lightning first, then the next model after the transcript-like reply was rejected.
    assert [c[0] for c in calls] == [config.MODEL_RESPONDER, config.MODEL_FAST]
    assert calls[0][1]["response_format"] == {"type": "json_object"}
    assert calls[0][1]["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}


def test_fallback_summary_when_llm_fails(env, monkeypatch):
    calls = _fake_llm(monkeypatch, [RuntimeError("no key")])
    out = asyncio.run(meeting_end.summarize(_save()))
    assert out["source"] == "fallback" and len(calls) == 3
    assert out["summary"].startswith("The team discussed Checkout flow.")
    assert not meeting_end.looks_like_transcript(out["summary"], _stored())
    assert "walk me through" not in out["summary"]
    assert out["action_items"] and out["action_items"][0]["title"] == "Follow up on Checkout flow"


def test_tickets_created_when_none_exist(env, monkeypatch):
    _fake_llm(monkeypatch, [GOOD])
    meeting = asyncio.run(meeting_end.ensure_tickets_and_summary(_save()))
    stored = _stored()
    [ticket] = stored["tickets"]
    assert ticket["title"] == "Add retries to payment capture" and ticket["owner"] == "Sam"
    assert ticket["status"] == "draft" and ticket["id"].startswith("T-")
    for key in ("body", "acceptance", "agent_prompt", "topic", "source", "links"):
        assert key in ticket
    assert stored["ai_summary"] == stored["summary"] == GOOD["summary"]
    assert stored["todos"][0]["ticket_id"] == ticket["id"]
    assert meeting["tickets"][0]["id"] == ticket["id"]


def test_owner_falls_back_to_first_human_speaker(env, monkeypatch):
    _fake_llm(monkeypatch, [RuntimeError("down")])
    asyncio.run(meeting_end.ensure_tickets_and_summary(_save()))
    assert [t["owner"] for t in _stored()["tickets"]] == ["Preet Karia"]


def test_tickets_not_duplicated_when_they_exist(env, monkeypatch):
    _fake_llm(monkeypatch, [GOOD])
    existing = {"type": "ticket", "id": "T-old", "title": "Existing", "status": "draft", "owner": "Sam",
                "links": {"jira": None, "slack": None, "agent": None}}
    asyncio.run(meeting_end.ensure_tickets_and_summary(_save([existing])))
    asyncio.run(meeting_end.ensure_tickets_and_summary(_stored()))  # second run is idempotent too
    assert [t["id"] for t in _stored()["tickets"]] == ["T-old"]


def test_create_tickets_off_creates_nothing(env, monkeypatch):
    _fake_llm(monkeypatch, [GOOD])
    monkeypatch.setattr(integrations, "ticket_permission", lambda s, w: "off")
    out = asyncio.run(meeting_end.run(None, _save()))
    assert _stored()["tickets"] == [] and out["pushed"] == [] and env["issues"] == 0


def test_pipeline_pushes_then_recap_has_jira_links(env, monkeypatch):
    _fake_llm(monkeypatch, [GOOD])
    out = asyncio.run(meeting_end.run(None, _save()))
    assert out["pushed"] == ["SCRUM-11"] and out["recap"] == {"ok": True}
    [body] = env["slack"]
    text = json.dumps(body)
    assert "<https://example.atlassian.net/browse/SCRUM-11|SCRUM-11 Add retries to payment capture>" in text
    assert "assignee: Sam" in text and "*Action items*" in text and "*Decisions*" in text
    assert GOOD["summary"] in text and "Preet Karia:" not in text and "pending" not in text
    assert body["blocks"][0]["text"]["text"].startswith("Twin recap: Checkout flow")
    # Dedupe per meeting: a second run does not post again or re-push.
    again = asyncio.run(meeting_end.run(None, _stored()))
    assert again["pushed"] == [] and len(env["slack"]) == 1 and env["issues"] == 1
