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
    with c.websocket_connect("/ws?room=crusoehacks-z&user=A") as ws:  # room prefix alone is never trusted
        ws.receive_json()
        assert getattr(SESSIONS["crusoehacks-z"], "workspace", None) is None
