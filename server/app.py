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


# ---------------------------------------------------------------- PC-23 integrations + tickets
# Registered before the persistence router so GET /tickets also includes live drafts
# (same {"tickets": [...]} shape and the same workspace+code auth).
def _ticket_scope(workspace: str, code: str, room: str) -> str | None:
    from fastapi import HTTPException
    if workspace:
        trusted, error = check_workspace(workspace, code)
        if error or not trusted:
            raise HTTPException(403, error or "Wrong invite code")
        return trusted
    if not room:
        raise HTTPException(401, "Workspace and invite code are required")
    if room_owner(room):
        raise HTTPException(403, f"Room {room} belongs to a workspace: invite code required")
    return None


@app.get("/integrations")
async def integrations_status(workspace: str = "", code: str = ""):
    from integrations import jira
    github = None
    trusted, error = check_workspace(workspace, code) if workspace else (None, None)
    if trusted and not error:
        try:
            from brain.workspace_store import get_workspace
            repo = get_workspace(trusted).get("repo_url")
            github = {"connected": bool(repo), "repo_url": repo}
        except Exception:  # noqa: BLE001
            log.exception("workspace repo lookup failed")
    return {"jira": await jira.status(), "github": github,
            "slack": {"connected": bool(os.getenv("SLACK_WEBHOOK_URL"))}}


@app.get("/tickets")
async def tickets_list(workspace: str = "", code: str = "", room: str = ""):
    import integrations
    return {"tickets": integrations.list_tickets(_ticket_scope(workspace, code, room), room or None)}


@app.post("/tickets/{ticket_id}/push_jira")
async def tickets_push_jira(ticket_id: str, workspace: str = "", code: str = "", room: str = ""):
    from fastapi.responses import JSONResponse
    import integrations
    trusted = _ticket_scope(workspace, code, room)
    try:
        ticket, session = await integrations.apply_action(ticket_id, "push_jira",
                                                          workspace=trusted, room=room or None)
    except integrations.TicketError as e:
        return JSONResponse({"error": str(e)}, status_code=e.status)
    if session is not None:
        await session.broadcast(integrations.wire(ticket))
    return ticket


from brain.persistence_api import router as persistence_router
app.include_router(persistence_router)
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
            n = await tts.prewarm(list(responder.ACKS) + [mouth.SKETCH_ACK])
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


@app.get("/cad/{model_id}.glb")
async def cad_model(model_id: str):
    """GLB built by server/cad (rough-then-refine 3D). 404 for unknown ids."""
    from fastapi import HTTPException, Response
    from cad.pipeline import get_glb

    data = get_glb(model_id)
    if data is None:
        raise HTTPException(status_code=404, detail="unknown model")
    return Response(content=data, media_type="model/gltf-binary",
                    headers={"Cache-Control": "public, max-age=86400"})


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
        from brain.workspace_auth import authenticate
        if authenticate(workspace, (code or '').strip()):
            return workspace, None
        return None, f"Wrong invite code for workspace {workspace}"
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
    from brain.workspace_auth import workspace_names
    names = set(workspace_table()) | set(workspace_names())
    for name in sorted(names, key=len, reverse=True):
        if r.startswith(f"{name}-"):
            return name
    return None


def room_access_error(room: str, trusted: str | None) -> str | None:
    """A workspace room needs that workspace's valid invite code on every join."""
    owner = room_owner(room)
    if trusted and not (room or '').lower().startswith(trusted + '-'):
        return 'Room is outside the authenticated workspace; check workspace and invite code'
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
    try:
        from cad import attach as cad_attach
        cad_attach(session)  # add_3d primitives -> real CAD GLB, swapped in place
    except Exception:  # noqa: BLE001 - the meeting must work without CAD
        log.exception("cad attach failed")
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
            if isinstance(raw, str) and '"ticket_action"' in raw:
                try:
                    data = json.loads(raw)
                except ValueError:
                    data = None
                if isinstance(data, dict) and data.get("type") == "ticket_action":
                    await session.ticket_action(ws, data)  # PC-23 (v2.2 proposed message)
                    continue
            if isinstance(raw, str) and '"annotate"' in raw and len(raw) <= 64_000:
                try:
                    data = json.loads(raw)
                except ValueError:
                    data = None
                if isinstance(data, dict) and data.get("type") == "annotate":
                    # Human annotation (screen share or board), Zoom-style: everyone sees it with who drew it.
                    data["by"] = session.sockets.get(ws, user) or "guest"
                    await session.broadcast_except(ws, data)
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
            if not session.sockets:
                try:
                    from brain.meeting_store import save_meeting
                    save_meeting(session)
                except Exception:
                    log.exception('[%s] could not save meeting summary', room)
                # Nobody left: the next join is a brand-new meeting (fresh history, board, topic).
                # The summary is already saved; the workspace repo/memory reload on join.
                if not session.sockets and SESSIONS.get(room) is session:
                    SESSIONS.pop(room, None)
                    log.info("[%s] room empty: meeting ended, next join starts fresh", room)
        log.info("[%s] %s disconnected (%d sockets)", room, user, len(session.sockets))
