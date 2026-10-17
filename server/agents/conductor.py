"""M1 routing from extracted moments to specialist jobs."""

import asyncio
import logging

import config
from agents.diagrammer import Diagrammer, StaleBoardError, emit_event

log = logging.getLogger(__name__)


class Conductor:
    def __init__(self, diagrammer=None):
        self.diagrammer = diagrammer or Diagrammer()

    async def handle(self, session, moments: list[dict], parent_id: str | None = None) -> None:
        event_id = await emit_event(session, "Conductor", config.MODEL_SMART, "started",
                                    f"route {len(moments)} moment(s)", parent_id=parent_id)
        try:
            facts = [m for m in moments if m.get("type") == "system_fact"]
            if facts:
                await self.diagrammer.handle(session, facts, parent_id=event_id)
            await emit_event(session, "Conductor", config.MODEL_SMART, "done",
                             f"routed {len(facts)} system fact(s)", event_id=event_id,
                             parent_id=parent_id)
        except StaleBoardError:
            await emit_event(session, "Conductor", config.MODEL_SMART, "cancelled",
                             "board changed during drawing", event_id=event_id,
                             parent_id=parent_id)
            raise
        except asyncio.CancelledError:
            await emit_event(session, "Conductor", config.MODEL_SMART, "cancelled",
                             "cancelled", event_id=event_id, parent_id=parent_id)
            raise
        except Exception as exc:
            log.exception("Conductor failed")
            await emit_event(session, "Conductor", config.MODEL_SMART, "failed",
                             str(exc)[:160], event_id=event_id, parent_id=parent_id)
            raise
