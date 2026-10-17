# Adapted from LoominPhysicsSandbox (Preet37, pre-existing project) lib/geometryPlan.ts (researchGeometryPlan) and app/api/geometry-render/route.ts (orchestration), ported to Python for Twin.
"""Any description -> a real CAD model (GLB), stored by id.

    result = await generate_model("a 6-axis industrial robot arm on a pedestal")
    # {"id", "url", "parts", "ms", "generator", "category", "referenceProduct", ...}

Flow (Loomin's geometry-render route, on Nemotron):
  1. model library hit for this topic -> instant replay;
  2. spec sheet research (cad.spec) -> validated real dimensions;
  3. researched part plan (cad.plan) grounded on the spec, validated, audited
     (cad.audit), one repair retry with the complaints fed back;
  4. if no valid plan: the category's known template (cad.templates), spec-grounded
     where one exists; an unknown object with no plan fails loudly (no blob);
  5. compile -> OpenSCAD -> STL -> Blender -> GLB (cad.render), save to the library.
Twin change: the researched plan runs BEFORE templates (Loomin did templates first),
because meeting requests carry specifics ("robot arm with 3 joints") a fixed template
ignores; CAD_PREFER_TEMPLATES=1 restores Loomin's order. The model only ever writes
JSON; all OpenSCAD is compiled here or fixed template source.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from threading import Lock

import config
import llm

from . import audit as A
from . import library as L
from . import plan as P
from . import render as R
from . import spec as S
from . import templates as T

log = logging.getLogger("twin.cad")

MAX_ATTEMPTS = 2           # first try + one repair retry with the validator's complaints
PLAN_MAX_TOKENS = 4000
PLAN_CALL_TIMEOUT = float(os.getenv("CAD_PLAN_TIMEOUT", "35"))
SPEC_TIMEOUT = float(os.getenv("CAD_SPEC_TIMEOUT", "30"))   # research keeps running (and caches) ...
SPEC_WAIT = float(os.getenv("CAD_SPEC_WAIT", "14"))         # ... but the plan waits at most this long
BUDGET_S = float(os.getenv("CAD_BUDGET", "85"))   # hook times a job out at 90 s
RENDER_RESERVE_S = 15
LRU_SIZE = 32
DISK_DIR = Path(__file__).resolve().parents[1] / "learning" / "runs" / "cad"  # runs/ is gitignored
_ID_RE = re.compile(r"^[a-f0-9]{12}$")

_store: "OrderedDict[str, bytes]" = OrderedDict()
_store_lock = Lock()


class CadError(RuntimeError):
    pass


# ── GLB store (served by GET /cad/<id>.glb) ──────────────────────────────────

def put_glb(model_id: str, data: bytes) -> None:
    with _store_lock:
        _store[model_id] = data
        _store.move_to_end(model_id)
        while len(_store) > LRU_SIZE:
            _store.popitem(last=False)
    try:
        DISK_DIR.mkdir(parents=True, exist_ok=True)
        (DISK_DIR / f"{model_id}.glb").write_bytes(data)
    except OSError:
        log.warning("could not persist CAD model %s to disk", model_id, exc_info=True)


def get_glb(model_id: str) -> bytes | None:
    if not _ID_RE.fullmatch(model_id or ""):
        return None
    with _store_lock:
        data = _store.get(model_id)
        if data is not None:
            _store.move_to_end(model_id)
            return data
    path = DISK_DIR / f"{model_id}.glb"
    try:
        return path.read_bytes() if path.is_file() else None
    except OSError:
        return None


def model_url(model_id: str) -> str:
    base = os.getenv("PUBLIC_SERVER_URL", "http://localhost:8000").rstrip("/")
    return f"{base}/cad/{model_id}.glb"


# ── LLM ──────────────────────────────────────────────────────────────────────

def plan_models() -> list[str]:
    """Nemotron models to try in order (Loomin fell back Gemini -> Groq; Twin stays on Crusoe)."""
    env = os.getenv("CAD_MODELS")
    names = env.split(",") if env else [config.MODEL_SMART, getattr(config, "MODEL_COACH", ""),
                                        getattr(config, "MODEL_FAST", "")]
    out: list[str] = []
    for n in (x.strip() for x in names):
        if n and n not in out:
            out.append(n)
    return out


async def _ask(prompt: str, timeout: float = PLAN_CALL_TIMEOUT) -> tuple[str, str]:
    """One JSON call; on an error (e.g. 503 no capacity) fall through to the next model.
    `timeout` bounds the whole call, across fallbacks."""
    end = time.monotonic() + timeout
    for model in plan_models():
        left = end - time.monotonic()
        if left < 5:
            break
        try:
            text = await asyncio.wait_for(llm.chat(
                model, [{"role": "user", "content": prompt}],
                response_format={"type": "json_object"}, max_tokens=PLAN_MAX_TOKENS, temperature=0.2,
                # Thinking measured at >120 s for one plan: far over the 90 s meeting budget.
                extra_body={"chat_template_kwargs": {"enable_thinking": False}}), min(PLAN_CALL_TIMEOUT, left))
        except Exception as e:  # noqa: BLE001
            log.warning("CAD call to %s failed: %s", model, str(e)[:200])
            continue
        if text.strip():
            return text, model
        log.warning("CAD call to %s returned no text", model)
    return "", ""


# ── Plan research ────────────────────────────────────────────────────────────

async def research_plan(description: str, spec: S.SpecSheet | None = None, deadline: float | None = None,
                        ) -> tuple[P.GeometryPlan, P.PlanValidation, int, list[str]]:
    """Plan, validate, audit; retry once with validation errors + audit issues fed back (as Loomin).
    Returns (plan, validation, attempts, remaining audit issues). Raises CadError if no valid plan."""
    deadline = deadline or (time.monotonic() + BUDGET_S)
    spec_block = S.format_spec_for_plan(spec)
    feedback: str | None = None
    best: tuple[P.GeometryPlan, P.PlanValidation, list[str]] | None = None
    best_invalid: tuple[P.GeometryPlan, P.PlanValidation] | None = None
    attempts = 0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        remaining = deadline - time.monotonic() - RENDER_RESERVE_S
        if attempt > 1 and remaining < 20:
            break
        attempts = attempt
        text, source = await _ask(P.build_plan_prompt(description, feedback, spec_block), remaining)
        plan = P.coerce_geometry_plan(P.extract_json(text), description, source)
        validation = P.validate_geometry_plan(plan)
        if validation.valid and plan is not None:
            issues = A.smoothness_audit(plan, description)
            if best is None or len(issues) < len(best[2]):
                best = (plan, validation, issues)
            if not issues:
                break
            feedback = "\n".join(f"- {i}" for i in issues)
            log.info("CAD plan valid but audit flagged: %s", "; ".join(issues))
            continue
        if best is not None:
            break  # the repair broke a valid plan; keep the valid one
        if plan is not None and (best_invalid is None or len(validation.errors) < len(best_invalid[1].errors)):
            best_invalid = (plan, validation)
        feedback = "\n".join(f"- {e}" for e in validation.errors + (P.floating_report(plan) if plan else []))
        log.warning("CAD plan attempt %d rejected: %s", attempt, "; ".join(validation.errors))
    if best is not None:
        plan, validation, issues = best
        log.info("CAD plan %r -> %d parts (attempt %d)", description[:60], len(plan.parts), attempts)
        return plan, validation, attempts, issues
    if best_invalid is not None and all("float disconnected" in e or "separate group" in e
                                          for e in best_invalid[1].errors):
        # Twin: connectivity was the only problem; snap the floating parts into contact.
        snapped = P.snap_floating(best_invalid[0])
        v = P.validate_geometry_plan(snapped)
        if v.valid:
            log.info("CAD plan %r: snapped floating parts into contact", description[:60])
            return snapped, v, attempts, A.smoothness_audit(snapped, description) + ["snapped floating parts"]
    errors = best_invalid[1].errors if best_invalid else ["No geometry plan was produced"]
    raise CadError(f'Could not work out what "{description[:80]}" looks like. {"; ".join(errors)}')


# ── Orchestration ────────────────────────────────────────────────────────────

async def _render(scad: str) -> bytes:
    scad, _fixes = A.polish_openscad(scad)
    return await asyncio.to_thread(R.render_glb, scad)


async def generate_model(description: str, *, use_library: bool = True, use_spec: bool | None = None) -> dict:
    description = " ".join((description or "").split())[:400]
    if not description:
        raise CadError("empty description")
    t0 = time.monotonic()
    deadline = t0 + BUDGET_S
    ms: dict[str, int] = {}

    def lap(name: str, since: float) -> float:
        now = time.monotonic()
        ms[name] = round((now - since) * 1000)
        return now

    if use_library:
        hit = L.lookup_model(description)
        if hit is not None:
            entry, glb = hit
            model_id = uuid.uuid4().hex[:12]
            put_glb(model_id, glb)
            lap("total", t0)
            return {"id": model_id, "url": model_url(model_id), "parts": entry.get("parts"), "ms": ms,
                    "generator": "library", "cached": True, "category": entry.get("category"),
                    "referenceProduct": entry.get("referenceProduct"), "names": entry.get("names", [])}

    category = T.classify_topic(description)
    spec: S.SpecSheet | None = None
    if use_spec if use_spec is not None else os.getenv("CAD_SPEC", "1") != "0":
        t = time.monotonic()
        task = asyncio.ensure_future(
            S.research_spec_sheet(description, lambda p: _ask(p, SPEC_TIMEOUT), timeout=SPEC_TIMEOUT))
        done, _ = await asyncio.wait({task}, timeout=SPEC_WAIT)
        spec, spec_v = task.result() if done else (None, S.validate_spec_sheet(None))
        if not done:  # Twin: don't block the meeting; the finished spec is cached for next time
            log.info("CAD spec for %r still researching after %.0fs; planning without it", description[:60], SPEC_WAIT)
        lap("spec", t)
        if spec is not None and not spec_v.valid:
            log.info("CAD spec for %r has errors (%s); using it as a hint only", description[:60], spec_v.errors)

    prefer_templates = os.getenv("CAD_PREFER_TEMPLATES") == "1"
    glb: bytes | None = None
    result: dict = {"category": category, "referenceProduct": spec.reference_product if spec else None,
                    "specDims": len(spec.dimensions) if spec else 0}
    errors: list[str] = []

    async def try_template() -> bytes | None:
        scad = T.template_for(description, spec, category)
        if scad is None:
            return None
        t = time.monotonic()
        try:
            data = await _render(scad)
        except R.RenderError as e:
            errors.append(f"template: {e}")
            return None
        lap("render", t)
        result.update(generator=f"template:{category}", parts=None, names=[])
        return data

    async def try_plan() -> bytes | None:
        t = time.monotonic()
        try:
            plan, _v, attempts, issues = await research_plan(description, spec, deadline)
        except CadError as e:
            errors.append(str(e))
            return None
        t = lap("plan", t)
        try:
            data = await _render(P.plan_to_openscad(plan))
        except R.RenderError as e:
            errors.append(f"render: {e}")
            return None
        lap("render", t)
        result.update(generator="plan", parts=len(P.expand_mirrors(plan.parts)), attempts=attempts,
                      audit=issues, names=[p.name for p in plan.parts],
                      referenceProduct=result["referenceProduct"] or plan.reference_product)
        return data

    order = (try_template, try_plan) if prefer_templates else (try_plan, try_template)
    for step in order:
        glb = await step()
        if glb is not None:
            break
    if glb is None:
        raise CadError("; ".join(errors) or f'no model for "{description[:80]}"')

    model_id = uuid.uuid4().hex[:12]
    put_glb(model_id, glb)
    L.save_model(description, glb, result["generator"], result.get("referenceProduct"),
                 extra={"category": category, "parts": result.get("parts"), "names": result.get("names", [])})
    lap("total", t0)
    log.info("CAD model %s (%s): %s parts, %d bytes, %s", model_id, result["generator"], result.get("parts"),
             len(glb), ms)
    return {"id": model_id, "url": model_url(model_id), "ms": ms, "cached": False, **result}
