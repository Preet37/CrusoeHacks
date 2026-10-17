import pytest
from fastapi.testclient import TestClient

import app as app_module
import mouth
from session import SESSIONS


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setattr(mouth, "attach", lambda s: None)
    monkeypatch.setattr(app_module, "METRICS_FILE", tmp_path / "none.json")
    monkeypatch.setenv("WORKSPACES", "crusoehacks:s3cret, other:xyz")


def test_check_workspace_rules(monkeypatch):
    assert app_module.check_workspace("", "") == (None, None)
    assert app_module.check_workspace("CrusoeHacks", "s3cret") == ("crusoehacks", None)
    t, err = app_module.check_workspace("crusoehacks", "nope")
    assert t is None and "invite code" in err and "workspace" in err
    monkeypatch.setenv("WORKSPACES", "")
    assert app_module.check_workspace("crusoehacks", "anything") == (None, None)  # not configured: isolated


def test_wrong_code_gets_error_and_valid_code_sets_trusted_workspace():
    c = TestClient(app_module.app)
    with c.websocket_connect("/ws?room=crusoehacks-x&user=A&workspace=crusoehacks&code=bad") as ws:
        m = ws.receive_json()
        assert m["type"] == "error" and "invite code" in m["message"]
    with c.websocket_connect("/ws?room=crusoehacks-y&user=A&workspace=crusoehacks&code=s3cret") as ws:
        assert ws.receive_json()["type"] == "status"
        assert SESSIONS["crusoehacks-y"].workspace == "crusoehacks"
    with c.websocket_connect("/ws?room=plainroom&user=A") as ws:  # non-workspace rooms stay open and untrusted
        ws.receive_json()
        assert getattr(SESSIONS["plainroom"], "workspace", None) is None


def test_workspace_room_rejects_no_code_and_other_workspace():
    c = TestClient(app_module.app)
    with c.websocket_connect("/ws?room=crusoehacks-lobby&user=A&workspace=crusoehacks&code=s3cret") as a:
        assert a.receive_json()["type"] == "status"
        with c.websocket_connect("/ws?room=crusoehacks-lobby&user=B") as b:  # no code
            m = b.receive_json()
            assert m["type"] == "error" and "invite code" in m["message"]
        with c.websocket_connect("/ws?room=crusoehacks-lobby&user=C&workspace=other&code=xyz") as d:  # other ws
            m = d.receive_json()
            assert m["type"] == "error" and "invite code" in m["message"]
        assert SESSIONS["crusoehacks-lobby"].workspace == "crusoehacks"


def test_prefixed_room_needs_code_even_before_anyone_joined():
    c = TestClient(app_module.app)
    with c.websocket_connect("/ws?room=crusoehacks-fresh&user=B") as b:
        assert b.receive_json()["type"] == "error"
    assert "crusoehacks-fresh" not in SESSIONS
