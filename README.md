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

## Testing and benchmarks

**Track: Version N+1** (Twin improves itself and proves it on held-out meetings). Shared Brain is the secondary story.

- **Model benchmarks:** [`BENCHMARKS.md`](BENCHMARKS.md), with real Crusoe runs per Nemotron model (Lightning, Nano, Super, Super-VL) and task: extraction F1, visual routing, fact-check catch rate, 3D plan validity, p50/p95 latency and tokens. Raw JSON is committed next to it.
- **Unit and contract tests:** `cd server && .venv/bin/pytest -q` gives 436 passed at the 3:12 freeze, covering protocol round-trips of every PROTOCOL.md example, turn-taking rules, barge-in, echo guard, TTS streaming, workspace auth, human edits and golden draw ops. `cd web && npx tsc -b` type-checks the app. `npm run test:e2e` runs the Playwright mock smoke test.
- **Live end to end:** `cd server && .venv/bin/python scripts/e2e_text.py --url ws://localhost:8000 --strict` drives a real meeting over WebSocket against the live models, 12 steps with PASS/FAIL and latencies. Best run was 11/12. Measured: instant ack 16-18 ms after an addressed line, barge-in stop_audio in 37-103 ms, first shape about 2-10 s depending on Crusoe load.
- **Per-turn latency** is logged to `server/learning/runs/latency.jsonl` (final -> first token -> first sentence -> audio).
- **Golden board ops:** `server/tests/golden_ops.json`, replayed in the browser at `web/golden-dev.html`.
