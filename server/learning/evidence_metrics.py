"""Read measured Scoreboard messages from the committed Coach evidence.

This is a fresh-clone fallback for the ignored ``runs/latest_metrics.json``.
The evidence already contains the exact wire rows from the real run; never
recompute or infer a result from summary scores here.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import protocol


EVIDENCE_FILE = Path(__file__).parent / "results" / "coach_prep_iteration_2026-10-17.json"


def load_committed_metrics(path: str | Path = EVIDENCE_FILE) -> list[dict]:
    """Return the saved protocol metric rows, or [] for missing/invalid evidence."""
    try:
        evidence = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(evidence, dict) or evidence.get("kind") != "prep_only_coach_iteration":
        return []
    rows = evidence.get("metric_messages")
    if not isinstance(rows, list) or not rows:
        return []
    for row in rows:
        if not isinstance(row, dict) or row.get("type") != "metric":
            return []
        if any(not isinstance(row.get(name), str) or not row[name]
               for name in ("version", "split", "name")):
            return []
        value = row.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return []
        try:
            if not math.isfinite(value):
                return []
            if protocol.parse(row).type != "metric":
                return []
        except (OverflowError, ValueError):
            return []
    return rows
