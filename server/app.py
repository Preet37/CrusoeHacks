"""FastAPI app: /health, /state (debug), and the meeting WebSocket at /ws."""
import logging

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

import protocol
from protocol import Error, Status
from session import SESSIONS, get_session

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("twin.app")

app = FastAPI(title="Twin")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/state")
async def state(room: str = "demo"):
    session = SESSIONS.get(room)
    if session is None:
        return {"room": room, "exists": False}
    return {"exists": True, **session.debug_state()}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket, room: str = "demo", user: str = "guest"):
    await ws.accept()
    session = get_session(room)
    session.add_socket(ws, user)
    log.info("[%s] %s connected (%d sockets)", room, user, len(session.sockets))
    try:
        await session.send_to(ws, Status(state=session.status))
        await session.send_to(ws, session.snapshot_message())
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
        log.info("[%s] %s disconnected (%d sockets)", room, user, len(session.sockets))
