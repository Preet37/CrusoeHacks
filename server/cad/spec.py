# Adapted from LoominPhysicsSandbox (Preet37, pre-existing project) lib/specSheet.ts, ported to Python for Twin.
"""Spec sheet: grounded dimensional research for CAD generation.

When an LLM writes both the geometry AND the numbers, the numbers are plausible
but arbitrary. So the jobs are split:
  1. research a real reference product and extract a DIMENSIONED spec sheet;
  2. validate it deterministically (units, ranges, area-vs-w*h cross-consistency);
  3. inject the validated numbers into the plan prompt / templates as constants.
Stage 2 is pure and unit-tested. Loomin's Gemini -> NVIDIA -> Groq chain is replaced
by Twin's Nemotron-on-Crusoe chain (cad.pipeline.plan_models()).
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("twin.cad")


@dataclass
class SpecDimension:
    key: str
    label: str
    value: float          # canonical unit for its class (mm, mm2, g, deg)
    unit: str
    raw_value: float
    raw_unit: str
    unit_class: str
    confidence: float


@dataclass
class SpecSheet:
    topic: str
    reference_product: str
    category: str
    dimensions: list[SpecDimension]
    attributes: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    model: str = ""


@dataclass
class SpecValidation:
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)


# ── Unit normalisation ───────────────────────────────────────────────────────

_UNITS: dict[str, tuple[str, str, float]] = {}
for _names, _cls, _canon, _f in [
    (("mm", "millimeter", "millimetre"), "length", "mm", 1),
    (("cm", "centimeter", "centimetre"), "length", "mm", 10),
    (("m", "meter", "metre"), "length", "mm", 1000),
    (("km",), "length", "mm", 1_000_000),
    (("in", "inch", "inches"), "length", "mm", 25.4),
    (("ft", "foot", "feet"), "length", "mm", 304.8),
    (("g", "gram", "grams"), "mass", "g", 1),
    (("kg", "kilogram"), "mass", "g", 1000),
    (("oz", "ounce"), "mass", "g", 28.349523),
    (("lb", "lbs", "pound"), "mass", "g", 453.59237),
    (("deg", "degree", "degrees"), "angle", "deg", 1),
    (("rad", "radian"), "angle", "deg", 57.29578),
    (("count",), "count", "count", 1),
    (("unitless", "ratio", "%"), "ratio", "ratio", 1),
]:
    for _n in _names:
        _UNITS[_n] = (_cls, _canon, _f)


def lookup_unit(raw: str) -> tuple[str, str, float]:
    """Resolve messy unit strings ("sq in", "in²", "mm^2", "lbs") to (class, canonical, factor).
    Squared units are derived by squaring the base length factor."""
    original = str(raw or "").strip().lower()
    body = original.replace("²", "2").replace("^", "")
    square_word = bool(re.search(r"\b(?:sq|square)\b\.?", body) or re.match(r"^sq(?=[a-z])", body))
    body = re.sub(r"\b(?:sq|square)\b\.?", " ", body)
    body = re.sub(r"^sq(?=[a-z])", " ", body)
    body = re.sub(r"[\s.]+", "", body)
    trailing_two = body.endswith("2")
    if trailing_two:
        body = body[:-1]
    d = _UNITS.get(body) or (_UNITS.get(body[:-1]) if body.endswith("s") else None)
    if d is None:
        return ("unknown", original or "unknown", 1.0)
    if (square_word or trailing_two) and d[0] == "length":
        return ("area", "mm2", d[2] * d[2])
    return d


def normalise_dimension(raw_value: float, raw_unit: str) -> tuple[float, str, str]:
    cls, canon, factor = lookup_unit(raw_unit)
    return raw_value * factor, canon, cls


# ── Deterministic validation ─────────────────────────────────────────────────

# Generous envelopes: a net for nonsense (a 3 km tennis racket), not per-topic asserts.
_RANGES = {
    "length": (0.05, 1_000_000), "area": (0.01, 1e12), "mass": (0.001, 1e12),
    "angle": (-360, 360), "count": (0, 100_000), "ratio": (-1e6, 1e6),
}


def validate_spec_sheet(spec: SpecSheet | None) -> SpecValidation:
    if spec is None:
        return SpecValidation(False, ["No spec sheet provided"])
    errors: list[str] = []
    warnings: list[str] = []
    checks: list[dict[str, Any]] = []

    def push(label: str, ok: bool) -> None:
        checks.append({"label": label, "pass": ok})

    dims = spec.dimensions
    push("At least 3 dimensions", len(dims) >= 3)
    if len(dims) < 3:
        errors.append(f"Only {len(dims)} dimension(s) — need at least 3 to model anything")

    named = len(spec.reference_product or "") > 2
    push("Named reference product", named)
    if not named:
        warnings.append("No named reference product — dimensions may be generic averages")

    non_finite = [d.key for d in dims if not math.isfinite(d.value)]
    push("All values numeric", not non_finite)
    if non_finite:
        errors.append(f"Non-numeric values: {', '.join(non_finite)}")

    unknown = [f"{d.key}({d.raw_unit})" for d in dims if d.unit_class == "unknown"]
    push("All units recognised", not unknown)
    if unknown:
        warnings.append(f"Unrecognised units: {', '.join(unknown)}")

    out_of_range = []
    for d in dims:
        r = _RANGES.get(d.unit_class)
        if r and math.isfinite(d.value) and not (r[0] <= d.value <= r[1]):
            out_of_range.append(f"{d.key}={d.value}{d.unit} (expected {r[0]}..{r[1]})")
    push("Values physically plausible", not out_of_range)
    if out_of_range:
        errors.append(f"Out of plausible range: {'; '.join(out_of_range)}")

    seen: set[str] = set()
    dupes: set[str] = set()
    for d in dims:
        k = d.key.lower()
        (dupes if k in seen else seen).add(k)
    push("No duplicate dimension keys", not dupes)
    if dupes:
        warnings.append(f"Duplicate keys: {', '.join(sorted(dupes))}")

    # Cross-consistency: *_area must agree with *_width x *_height (rect or ellipse, 25%).
    by_key = {d.key.lower(): d for d in dims}
    checked = failed = 0
    for d in dims:
        k = d.key.lower()
        if d.unit_class != "area" or not k.endswith("_area"):
            continue
        stem = k[: -len("_area")]
        w = by_key.get(f"{stem}_width")
        h = by_key.get(f"{stem}_height") or by_key.get(f"{stem}_length")
        if not w or not h or w.unit_class != "length" or h.unit_class != "length":
            continue
        checked += 1
        rect = w.value * h.value
        ellipse = math.pi / 4 * w.value * h.value
        closest = rect if abs(d.value - rect) < abs(d.value - ellipse) else ellipse
        ratio = d.value / closest if closest > 0 else math.inf
        if ratio < 0.75 or ratio > 1.25:
            failed += 1
            errors.append(f"{d.key}={round(d.value)}mm2 is inconsistent with {w.key}×{h.key} "
                          f"({round(w.value)}×{round(h.value)}mm ⇒ {round(closest)}mm2)")
    if checked:
        push(f"Area/dimension cross-check ({checked})", failed == 0)

    low = [d.key for d in dims if math.isfinite(d.confidence) and d.confidence < 0.4]
    push("No very-low-confidence values", not low)
    if low:
        warnings.append(f"Low confidence: {', '.join(low)}")

    return SpecValidation(not errors, errors, warnings, checks)


# ── Coercion ─────────────────────────────────────────────────────────────────

def coerce_spec_sheet(raw: Any, topic: str, model: str = "") -> SpecSheet | None:
    """Arbitrary LLM JSON -> normalised SpecSheet. Pure."""
    if not isinstance(raw, dict):
        return None
    dims: list[SpecDimension] = []
    for d in raw.get("dimensions") if isinstance(raw.get("dimensions"), list) else []:
        if not isinstance(d, dict):
            continue
        key = str(d.get("key") or d.get("name") or "").strip()
        if not key:
            continue
        raw_unit = str(d.get("unit") or "").strip()
        try:
            raw_value = float(d.get("value"))
        except (TypeError, ValueError):
            # Models sometimes emit "27 in" in the value field despite instructions.
            m = re.match(r"\s*(-?\d+\.?\d*)\s*(.*)", str(d.get("value") or ""))
            if not m:
                continue
            raw_value = float(m.group(1))
            raw_unit = raw_unit or m.group(2).strip()
        if not math.isfinite(raw_value):
            continue
        value, unit, cls = normalise_dimension(raw_value, raw_unit)
        try:
            conf = max(0.0, min(1.0, float(d.get("confidence"))))
        except (TypeError, ValueError):
            conf = 0.5
        dims.append(SpecDimension(
            key=re.sub(r"\s+", "_", key).lower()[:60], label=str(d.get("label") or key)[:80],
            value=value, unit=unit, raw_value=raw_value, raw_unit=raw_unit or unit,
            unit_class=cls, confidence=conf))
    attrs = raw.get("attributes") if isinstance(raw.get("attributes"), dict) else {}
    notes = raw.get("notes") if isinstance(raw.get("notes"), list) else []
    return SpecSheet(
        topic=topic,
        reference_product=str(raw.get("referenceProduct") or raw.get("reference") or "").strip()[:120],
        category=str(raw.get("category") or "").strip()[:60],
        dimensions=dims[:20],
        attributes={str(k)[:40]: str(v)[:80] for k, v in attrs.items() if v is not None},
        notes=[str(n)[:200] for n in notes[:10]],
        model=model,
    )


# ── Prompts ──────────────────────────────────────────────────────────────────

def _r(n: float) -> float:
    if not math.isfinite(n):
        return 0
    return round(n) if abs(n) >= 100 else round(n, 2)


def format_spec_for_plan(spec: SpecSheet | None) -> str:
    """The block geometryPlan's prompt embeds: verified dimensions the plan MUST match."""
    if not spec or not spec.dimensions:
        return ""
    lines = "\n".join(f"- {d.label}: {_r(d.value)} {d.unit}" for d in spec.dimensions)
    return f"\nVERIFIED REAL DIMENSIONS for {spec.reference_product or spec.topic} — your plan MUST match these:\n{lines}\n"


