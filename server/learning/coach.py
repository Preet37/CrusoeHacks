"""One bounded, evidence-based Coach iteration with a held-out acceptance gate."""

from __future__ import annotations

import inspect
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Callable

import config
import llm


ROOT = Path(__file__).resolve().parents[1]
PROMPT_SOURCES = {
    name: ROOT / "agents" / "prompts" / f"{name}.md"
    for name in ("listener", "conductor", "diagrammer")
}
PROMPT_SOURCES["prep"] = Path(__file__).parent / "playbooks" / "prep.md"
COACH_PROMPT = Path(__file__).parent / "playbooks" / "coach.md"
DEFAULT_STORE = Path(__file__).parent / "runs" / "coach"
HIGHER_IS_BETTER = ("impact_accuracy", "false_claim_recall", "task_accuracy",
                    "visual_accuracy", "topic_accuracy", "prediction_score")
LOWER_IS_BETTER = ("false_alarm_rate",)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _read_or_snapshot_v1(store: Path) -> dict[str, str]:
    path = store / "versions" / "v1.json"
    if path.exists():
        saved = json.loads(path.read_text(encoding="utf-8"))
        prompts = saved.get("prompts")
        if not isinstance(prompts, dict) or set(prompts) != set(PROMPT_SOURCES):
            raise ValueError("invalid v1 prompt snapshot")
        return {name: str(prompts[name]) for name in PROMPT_SOURCES}
    prompts = {name: path.read_text(encoding="utf-8") for name, path in PROMPT_SOURCES.items()}
    _write_json(path, {"version": "v1", "prompts": prompts})
    return prompts


def active_prompts(store_dir: str | Path = DEFAULT_STORE) -> dict[str, str]:
    """Load the selected prompt text for an offline replay or runtime hook."""
    store = Path(store_dir)
    active_path = store / "active.json"
    version = json.loads(active_path.read_text())["version"] if active_path.exists() else "v1"
    path = store / "versions" / f"{version}.json"
    if not path.exists() and version == "v1":
        return _read_or_snapshot_v1(store)
    return json.loads(path.read_text(encoding="utf-8"))["prompts"]


def _candidate(prompts: dict[str, str], proposal: Any, allowed_targets: tuple[str, ...]) -> tuple[dict[str, str], dict[str, str]]:
    if not isinstance(proposal, dict) or set(proposal) != {"target", "append", "reason"}:
        raise ValueError("Coach must propose exactly target, append, reason")
    target, append, reason = (proposal[key] for key in ("target", "append", "reason"))
    if target not in allowed_targets or target not in PROMPT_SOURCES or not all(
        isinstance(value, str) for value in (append, reason)
    ):
        raise ValueError("Coach edit has invalid target or field types")
    if not 1 <= len(append.strip()) <= 300 or not 1 <= len(reason.strip()) <= 300:
        raise ValueError("Coach edit is empty or exceeds its size limit")
    if "\n" in append or append.casefold() in prompts[target].casefold():
        raise ValueError("Coach edit must be one new rule")
    revised = dict(prompts)
    revised[target] = prompts[target].rstrip() + "\n" + append.strip() + "\n"
    return revised, {"target": target, "append": append.strip(), "reason": reason.strip()}


async def _replay_all(cases: list[dict], replay_fn: Callable, prompts: dict[str, str]) -> list[dict]:
    observations = []
    for case in cases:
        result = replay_fn(case, prompt_override=prompts)
        observations.append(await result if inspect.isawaitable(result) else result)
    return observations


def _quality_gate(baseline: dict[str, Any], candidate: dict[str, Any]) -> tuple[bool, str]:
    before, after = baseline.get("metrics", {}), candidate.get("metrics", {})
    if not isinstance(before, dict) or not isinstance(after, dict):
        return False, "validation metrics missing"
    quality = set(HIGHER_IS_BETTER) | set(LOWER_IS_BETTER)
    available = quality & before.keys()
    if "overall_score" not in before or "overall_score" not in after or not available:
        return False, "validation quality metrics incomplete"
    if available != quality & after.keys():
        return False, "validation quality metrics differ"
    try:
        if not all(math.isfinite(float(value)) for name in available | {"overall_score"}
                   for value in (before[name], after[name])):
            return False, "validation quality metric is not finite"
        if float(after["overall_score"]) <= float(before["overall_score"]) + 1e-9:
            return False, "validation overall score did not improve"
        for name in set(HIGHER_IS_BETTER) & available:
            if float(after[name]) < float(before[name]) - 1e-9:
                return False, f"validation {name} regressed"
        for name in set(LOWER_IS_BETTER) & available:
            if float(after[name]) > float(before[name]) + 1e-9:
                return False, f"validation {name} regressed"
    except (TypeError, ValueError):
        return False, "validation quality metric is not numeric"
    return True, "validation improved without quality regression"


def _coach_evidence(cases: list[dict], observations: list[dict], score: dict) -> dict:
    # The Coach sees train examples and their scored failures, never validation
    # or locked material. Keep its input bounded even for larger future sets.
    examples = []
    for case, observed, case_score in zip(cases[:8], observations[:8], score.get("cases", [])[:8]):
        examples.append({
            "id": str(case.get("id", ""))[:80],
            "lines": case.get("lines", [])[:12],
            "ground_truth": case.get("ground_truth", {}),
            "observed": {key: value for key, value in observed.items()
                         if key in {"draws", "tasks", "updates", "topics", "predictions"}},
            "score": case_score,
        })
    return {"metrics": score.get("metrics", {}), "examples": examples}


