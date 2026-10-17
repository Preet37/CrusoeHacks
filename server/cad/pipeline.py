# Adapted from LoominPhysicsSandbox (Preet37, pre-existing project) lib/geometryPlan.ts (researchGeometryPlan), ported to Python for Twin.
"""description -> Nemotron JSON part plan (one repair retry) -> OpenSCAD -> GLB, stored by id.

    result = await generate_model("a small propeller airplane")
    # {"id": "...", "url": "http://localhost:8000/cad/<id>.glb", "parts": 9, "ms": {...}}
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

from . import plan as P
from . import render as R

log = logging.getLogger("twin.cad")

MAX_ATTEMPTS = 2           # first try + one repair retry with the validator's complaints
PLAN_MAX_TOKENS = 3000
PLAN_CALL_TIMEOUT = float(os.getenv("CAD_PLAN_TIMEOUT", "35"))
LRU_SIZE = 32
DISK_DIR = Path(__file__).resolve().parents[1] / "learning" / "runs" / "cad"  # runs/ is gitignored
_ID_RE = re.compile(r"^[a-f0-9]{12}$")

_store: "OrderedDict[str, bytes]" = OrderedDict()
_store_lock = Lock()


class CadError(RuntimeError):
    pass


# ── GLB store ────────────────────────────────────────────────────────────────

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


# ── Plan research ────────────────────────────────────────────────────────────

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


async def _ask(prompt: str) -> tuple[str, str]:
    """One plan call; on an error (e.g. 503 no capacity) fall through to the next model."""
    for model in plan_models():
        try:
            text = await asyncio.wait_for(llm.chat(
                model, [{"role": "user", "content": prompt}],
                response_format={"type": "json_object"}, max_tokens=PLAN_MAX_TOKENS, temperature=0.2,
                extra_body={"chat_template_kwargs": {"enable_thinking": False}}), PLAN_CALL_TIMEOUT)
        except Exception as e:  # noqa: BLE001
            log.warning("CAD plan call to %s failed: %s", model, str(e)[:200])
            continue
        if text.strip():
            return text, model
        log.warning("CAD plan call to %s returned no text", model)
    return "", ""


async def research_plan(description: str) -> tuple[P.GeometryPlan, P.PlanValidation, int]:
    """Ask Nemotron for a plan, retrying once with the validation errors fed back (as Loomin)."""
    feedback: str | None = None
    best: P.GeometryPlan | None = None
    best_v = P.validate_geometry_plan(None)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        prompt = P.build_plan_prompt(description, feedback)
        text, source = await _ask(prompt)
        plan = P.coerce_geometry_plan(P.extract_json(text), description, source)
        validation = P.validate_geometry_plan(plan)
        if validation.valid and plan is not None:
            log.info("CAD plan %r -> %d parts (attempt %d)", description[:60], len(plan.parts), attempt)
            return plan, validation, attempt
        if plan is not None and (best is None or len(validation.errors) < len(best_v.errors)):
            best, best_v = plan, validation
        feedback = "\n".join(f"- {e}" for e in validation.errors)
        log.warning("CAD plan attempt %d rejected: %s", attempt, "; ".join(validation.errors))
    raise CadError(f"no valid plan: {'; '.join(best_v.errors)}")


async def generate_model(description: str) -> dict:
    description = " ".join((description or "").split())[:400]
    if not description:
        raise CadError("empty description")
    t0 = time.monotonic()
    plan, _validation, attempts = await research_plan(description)
    t_plan = time.monotonic()
    scad = P.plan_to_openscad(plan)
    glb = await asyncio.to_thread(R.render_glb, scad)
    t_render = time.monotonic()
    model_id = uuid.uuid4().hex[:12]
    put_glb(model_id, glb)
    parts = len(P.expand_mirrors(plan.parts))
    ms = {"plan": round((t_plan - t0) * 1000), "render": round((t_render - t_plan) * 1000),
          "total": round((t_render - t0) * 1000)}
    log.info("CAD model %s: %d parts, %d bytes, %s", model_id, parts, len(glb), ms)
    return {"id": model_id, "url": model_url(model_id), "parts": parts, "ms": ms,
            "attempts": attempts, "names": [p.name for p in plan.parts]}
