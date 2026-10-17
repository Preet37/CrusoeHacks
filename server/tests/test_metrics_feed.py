import json

from fastapi.testclient import TestClient

import app as app_module
import mouth


def test_metrics_sent_on_join_and_refresh(tmp_path, monkeypatch):
    f = tmp_path / "latest_metrics.json"
    f.write_text(json.dumps([
        {"type": "metric", "version": "v1", "split": "locked_test", "name": "prediction_score", "value": 0.42},
        {"type": "metric", "version": "v2", "split": "locked_test", "name": "prediction_score", "value": 0.58},
        {"type": "bogus"},
    ]))
    monkeypatch.setattr(app_module, "METRICS_FILE", f)
    monkeypatch.setattr(mouth, "attach", lambda s: None)
    c = TestClient(app_module.app)
    with c.websocket_connect("/ws?room=mtest&user=A") as ws:
        got = [ws.receive_json() for _ in range(4)]
        metrics = [m for m in got if m["type"] == "metric"]
        assert [m["version"] for m in metrics] == ["v1", "v2"]
    assert app_module.load_metrics()[1].value == 0.58


def test_missing_metrics_file_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "METRICS_FILE", tmp_path / "nope.json")
    assert app_module.load_metrics() == []
