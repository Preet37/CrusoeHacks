"""A Coach edit is sandboxed, validated, and judged before locked scoring."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from learning import coach, prep


RULE = "When JWT login is evidenced, predict an auth topic using the terms JWT and login."


def _cases():
    return ([{"id": "train-1", "lines": [{"speaker": "Preet", "text": "TRAIN"}],
              "ground_truth": {"topics": ["auth"]}}],
            [{"id": "validation-1", "lines": [{"speaker": "Preet", "text": "VALIDATION"}]}],
            [{"id": "locked-1", "lines": [{"speaker": "Preet", "text": "LOCKED_SECRET"}]}])


def _score(cases, observations, split, *, version):
    improved = all(item["improved"] for item in observations)
    overall = 0.6 if improved else 0.5
    metrics = {"prediction_score": 0.6 if improved else 0.5,
               "overall_score": overall}
    return {"version": version, "split": split, "metrics": metrics,
            "cases": [{"id": case["id"], "score": overall} for case in cases],
            "metric_messages": [{"type": "metric", "version": version, "split": split,
                                 "name": "overall_score", "value": overall}]}


@pytest.mark.asyncio
async def test_coach_keeps_one_improving_edit_and_hides_locked_input(monkeypatch, tmp_path):
    calls = []

    async def replay(case, *, prompt_override):
        changed = RULE in prompt_override["prep"]
        calls.append((case["id"], "v2" if changed else "v1"))
        return {"improved": changed, "topics": ["auth"], "updates": []}

    async def model(model, messages, **kwargs):
        calls.append(("coach", "model"))
        assert model == coach.config.MODEL_COACH
        assert "LOCKED_SECRET" not in json.dumps(messages)
        assert "VALIDATION" not in json.dumps(messages)
        return json.dumps({"target": "prep", "append": RULE,
                           "reason": "Train omitted evidenced auth terms."})

    monkeypatch.setattr(coach.llm, "chat", model)
    result = await coach.run_iteration(*_cases(), replay_fn=replay,
                                       store_dir=tmp_path, score_fn=_score)
    assert result["accepted"] is True and result["selected_version"] == "v2"
    assert calls == [("train-1", "v1"), ("validation-1", "v1"), ("coach", "model"),
                     ("validation-1", "v2"), ("locked-1", "v1"), ("locked-1", "v2")]
    assert coach.active_prompts(tmp_path)["prep"].count(RULE) == 1
    assert [message["version"] for message in result["metric_messages"]] == ["v1", "v2"]
    assert json.loads((tmp_path / "versions" / "v2.json").read_text())["status"] == "accepted"
    assert json.loads((tmp_path / "coach_log.jsonl").read_text().splitlines()[0])["accepted"] is True


@pytest.mark.asyncio
async def test_equal_or_regressing_candidate_is_rejected(monkeypatch, tmp_path):
    async def model(*args, **kwargs):
        return json.dumps({"target": "prep", "append": RULE,
                           "reason": "Try a small clarification."})

    monkeypatch.setattr(coach.llm, "chat", model)
    calls = []

    async def replay(case, *, prompt_override):
        calls.append(case["id"])
        return {"improved": RULE in prompt_override["prep"]}

    def equal_score(cases, observations, split, *, version):
        result = _score(cases, observations, split, version=version)
        result["metrics"]["overall_score"] = 0.5
        return result

    result = await coach.run_iteration(*_cases(), replay_fn=replay,
                                       store_dir=tmp_path, score_fn=equal_score)
    assert result["accepted"] is False and result["selected_version"] == "v1"
    assert "did not improve" in result["reason"]
    assert calls.count("locked-1") == 2
    assert [message["split"] for message in result["metric_messages"]] == ["locked_test", "locked_test", "rejected"]
    assert coach.active_prompts(tmp_path)["prep"] == json.loads(
        (tmp_path / "versions" / "v1.json").read_text())["prompts"]["prep"]

    def regression_score(cases, observations, split, *, version):
        result = _score(cases, observations, split, version=version)
        if version == "v2":
            result["metrics"]["prediction_score"] = 0.4
        return result

    regression_store = tmp_path / "regression"
    regression = await coach.run_iteration(*_cases(), replay_fn=replay,
                                           store_dir=regression_store, score_fn=regression_score)
    assert regression["accepted"] is False
    assert "prediction_score regressed" in regression["reason"]


@pytest.mark.asyncio
async def test_coach_cannot_edit_arbitrary_path_or_remove_grounding(monkeypatch, tmp_path):
    async def replay(case, *, prompt_override):
        return {"improved": False}

    async def arbitrary(*args, **kwargs):
        return json.dumps({"target": "../../config.py", "append": "x", "reason": "bad"})

    monkeypatch.setattr(coach.llm, "chat", arbitrary)
    result = await coach.run_iteration(*_cases(), replay_fn=replay,
                                       store_dir=tmp_path, score_fn=_score)
    assert result["selected_version"] == "v1"
    assert not (tmp_path / "versions" / "v2.json").exists()


@pytest.mark.asyncio
async def test_prep_uses_current_evidence_for_three_short_predictions(monkeypatch):
    captured = {}

    async def model(model, messages, **kwargs):
        captured.update(json.loads(messages[-1]["content"]))
        return json.dumps({"predictions": ["JWT migration status", "Billing dependencies",
                                           "Notification owner", "fourth is dropped"]})

    monkeypatch.setattr(prep.llm, "chat", model)
    memory = SimpleNamespace(current_facts=lambda: [
        {"claim": "Auth currently uses JWT", "source": "auth/login.py:18"}])
    code_map = SimpleNamespace(files={"auth/login.py": {
        "path": "auth/login.py", "summary": "JWT login"}})
    session = SimpleNamespace(memory=memory, code_map=code_map)
    predictions = await prep.brief_provider(session, "Daily standup")
    assert predictions == ["JWT migration status", "Billing dependencies", "Notification owner"]
    assert captured["evidence"]["facts"][0]["source"] == "auth/login.py:18"
    assert captured["evidence"]["files"][0]["path"] == "auth/login.py"


@pytest.mark.asyncio
async def test_prep_runtime_uses_only_selected_prompt(monkeypatch, tmp_path):
    monkeypatch.setattr(coach, "DEFAULT_STORE", tmp_path)
    baseline = coach._read_or_snapshot_v1(tmp_path)
    improved = dict(baseline)
    improved["prep"] += "\nSelected Coach rule.\n"
    coach._write_json(tmp_path / "versions" / "v2.json", {"prompts": improved})
    coach._write_json(tmp_path / "active.json", {"version": "v2"})
    seen = []

    async def model(model, messages, **kwargs):
        seen.append(messages[0]["content"])
        return '{"predictions":[]}'

    monkeypatch.setattr(prep.llm, "chat", model)
    session = SimpleNamespace(memory=None, code_map=None)
    await prep.brief_provider(session, "Auth planning")
    assert "Selected Coach rule." in seen[-1]
    coach._write_json(tmp_path / "active.json", {"version": "v1"})
    await prep.brief_provider(session, "Auth planning")
    assert "Selected Coach rule." not in seen[-1]
