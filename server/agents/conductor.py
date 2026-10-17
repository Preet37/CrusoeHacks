"""M1 routing from extracted moments to specialist jobs."""

import asyncio
import logging

import config
from agents.diagrammer import Diagrammer, StaleBoardError, emit_event

log = logging.getLogger(__name__)


class Conductor:
    def __init__(self, diagrammer=None):
        self.diagrammer = diagrammer or Diagrammer()

    async def handle(self, session, moments: list[dict], parent_id: str | None = None,
                     *, topic_id: str | None = None, title: str | None = None,
                     topic_changed: bool = False,
                     source_lines: list[dict] | None = None) -> None:
        source_lines = (source_lines or [])[-12:]
        source_ids = ",".join(str(line.get("line_id", "")) for line in source_lines)
        detail = f"route {len(moments)} moment(s)"
        if topic_id:
            detail += f" topic={topic_id}"
        if source_ids:
            detail += f" lines={source_ids}"
        event_id = await emit_event(session, "Conductor", config.MODEL_SMART, "started",
                                    detail[:200], parent_id=parent_id)
        try:
            facts = [m for m in moments if m.get("type") == "system_fact"]
            if facts or (topic_id is not None and topic_changed):
                if topic_id is None:
                    await self.diagrammer.handle(session, facts, parent_id=event_id)
                else:
                    await self.diagrammer.handle(session, facts, parent_id=event_id,
                                                  topic_id=topic_id, title=title,
                                                  topic_changed=topic_changed,
                                                  source_lines=source_lines)
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
