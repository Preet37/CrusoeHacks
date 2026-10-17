"""FastAPI app: /health, /state (debug), /calendar, /simulate/calendar_join, and the meeting WebSocket at /ws."""
import json
import logging

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

import protocol
from protocol import Error, Status
import mouth
from session import DEMO_CALENDAR, SESSIONS, get_session

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("twin.app")

app = FastAPI(title="Twin")
# Two-laptop demo: any origin (LAN IP, cloudflared tunnel) may call the API.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.on_event("startup")
async def _prewarm_acks() -> None:
    """Cache Twin's short acknowledgments so the first sound of a reply is instant."""
    import asyncio
    import sys

    if "pytest" in sys.modules:
        return  # never touch the network from tests
    from agents import responder
    from voice import tts

    async def run() -> None:
        try:
            n = await tts.prewarm(responder.ACKS)
            log.info("prewarmed %d ack phrases", n)
        except Exception:  # noqa: BLE001
            log.exception("ack prewarm failed")

    asyncio.get_running_loop().create_task(run())


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/state")
async def state(room: str = "demo"):
    session = SESSIONS.get(room)
    if session is None:
        return {"room": room, "exists": False}
    return {"exists": True, **session.debug_state()}


@app.get("/calendar")
async def calendar():
    try:
        return json.loads(DEMO_CALENDAR.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"meetings": []}


@app.post("/simulate/calendar_join")
async def simulate_calendar_join(room: str = "demo", meeting: str = "Daily standup"):
    """Demo button: same flow as a `calendar_join` message from the browser."""
    session = SESSIONS.get(room)
    if session is None or not session.sockets:
        return {"ok": False, "error": f"no one is connected to room {room!r}"}
    brief = await session.calendar_join(meeting)
    return {"ok": True, "brief": brief}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket, room: str = "demo", user: str = "guest"):
    await ws.accept()
    session = get_session(room)
    mouth.attach(session)
    session.add_socket(ws, user)
    log.info("[%s] %s connected (%d sockets)", room, user, len(session.sockets))
    try:
        await session.send_to(ws, Status(state=session.status))
        await session.send_to(ws, session.snapshot_message())
        await session.send_initial_state(ws)
        while True:
            frame = await ws.receive()
            if frame["type"] == "websocket.disconnect":
                break
            raw = frame.get("text") if frame.get("text") is not None else frame.get("bytes")
            if raw is None:
                continue
            try:
                msg = protocol.parse(raw)
            except ValueError as e:
                await session.send_to(ws, Error(message=str(e)[:500], recoverable=True))
                continue
            try:
                await session.handle_message(ws, session.sockets.get(ws, user), msg)
            except Exception as e:  # noqa: BLE001 - never kill the socket over one message
                log.exception("[%s] error handling %s", room, msg.type)
                await session.send_to(ws, Error(message=f"server error: {e}"[:500], recoverable=True))
    except WebSocketDisconnect:
        pass
    finally:
        session.remove_socket(ws)
        try:
            await session.broadcast_participants()
        except Exception:  # noqa: BLE001
            log.exception("[%s] participants broadcast failed", room)
        if not session.sockets:
            await mouth.detach(session)
        log.info("[%s] %s disconnected (%d sockets)", room, user, len(session.sockets))
