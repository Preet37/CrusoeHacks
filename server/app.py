"""FastAPI app: /health and the meeting WebSocket at /ws."""
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Twin")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/health")
async def health():
    return {"ok": True}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket, room: str = "demo", user: str = "guest"):
    await ws.accept()
    await ws.send_json({"type": "status", "state": "listening"})
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
