"""Offline contracts for a measured M3 evaluation and real text replay path."""

import json

import pytest

import llm
import protocol
from agents import listener
from learning.evaluate import load_cases, save_metric_payload, score_case, score_run
from learning.replay import replay_case, replay_prep_case
from learning import prep


def test_corpus_has_explicit_splits_and_locked_requires_final_gate():
    train = load_cases("train")
    validation = load_cases("validation")
    with pytest.raises(PermissionError):
        load_cases("locked_test")
    locked = load_cases("locked_test", include_locked=True)
    assert (len(train), len(validation), len(locked)) == (8, 2, 2)
    assert len({case["id"] for case in train + validation + locked}) == 12
    assert all(case["ground_truth"] and case["lines"]
               for case in train + validation + locked)


def test_scores_use_recorded_evidence_and_emit_protocol_metrics(tmp_path):
    case = {"id": "test", "split": "validation", "ground_truth": {
        "impacted_files": ["auth/login.py", "web/routes.py"],
        "false_claim_sources": ["demo_project/auth/login.py:18-24"],
        "tasks": [{"keywords": ["login", "Redis"], "owner": "Avery Chen"}],
        "visual_ops": ["highlight"],
    }}
    observed = {"id": "test", "file_paths": ["auth/login.py"],
                "updates": [{"text": "Correction (source: demo_project/auth/login.py:18-24).",
                             "urgent": True}],
                "tasks": [{"title": "Migrate login to Redis", "owner": "Avery Chen"}],
                "draws": [{"op": "highlight", "ids": ["file_login"], "on": True}]}
    result = score_case(case, observed)
    assert result["metrics"]["impact_accuracy"] == pytest.approx(2 / 3)
    assert result["metrics"]["false_claim_recall"] == 1
    assert result["metrics"]["task_accuracy"] == 1
    assert result["metrics"]["visual_accuracy"] == 1
    assert "tokens" not in result["metrics"]
    assert "latency_ms" not in result["metrics"]
    report = score_run([case], [observed], "validation", version="v2")
    assert report["metrics"]["overall_score"] == pytest.approx(11 / 12)
    assert all(protocol.parse(message).type == "metric"
               for message in report["metric_messages"])
    path = tmp_path / "latest_metrics.json"
    assert save_metric_payload([report], path) == json.loads(path.read_text())
    assert isinstance(json.loads(path.read_text()), list)


def test_missing_observation_and_split_mismatch_rejected():
    case = {"id": "test", "split": "train", "ground_truth": {}}
    with pytest.raises(ValueError, match="missing observations"):
        score_run([case], [], "train")
    with pytest.raises(ValueError, match="split mismatch"):
        score_run([case], [{"id": "test"}], "validation")


def test_prediction_matching_uses_maximum_one_to_one_assignment():
    case = {"id": "overlap", "ground_truth": {
        "prediction_topics": [["login"], ["login", "Redis"]]}}
    observed = {"predictions": ["login Redis migration", "login page"]}
    assert score_case(case, observed)["metrics"]["prediction_score"] == 1


@pytest.mark.asyncio
async def test_prep_only_replay_scores_actual_predictions_without_other_metrics(monkeypatch):
    async def fake_prep(model, messages, **kw):
        assert "CANDIDATE_PREP" in messages[0]["content"]
        assert "LOCKED_SECRET_SHOULD_NOT_LEAK" not in json.dumps(messages)
        return '{"predictions":["Login JWT behavior","Redis migration risk"]}'

    monkeypatch.setattr(llm, "chat", fake_prep)
    case = {"id": "prep-test", "split": "validation", "meeting": "Login planning",
            "lines": [{"speaker": "Preet", "text": "Discuss Redis migration"}],
            "ground_truth": {"prediction_topics": [["JWT"], ["Redis"]],
                             "impacted_files": ["auth/login.py"],
                             "false_claim_sources": [],
                             "tasks": [{"keywords": ["Redis"]}],
                             "secret": "LOCKED_SECRET_SHOULD_NOT_LEAK"}}
    original_prompt = prep.PROMPT
    observed = await replay_prep_case(case, prompt_override={"prep": "CANDIDATE_PREP"})
    report = score_run([case], [observed], "validation", version="v2")
    assert observed["predictions"] == ["Login JWT behavior", "Redis migration risk"]
    assert report["metrics"]["prediction_score"] == 1
    assert "impact_accuracy" not in report["metrics"]
    assert "tokens" not in report["metrics"]
    assert prep.PROMPT == original_prompt


@pytest.mark.asyncio
async def test_replay_runs_real_listener_conductor_and_restores_candidate_prompt(monkeypatch):
    seen_marker = []

    async def fake_chat(model, messages, **kw):
        system = messages[0]["content"]
        if "CANDIDATE_MARKER" in system:
            seen_marker.append(True)
        if "Listener" in system or "moments" in system.lower():
            return json.dumps({"moments": [{"type": "question",
                                           "text": "What does changing login touch?",
                                           "entities": []}],
                               "topic_id": "t_auth", "title": "Auth", "topic_changed": True})
        if "Conductor router" in system:
            return '{"routes":[]}'
        if "Code Analyst" in system:
            return json.dumps({"summary": "Login affects the mapped files.",
                               "paths": ["auth/login.py", "web/routes.py"]})
        raise AssertionError(system[:100])

    monkeypatch.setattr(llm, "chat", fake_chat)
    original = listener.PROMPT
    case = {"id": "test-replay", "split": "train",
            "lines": [{"speaker": "Preet", "text": "Twin, what does changing login touch?"}],
            "ground_truth": {}}
    observed = await replay_case(case, prompt_override={
        "listener": original.read_text() + "\nCANDIDATE_MARKER\n"}, timeout_per_line=2)
    assert seen_marker
    assert listener.PROMPT == original
    assert "auth/login.py" in observed["file_paths"]
    assert any(event["agent"] == "Code Analyst" for event in observed["events"])
    assert any(draw["op"] == "highlight" for draw in observed["draws"])
    assert observed["updates"]
    assert "tokens" not in observed  # fake model reported no usage
    assert observed["latency_ms"] >= 0
    assert all(protocol.parse(message) for message in observed["messages"])
