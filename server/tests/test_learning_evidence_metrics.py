"""The committed real Coach result is a faithful fresh-clone Scoreboard feed."""

import json

import pytest

import protocol
from learning.evidence_metrics import EVIDENCE_FILE, load_committed_metrics


def test_committed_feed_preserves_actual_scores_versions_and_rejection():
    evidence = json.loads(EVIDENCE_FILE.read_text(encoding="utf-8"))
    rows = load_committed_metrics()
    assert rows == evidence["metric_messages"]
    assert evidence["selected_version"] == "v1"
    assert evidence["candidate_status"] == "rejected"
    assert evidence["scores"]["validation"]["v1"]["prediction_score"] == pytest.approx(0.6)
    assert evidence["scores"]["validation"]["v2"]["prediction_score"] == pytest.approx(0.4)
    assert evidence["scores"]["locked_test"]["v1"]["prediction_score"] == pytest.approx(0.6)
    assert evidence["scores"]["locked_test"]["v2_candidate"]["prediction_score"] == pytest.approx(0.6)
    measured = {(row["version"], row["split"], row["name"]): row["value"] for row in rows}
    assert measured[("v1", "locked_test", "prediction_score")] == pytest.approx(0.6)
    assert measured[("v2 candidate (rejected)", "locked_test", "prediction_score")] == pytest.approx(0.6)
    rejection = [row for row in rows if row["split"] == "rejected"]
    assert len(rejection) == 1
    assert rejection[0]["name"].startswith("rejected:")
    assert rejection[0]["selected_version"] == "v1"
    assert all(protocol.parse(row).type == "metric" for row in rows)


def test_missing_or_invalid_evidence_never_yields_partial_or_invented_rows(tmp_path):
    path = tmp_path / "evidence.json"
    assert load_committed_metrics(path) == []
    path.write_text("{bad json")
    assert load_committed_metrics(path) == []
    evidence = json.loads(EVIDENCE_FILE.read_text(encoding="utf-8"))
    evidence["metric_messages"][-1]["value"] = float("nan")
    path.write_text(json.dumps(evidence))
    assert load_committed_metrics(path) == []
    evidence["metric_messages"][-1]["value"] = 0
    evidence["metric_messages"][-1]["type"] = "draw"
    path.write_text(json.dumps(evidence))
    assert load_committed_metrics(path) == []
    evidence["metric_messages"][-1]["type"] = "metric"
    evidence["metric_messages"][-1]["value"] = 10 ** 400
    path.write_text(json.dumps(evidence))
    assert load_committed_metrics(path) == []
