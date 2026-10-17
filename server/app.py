"""FastAPI app: /health, /state (debug), /calendar, /simulate/calendar_join, and the meeting WebSocket at /ws."""
import hmac
import json
import os
import logging
from pathlib import Path

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


METRICS_FILE = Path(__file__).resolve().parent / "learning" / "runs" / "latest_metrics.json"


def load_metrics() -> list[dict]:
    """Coach results (M3) written by PREET-CODEX: a JSON array of protocol `metric` messages."""
    try:
        data = json.loads(METRICS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if data is None and METRICS_FILE.name == "latest_metrics.json":
        # Fresh clone: no live run file yet, use the committed Coach evidence (PREET-CODEX).
        try:
            from learning.evidence_metrics import load_committed_metrics
            data = load_committed_metrics()
        except Exception:  # noqa: BLE001
            data = []
    out = []
    for m in data if isinstance(data, list) else []:
        try:
            msg = protocol.parse({**m, "type": "metric"}) if isinstance(m, dict) else None
        except ValueError:
            continue
        if msg is not None and msg.type == "metric":
            out.append(msg)
    return out


@app.post("/metrics/refresh")
async def metrics_refresh(room: str = "demo"):
    """Re-send the latest Coach metrics to everyone in the room (Scoreboard updates live)."""
    session = SESSIONS.get(room)
    metrics = load_metrics()
    if session is not None:
        for m in metrics:
            await session.broadcast(m)
    return {"ok": True, "sent": len(metrics) if session else 0}


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


def workspace_table() -> dict[str, str]:
    """WORKSPACES=name:code,name2:code2 (server/.env). Empty = no workspaces, rooms stay isolated."""
    out: dict[str, str] = {}
    for item in os.getenv("WORKSPACES", "").split(","):
        name, _, code = item.strip().partition(":")
        if name.strip() and code.strip():
            out[name.strip().lower()] = code.strip()
    return out


def check_workspace(workspace: str, code: str) -> tuple[str | None, str | None]:
    """(trusted_workspace, error). Only a valid invite code makes a workspace trusted."""
    workspace = (workspace or "").strip().lower()
    if not workspace:
        return None, None
    table = workspace_table()
    if not table:
        return None, None  # workspaces not configured: everything still works, rooms isolated
    expected = table.get(workspace)
    if expected is None or not hmac.compare_digest(expected, (code or "").strip()):
        return None, f"Wrong invite code for workspace {workspace}"
    return workspace, None


def room_owner(room: str) -> str | None:
    """Which workspace a room belongs to: the live session's workspace, else a configured `<ws>-` prefix."""
    live = SESSIONS.get(room)
    if live is not None and getattr(live, "workspace", None):
        return live.workspace
    r = (room or "").lower()
    for name in workspace_table():
        if r.startswith(f"{name}-"):
            return name
    return None


def room_access_error(room: str, trusted: str | None) -> str | None:
    """A workspace room needs that workspace's valid invite code on every join."""
    owner = room_owner(room)
    if owner is None or owner == trusted:
        return None
    return f"Room {room} belongs to workspace {owner}: wrong or missing invite code"


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket, room: str = "demo", user: str = "guest", workspace: str = "", code: str = ""):
    await ws.accept()
    trusted, ws_error = check_workspace(workspace, code)
    if not ws_error:
        ws_error = room_access_error(room, trusted)
    if ws_error:
        await ws.send_text(json.dumps(protocol.dump(Error(message=ws_error, recoverable=False))))
        await ws.close(code=4403)
        return
    session = get_session(room)
    if trusted and getattr(session, "workspace", None) is None:
        session.workspace = trusted  # set once, BEFORE the brain attaches; never reassigned
        if getattr(session, "_mind_runtime_ready", False):
            try:
                from brain import runtime as mind_runtime
                mind_runtime.attach(session)
            except Exception:  # noqa: BLE001
                log.exception("brain re-attach for workspace failed")
    mouth.attach(session)
    session.add_socket(ws, user)
    log.info("[%s] %s connected (%d sockets)", room, user, len(session.sockets))
    try:
        await session.send_to(ws, Status(state=session.status))
        await session.send_to(ws, session.snapshot_message())
        for metric in load_metrics():  # M3 scoreboard: v1 vs coached version on the locked test
            await session.send_to(ws, metric)
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
