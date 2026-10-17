"""Workspace meeting snapshots survive restart without crossing tenants."""

from types import SimpleNamespace
import pytest

from brain.meeting_store import list_meetings, list_tickets, save_meeting


def session(room="acme-standup", workspace="acme"):
    return SimpleNamespace(room=room, workspace=workspace, history=[], ticket_drafts={},
                           active_topic={"title": "Auth planning"})


def test_meeting_upsert_persists_summary_decisions_todos_and_owner(monkeypatch, tmp_path):
    monkeypatch.setenv("TWIN_DB_PATH", str(tmp_path / "shared.sqlite3"))
    s = session()
    s.history = [{"speaker": "Preet", "text": "We decided to move login sessions to Redis.", "ts": 123},
                 {"speaker": "Parsh", "text": "Avery owns the migration.", "ts": 124}]
    s.ticket_drafts = {"decision": {"type": "ticket", "id": "T-1", "title": "Move login sessions",
                                    "body": "Decision: Move login sessions to Redis\nSource: meeting",
                                    "owner": "Avery", "status": "draft",
                                    "links": {"jira": None, "slack": None, "agent": None}}}
    first = save_meeting(s)
    assert first and first["ended_at"].endswith("+00:00")
    assert first["summary"].startswith("We decided to move login sessions")
    assert "Move login sessions to Redis" in first["decisions"]
    assert first["todos"][0]["owner"] == "Avery"
    s.history.append({"speaker": "Preet", "text": "We will test the rollback."})
    second = save_meeting(s)
    assert second["id"] == first["id"]
    # A new call opens SQLite again, the same behavior as a process restart.
    meetings = list_meetings("acme")
    assert len(meetings) == 1
    assert len(meetings[0]["notes"]) == 3
    tickets = list_tickets("acme")
    assert len(tickets) == 1
    assert tickets[0]["owner"] == "Avery"
    assert tickets[0]["links"] == {"jira": None, "slack": None, "agent": None}
    assert tickets[0]["meeting_id"] == first["id"]


def test_workspace_isolation_and_lobby_or_empty_skip(monkeypatch, tmp_path):
    monkeypatch.setenv("TWIN_DB_PATH", str(tmp_path / "shared.sqlite3"))
    acme = session()
    acme.history = [{"speaker": "Preet", "text": "Acme plan"}]
    other = session("other-retro", "other")
    other.history = [{"speaker": "Parsh", "text": "Other plan"}]
    assert save_meeting(acme)
    assert save_meeting(other)
    assert [m["summary"] for m in list_meetings("acme")] == ["Acme plan"]
    assert [m["summary"] for m in list_meetings("other")] == ["Other plan"]
    lobby = session("acme-lobby")
    lobby.history = [{"speaker": "Preet", "text": "Lobby chat"}]
    assert save_meeting(lobby) is None
    anonymous = session("demo", None)
    anonymous.history = [{"speaker": "Preet", "text": "Public meeting"}]
    assert save_meeting(anonymous) is None
    assert save_meeting(session("acme-empty")) is None
    assert len(list_meetings("acme")) == 1


def test_ticket_only_meeting_and_cross_workspace_id_reuse(monkeypatch, tmp_path):
    monkeypatch.setenv("TWIN_DB_PATH", str(tmp_path / "shared.sqlite3"))
    s = session()
    s.ticket_drafts = {"one": {"id": "T-2", "title": "Add rollback test", "owner": "Parsh",
                               "status": "draft", "links": {"jira": "https://example.com/T-2"}}}
    saved = save_meeting(s)
    assert saved["summary"] == "Add rollback test"
    assert list_tickets("acme")[0]["owner"] == "Parsh"
    assert list_tickets("other") == []
    s.workspace = "other"
    with pytest.raises(ValueError, match="another workspace"):
        save_meeting(s)
    assert list_meetings("other") == []
    assert list_meetings("acme")[0]["id"] == saved["id"]