def build_spec_prompt(topic: str) -> str:
    return f"""You are a mechanical engineering researcher compiling a dimensioned spec sheet.

TOPIC: "{topic}"

Identify a SPECIFIC, REAL, well-documented reference example of this object (an actual
product, model number, or standardised specification — not a generic average). Then
report its measured dimensions.

Output ONLY a JSON object, no markdown fences, no commentary:

{{
  "referenceProduct": "<specific real product or standard, e.g. 'Babolat Pure Drive 2021' or 'ISO 4210 city bicycle'>",
  "category": "<short category, e.g. 'sports equipment' | 'rotating machinery' | 'structure'>",
  "dimensions": [
    {{
      "key": "snake_case_name",
      "label": "Human Readable Name",
      "value": <number only, no units in this field>,
      "unit": "<mm|cm|m|in|ft|mm2|cm2|in2|g|kg|oz|lb|deg|count|ratio>",
      "confidence": <0.0-1.0>
    }}
  ],
  "attributes": {{ "<non_numeric_fact>": "<value>" }},
  "notes": ["<modelling-relevant observation>"]
}}

RULES
- 6 to 14 dimensions. Cover overall envelope (length/width/height or diameter) plus
  the major sub-features someone would need to model it.
- Naming: if you give an area for a feature, ALSO give that feature's width and
  height using the SAME stem, so they can be cross-checked.
  Example: head_area + head_width + head_height.
- "value" must be a bare number. Units belong in "unit".
- Use real measured values for the reference product you named. If you are
  estimating rather than recalling, lower the confidence accordingly.
- Non-numeric facts (string patterns, materials, colours, gear ratios expressed
  as "3:1") go in "attributes", NOT in "dimensions".
- Report counts (number of blades, teeth, spokes) as dimensions with unit "count"."""


