# Twin

A personal agent that joins your meetings as a real participant. It knows your codebase, your projects and you. It speaks up, draws on the whiteboard, checks claims, turns decisions into tasks, and rewrites its own playbook after every meeting so it measurably gets better.

Not a note-taker: note-takers remember your meetings. Twin understands them.

Built at the Recursive Agents Hackathon (Cerebral Valley x Crusoe, Oct 17 2026). Track: **Version N+1**. All reasoning runs on **NVIDIA Nemotron via Crusoe Managed Inference**.

## Start here
- `ARCHITECTURE.md`: the full plan
- `PROTOCOL.md`: browser/server messages
- `TASKS.md`: who is building what
- `AGENT_CHAT.txt`: coordination log between our coding agents
- `KICKOFF_PROMPTS.md`: first prompts for each coding agent

## Run (once built)
```
cd server && pip install -r requirements.txt && cp .env.example .env   # fill in keys locally, never commit
uvicorn app:app --reload --port 8000
cd web && npm install && npm run dev                                     # open http://localhost:5173 in Chrome
```

## Two laptops

Both people join the same room (`/room/demo`). The server runs on laptop A only.

1. **Laptop A (server + web):**
   ```bash
   cd server && .venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
   cd web && npm run dev -- --host        # --host exposes Vite on the LAN (port 5173)
   ```
   Find A's LAN IP: `ipconfig getifaddr en0`.
2. **Laptop B, same Wi-Fi:** open `http://<A-ip>:5173/room/demo`. The web app connects to the same host on port 8000 by default.
3. **Different networks (or Wi-Fi blocks peer traffic):** on A run `cloudflared tunnel --url http://localhost:8000`, then start the web app with `VITE_SERVER_URL=wss://<tunnel-host> npm run dev -- --host` (B can use its own local web dev server with the same variable). CORS on the server allows all origins.
4. Check: both laptops see each other in the People panel (`participants` message); `curl http://<A-ip>:8000/state?room=demo` lists both users.

Calendar demo: `curl -X POST "http://localhost:8000/simulate/calendar_join?room=demo&meeting=Daily%20standup"` makes Twin join, send its `brief`, and say hi (`GET /calendar` lists the demo meetings).
