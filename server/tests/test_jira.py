"""PC-23 Jira integration: httpx is mocked with MockTransport, no real network."""
import asyncio
import json
from collections import OrderedDict
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

import app
import integrations
from integrations import jira
from session import SESSIONS, Session

TOKEN = "secret-token-should-never-leak"


@pytest.fixture
def jira_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TWIN_DB_PATH", str(tmp_path / "company.sqlite3"))
    monkeypatch.setenv("WORKSPACES", "acme:invite")
    monkeypatch.setenv("JIRA_BASE_URL", "https://example.atlassian.net/")
    monkeypatch.setenv("JIRA_EMAIL", "bot@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", TOKEN)
    monkeypatch.setenv("JIRA_PROJECT_KEY", "SCRUM")
    monkeypatch.setattr(jira, "_status_cache", {"key": None, "at": 0.0, "value": None})
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        if path == "/rest/api/3/myself":
            return httpx.Response(200, json={"displayName": "Twin Bot"})
        if path == "/rest/api/3/project/SCRUM":
            return httpx.Response(200, json={"key": "SCRUM", "name": "Scrum"})
        if path == "/rest/api/3/user/search":
            if request.url.params["query"] == "Avery":
                return httpx.Response(200, json=[{"accountId": "acc-1", "accountType": "atlassian",
                                                  "active": True}])
            return httpx.Response(200, json=[])
        if path == "/rest/api/3/issue" and request.method == "POST":
            return httpx.Response(201, json={"id": "1", "key": "SCRUM-7"})
        return httpx.Response(404, json={"errorMessages": ["nope"]})

    monkeypatch.setattr(jira, "_TRANSPORT", httpx.MockTransport(handler))
    yield calls
    for room in [r for r in SESSIONS if r.startswith(("acme-", "jira-"))]:
        SESSIONS.pop(room, None)


def _draft(tid="T-1", owner="Avery"):
    return {"type": "ticket", "id": tid, "title": "Add retries", "body": "Decision: add retries",
            "owner": owner, "acceptance": "Retries work", "status": "draft",
            "links": {"jira": None, "slack": None, "agent": None}}


def test_status_connected_and_cached(jira_env):
    first = asyncio.run(jira.status())
    assert first["connected"] and first["account"] == "Twin Bot" and first["project"] == "SCRUM"
    assert first["site"] == "https://example.atlassian.net"
    asyncio.run(jira.status())
    assert len(jira_env) == 2  # second call served from cache
    assert TOKEN not in json.dumps(first)


def test_status_not_configured(monkeypatch):
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
    monkeypatch.setenv("JIRA_API_TOKEN", "")
    out = asyncio.run(jira.status())
    assert out["connected"] is False and "not configured" in out["error"]


def test_status_auth_error_is_reported_without_token(jira_env, monkeypatch):
    monkeypatch.setattr(jira, "_TRANSPORT", httpx.MockTransport(
        lambda r: httpx.Response(401, json={"errorMessages": ["Unauthorized"]})))
    out = asyncio.run(jira.status())
    assert out["connected"] is False and "401" in out["error"] and TOKEN not in out["error"]


def test_create_issue_with_assignee_and_adf(jira_env):
    issue = asyncio.run(jira.create_issue("Add retries", "Line one\nLine two", owner="Avery",
                                          meeting="standup"))
    assert issue == {"key": "SCRUM-7", "url": "https://example.atlassian.net/browse/SCRUM-7"}
    post = [c for c in jira_env if c.method == "POST"][0]
    assert post.headers["authorization"].startswith("Basic ")
    fields = json.loads(post.content)["fields"]
    assert fields["project"] == {"key": "SCRUM"} and fields["issuetype"] == {"name": "Task"}
    assert fields["assignee"] == {"id": "acc-1"} and fields["labels"] == ["twin"]
    texts = [p["content"][0]["text"] for p in fields["description"]["content"]]
    assert texts == ["Line one", "Line two", "Created by Twin from meeting standup"]


def test_create_issue_unknown_owner_is_unassigned(jira_env):
    asyncio.run(jira.create_issue("X", "y", owner="Nobody"))
    fields = json.loads([c for c in jira_env if c.method == "POST"][0].content)["fields"]
    assert "assignee" not in fields


