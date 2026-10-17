"""Auto Jira at meeting end + Ticket Agent (PARSH-CLAUDE). Jira, git clone and Nemotron are all faked: no network."""
import asyncio
import json
import subprocess
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import app
import integrations
import llm
from brain import repo_connector
from integrations import jira, ticket_agent
from session import SESSIONS

REPO_URL = "https://github.com/acme/shop"
CODE = "invite"


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("TWIN_DB_PATH", str(tmp_path / "company.sqlite3"))
    monkeypatch.setenv("WORKSPACES", f"acme:{CODE}")
    monkeypatch.setenv("JIRA_BASE_URL", "https://example.atlassian.net/")
    monkeypatch.setenv("JIRA_EMAIL", "bot@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    monkeypatch.setenv("JIRA_PROJECT_KEY", "SCRUM")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(jira, "_status_cache", {"key": None, "at": 0.0, "value": None})
    state = {"issues": 0, "comments": [], "attachments": [], "transitions": []}

    def handler(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p == "/rest/api/3/user/search":
            return httpx.Response(200, json=[{"accountId": "acc-parsh", "accountType": "atlassian", "active": True}])
        if p == "/rest/api/3/issue" and req.method == "POST":
            state["issues"] += 1
            return httpx.Response(201, json={"key": f"SCRUM-{10 + state['issues']}"})
        if p.endswith("/attachments"):
            state["attachments"].append(p)
            return httpx.Response(200, json=[{"filename": "x"}])
        if p.endswith("/comment"):
            state["comments"].append(json.loads(req.content))
            return httpx.Response(201, json={})
        if p.endswith("/transitions") and req.method == "GET":
            return httpx.Response(200, json={"transitions": [{"id": "21", "to": {"name": "In Progress"}},
                                                             {"id": "31", "to": {"name": "In Review"}}]})
        if p.endswith("/transitions"):
            state["transitions"].append(json.loads(req.content)["transition"]["id"])
            return httpx.Response(204)
        if p.startswith("/rest/api/3/issue/SCRUM-") and req.method == "GET":
            return httpx.Response(200, json={"fields": {
                "summary": "Add retries", "assignee": {"displayName": "Parsh"}, "reporter": {"displayName": "Preet"},
                "status": {"name": "To Do"},
                "description": {"type": "doc", "version": 1, "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "Retry the Jira push on 429."}]}]},
                "attachment": [{"id": "1", "filename": "SCRUM-11-agent-brief.md", "mimeType": "text/markdown",
                                "size": 60, "content": "https://example.atlassian.net/att/1"}]}})
        if p == "/att/1":
            return httpx.Response(200, content=b"# Brief\nChange shop/jira.py so push retries three times.")
        return httpx.Response(404, json={"errorMessages": ["nope"]})

    monkeypatch.setattr(jira, "_TRANSPORT", httpx.MockTransport(handler))

    # A tiny git repo stands in for the GitHub clone.
    src = tmp_path / "src"
    (src / "shop").mkdir(parents=True)
    (src / "shop" / "jira.py").write_text("def push(issue):\n    return send(issue)\n")
    subprocess.run(["git", "init", "-q", str(src)], check=True)
    subprocess.run(["git", "-C", str(src), "-c", "user.email=t@t", "-c", "user.name=t", "add", "."], check=True)
    subprocess.run(["git", "-C", str(src), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
                   check=True)

    async def fake_clone(url, target):
        assert url == REPO_URL + ".git"
        subprocess.run(["git", "clone", "-q", str(src), str(target)], check=True)

    monkeypatch.setattr(repo_connector, "_clone", fake_clone)
    monkeypatch.setattr(integrations, "_repo_url", lambda *a, **k: REPO_URL)
    yield state
    for room in [r for r in SESSIONS if r.startswith("acme-")]:
        SESSIONS.pop(room, None)


def _save_meeting(tickets):
    from brain.meeting_store import _connect
    payload = {"id": "m1", "workspace": "acme", "room": "acme-standup", "title": "Jira", "ended_at": "2026-10-17T23:00:00",
               "summary": "", "notes": [{"speaker": "Parsh", "text": "We decided to add retries to the Jira push."}],
               "decisions": [], "todos": [], "tickets": tickets}
    with _connect() as db:
        db.execute("INSERT INTO meetings(id, workspace, ended_at, payload_json) VALUES(?,?,?,?)",
                   ("m1", "acme", payload["ended_at"], json.dumps(payload)))
        db.commit()
    return payload


def _ticket(tid="T-1", **kw):
    return {"type": "ticket", "id": tid, "title": "Add retries", "owner": "Parsh", "status": "draft",
            "body": "Decision: Add retries to the Jira push\nRelevant indexed files: shop/jira.py",
            "acceptance": "Push retries three times.", "links": {"jira": None, "slack": None, "agent": None}, **kw}


def _fake_model(monkeypatch, content):
    calls = []

    async def fake_chat(model, messages, **kw):
        calls.append((model, messages, kw))
        return json.dumps({"summary": "Push now retries.", "plan": ["wrap send in a retry loop"],
                           "changes": [{"path": "shop/jira.py", "action": "modify", "content": content}],
                           "tests": ["call push with a failing send"], "notes": ""})

    monkeypatch.setattr(llm, "chat", fake_chat)
    return calls


def test_meeting_end_auto_creates_jira_and_records_people(env, monkeypatch):
    started = []
    monkeypatch.setenv("TICKET_AGENT", "auto")
    monkeypatch.setattr(ticket_agent, "start", lambda tid, ws, room: started.append((tid, ws, room)) or True)
    meeting = _save_meeting([_ticket("T-1"), _ticket("T-2", status="dismissed"),
                             _ticket("T-3", links={"jira": "https://example.atlassian.net/browse/SCRUM-1"})])
    pushed = asyncio.run(integrations.auto_push_meeting(meeting))
    assert pushed == ["SCRUM-11"] and env["issues"] == 1       # dismissed + already-pushed are skipped
    saved = {t["id"]: t for t in integrations.list_tickets("acme")}
    assert saved["T-1"]["jira_key"] == "SCRUM-11" and saved["T-1"]["status"] == "sent"
    assert saved["T-1"]["jira"] == {"assignee": "Parsh", "reporter": "Preet", "status": "To Do",
                                    "url": "https://example.atlassian.net/browse/SCRUM-11"}
    assert started == [("T-1", "acme", "acme-standup")]       # the Ticket Agent spawns after the push


def test_auto_push_respects_create_tickets_off(env, monkeypatch):
    monkeypatch.setattr(integrations, "ticket_permission", lambda s, w: "off")
    assert asyncio.run(integrations.auto_push_meeting(_save_meeting([_ticket()]))) == []
    assert env["issues"] == 0


def test_ticket_agent_end_to_end(env, monkeypatch):
    calls = _fake_model(monkeypatch, "def push(issue, attempts=3):\n    for i in range(attempts):\n"
                                     "        try:\n            return send(issue)\n        except Exception:\n"
                                     "            if i == attempts - 1:\n                raise\n")
    _save_meeting([_ticket(jira_key="SCRUM-11", links={"jira": "https://example.atlassian.net/browse/SCRUM-11"})])
    result = asyncio.run(ticket_agent.run("T-1", "acme", "acme-standup"))
    assert result["status"] == "done" and result["files"] == [{"path": "shop/jira.py", "action": "modify"}]
    assert result["checks"][0]["ok"] and "+    for i in range(attempts):" in result["patch"]
    prompt = calls[0][1][1]["content"]
    assert "Retry the Jira push on 429." in prompt                 # Jira description
    assert "Change shop/jira.py so push retries" in prompt         # downloaded agent-brief.md attachment
    assert "## File: shop/jira.py" in prompt                       # relevant file from the clone
    assert env["attachments"] == ["/rest/api/3/issue/SCRUM-11/attachments"]  # SCRUM-11-agent.patch
    comment = json.dumps(env["comments"][0])
    assert "Twin ticket agent" in comment and "modify: shop/jira.py" in comment
    assert env["transitions"] == ["31"]                            # moved to In Review
    saved = integrations.list_tickets("acme")[0]
    assert saved["agent"]["status"] == "done" and saved["jira"]["status"] == "In Review"
    assert saved["jira"]["assignee"] == "Parsh"


def test_ticket_agent_flags_syntax_errors_and_rejects_escaping_paths(env, monkeypatch):
    async def fake_chat(model, messages, **kw):
        return json.dumps({"summary": "x", "changes": [
            {"path": "shop/jira.py", "action": "modify", "content": "def push(:\n"},
            {"path": "../outside.py", "action": "create", "content": "x = 1\n"}]})
    monkeypatch.setattr(llm, "chat", fake_chat)
    _save_meeting([_ticket(jira_key="SCRUM-11")])
    result = asyncio.run(ticket_agent.run("T-1", "acme", "acme-standup"))
    assert result["status"] == "needs_review"
    assert any(not c["ok"] and "SyntaxError" in c["detail"] for c in result["checks"])
    assert any("outside the repository" in c["detail"] for c in result["checks"])


def test_agent_routes_and_patch_download(env, monkeypatch):
    _fake_model(monkeypatch, "def push(issue):\n    return send(issue, retries=3)\n")
    _save_meeting([_ticket(jira_key="SCRUM-11")])
    client = TestClient(app.app)
    q = {"workspace": "acme", "code": CODE}
    assert client.post("/tickets/T-1/agent", params={**q, "code": "bad"}).status_code == 403
    assert client.get("/tickets/T-1/agent.patch", params=q).status_code == 404       # nothing yet
    asyncio.run(ticket_agent.run("T-1", "acme", "acme-standup"))
    listed = client.get("/tickets", params=q).json()["tickets"][0]
    assert listed["agent"]["status"] == "done" and listed["agent"]["has_patch"] and "patch" not in listed["agent"]
    patch = client.get("/tickets/T-1/agent.patch", params=q)
    assert patch.status_code == 200 and "retries=3" in patch.text
    assert "attachment" in patch.headers["content-disposition"]