async def run_iteration(
    train_cases: list[dict], validation_cases: list[dict], locked_cases: list[dict],
    replay_fn: Callable | None = None, store_dir: str | Path = DEFAULT_STORE,
    *, score_fn: Callable | None = None, allowed_targets: tuple[str, ...] = ("prep",),
    metrics_path: str | Path | None = None,
) -> dict[str, Any]:
    """Propose one edit, gate on validation, then score locked once per version.

    ``replay_fn(case, prompt_override=prompts)`` may be async or sync. Locked
    cases are not replayed until the candidate has been accepted or rejected.
    """
    if replay_fn is None:
        from learning.replay import replay_case
        replay_fn = replay_case
    if score_fn is None:
        from learning.evaluate import score_run
        score_fn = score_run
    store = Path(store_dir)
    store.mkdir(parents=True, exist_ok=True)
    baseline_prompts = _read_or_snapshot_v1(store)
    train_observations = await _replay_all(train_cases, replay_fn, baseline_prompts)
    train_score = score_fn(train_cases, train_observations, "train", version="v1")
    baseline_val_obs = await _replay_all(validation_cases, replay_fn, baseline_prompts)
    baseline_val = score_fn(validation_cases, baseline_val_obs, "validation", version="v1")

    proposal: dict[str, str] | None = None
    candidate_prompts: dict[str, str] | None = None
    candidate_val: dict[str, Any] | None = None
    try:
        raw = await llm.chat(config.MODEL_COACH, [
            {"role": "system", "content": COACH_PROMPT.read_text(encoding="utf-8")},
            {"role": "user", "content": json.dumps({"train": _coach_evidence(
                train_cases, train_observations, train_score), "prompts": baseline_prompts},
                ensure_ascii=False, default=str)},
        ], max_tokens=700, response_format={"type": "json_object"},
            extra_body={"chat_template_kwargs": {"enable_thinking": False}})
        candidate_prompts, proposal = _candidate(baseline_prompts, json.loads(raw), allowed_targets)
        _write_json(store / "versions" / "v2.json", {
            "version": "v2", "status": "candidate", "proposal": proposal,
            "prompts": candidate_prompts,
        })
        candidate_obs = await _replay_all(validation_cases, replay_fn, candidate_prompts)
        candidate_val = score_fn(validation_cases, candidate_obs, "validation", version="v2")
        accepted, reason = _quality_gate(baseline_val, candidate_val)
    except Exception as exc:
        accepted, reason = False, f"proposal failed: {type(exc).__name__}: {exc}"[:300]

    selected_version = "v2" if accepted else "v1"
    _write_json(store / "active.json", {"version": selected_version, "reason": reason})
    if candidate_prompts is not None:
        _write_json(store / "versions" / "v2.json", {
            "version": "v2", "status": "accepted" if accepted else "rejected",
            "proposal": proposal, "prompts": candidate_prompts,
        })

    # Hold-out data enters only here, after active.json records the decision.
    locked_baseline_obs = await _replay_all(locked_cases, replay_fn, baseline_prompts)
    locked_baseline = score_fn(locked_cases, locked_baseline_obs, "locked_test", version="v1")
    locked_candidate = None
    if candidate_prompts is not None:
        locked_candidate_obs = await _replay_all(locked_cases, replay_fn, candidate_prompts)
        locked_candidate = score_fn(locked_cases, locked_candidate_obs,
                                    "locked_test", version="v2")
    locked_selected = locked_candidate if accepted else locked_baseline
    messages = list(locked_baseline.get("metric_messages", []))
    if locked_candidate is not None:
        if accepted:
            messages.extend(locked_candidate.get("metric_messages", []))
        else:
            # Display the measured candidate beside v1 without presenting it
            # as the selected runtime version, and retain a rejection card.
            messages.extend({**message, "version": "v2 candidate (rejected)"}
                            for message in locked_candidate.get("metric_messages", []))
            messages.append({"type": "metric", "version": "v2", "split": "rejected",
                             "name": f"rejected: {reason}; kept v1", "value": 0,
                             "status": "rejected", "reason": reason,
                             "selected_version": "v1"})
    if metrics_path is not None:
        _write_json(Path(metrics_path), messages)
    result = {
        "selected_version": selected_version, "accepted": accepted, "reason": reason,
        "proposal": proposal, "train": train_score,
        "validation": {"v1": baseline_val, "v2": candidate_val},
        "locked": {"v1": locked_baseline, "candidate": locked_candidate,
                   "selected": locked_selected},
        "metric_messages": messages,
    }
    log_path = store / "coach_log.jsonl"
    with log_path.open("a", encoding="utf-8") as log:
        log.write(json.dumps({"selected_version": selected_version, "accepted": accepted,
                              "reason": reason, "proposal": proposal,
                              "validation_before": baseline_val.get("metrics"),
                              "validation_candidate": candidate_val.get("metrics") if candidate_val else None,
                              "locked_before": locked_baseline.get("metrics"),
                              "locked_selected": locked_selected.get("metrics")},
                             ensure_ascii=False, allow_nan=False) + "\n")
    _write_json(store / "last_result.json", result)
    return result


async def main() -> None:
    from learning.evaluate import LATEST_METRICS, load_cases
    from learning.replay import replay_prep_case

    result = await run_iteration(
        load_cases("train"), load_cases("validation"),
        load_cases("locked_test", include_locked=True), replay_fn=replay_prep_case,
        metrics_path=LATEST_METRICS,
    )
    print(json.dumps({"selected_version": result["selected_version"],
                      "accepted": result["accepted"], "reason": result["reason"],
                      "validation": {version: (report or {}).get("metrics") for version, report
                                     in result["validation"].items()},
                      "locked": {version: (report or {}).get("metrics") for version, report
                                 in result["locked"].items()},
                      "metrics_path": str(LATEST_METRICS)}, indent=2))


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
