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

PRIMITIVES = ("box", "cylinder", "cone", "sphere", "torus", "wedge")  # Twin: + wedge (roofs, ramps)
# Topics where an all-box plan is correct, not a blob (Loomin's audit list + Twin's racks/rooms).
RECTILINEAR = re.compile(
    r"breadboard|brick|block|cube|box|crate|shelf|shelves|building|circuit|rack|server|cabinet|room|"
    r"office|layout|floor ?plan|warehouse|desk|table|bookcase|container|data ?cent(?:er|re)|wall|stair|"
    r"house|tower|skyscraper|grid|pallet|locker|keyboard|console|panel|apartment|garage|barn|factory",
    re.I,
)
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
    attach: dict | None = None        # Twin: {"to": parent name, "face": "+z", "offset": [a, b]}; sets position
    copies: dict | None = None        # Twin: {"radial": n, "axis": "z"} or {"count": n, "step": [dx,dy,dz]}
    spin: tuple | None = None         # Twin (expanded copies): (axis index, degrees, centre) applied last


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
    if re.search(r"wedge|prism|triang|ramp|gable", s):
        return "wedge"
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
            attach=_coerce_attach(_first(rp, "attach", "attachTo", "parent")),
            copies=_coerce_copies(rp),
        ))
    parts = resolve_attachments(parts)
    parts = expand_copies(parts)
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


def _coerce_attach(v: Any) -> dict | None:
    if isinstance(v, str):
        v = {"to": v}
    if not isinstance(v, dict) or not str(v.get("to") or v.get("parent") or "").strip():
        return None
    face = _coerce_axis(v.get("face") or v.get("side") or "+z") or "+z"
    off = v.get("offset") if isinstance(v.get("offset"), (list, tuple)) else []
    offset = (_num(off[0], 0.0) if len(off) > 0 else 0.0, _num(off[1], 0.0) if len(off) > 1 else 0.0)
    return {"to": str(v.get("to") or v.get("parent")).strip()[:60], "face": face, "offset": offset,
            "inset": max(0.0, _num(v.get("inset"), 0.0))}


def _coerce_copies(rp: dict) -> dict | None:
    r = rp.get("radial") if isinstance(rp.get("radial"), dict) else None
    if r is not None:
        n = int(_num(r.get("count"), 0))
        if 2 <= n <= 36:
            axis = (_coerce_axis(r.get("axis") or "z") or "+z")[1]
            c = r.get("center")
            center = _triple(c, (0.0, 0.0, 0.0)) if isinstance(c, (list, tuple)) else None
            return {"radial": n, "axis": axis, "center": center}
    a = rp.get("array") if isinstance(rp.get("array"), dict) else None
    if a is not None:
        n = int(_num(a.get("count"), 0))
        step = _triple(a.get("step") or a.get("spacing"), (0.0, 0.0, 0.0))
        if 2 <= n <= 40 and any(step):
            return {"count": n, "step": step}
    return None


def expand_copies(parts: list[PlanPart]) -> list[PlanPart]:
    """Twin: radial copies (spokes, legs of a star base, blades, bolts) spin about an axis
    through the parent's centre (or "center"); linear arrays (racks in a row, windows)
    step by a fixed offset. Expanded into explicit, named parts before validation."""
    by_name = {p.name.lower(): p for p in parts}
    out: list[PlanPart] = []
    for p in parts:
        c = p.copies
        if not c:
            out.append(p)
            continue
        base = replace(p, copies=None)
        if "radial" in c:
            k = _AXIS[c["axis"]]
            center = c["center"]
            if center is None:
                parent = by_name.get(str((p.attach or {}).get("to", "")).lower())
                if parent is not None:
                    lo, hi = part_aabb(parent)
                    center = tuple((lo[i] + hi[i]) / 2 for i in range(3))
                else:
                    center = (0.0, 0.0, 0.0)
            for n in range(c["radial"]):
                out.append(replace(base, name=base.name if n == 0 else f"{base.name} {n + 1}",
                                   spin=(k, 360.0 * n / c["radial"], tuple(center)) if n else None))
        else:
            for n in range(c["count"]):
                pos = tuple(base.position[i] + c["step"][i] * n for i in range(3))
                out.append(replace(base, name=base.name if n == 0 else f"{base.name} {n + 1}", position=pos))
        if len(out) >= MAX_PARTS:
            break
    return out[:MAX_PARTS]


