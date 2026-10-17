"""Agent brief + board diagram for Jira tickets (PARSH-CLAUDE)."""
import asyncio
import json
from types import SimpleNamespace

import board as board_mod
import integrations
from integrations import agent_brief, jira
from test_jira import jira_env  # noqa: F401  (shared fixture: fake Jira + workspace "acme" with code "invite")


def _board():
    b = board_mod.BoardState()
    for op in [
        {"op": "add_box", "id": "web", "label": "Web app", "x": 100, "y": 300, "kind": "client"},
        {"op": "add_box", "id": "auth", "label": 'Auth "service"', "x": 420, "y": 300, "kind": "service"},
        {"op": "add_box", "id": "db", "label": "Postgres", "x": 420, "y": 650, "kind": "datastore"},
        {"op": "connect", "id": "e_web_auth", "from_id": "web", "to_id": "auth", "label": "login"},
        {"op": "connect", "id": "e_auth_db", "from_id": "auth", "to_id": "db"},
    ]:
        b.apply(op)
    return b


TICKET = {
    "id": "T-abc", "title": "Add retry to Jira push", "owner": "Parsh", "status": "draft",
    "body": "Decision: Add retry with backoff to the Jira push\nSource: meeting acme-standup\n"
            "Relevant indexed files: server/integrations/jira.py",
    "acceptance": "A failed push is retried. The dashboard shows the Jira link.",
    "agent_prompt": "Implement this recorded meeting decision: Add retry with backoff",
}
CONTRIB = {"current": "jira.py posts once.", "impact": ["create_issue changes"], "risks": ["duplicate issues"],
           "question": "How many retries?", "sources": ["server/integrations/jira.py:151-183"]}
NOTES = [{"speaker": "Parsh", "text": "Jira pushes fail on 429."},
         {"speaker": "Parsh", "text": "We decided to add retry with backoff to the Jira push."},
         {"speaker": "Twin", "text": "That touches create_issue."}]


def test_board_flowchart_converts_boxes_and_arrows():
    flow = agent_brief.board_flowchart(_board().snapshot())
    assert flow.startswith("flowchart LR")
    assert 'web(["Web app"])' in flow and 'db[("Postgres")]' in flow
    assert "Auth 'service'" in flow  # quotes made safe for Mermaid
    assert 'web -->|"login"| auth' in flow and "auth --> db" in flow


def test_brief_has_every_section_from_saved_meeting():
    meeting = {"room": "acme-standup", "summary": "Retry Jira pushes.", "notes": NOTES,
               "board": _board().snapshot(), "contribution": CONTRIB}
    md = integrations.agent_brief_markdown(TICKET, None, meeting, None, {"key": "SCRUM-9", "url": "https://x/browse/SCRUM-9"})
    for part in ["# SCRUM-9: Add retry to Jira push", "## Goal", "## Discussion", "## Twin's analysis", "```mermaid",
                 "flowchart LR", "## Relevant files", "server/integrations/jira.py", "## Acceptance criteria",
                 "- [ ] A failed push is retried.", "## How to work on this", "## Agent prompt"]:
        assert part in md, part


def test_jira_description_uses_saved_board_when_meeting_is_over():
    meeting = {"room": "acme-standup", "notes": NOTES, "board": _board().snapshot(), "contribution": CONTRIB}
    doc, speaker = integrations.build_jira_description(TICKET, None, meeting, None)
    text = json.dumps(doc)
    assert "Diagram (Mermaid)" in text and "flowchart LR" in text
    assert "Twin's take" in text and "attached to this issue" in text
    assert speaker == "Parsh"


def test_attach_agent_brief_sends_md_and_mmd(monkeypatch):
    sent = {}

    async def fake_attach(key, files):
        sent["key"], sent["files"] = key, files
        return [f[0] for f in files]

    monkeypatch.setattr(jira, "attach_files", fake_attach)
    session = SimpleNamespace(board=_board(), latest_contribution=CONTRIB, history=NOTES, room="acme-standup",
                              repo_url="https://github.com/Preet37/CrusoeHacks.git")
    names = asyncio.run(integrations.attach_agent_brief(TICKET, session, None, None,
                                                        {"key": "SCRUM-9", "url": "https://x/browse/SCRUM-9"}))
    assert names == ["SCRUM-9-agent-brief.md", "SCRUM-9-diagram-1.mmd"]
    md = sent["files"][0][1].decode()
    assert "https://github.com/Preet37/CrusoeHacks/blob/HEAD/server/integrations/jira.py#L151-L183" in md
    assert sent["files"][1][1].decode().startswith("flowchart LR")


def test_brief_route_requires_code_and_returns_markdown(jira_env):
    from collections import OrderedDict
    from fastapi.testclient import TestClient
    import app
    from session import SESSIONS, Session
    session = SESSIONS["acme-standup"] = Session("acme-standup")
    session.workspace = "acme"
    session.ticket_drafts = OrderedDict(t=dict(TICKET))
    client = TestClient(app.app)
    ok = client.get("/tickets/T-abc/brief.md", params={"workspace": "acme", "code": "invite"})
    assert ok.status_code == 200 and ok.text.startswith("# Add retry to Jira push")
    assert "attachment" in ok.headers["content-disposition"]
    assert client.get("/tickets/T-abc/brief.md", params={"workspace": "acme", "code": "bad"}).status_code == 403