def extract_json(text: str) -> Any:
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.I)
    candidate = fenced.group(1) if fenced else text
    m = re.search(r"\{[\s\S]*\}", candidate)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        try:  # trailing-comma and single-quote repair
            return json.loads(re.sub(r",\s*([}\]])", r"\1", m.group(0)).replace("'", '"'))
        except ValueError:
            return None


# ── Research (cached, in-flight shared) ──────────────────────────────────────

_VALID_TTL = 24 * 3600
_INVALID_TTL = 30 * 60
_CACHE_MAX = 200
_cache: dict[str, tuple[float, SpecSheet | None, SpecValidation]] = {}
_in_flight: dict[str, asyncio.Future] = {}


def _cache_key(topic: str) -> str:
    return " ".join(topic.strip().lower().split())


def clear_spec_cache() -> None:
    _cache.clear()
    _in_flight.clear()


async def research_spec_sheet(topic: str, ask, timeout: float = 25.0,
                              retry_on_invalid: bool = True) -> tuple[SpecSheet | None, SpecValidation]:
    """Research + validate a spec sheet. `ask(prompt) -> (text, model)` is the LLM call.
    Retries once on blocking errors; keeps whichever attempt is better. Cached."""
    key = _cache_key(topic)
    hit = _cache.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1], hit[2]
    pending = _in_flight.get(key)
    if pending is not None:
        return await asyncio.shield(pending)
    fut = asyncio.get_running_loop().create_future()
    _in_flight[key] = fut
    try:
        result = await asyncio.wait_for(_research(topic, ask, retry_on_invalid), timeout)
    except asyncio.CancelledError:
        _in_flight.pop(key, None)
        fut.cancel()
        raise
    except Exception as e:  # noqa: BLE001 - geometry generation works without verified dims
        log.info("spec research for %r failed: %s", topic[:60], e)
        result = (None, validate_spec_sheet(None))
    finally:
        _in_flight.pop(key, None)
    if not fut.done():
        fut.set_result(result)
    spec, validation = result
    if spec is not None:
        if len(_cache) >= _CACHE_MAX:
            _cache.pop(next(iter(_cache)))
        _cache[key] = (time.monotonic() + (_VALID_TTL if validation.valid else _INVALID_TTL), spec, validation)
    return result


async def _research(topic: str, ask, retry_on_invalid: bool) -> tuple[SpecSheet | None, SpecValidation]:
    async def once() -> SpecSheet | None:
        text, model = await ask(build_spec_prompt(topic))
        spec = coerce_spec_sheet(extract_json(text), topic, model)
        return spec if spec and spec.dimensions else None

    spec = await once()
    validation = validate_spec_sheet(spec)
    if not validation.valid and retry_on_invalid:
        log.info("spec for %r invalid (%s), retrying", topic[:60], "; ".join(validation.errors))
        second = await once()
        second_v = validate_spec_sheet(second)
        if second_v.valid or len(second_v.errors) < len(validation.errors):
            spec, validation = second, second_v
    return spec, validation
