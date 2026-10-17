"""Benchmark scoring must use explicit labels and leave failures unscored."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench"))
import run_bench as bench


def test_extraction_exact_f1_and_penalty_for_invented_edge():
    case = {"id": "04_web_auth_database"}
    clean = bench._score("extraction", {"entities": ["web app", "auth", "postgres"],
                                         "edges": [["web app", "auth"], ["auth", "postgres"]]}, case)
    extra = bench._score("extraction", {"entities": ["web app", "auth", "postgres"],
                                         "edges": [["web app", "auth"], ["auth", "postgres"],
                                                   ["web app", "postgres"]]}, case)
    assert clean["combined_f1"] == 1
    assert extra["edge_f1"] < clean["edge_f1"]
    narrated = bench._score("extraction", {"entities": ["web app", "auth service", "postgres"],
                                            "edges": [["web app", "talks to", "auth service"],
                                                      ["auth service", "stores in", "postgres"]]}, case)
    assert narrated["combined_f1"] == 1


def test_3d_invalid_scene_counts_as_failure_not_missing_observation():
    scored = bench._score("3d_validity", {"scene": {"objects": [
        {"id": "ball", "shape": "sphere", "pos": [0, 0, 0],
         "scale": [1, 1, 1], "color": "#4488cc"}]}}, None)
    assert scored["valid"] == 0
    assert "validation_issue" in scored


def test_summary_does_not_make_up_missing_measurements():
    rows = [{"model": "Nano", "task": "3d_validity", "error": "TimeoutError",
             "latency_ms": 25000, "case_id": "3d_validity"}]
    row = next(item for item in bench.summarize(rows)
               if item["model"] == "Nano" and item["task"] == "3d_validity")
    assert row["N_attempted"] == 1
    assert row["N_scored"] == 0
    assert row["p50_ms"] is None
    assert row["mean_tokens"] is None


def test_percentile_small_n_is_interpolated_not_claimed_as_distribution():
    assert bench._percentile([], .95) is None
    assert bench._percentile([100], .95) == 100
    assert bench._percentile([100, 200], .95) == 195
