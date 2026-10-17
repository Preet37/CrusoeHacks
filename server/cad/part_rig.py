"""POST /cad/part-rig: which parts of a library GLB move, boxed in two orthographic views.

Adapted from LoominPhysicsSandbox (Preet37, pre-existing project) app/api/part-rig/route.ts.
The browser renders the model front + top, the vision model (MODEL_COACH) boxes the moving
parts in both, and the browser cuts those parts into pivots and spins them. Results are cached
per GLB key in memory and on disk beside the CAD library so each model is rigged once.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from pathlib import Path

from fastapi import APIRouter, Request

import config
import llm
from cad.library import library_dir

log = logging.getLogger(__name__)
router = APIRouter()

TIMEOUT_S = 8
VIEWS = ("front", "top")
KINDS = ("spin", "tilt", "static")
MAX_VIEW_B64 = 400_000
_memory: dict[str, dict] = {}

PROMPT = """Two orthographic renders of ONE 3D model of {topic}, each image square on a plain background.
Image 1 = "front" (camera on a horizontal axis), image 2 = "top" (camera looking straight down).
Find the parts that visibly MOVE on a real {topic} (wind turbine blades/rotor, propellers, wheels, fans)
plus at most 3 notable static parts worth labelling. Box each part in BOTH images.
Return ONLY JSON: {{"parts":[{{"name":"rotor","kind":"spin","axis":"z","boxes":{{"front":[x0,y0,x1,y1],"top":[x0,y0,x1,y1]}}}}]}}
kind is "spin", "tilt" or "static"; axis is x, y or z (spin axle, model space: x right, y up, z toward front camera).
Coordinates are fractions 0..1 of the image, origin top-left, tight around the part. At most 6 parts.
A rotor with blades is ONE part. If nothing moves, return {{"parts":[]}}."""


def _cache_dir() -> Path:
    return library_dir().parent / "part_rigs"


def _key(raw: str) -> str:
    return hashlib.sha1(raw.split("?")[0].encode()).hexdigest()[:20]


def _read(key: str) -> dict | None:
    if key in _memory:
        return _memory[key]
    try:
        data = json.loads((_cache_dir() / f"{key}.json").read_text("utf-8"))
    except (OSError, ValueError):
        return None
    _memory[key] = data
    return data


def _write(key: str, data: dict) -> None:
    _memory[key] = data
    try:
        _cache_dir().mkdir(parents=True, exist_ok=True)
        (_cache_dir() / f"{key}.json").write_text(json.dumps(data), "utf-8")
    except OSError as exc:
        log.warning("part-rig cache write failed: %s", exc)


def _box(b: object) -> list[float] | None:
    if not isinstance(b, list) or len(b) != 4:
        return None
    try:
        n = [float(x) for x in b]
    except (TypeError, ValueError):
        return None
    mx = max(n)
    scale = 1 if mx <= 1.5 else 448 if mx <= 470 else 1000
    x0, y0, x1, y1 = (min(1.0, max(0.0, v / scale)) for v in n)
    if abs(x1 - x0) < 0.01 or abs(y1 - y0) < 0.01:
        return None
    return [round(min(x0, x1), 4), round(min(y0, y1), 4), round(max(x0, x1), 4), round(max(y0, y1), 4)]


def parse_parts(text: str) -> list[dict]:
    clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    at = clean.rfind('{"parts"')
    raw = clean[at:clean.rfind("}") + 1] if at >= 0 else clean
    try:
        value = json.loads(raw)
    except ValueError:
        m = re.findall(r"\{[\s\S]*\}", clean)
        try:
            value = json.loads(m[-1]) if m else {}
        except ValueError:
            return []
    out = []
    for p in value.get("parts", []) if isinstance(value, dict) else []:
        if not isinstance(p, dict) or not str(p.get("name") or "").strip():
            continue
        kind = p.get("kind") if p.get("kind") in KINDS else "static"
        axis = p.get("axis") if p.get("axis") in ("x", "y", "z") else None
        boxes = {v: b for v in VIEWS if (b := _box((p.get("boxes") or {}).get(v)))}
        if len(boxes) != 2:
            continue
        out.append({"name": str(p["name"]).strip()[:30], "kind": kind, "axis": axis, "boxes": boxes})
    return out[:6]


async def rig(key: str, topic: str, views: dict) -> dict:
    """Cached rig for one GLB; {parts: []} on any failure (never cached)."""
    k = _key(key)
    hit = _read(k)
    if hit is not None:
        return {**hit, "cached": True}
    imgs = []
    for v in VIEWS:
        img = views.get(v) if isinstance(views, dict) else None
        if not isinstance(img, str) or not img or len(img) > MAX_VIEW_B64:
            return {"parts": []}
        imgs.append(img if img.startswith("data:image/") else "data:image/jpeg;base64," + img)
    try:
        text = await asyncio.wait_for(llm.chat(config.MODEL_COACH, [{"role": "user", "content": [
            {"type": "text", "text": PROMPT.format(topic=topic or "an object")},
            *({"type": "image_url", "image_url": {"url": u}} for u in imgs),
        ]}], max_tokens=900, temperature=0.1,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}}), timeout=TIMEOUT_S)
        parts = parse_parts(text)
    except Exception as exc:  # timeout, API error, bad JSON: no rig this time
        log.warning("part-rig failed: %s", type(exc).__name__)
        return {"parts": []}
    result = {"parts": parts}
    if parts:
        _write(k, result)
    return result


@router.post("/cad/part-rig")
async def part_rig(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    key = str(body.get("key") or body.get("url") or "")[:500]
    if not key:
        return {"parts": []}
    return await rig(key, str(body.get("topic") or "")[:80], body.get("views") or {})
