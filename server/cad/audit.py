# Adapted from LoominPhysicsSandbox (Preet37, pre-existing project) lib/geometryAudit.ts, ported to Python for Twin.
"""Deterministic geometry-quality gates, applied to part plans and compiled OpenSCAD.

Loomin's audit inspected generated React-Three-Fiber code. Twin never runs
model-written code, so the same rules are applied to what Twin does have:
  * smoothness_audit(plan)  — box dominance ("reads as LEGO"), no curved parts on a
    non-rectilinear topic, detail parts too small to see; returned as issues that are
    fed back into the plan repair retry, like Loomin's agent loop.
  * polish_openscad(src)    — the deterministic half: raise literal $fn below the
    documented minimum (Loomin raised three.js segment counts the same way).
"""
from __future__ import annotations

import re

from .plan import RECTILINEAR, GeometryPlan, expand_mirrors, plan_bounds

MIN_FN = 32
BOX_RATIO_LIMIT = 0.45
_RECTILINEAR = RECTILINEAR


def smoothness_audit(plan: GeometryPlan, topic: str) -> list[str]:
    issues: list[str] = []
    parts = expand_mirrors(plan.parts)
    boxes = sum(1 for p in parts if p.primitive == "box")
    curved = len(parts) - boxes
    rectilinear = bool(_RECTILINEAR.search(topic or ""))
    if not rectilinear and len(parts) >= 6:
        ratio = boxes / len(parts)
        if ratio > BOX_RATIO_LIMIT:
            issues.append(
                f"SMOOTHNESS: {boxes} of {len(parts)} parts are boxes ({round(ratio * 100)}%) — this reads "
                "as a LEGO model. Use cylinders, cones, spheres or tori for the parts that are round, "
                "turned, or tapered in the real object.")
        if curved == 0:
            issues.append("SMOOTHNESS: no curved parts at all. Real objects have round and tapered parts; "
                          "model those with cylinders, cones or spheres.")
    lo, hi = plan_bounds(plan)
    largest = max(hi[i] - lo[i] for i in range(3))
    tiny = sorted({p.name for p in parts if max(p.size) < largest * 0.01})
    if tiny:
        issues.append(f"DETAIL: {', '.join(tiny[:6])} are under 1% of the object's size and will be invisible; "
                      "enlarge them or drop them.")
    return issues


def polish_openscad(src: str) -> tuple[str, list[str]]:
    """Raise literal $fn values below MIN_FN. Returns (source, fixes)."""
    fixes: list[str] = []

    def bump(m: re.Match) -> str:
        n = int(m.group(1))
        if 2 < n < MIN_FN:
            fixes.append(f"$fn {n} → {MIN_FN}")
            return f"$fn={MIN_FN}"
        return m.group(0)

    out = re.sub(r"\$fn\s*=\s*(\d+)", bump, src)
    return out, fixes