def _spin_point(pt, spin) -> list[float]:
    k, deg, c = spin
    a = math.radians(deg)
    ca, sa = math.cos(a), math.sin(a)
    i, j = [(1, 2), (2, 0), (0, 1)][k]  # right-handed rotation about axis k
    v = [pt[n] - c[n] for n in range(3)]
    out = list(v)
    out[i] = v[i] * ca - v[j] * sa
    out[j] = v[i] * sa + v[j] * ca
    return [out[n] + c[n] for n in range(3)]


def resolve_attachments(parts: list[PlanPart]) -> list[PlanPart]:
    """Twin: place every part with "attach" against its parent's face (touching, offset
    in-plane from the face centre), in dependency order. Models get relations right far
    more often than coordinates, and attached parts can never float. A part attached to a
    mirrored parent inherits the mirror (a wheel on a mirrored axle)."""
    by_name = {p.name.lower(): i for i, p in enumerate(parts)}
    out = list(parts)
    state = [0] * len(parts)  # 0 todo, 1 visiting, 2 done

    def place(i: int) -> None:
        if state[i] == 2:
            return
        if state[i] == 1:  # cycle: keep the given position
            return
        part = out[i]
        att = part.attach
        j = by_name.get(att["to"].lower()) if att else None
        if att is None or j is None or j == i:
            state[i] = 2
            return
        state[i] = 1
        place(j)
        parent = out[j]
        plo, phi = part_aabb(parent)
        clo, chi = part_aabb(replace(part, position=(0.0, 0.0, 0.0)))
        k = _AXIS[att["face"][1]]
        others = [a for a in range(3) if a != k]
        pos = [0.0, 0.0, 0.0]
        for n, a in enumerate(others):
            centre, reach = (plo[a] + phi[a]) / 2, ((phi[a] - plo[a]) + (chi[a] - clo[a])) / 2 * 0.98
            # An attached part always keeps contact: clamp the offset to the face's footprint.
            pos[a] = centre + max(-reach, min(reach, att["offset"][n]))
        if att["face"][0] == "+":
            pos[k] = phi[k] + chi[k] - att["inset"]
        else:
            pos[k] = plo[k] + clo[k] + att["inset"]
        mirror = part.mirror or parent.mirror
        out[i] = replace(part, position=tuple(pos), mirror=mirror)
        state[i] = 2

    for i in range(len(out)):
        place(i)
    return out


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
    lo, hi = [px - hx, py - hy, pz - hz], [px + hx, py + hy, pz + hz]
    if part.spin and part.spin[1]:
        corners = [_spin_point((x, y, z), part.spin) for x in (lo[0], hi[0]) for y in (lo[1], hi[1])
                   for z in (lo[2], hi[2])]
        lo = [min(c[i] for c in corners) for i in range(3)]
        hi = [max(c[i] for c in corners) for i in range(3)]
    return lo, hi


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
    spin = part.spin
    if spin:
        k, deg, c = spin
        cc = list(c)
        cc[axis] = -cc[axis]
        spin = (k, deg if k == axis else -deg, tuple(cc))
    return replace(part, position=tuple(pos), rotation=tuple(rot), mirror=None, axis=direction, spin=spin)


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
    if p == "wedge":
        return x * y * z / 2
    return x * y * z


# ── Validation ───────────────────────────────────────────────────────────────

