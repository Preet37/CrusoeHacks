# Adapted from LoominPhysicsSandbox (Preet37, pre-existing project) lib/geometryPlan.ts, ported to Python for Twin.
"""Geometry plans: an LLM structural part plan, validated deterministically, compiled to OpenSCAD.

The LLM is asked for a STRUCTURAL breakdown (named parts, primitive shapes,
millimetre sizes and positions) which is validated by pure deterministic rules
before a single polygon is generated. A plan that is really a blob (too few
parts, one primitive type, disconnected pieces) fails validation so the caller
can retry with the complaints, or fail loudly instead of shipping a ball.

The model only ever produces this JSON. OpenSCAD source is generated here from
numbers and sanitised names; no model-written code is ever executed.

Twin changes vs Loomin (marked "Twin:" inline):
  * axis convention spelled out in the prompt (+x nose/front, y left-right, z up);
  * part bounding boxes respect rotation (Loomin ignored rotation), so a cylinder
    rotated to lie along x is checked/normalised along x;
  * mirrored copies are emitted with OpenSCAD mirror(), so rotations mirror too;
  * comment text from the model is stripped of newlines/control characters;
  * optional "axis" (+x/-x/+y/-y/+z/-z) for cylinder/cone/torus: "size" is then the
    part's WORLD extent and the compiler orients it. Models get world-space boxes
    right far more often than Euler rotations; without "axis" Loomin's semantics hold.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field, replace
from typing import Any

PRIMITIVES = ("box", "cylinder", "cone", "sphere", "torus")
MIN_PARTS = 4
MAX_ASPECT = 60
MAX_PARTS = 60          # Twin: cap so one plan cannot make OpenSCAD run forever
_AXIS = {"x": 0, "y": 1, "z": 2}

Vec3 = tuple[float, float, float]


@dataclass
class PlanPart:
    name: str
    primitive: str
    size: Vec3                       # full extents in mm; cylinder/cone/torus: x,y diameters
    position: Vec3                   # centre, mm, relative to object origin
    rotation: Vec3 = (0.0, 0.0, 0.0)  # Euler degrees (OpenSCAD rotate order: x, then y, then z)
    mirror: str | None = None         # duplicate mirrored across this axis
    axis: str | None = None           # Twin: "+x".."-z" for cylinder/cone/torus; size is then world extent


@dataclass
class GeometryPlan:
    topic: str
    summary: str
    reference_product: str
    parts: list[PlanPart]
    source: str = ""


@dataclass
class PlanValidation:
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)


# ── Coercion ─────────────────────────────────────────────────────────────────

def _num(v: Any, fallback: float) -> float:
    if isinstance(v, bool):
        return fallback
    if isinstance(v, str):
        try:
            v = float(v)
        except ValueError:
            return fallback
    if isinstance(v, (int, float)) and math.isfinite(v):
        return float(v)
    return fallback


def _triple(v: Any, fallback: Vec3) -> Vec3:
    if not isinstance(v, (list, tuple)) or len(v) < 3:
        return fallback
    return (_num(v[0], fallback[0]), _num(v[1], fallback[1]), _num(v[2], fallback[2]))


def _coerce_primitive(v: Any) -> str:
    s = str(v or "").lower().strip()
    if s in PRIMITIVES:
        return s
    # Common synonyms the models reach for.
    if re.search(r"capsule|pill|rod|tube|pipe|shaft", s):
        return "cylinder"
    if re.search(r"disc|disk|wheel|ring", s):
        return "cylinder"
    if re.search(r"cone|nose|taper|funnel", s):
        return "cone"
    if re.search(r"ball|dome|bubble", s):
        return "sphere"
    if re.search(r"torus|donut|doughnut|loop", s):
        return "torus"
    return "box"


_AXES = ("+x", "-x", "+y", "-y", "+z", "-z")


def _coerce_axis(v: Any) -> str | None:
    s = str(v or "").lower().strip().replace(" ", "")
    if s in ("x", "y", "z"):
        s = "+" + s
    return s if s in _AXES else None


def _coerce_mirror(v: Any) -> str | None:
    s = str(v or "").lower().strip()
    return s if s in _AXIS else None


def coerce_geometry_plan(raw: Any, topic: str, source: str = "") -> GeometryPlan | None:
    """Turn arbitrary LLM JSON into a GeometryPlan. Pure: no network, no raise."""
    if not isinstance(raw, dict):
        return None
    raw_parts = raw.get("parts") if isinstance(raw.get("parts"), list) else []
    parts: list[PlanPart] = []
    for rp in raw_parts[:MAX_PARTS]:
        if not isinstance(rp, dict):
            continue
        size = _triple(_first(rp, "size", "dimensions", "scale"), (0.0, 0.0, 0.0))
        if any(not (s > 0) for s in size):
            continue
        parts.append(PlanPart(
            name=str(_first(rp, "name", "id") or f"part_{len(parts) + 1}")[:60],
            primitive=_coerce_primitive(_first(rp, "primitive", "type", "shape")),
            size=size,
            position=_triple(_first(rp, "position", "location", "offset"), (0.0, 0.0, 0.0)),
            rotation=_triple(_first(rp, "rotation", "rotate"), (0.0, 0.0, 0.0)),
            mirror=_coerce_mirror(_first(rp, "mirror", "mirrorAxis")),
            axis=_coerce_axis(_first(rp, "axis", "direction")),
        ))
    parts = _drop_mirror_duplicates(parts)
    if not parts:
        return None
    return GeometryPlan(
        topic=topic,
        summary=str(_first(raw, "summary", "description") or "")[:400],
        reference_product=str(_first(raw, "referenceProduct", "reference") or topic)[:120],
        parts=parts,
        source=source,
    )


def _drop_mirror_duplicates(parts: list[PlanPart]) -> list[PlanPart]:
    """Twin: models often set "mirror" AND list the twin part ("left_wing" + "right_wing").
    Drop an explicit part that coincides with another part's mirrored copy."""
    drop: set[int] = set()
    for i, p in enumerate(parts):
        if not p.mirror or i in drop:
            continue
        twin = _mirrored(p)
        if all(abs(c) < 1e-6 for c in (p.position[_AXIS[p.mirror]],)):
            continue  # mirrored onto itself
        tol = 0.05 * max(max(p.size), 1e-6)
        for j, q in enumerate(parts):
            if j != i and j not in drop and q.primitive == p.primitive and \
                    all(abs(a - b) <= tol for a, b in zip(q.position, twin.position)) and \
                    all(abs(a - b) <= tol for a, b in zip(q.size, p.size)):
                drop.add(j)
    out = []
    for i, p in enumerate(parts):
        if i in drop:
            continue
        if p.mirror and abs(p.position[_AXIS[p.mirror]]) < 1e-6:
            p = replace(p, mirror=None)  # a copy on top of itself adds nothing
        out.append(p)
    return out