def test_create_issue_error_raises(jira_env, monkeypatch):
    monkeypatch.setattr(jira, "_TRANSPORT", httpx.MockTransport(
        lambda r: httpx.Response(400, json={"errors": {"summary": "required"}})))
    with pytest.raises(jira.JiraError, match="summary: required"):
        asyncio.run(jira.create_issue("X", "y"))


def test_routes_push_live_ticket(jira_env):
    session = SESSIONS["acme-standup"] = Session("acme-standup")
    session.workspace = "acme"
    session.ticket_drafts = OrderedDict(retry=_draft())
    sent = []

    async def fake_broadcast(msg):
        sent.append(msg)
    session.broadcast = fake_broadcast
    client = TestClient(app.app)
    params = {"workspace": "acme", "code": "invite"}
    listed = client.get("/tickets", params=params).json()["tickets"]
    assert [t["id"] for t in listed] == ["T-1"]
    assert client.get("/tickets", params={**params, "code": "bad"}).status_code == 403
    resp = client.post("/tickets/T-1/push_jira", params=params)
    assert resp.status_code == 200, resp.text
    ticket = resp.json()
    assert ticket["status"] == "sent" and ticket["links"]["jira"].endswith("/browse/SCRUM-7")
    assert sent and sent[0]["type"] == "ticket" and sent[0]["status"] == "sent"
    # Pushed again: no duplicate issue.
    client.post("/tickets/T-1/push_jira", params=params)
    assert len([c for c in jira_env if c.method == "POST"]) == 1
    # Persisted via the meeting store too.
    from brain.meeting_store import list_tickets
    assert list_tickets("acme")[0]["links"]["jira"].endswith("SCRUM-7")
    integ = client.get("/integrations", params=params).json()
    assert integ["jira"]["connected"] and integ["github"] == {"connected": False, "repo_url": None}
    assert "slack" in integ


def test_push_saved_ticket_updates_store(jira_env):
    from brain.meeting_store import list_tickets, save_meeting
    save_meeting(SimpleNamespace(workspace="acme", room="acme-old", history=[],
                                 ticket_drafts={"r": _draft("T-9", owner=None)}))
    client = TestClient(app.app)
    resp = client.post("/tickets/T-9/push_jira", params={"workspace": "acme", "code": "invite"})
    assert resp.status_code == 200
    saved = list_tickets("acme")[0]
    assert saved["status"] == "sent" and saved["jira_key"] == "SCRUM-7"


def test_push_not_configured_is_409(jira_env, monkeypatch):
    monkeypatch.setenv("JIRA_BASE_URL", "")
    session = SESSIONS["jira-demo"] = Session("jira-demo")
    session.ticket_drafts = OrderedDict(retry=_draft())
    resp = TestClient(app.app).post("/tickets/T-1/push_jira", params={"room": "jira-demo"})
    assert resp.status_code == 409 and resp.json() == {"error": "connect Jira"}
    assert session.ticket_drafts["retry"]["status"] == "draft"


def test_ws_ticket_actions_and_permission_off(jira_env):
    session = SESSIONS["jira-ws"] = Session("jira-ws")
    session.ticket_drafts = OrderedDict(retry=_draft())
    out = []

    async def fake_send(ws, msg):
        out.append(("to", msg))
        return True

    async def fake_broadcast(msg):
        out.append(("all", msg))
    session.send_to, session.broadcast = fake_send, fake_broadcast
    asyncio.run(session.ticket_action(None, {"type": "ticket_action", "id": "T-1", "action": "approve"}))
    assert out[-1][0] == "all" and out[-1][1]["status"] == "approved"
    asyncio.run(session.ticket_action(None, {"id": "T-1", "action": "push_jira"}))
    assert out[-1][1]["status"] == "sent" and out[-1][1]["links"]["jira"]
    session.permissions = {"create_tickets": "off"}
    session.ticket_drafts["other"] = _draft("T-2")
    asyncio.run(session.ticket_action(None, {"id": "T-2", "action": "push_jira"}))
    assert out[-1][0] == "to" and out[-1][1].message == integrations.TICKETS_OFF
    asyncio.run(session.ticket_action(None, {"id": "T-404", "action": "approve"}))
    assert "Unknown ticket" in out[-1][1].message
    asyncio.run(session.ticket_action(None, {"id": "T-2", "action": "dismiss"}))
    assert out[-1][1]["status"] == "dismissed"
