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
    assert len([c for c in jira_env if c.method == "POST" and c.url.path.endswith("/issue")]) == 1
    # The agent brief is attached once, to the created issue.
    assert [c.url.path for c in jira_env if c.url.path.endswith("/attachments")] == ["/rest/api/3/issue/SCRUM-7/attachments"]
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


# ------------------------------------------------------------ rich ADF description

def _walk(node):
    yield node
    for child in node.get("content") or []:
        yield from _walk(child)


def _texts(adf):
    return [n["text"] for n in _walk(adf) if n.get("type") == "text"]


def _rich_session(room="acme-rich"):
    session = SESSIONS[room] = Session(room)
    session.workspace = "acme"
    session.repo_url = "https://github.com/acme/twin.git"
    session.history = [
        {"speaker": "Bea", "text": "Morning all", "ts": 1},
        {"speaker": "Avery", "text": "The client keeps dropping uploads", "ts": 2},
        {"speaker": "Twin", "text": "Uploads go through upload.py", "ts": 3},
        {"speaker": "Casey", "text": "We will add retries to the uploader", "ts": 4},
        {"speaker": "Bea", "text": "Sounds good", "ts": 5},
        {"speaker": "Avery", "text": "Next topic", "ts": 6},
        {"speaker": "Bea", "text": "Lunch?", "ts": 7},
    ]
    session.latest_contribution = {
        "type": "contribution", "topic_id": "uploads", "current": "Uploads fail once.",
        "impact": ["fewer lost files"], "risks": ["duplicate writes"],
        "question": "Should retries be idempotent?", "sources": ["server/upload.py:10-24"]}
    session.board.apply({"op": "frame", "id": "uploads", "title": "Uploads",
                         "x": 0, "y": 0, "w": 800, "h": 600})
    session.board.apply({"op": "add_mermaid", "id": "m1", "x": 10, "y": 10,
                         "frame": "uploads", "code": "graph TD; A-->B"})
    ticket = {"type": "ticket", "id": "T-R", "title": "Add retries to the uploader",
              "body": "Decision: We will add retries to the uploader\nSource: meeting acme-rich @ 4\n"
                      "Relevant indexed files: server/upload.py, server/client.py",
              "owner": None, "acceptance": "Retries happen. Tests pass.",
              "agent_prompt": "Implement retries", "topic": "uploads",
              "source": "meeting acme-rich @ 4", "status": "draft",
              "links": {"jira": None, "slack": None, "agent": None}}
    session.ticket_drafts = OrderedDict(r=ticket)
    return session, ticket


def _posted_fields(calls):
    return json.loads([c for c in calls if c.method == "POST"][0].content)["fields"]


def test_rich_description_sections_links_mermaid_and_speaker_fallback(jira_env):
    session, ticket = _rich_session()
    orig = jira_env  # calls list

    resp = TestClient(app.app).post("/tickets/T-R/push_jira",
                                    params={"workspace": "acme", "code": "invite"})
    assert resp.status_code == 200, resp.text
    fields = _posted_fields(orig)
    adf = fields["description"]
    headings = [n["content"][0]["text"] for n in adf["content"] if n["type"] == "heading"]
    assert headings == ["Decision", "Context", "Twin's take", "Relevant files",
                        "Diagram (Mermaid)", "Agent prompt", "Acceptance"]
    assert all(n["attrs"]["level"] == 3 for n in adf["content"] if n["type"] == "heading")
    texts = _texts(adf)
    assert "Casey: We will add retries to the uploader" in texts
    assert "Avery: The client keeps dropping uploads" in texts and "Bea: Lunch?" not in texts
    assert "Current: Uploads fail once." in texts and "Risk: duplicate writes" in texts
    assert "Question: Should retries be idempotent?" in texts
    links = {n["text"]: n["marks"][0]["attrs"]["href"] for n in _walk(adf)
             if n.get("type") == "text" and n.get("marks")}
    assert links["server/upload.py:10-24"] == \
        "https://github.com/acme/twin/blob/HEAD/server/upload.py#L10-L24"
    assert links["server/client.py"] == "https://github.com/acme/twin/blob/HEAD/server/client.py"
    blocks = [n for n in adf["content"] if n["type"] == "codeBlock"]
    assert blocks[0]["attrs"] == {"language": "mermaid"}
    assert blocks[0]["content"][0]["text"] == "graph TD; A-->B"
    assert blocks[1]["content"][0]["text"] == "Implement retries"
    accept = [n for n in adf["content"] if n["type"] == "bulletList"][-1]
    assert [li["content"][0]["content"][0]["text"] for li in accept["content"]] == \
        ["Retries happen.", "Tests pass."]
    assert adf["content"][-1]["content"][0]["text"].startswith("Created by Twin from meeting")
    # No owner: Casey (who said the decision) is searched; no Jira match -> unassigned.
    searches = [c.url.params["query"] for c in orig if c.url.path == "/rest/api/3/user/search"]
    assert searches == ["Casey"] and "assignee" not in fields
    assert TOKEN not in json.dumps(fields)


def test_speaker_fallback_assigns_when_jira_user_found(jira_env):
    session, ticket = _rich_session("acme-rich2")
    session.history[3]["speaker"] = "Avery"  # Avery is a Jira user in the mock
    resp = TestClient(app.app).post("/tickets/T-R/push_jira",
                                    params={"workspace": "acme", "code": "invite"})
    assert resp.status_code == 200
    assert _posted_fields(jira_env)["assignee"] == {"id": "acc-1"}


def test_plain_text_files_without_github_repo(jira_env):
    session, ticket = _rich_session("acme-rich3")
    session.repo_url = "https://gitlab.com/acme/twin"
    adf, speaker = integrations.build_jira_description(ticket, session)
    assert speaker == "Casey"
    assert "server/upload.py:10-24" in _texts(adf)
    assert not any(n.get("marks") for n in _walk(adf))


def test_saved_ticket_without_session_uses_meeting_notes(jira_env):
    from brain.meeting_store import save_meeting
    _, ticket = _rich_session("acme-tmp")
    history = SESSIONS.pop("acme-tmp").history
    ticket["id"] = "T-S"
    save_meeting(SimpleNamespace(workspace="acme", room="acme-gone", history=history,
                                 ticket_drafts={"r": ticket}))
    resp = TestClient(app.app).post("/tickets/T-S/push_jira",
                                    params={"workspace": "acme", "code": "invite"})
    assert resp.status_code == 200, resp.text
    adf = _posted_fields(jira_env)["description"]
    texts = _texts(adf)
    assert "Casey: We will add retries to the uploader" in texts
    assert "Diagram (Mermaid)" not in texts and "Twin's take" not in texts
    assert "Agent prompt" in texts and "Acceptance" in texts
    assert "server/client.py" in texts  # no repo_url saved for acme -> plain text


def test_build_description_minimal_ticket_no_session():
    adf, speaker = integrations.build_jira_description({"id": "T", "title": "Do it"})
    assert adf["type"] == "doc" and speaker is None and adf["content"]