def _groups(boxes, slack: float) -> list[list[int]]:
    n = len(boxes)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(n):
        for j in range(i + 1, n):
            if _overlaps_or_touches(boxes[i], boxes[j], slack):
                parent[find(i)] = find(j)
    out: dict[int, list[int]] = {}
    for k in range(n):
        out.setdefault(find(k), []).append(k)
    return list(out.values())


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
    # Twin: an all-box rack, room or building is a real model, not a blob.
    boxy_ok = kinds <= {"box", "wedge"} and bool(RECTILINEAR.search(plan.topic or ""))
    record("uses more than one primitive type", len(kinds) >= 2 or boxy_ok,
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

    # Twin: Loomin only rejected parts touching nothing; two detached clusters (a row of
    # tiles beside the floor) passed. Require ONE connected group.
    if not orphan_names:
        groups = _groups(boxes, slack)
        if len(groups) > 1:
            main = next(g for g in groups if 0 in g)
            stray = sorted({names[k] for g in groups if g is not main for k in g})
            record("parts form a single group", False,
                   f"{len(stray)} part(s) form a separate group detached from the main body ({', '.join(stray[:6])})")

    volumes = [_part_volume(p) for p in parts]
    total = sum(volumes)
    dominant = max(volumes) / total if total > 0 else 1
    if dominant > 0.92 and len(parts) < 8:
        warnings.append(f"One part is {dominant * 100:.0f}% of the volume — detail parts may be too small to see")
    if not plan.summary:
        warnings.append("Plan has no summary describing the object")

    return PlanValidation(not errors, errors, warnings, checks)


# ── Twin: connectivity feedback + last-resort snapping ───────────────────────

def _gap(a, b) -> list[float]:
    """Per-axis signed gap from box a to box b (0 where they overlap)."""
    out = []
    for i in range(3):
        if a[1][i] < b[0][i]:
            out.append(b[0][i] - a[1][i])
        elif b[1][i] < a[0][i]:
            out.append(-(a[0][i] - b[1][i]))
        else:
            out.append(0.0)
    return out


def floating_report(plan: GeometryPlan) -> list[str]:
    """Human-readable "X is N mm from Y on z" lines for orphan parts (fed into the repair prompt)."""
    lo, hi = plan_bounds(plan)
    slack = max(hi[i] - lo[i] for i in range(3)) * 0.06
    parts = expand_mirrors(plan.parts)
    boxes = [part_aabb(p) for p in parts]
    lines = []
    groups = _groups(boxes, slack)
    main = set(next((g for g in groups if 0 in g), [])) if groups else set()
    for i, box in enumerate(boxes):
        if i in main:
            continue
        j = min((j for j in main), key=lambda j: math.hypot(*_gap(box, boxes[j])), default=None)
        if j is None:
            continue
        g = _gap(box, boxes[j])
        move = ", ".join(f"{'+-'[v < 0]}{'xyz'[k]} by {abs(v):.0f} mm" for k, v in enumerate(g) if v)
        lines.append(f"{parts[i].name} floats free: move it {move} to touch {parts[j].name}")
    return sorted(set(lines))[:8]


def snap_floating(plan: GeometryPlan, max_rounds: int = 40) -> GeometryPlan:
    """Move each detached group of parts the minimal distance to touch the main body
    (the group holding the first part, which the prompt makes the main body). Twin-only last resort, used when connectivity
    is a plan's only validation error. A mirrored part moves symmetrically."""
    parts = list(plan.parts)
    for _ in range(max_rounds):
        expanded: list[PlanPart] = []
        owner: list[tuple[int, bool]] = []  # (index into parts, is mirrored copy)
        for idx, p in enumerate(parts):
            expanded.append(p)
            owner.append((idx, False))
            if p.mirror:
                expanded.append(_mirrored(p))
                owner.append((idx, True))
        boxes = [part_aabb(p) for p in expanded]
        lo = [min(b[0][i] for b in boxes) for i in range(3)]
        hi = [max(b[1][i] for b in boxes) for i in range(3)]
        slack = max(hi[i] - lo[i] for i in range(3)) * 0.06
        n = len(boxes)
        comp = list(range(n))

        def find(x: int) -> int:
            while comp[x] != x:
                comp[x] = comp[comp[x]]
                x = comp[x]
            return x

        for i in range(n):
            for j in range(i + 1, n):
                if owner[i][0] == owner[j][0] or _overlaps_or_touches(boxes[i], boxes[j], slack):
                    comp[find(i)] = find(j)
        main = find(0)  # the first part is the main body (prompt rule 4)
        detached = [k for k in range(n) if find(k) != main]
        if not detached:
            break
        group = [k for k in detached if find(k) == find(detached[0])]
        mains = [k for k in range(n) if find(k) == main]
        a, b = min(((a, b) for a in group for b in mains), key=lambda ab: math.hypot(*_gap(boxes[ab[0]], boxes[ab[1]])))
        delta = _gap(boxes[a], boxes[b])
        for idx in {owner[k][0] for k in group}:
            src = parts[idx]
            d = list(delta)
            mirrored_only = all(owner[k][1] for k in group if owner[k][0] == idx)
            if src.mirror and mirrored_only:
                d[_AXIS[src.mirror]] *= -1  # we measured on the mirrored copy
            parts[idx] = replace(src, position=tuple(c + v for c, v in zip(src.position, d)))
    return replace(plan, parts=parts)


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
    if p == "wedge":  # triangular prism: base x, apex height z, ridge along y
        return (f"rotate([90, 0, 0]) linear_extrude(height={_fmt(sy)}, center=true) "
                f"polygon([[{_fmt(-sx / 2)}, {_fmt(-sz / 2)}], [{_fmt(sx / 2)}, {_fmt(-sz / 2)}], [0, {_fmt(sz / 2)}]]);")
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
    placed = f"translate([{_fmt(px)}, {_fmt(py)}, {_fmt(pz)}]) {rotated}"
    if part.spin and part.spin[1]:
        k, deg, c = part.spin
        v = [0, 0, 0]
        v[k] = 1
        cx, cy, cz = (x * scale for x in c)
        placed = (f"translate([{_fmt(cx)}, {_fmt(cy)}, {_fmt(cz)}]) rotate(a={_fmt(deg)}, v=[{v[0]}, {v[1]}, {v[2]}]) "
                  f"translate([{_fmt(-cx)}, {_fmt(-cy)}, {_fmt(-cz)}]) {placed}")
    return placed


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

def build_plan_prompt(topic: str, feedback: str | None = None, spec_block: str = "") -> str:
    """spec_block: cad.spec.format_spec_for_plan(spec) — verified dimensions to match."""
    feedback_block = (f"\nYOUR PREVIOUS ATTEMPT WAS REJECTED. Fix these problems:\n{feedback}\n"
                      if feedback else "")
    return f"""You are a mechanical designer. Break the real object "{topic}" down into the
primitive solids a CAD model would be assembled from.
{spec_block}{feedback_block}
Return ONLY JSON in exactly this shape:
{{
  "referenceProduct": "the specific real product you are modelling",
  "summary": "one sentence describing its shape",
  "parts": [
    {{
      "name": "main body",
      "primitive": "box|cylinder|cone|sphere|torus|wedge",
      "size": [x, y, z],
      "position": [x, y, z],
      "axis": "+z",
      "rotation": [0, 0, 0],
      "mirror": "y",
      "attach": {{"to": "name of an earlier part", "face": "+z", "offset": [a, b], "inset": 0}},
      "radial": {{"count": 5, "axis": "z"}},
      "array": {{"count": 4, "step": [800, 0, 0]}}
    }}
  ]
}}

RULES:
1. All sizes in MILLIMETRES, using the object's real-world size.
2. "size" is the part's full extent along the WORLD x, y and z axes as placed:
   an upright panel (backrest, door, screen, wall) is thin in x or y and tall
   in z; a flat part (seat, tabletop, floor, shelf) is thin in z.
3. AXES: +x is the FRONT (forward direction), y is left-right, +z is UP.
4. PLACEMENT. The first part is the main body and gives "position" (its
   CENTRE). EVERY other part should give "attach" instead of "position":
   it is placed touching the named parent's "face" ("+x", "-x", "+y", "-y",
   "+z" = on top, "-z" = underneath), centred on that face and shifted by
   "offset" [a, b] mm along the face's two other axes in x, y, z order
   (face +z/-z: offset is [x, y]; face +x/-x: [y, z]; face +y/-y: [x, z]).
   "inset" (mm) sinks the part into its parent, e.g. half a wheel's width.
   Attached parts can never float, so prefer "attach" for everything.
5. Use "mirror" ("x" or "y") for parts that exist as a symmetric pair or set
   (wheels, legs, arms, handles) and describe only the positive-side one;
   never also list the other side. Parts attached to a mirrored part follow it.
6. REPEATS: "radial" makes `count` copies spun evenly about an axis through the
   parent's centre (the 5 legs of a star base, fan blades, spokes, bolts, chairs
   round a table): describe the first copy only. "array" makes `count` copies
   stepped by "step" mm (racks in a row, windows, shelves). Use at most one of
   radial / array per part. At least 6 parts. Name every part after the real component it represents.
7. Do NOT output a generic blob. If "{topic}" is a specific product, model that
   product's actual silhouette and proportions.
8. Cylinder, cone and torus also give "axis": the world direction they run
   along ("+x", "-x", "+y", "-y", "+z", "-z"); a cone's tip points that way.
   A post standing up is axis "+z"; an axle or wheel turning left-right is "+y".
   Do NOT use "rotation" to orient parts: leave it [0, 0, 0] except for tilts
   (a leaning backrest, a splayed leg, a swept blade).
9. "wedge" is a triangular prism (roofs, ramps, gables): base spans x, apex at
   the top, ridge along y; size is [base, ridge length, height]. Rotation
   [0, 0, 90] turns the ridge to run along x.
10. Thin parts (plates, blades, fins, shelves, panels) are boxes with one small
   dimension; give them real thickness (at least 1% of the object's length).
11. Pose articulated objects (robot arms, cranes, lamps, figures) in a natural
   working pose: tilt segments with "rotation" so they bend at the joints
   instead of stacking in one straight column.
12. Make the features that identify the object separate parts: legs, spokes,
   blades, arms, handles, screens, doors are individual parts, not one block.
   Keep proportions realistic relative to the main body.
13. Layouts (rooms, racks, desks, floor plans, factories): the floor or base is
   the main body (one thin box); every item attaches to its "+z" face with an
   offset giving its place on the floor; repeated items are separate parts,
   up to 50 parts.

EXAMPLE of the conventions (a different object, a 4000 mm car, abbreviated):
{{"referenceProduct": "compact hatchback", "summary": "boxy car on four wheels",
  "parts": [
   {{"name": "body", "primitive": "box", "size": [4000, 1700, 700], "position": [0, 0, 650]}},
   {{"name": "cabin", "primitive": "box", "size": [2200, 1500, 600], "attach": {{"to": "body", "face": "+z", "offset": [-200, 0]}}}},
   {{"name": "front wheel", "primitive": "cylinder", "size": [620, 220, 620], "axis": "+y", "mirror": "y",
     "attach": {{"to": "body", "face": "+y", "offset": [1300, -340], "inset": 110}}}},
   {{"name": "rear wheel", "primitive": "cylinder", "size": [620, 220, 620], "axis": "+y", "mirror": "y",
     "attach": {{"to": "body", "face": "+y", "offset": [-1300, -340], "inset": 110}}}},
   {{"name": "headlight", "primitive": "sphere", "size": [120, 200, 120], "mirror": "y",
     "attach": {{"to": "body", "face": "+x", "offset": [550, 100], "inset": 40}}}}
  ]}}

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
