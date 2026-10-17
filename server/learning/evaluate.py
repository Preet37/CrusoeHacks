"""Deterministic meeting scores; the locked corpus is an explicit final gate.

Observations must come from replay.py or another recorded run. No model judges
or inferred results are used here. Missing evidence leaves a metric unscored.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import mean

import protocol

MEETINGS = Path(__file__).parent / "meetings"
LATEST_METRICS = Path(__file__).parent / "runs" / "latest_metrics.json"
SPLITS = {"train", "validation", "locked_test"}


def load_cases(split: str, *, include_locked: bool = False) -> list[dict]:
    if split not in SPLITS:
        raise ValueError(f"unknown split: {split}")
    if split == "locked_test" and not include_locked:
        raise PermissionError("locked_test requires an explicit final evaluation")
    cases = []
    for path in sorted(MEETINGS.glob("*.json")):
        case = json.loads(path.read_text())
        if case.get("split") == split:
            if not case.get("id") or not isinstance(case.get("lines"), list):
                raise ValueError(f"invalid meeting fixture: {path.name}")
            cases.append(case)
    return cases


def _paths(observed: dict) -> set[str]:
    paths = observed.get("file_paths")
    if paths is not None:
        return {str(path) for path in paths}
    return {str(draw.get("label")) for draw in observed.get("draws", [])
            if draw.get("op") == "add_box" and "/" in str(draw.get("label", ""))}


def _f1(expected: set[str], actual: set[str]) -> float:
    if not expected and not actual:
        return 1.0
    if not expected or not actual:
        return 0.0
    overlap = len(expected & actual)
    return 2 * overlap / (len(expected) + len(actual))


def _prediction_score(groups: list[list[str]], predictions: list[str]) -> float:
    """One-to-one keyword matches give an auditable precision/recall F1."""
    if not groups and not predictions:
        return 1.0
    if not groups or not predictions:
        return 0.0
    adjacency = [[index for index, prediction in enumerate(predictions)
                  if all(str(keyword).casefold() in prediction.casefold()
                         for keyword in group)] for group in groups]
    # Maximum bipartite matching, not greedy assignment: a broad "login"
    # group must not consume the only "login Redis" prediction needed by a
    # more specific group when another plain login prediction exists.
    owner: dict[int, int] = {}

    def assign(group: int, seen: set[int]) -> bool:
        for prediction in adjacency[group]:
            if prediction in seen:
                continue
            seen.add(prediction)
            if prediction not in owner or assign(owner[prediction], seen):
                owner[prediction] = group
                return True
        return False

    matched = sum(assign(group, set()) for group in range(len(groups)))
    return 2 * matched / (len(groups) + len(predictions))


def score_case(case: dict, observed: dict) -> dict:
    """Return only metrics whose ground truth and observations are available."""
    truth = case.get("ground_truth") or {}
    metrics: dict[str, float] = {}
    updates = observed.get("updates", [])
    urgent = [str(item.get("text") or "") for item in updates
              if isinstance(item, dict) and item.get("urgent") is True]
    if "impacted_files" in truth and ("file_paths" in observed or "draws" in observed):
        metrics["impact_accuracy"] = _f1(
            set(map(str, truth["impacted_files"])), _paths(observed))
    if "false_claim_sources" in truth and "updates" in observed:
        sources = list(map(str, truth["false_claim_sources"]))
        if sources:
            metrics["false_claim_recall"] = sum(
                any(source.casefold() in text.casefold() for text in urgent)
                for source in sources) / len(sources)
        else:
            metrics["false_alarm_rate"] = float(bool(urgent))
    if "tasks" in truth and "tasks" in observed:
        actual = observed.get("tasks", [])
        expected = truth["tasks"]
        matches = 0
        for task in expected:
            keywords = [str(word).casefold() for word in task.get("keywords", [])]
            owner = str(task.get("owner") or "").casefold()
            if any(all(word in str(item.get("title") or "").casefold()
                       for word in keywords)
                   and (not owner or owner == str(item.get("owner") or "").casefold())
                   for item in actual):
                matches += 1
        metrics["task_accuracy"] = matches / len(expected) if expected else float(not actual)
    if "visual_ops" in truth and "draws" in observed:
        expected = set(map(str, truth["visual_ops"]))
        actual = {str(draw.get("op")) for draw in observed.get("draws", [])}
        metrics["visual_accuracy"] = (len(expected & actual) / len(expected)
                                      if expected else float(not actual))
    if "topic_titles" in truth and "topics" in observed:
        actual = {str(topic.get("title") or "").casefold()
                  for topic in observed.get("topics", [])}
        expected = {str(title).casefold() for title in truth["topic_titles"]}
        metrics["topic_accuracy"] = (len(expected & actual) / len(expected)
                                     if expected else float(not actual))
    if "prediction_topics" in truth and "predictions" in observed:
        metrics["prediction_score"] = _prediction_score(
            truth["prediction_topics"], [str(text) for text in observed["predictions"]])
    elif "predictions" in truth and "predictions" in observed:
        expected = {str(text).casefold() for text in truth["predictions"]}
        actual = {str(text).casefold() for text in observed["predictions"]}
        metrics["prediction_score"] = _f1(expected, actual)
    latency = observed.get("latency_ms")
    if isinstance(latency, (int, float)) and not isinstance(latency, bool) and latency >= 0:
        metrics["latency_ms"] = float(latency)
    tokens = observed.get("tokens")
    if isinstance(tokens, (int, float)) and not isinstance(tokens, bool) and tokens >= 0:
        metrics["tokens"] = float(tokens)
    return {"id": case["id"], "metrics": metrics}


def score_run(cases: list[dict], observations: list[dict] | dict[str, dict],
              split: str, *, version: str = "v1") -> dict:
    if split not in SPLITS:
        raise ValueError(f"unknown split: {split}")
    if any(case.get("split") != split for case in cases):
        raise ValueError("case split mismatch")
    if isinstance(observations, list):
        observations = {item["id"]: item for item in observations}
    missing = [case["id"] for case in cases if case["id"] not in observations]
    if missing:
        raise ValueError(f"missing observations: {', '.join(missing)}")
    per_case = [score_case(case, observations[case["id"]]) for case in cases]
    names = {name for item in per_case for name in item["metrics"]}
    metrics = {name: mean(item["metrics"][name] for item in per_case
                          if name in item["metrics"]) for name in sorted(names)}
    # Only quality metrics enter the aggregate. Latency/tokens are reported as
    # measured cost, not silently treated as accuracy or invented as zero.
    quality = [metrics[name] for name in (
        "impact_accuracy", "false_claim_recall", "task_accuracy", "visual_accuracy",
        "topic_accuracy", "prediction_score") if name in metrics]
    if "false_alarm_rate" in metrics:
        quality.append(1 - metrics["false_alarm_rate"])
    if quality:
        metrics["overall_score"] = mean(quality)
    messages = [{"type": "metric", "version": version, "split": split,
                 "name": name, "value": float(value)}
                for name, value in sorted(metrics.items())]
    for message in messages:
        protocol.parse(message)
    return {"version": version, "split": split, "metrics": metrics,
            "cases": per_case, "metric_messages": messages}


def save_metric_payload(reports: list[dict], path: str | Path = LATEST_METRICS) -> list[dict]:
    """Write the plain protocol-message array consumed by Session/Scoreboard."""
    messages = [message for report in reports for message in report.get("metric_messages", [])]
    for message in messages:
        protocol.parse(message)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(messages, indent=2) + "\n")
    return messages


def main() -> None:
    parser = argparse.ArgumentParser(description="Score recorded v1/v2 meeting replays")
    parser.add_argument("--split", choices=sorted(SPLITS), required=True)
    parser.add_argument("--v1", type=Path, required=True, help="v1 observations JSON")
    parser.add_argument("--v2", type=Path, required=True, help="v2 observations JSON")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cases = load_cases(args.split, include_locked=args.split == "locked_test")
    if not cases:
        parser.error("no cases in selected split")
    reports = [score_run(cases, json.loads(path.read_text()), args.split, version=version)
               for version, path in (("v1", args.v1), ("v2", args.v2))]
    artifact = {"split": args.split, "versions": reports,
                "metric_messages": [message for report in reports
                                    for message in report["metric_messages"]]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(artifact, indent=2) + "\n")
    save_metric_payload(reports)
    print(f"Wrote measured {args.split} comparison to {args.output}")


if __name__ == "__main__":
    main()
