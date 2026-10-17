"""Meeting hook (written for Twin): rough primitives first, real CAD model replaces them in place.

When the Diagrammer broadcasts `add_3d` (rough primitives), a background job asks
cad.pipeline for a real CAD model; on success Twin broadcasts
    {"type":"draw","op":"update","id":<same id>,"scene":{"gltf":url,"autoRotate":true},"title":...}
so the browser swaps the primitives for the GLB. On failure the primitives stay.

Rules: one job per add_3d id (no duplicates), at most one running job per room
(the newest waiting request is queued behind it, older waiting ones dropped),
90 s timeout per job.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any

log = logging.getLogger("twin.cad")

JOB_TIMEOUT = 90.0
_STATE_ATTR = "_cad_hook"


class _CadHook:
    def __init__(self, session) -> None:
        self.session = session
        self.seen: set[str] = set()
        self.running: str | None = None
        self.pending: tuple[str, str, str] | None = None
        self.jobs_started = 0

    # sync broadcast observer: must never raise or block
    def on_broadcast(self, data: Any) -> None:
        if not isinstance(data, dict) or data.get("type") != "draw":
            return
        if data.get("op") == "clear":  # ids may be reused on a fresh board
            self.reset()
            return
        if data.get("op") != "add_3d":
            return
        item_id = str(data.get("id") or "")
        scene = data.get("scene") if isinstance(data.get("scene"), dict) else {}
        if not item_id or item_id in self.seen or scene.get("gltf"):
            return
        self.seen.add(item_id)
        title = str(data.get("title") or "").strip()
        description = self._description(title)
        if not description:
            return
        if self.running is not None:
            if self.pending is not None:
                log.info("[%s] CAD: dropping queued request %s for newer %s",
                         self.session.room, self.pending[0], item_id)
            self.pending = (item_id, title, description)
            return
        self._start(item_id, title, description)

    def _description(self, title: str) -> str:
        parts = [title] if title else []
        history = getattr(self.session, "history", None) or []
        if history:
            last = history[-1]
            try:
                from session import TWIN_SPEAKER
            except Exception:  # noqa: BLE001
                TWIN_SPEAKER = "Twin"
            text = str(last.get("text") or "").strip() if isinstance(last, dict) else ""
            if text and last.get("speaker") != TWIN_SPEAKER and text.lower() != title.lower():
                parts.append(text)
        return ". ".join(parts)[:400]

    def _start(self, item_id: str, title: str, description: str) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            log.warning("CAD: no running loop; skipping %s", item_id)
            return
        self.running = item_id
        self.jobs_started += 1
        self.session.spawn(f"cad:{item_id}", self._job(item_id, title, description))

    async def _job(self, item_id: str, title: str, description: str) -> None:
        from . import pipeline

        try:
            result = await asyncio.wait_for(pipeline.generate_model(description), JOB_TIMEOUT)
            op = {"type": "draw", "op": "update", "id": item_id,
                  "scene": {"gltf": result["url"], "autoRotate": True}}
            if title:
                op["title"] = title
            try:  # the board only accepts https glTF URLs; a local http URL is broadcast-only
                self.session.board.apply({k: v for k, v in op.items() if k != "type"})
            except Exception as e:  # noqa: BLE001
                log.info("[%s] CAD: board kept primitives for %s (%s)", self.session.room, item_id, e)
            await self.session.broadcast(op)
            log.info("[%s] CAD model for %s: %s parts in %s ms", self.session.room, item_id,
                     result.get("parts"), (result.get("ms") or {}).get("total"))
        except asyncio.CancelledError:
            self.pending = None
            raise
        except asyncio.TimeoutError:
            log.warning("[%s] CAD job %s timed out after %.0fs; keeping primitives",
                        self.session.room, item_id, JOB_TIMEOUT)
        except Exception as e:  # noqa: BLE001
            log.warning("[%s] CAD job %s failed; keeping primitives: %s", self.session.room, item_id, e)
        finally:
            if self.running == item_id:
                self.running = None
            nxt, self.pending = self.pending, None
            if nxt is not None:
                self._start(*nxt)

    def reset(self) -> None:
        self.seen.clear()
        self.pending = None


def _enabled_by_default() -> bool:
    if os.getenv("TWIN_CAD", "1").strip().lower() in ("0", "false", "off", "no"):
        return False
    return "pytest" not in sys.modules  # never start real model/render jobs from unrelated tests


def attach(session, force: bool = False) -> _CadHook | None:
    """Register the CAD observer on a session (idempotent). Disabled by TWIN_CAD=0 and under pytest."""
    if not (force or _enabled_by_default()):
        return None
    hook = getattr(session, _STATE_ATTR, None)
    if hook is None:
        hook = _CadHook(session)
        setattr(session, _STATE_ATTR, hook)
        on_reset = getattr(session, "on_reset", None)
        if callable(on_reset):
            on_reset(lambda _s: hook.reset())
    observers = getattr(session, "_broadcast_observers", None)
    if observers is not None and hook.on_broadcast not in observers:
        observers.append(hook.on_broadcast)
    return hook