def _first(d: dict, *keys: str) -> Any:
    for k in keys:
        if d.get(k) is not None:
            return d[k]
    return None


# ── Geometry helpers ─────────────────────────────────────────────────────────

def _rot_matrix(rot: Vec3) -> list[list[float]]:
    """OpenSCAD rotate([a,b,c]) == Rz(c) * Ry(b) * Rx(a)."""
    a, b, c = (math.radians(r) for r in rot)
    ca, sa, cb, sb, cc, sc = math.cos(a), math.sin(a), math.cos(b), math.sin(b), math.cos(c), math.sin(c)
    rx = [[1, 0, 0], [0, ca, -sa], [0, sa, ca]]
    ry = [[cb, 0, sb], [0, 1, 0], [-sb, 0, cb]]
    rz = [[cc, -sc, 0], [sc, cc, 0], [0, 0, 1]]
    return _matmul(rz, _matmul(ry, rx))


def _matmul(m: list[list[float]], n: list[list[float]]) -> list[list[float]]:
    return [[sum(m[i][k] * n[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def part_aabb(part: PlanPart) -> tuple[list[float], list[float]]:
    """Axis-aligned envelope of the part. Twin: respects rotation (Loomin ignored it)."""
    sx, sy, sz = part.size
    hx, hy, hz = sx / 2, sy / 2, sz / 2
    if any(part.rotation):
        m = _rot_matrix(part.rotation)
        # Half-extent of a rotated box along each world axis.
        hx, hy, hz = (abs(m[i][0]) * sx / 2 + abs(m[i][1]) * sy / 2 + abs(m[i][2]) * sz / 2 for i in range(3))
    px, py, pz = part.position
    return [px - hx, py - hy, pz - hz], [px + hx, py + hy, pz + hz]


def _overlaps_or_touches(a, b, slack: float) -> bool:
    for i in range(3):
        if a[0][i] - slack > b[1][i]:
            return False
        if b[0][i] - slack > a[1][i]:
            return False
    return True


def _mirrored(part: PlanPart) -> PlanPart:
    axis = _AXIS[part.mirror]  # type: ignore[index]
    pos = list(part.position)
    pos[axis] = -pos[axis]
    # Mirroring across an axis negates the rotation about the other two axes.
    rot = [r if i == axis else -r for i, r in enumerate(part.rotation)]
    direction = part.axis
    if direction and direction[1] == part.mirror:
        direction = ("-" if direction[0] == "+" else "+") + direction[1]
    return replace(part, position=tuple(pos), rotation=tuple(rot), mirror=None, axis=direction)


def expand_mirrors(parts: list[PlanPart]) -> list[PlanPart]:
    """A mirrored part is a second real solid: bounds, scaling and connectivity all see it."""
    out: list[PlanPart] = []
    for part in parts:
        out.append(part)
        if part.mirror:
            out.append(_mirrored(part))
    return out


def plan_bounds(plan: GeometryPlan) -> tuple[list[float], list[float]]:
    lo = [math.inf] * 3
    hi = [-math.inf] * 3
    for part in expand_mirrors(plan.parts):
        a, b = part_aabb(part)
        for i in range(3):
            lo[i] = min(lo[i], a[i])
            hi[i] = max(hi[i], b[i])
    return lo, hi


def _part_volume(part: PlanPart) -> float:
    x, y, z = part.size
    p = part.primitive
    if p == "sphere":
        return (math.pi / 6) * x * y * z
    if p == "cylinder":
        return (math.pi / 4) * x * y * z
    if p == "cone":
        return (math.pi / 12) * x * y * z
    if p == "torus":
        return (math.pi * math.pi / 4) * x * y * z * 0.25
    return x * y * z


# ── Validation ───────────────────────────────────────────────────────────────

def validate_geometry_plan(plan: GeometryPlan | None) -> PlanValidation:
    """Deterministic structural checks (part count, shape variety, connectivity), not realism."""
    if plan is None or not plan.parts:
        return PlanValidation(False, ["No geometry plan was produced"], [], [{"label": "plan exists", "pass": False}])

    errors: list[str] = []
    warnings: list[str] = []
    checks: list[dict[str, Any]] = []

    def record(label: str, ok: bool, message: str) -> None:
        checks.append({"label": label, "pass": ok})
        if not ok:
            errors.append(message)

    parts = plan.parts
    record(f"has at least {MIN_PARTS} parts", len(parts) >= MIN_PARTS,
           f"Plan has only {len(parts)} part(s); a recognisable object needs at least {MIN_PARTS}")

    kinds = {p.primitive for p in parts}
    record("uses more than one primitive type", len(kinds) >= 2,
           f'Every part is a {next(iter(kinds))} — that is a blob, not a model of "{plan.topic}"')

    named = sum(1 for p in parts if not re.fullmatch(r"part_\d+", p.name))
    record("parts are named", named >= math.ceil(len(parts) / 2),
           "Most parts are unnamed, so the plan is not a real structural breakdown")

    finite = all(all(math.isfinite(s) and s > 0 for s in p.size) and all(math.isfinite(c) for c in p.position)
                 for p in parts)
    record("all sizes and positions are finite", finite, "Plan contains non-finite sizes or positions")

    lo, hi = plan_bounds(plan)
    extents = [hi[i] - lo[i] for i in range(3)]
    largest = max(extents)
    smallest = max(min(extents), 1e-6)
    record("overall proportions are sane", largest / smallest <= MAX_ASPECT,
           f"Overall shape is degenerate (aspect ratio {largest / smallest:.0f}:1)")

    # Connectivity: a part floating far from everything else means scattered debris.
    slack = largest * 0.06
    boxes = [part_aabb(p) for p in expand_mirrors(parts)]
    names = [p.name for p in expand_mirrors(parts)]
    orphan_names = [names[i] for i, box in enumerate(boxes)
                    if not any(i != j and _overlaps_or_touches(box, other, slack) for j, other in enumerate(boxes))]
    record("parts form a connected object", not orphan_names,
           f"{len(orphan_names)} part(s) float disconnected from the rest of the object"
           + (f" ({', '.join(sorted(set(orphan_names))[:6])})" if orphan_names else ""))

    volumes = [_part_volume(p) for p in parts]
    total = sum(volumes)
    dominant = max(volumes) / total if total > 0 else 1
    if dominant > 0.92 and len(parts) < 8:
        warnings.append(f"One part is {dominant * 100:.0f}% of the volume — detail parts may be too small to see")
    if not plan.summary:
        warnings.append("Plan has no summary describing the object")

    return PlanValidation(not errors, errors, warnings, checks)


# ── Plan → OpenSCAD ──────────────────────────────────────────────────────────

def _fmt(n: float) -> str:
    out = f"{round(n, 4):.4f}".rstrip("0").rstrip(".")
    return "0" if out in ("-0", "") else out


def _comment(text: str) -> str:
    """Twin: model text only ever lands inside a // comment, on one line."""
    return re.sub(r"[\x00-\x1f\x7f]+", " ", str(text))[:200]


# Twin: rotate a +z-pointing primitive onto each axis, and which world extent feeds
# the primitive's local (x, y, z) size afterwards.
_ORIENT = {
    "+x": ("[0, 90, 0]", (2, 1, 0)), "-x": ("[0, -90, 0]", (2, 1, 0)),
    "+y": ("[-90, 0, 0]", (0, 2, 1)), "-y": ("[90, 0, 0]", (0, 2, 1)),
    "+z": ("", (0, 1, 2)), "-z": ("[180, 0, 0]", (0, 1, 2)),
}


def _oriented(part: PlanPart) -> tuple[str, Vec3]:
    """(orientation rotate() vector or "", local size) for the primitive body."""
    if part.axis and part.primitive in ("cylinder", "cone", "torus"):
        rot, idx = _ORIENT[part.axis]
        return rot, tuple(part.size[i] for i in idx)  # type: ignore[return-value]
    return "", part.size


def _part_body(part: PlanPart, scale: float, fn: int, size: Vec3 | None = None) -> str:
    sx, sy, sz = (s * scale for s in (size or part.size))
    p = part.primitive
    if p == "box":
        return f"cube([{_fmt(sx)}, {_fmt(sy)}, {_fmt(sz)}], center=true);"
    if p == "cylinder":
        return (f"scale([1, {_fmt(sy / sx or 1)}, 1]) "
                f"cylinder(h={_fmt(sz)}, r={_fmt(sx / 2)}, center=true, $fn={fn});")
    if p == "cone":
        return (f"scale([1, {_fmt(sy / sx or 1)}, 1]) "
                f"cylinder(h={_fmt(sz)}, r1={_fmt(sx / 2)}, r2={_fmt(sx / 16)}, center=true, $fn={fn});")
    if p == "sphere":
        return (f"scale([1, {_fmt(sy / sx or 1)}, {_fmt(sz / sx or 1)}]) "
                f"sphere(r={_fmt(sx / 2)}, $fn={max(16, fn * 3 // 4)});")
    if p == "torus":
        return (f"rotate_extrude($fn={fn}) translate([{_fmt(max(sx / 2 - sz / 2, sz / 2))}, 0, 0]) "
                f"circle(r={_fmt(sz / 2)}, $fn=24);")
    raise ValueError(f"unknown primitive {p!r}")


def _emit_part(part: PlanPart, scale: float, fn: int) -> str:
    px, py, pz = (c * scale for c in part.position)
    rx, ry, rz = part.rotation
    orient, local = _oriented(part)
    body = _part_body(part, scale, fn, local)
    if orient:
        body = f"rotate({orient}) {body}"
    rotated = f"rotate([{_fmt(rx)}, {_fmt(ry)}, {_fmt(rz)}]) {body}" if (rx or ry or rz) else body
    return f"translate([{_fmt(px)}, {_fmt(py)}, {_fmt(pz)}]) {rotated}"


def plan_to_openscad(plan: GeometryPlan, target_units: float = 4, fn: int = 48) -> str:
    """Compile a validated plan to OpenSCAD, normalised so the longest axis is `target_units`."""
    lo, hi = plan_bounds(plan)
    largest = max(max(hi[i] - lo[i] for i in range(3)), 1e-6)
    scale = target_units / largest
    lines = [
        f"$fn = {fn};",
        f"// {_comment(plan.reference_product)} — researched structural plan ({len(plan.parts)} parts)",
    ]
    if plan.summary:
        lines.append(f"// {_comment(plan.summary)}")
    for part in plan.parts:
        lines.append(f"// {_comment(part.name)}")
        emitted = _emit_part(part, scale, fn)
        lines.append(emitted)
        if part.mirror:
            vec = [0, 0, 0]
            vec[_AXIS[part.mirror]] = 1
            lines.append(f"mirror([{vec[0]}, {vec[1]}, {vec[2]}]) {emitted}")
    return "\n".join(lines) + "\n"


# ── Prompt ───────────────────────────────────────────────────────────────────

def build_plan_prompt(topic: str, feedback: str | None = None) -> str:
    feedback_block = (f"\nYOUR PREVIOUS ATTEMPT WAS REJECTED. Fix these problems:\n{feedback}\n"
                      if feedback else "")
    return f"""You are a mechanical designer. Break the real object "{topic}" down into the
primitive solids a CAD model would be assembled from.
{feedback_block}
Return ONLY JSON in exactly this shape:
{{
  "referenceProduct": "the specific real product you are modelling",
  "summary": "one sentence describing its shape",
  "parts": [
    {{
      "name": "fuselage",
      "primitive": "box|cylinder|cone|sphere|torus",
      "size": [x, y, z],
      "position": [x, y, z],
      "axis": "+x",
      "rotation": [0, 0, 0],
      "mirror": "y"
    }}
  ]
}}

RULES:
1. All sizes and positions in MILLIMETRES, using the object's real-world size.
2. "size" is the full extent on each world axis (see rule 9 for cylinders,
   cones and tori).
3. "position" is the CENTRE of the part, relative to the object's own origin.
4. Use "mirror" for parts that exist as a symmetric pair (wheels, wings, legs,
   handles) and describe only the positive-axis one.
5. At least 6 parts. Name every part after the real component it represents.
6. Parts must touch or overlap so the result is ONE connected object.
7. Do NOT output a generic blob. If "{topic}" is a specific product, model that
   product's actual silhouette and proportions.
8. AXES: +x is the FRONT (nose / forward direction), y is left-right, +z is UP.
   The object rests on or above z = 0.
9. "size" is the part's extent along the WORLD x, y and z axes as placed.
   For cylinder, cone and torus also give "axis": the world direction the
   part runs along ("+x", "-x", "+y", "-y", "+z", "-z"). A cone's tip points
   toward its axis direction. Example: a fuselage 7000 long and 1200 thick
   running front-to-back is a cylinder with size [7000, 1200, 1200] and axis
   "+x"; a wheel on a left-right axle is a cylinder with size [600, 200, 600]
   and axis "+y". Do NOT use "rotation" to orient parts: leave it [0, 0, 0]
   except for small tilts (dihedral, sweep).
10. Thin flat parts (wings, fins, blades, plates) are boxes with one small
   dimension; give them real thickness (at least 1% of the object's length).
11. Make the features that identify the object separate parts: blades, spokes,
   legs, fins, handles are individual thin parts, not a solid disc or block.
   Keep proportions realistic relative to the main body. Round off the ends
   of long bodies with a cone or sphere (nose, tail) instead of a flat cut.
12. Before answering, check every part's box [position - size/2, position + size/2]
   overlaps the box of the part it attaches to (wing root inside the fuselage,
   fin base on the fuselage, propeller against the nose).

EXAMPLE of the conventions (a different object, a 4000 mm car, abbreviated):
{{"referenceProduct": "compact hatchback", "summary": "boxy car on four wheels",
  "parts": [
   {{"name": "body", "primitive": "box", "size": [4000, 1700, 700], "position": [0, 0, 650], "rotation": [0, 0, 0]}},
   {{"name": "cabin", "primitive": "box", "size": [2200, 1500, 600], "position": [-200, 0, 1250], "rotation": [0, 0, 0]}},
   {{"name": "front wheel", "primitive": "cylinder", "size": [620, 220, 620], "position": [1300, 800, 310], "axis": "+y", "rotation": [0, 0, 0], "mirror": "y"}},
   {{"name": "rear wheel", "primitive": "cylinder", "size": [620, 220, 620], "position": [-1300, 800, 310], "axis": "+y", "rotation": [0, 0, 0], "mirror": "y"}},
   {{"name": "headlight", "primitive": "sphere", "size": [120, 200, 120], "position": [2000, 550, 750], "rotation": [0, 0, 0], "mirror": "y"}}
  ]}}
(Each wheel is listed ONCE with "mirror": "y"; never also list the other side.)

Return the JSON only — no markdown, no commentary."""


def extract_json(text: str) -> Any:
    cleaned = re.sub(r"```(?:json)?", "", text or "", flags=re.I).strip()
    try:
        return json.loads(cleaned)
    except ValueError:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(cleaned[start:end + 1])
    except ValueError:
        return None
