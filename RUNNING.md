# RUNNING.md: run Twin on your laptop and test it in Chrome

Everything runs on your own machine. You open it in **Google Chrome** (speech recognition works best there). Two terminals: one for the server, one for the web app.

---

## 0. One-time setup (about 5 minutes)

You need: **Python 3.11 or newer** (`python3 --version`), **Node.js 20 or newer** (`node --version`), **Chrome**, and ideally **headphones**.

```bash
git clone https://github.com/Preet37/CrusoeHacks.git
cd CrusoeHacks
```

### Server (Python)
```bash
cd server
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```
Open `server/.env` in any editor and fill in (never commit this file, never paste keys in chat):

| Variable | Where to get it | Needed? |
|---|---|---|
| `CRUSOE_API_KEY` | Crusoe console > Intelligence Foundry > API Keys | **Required** (all thinking) |
| `NVIDIA_API_KEY` | build.nvidia.com > Magpie TTS model page > Get API key (free) | Recommended (Twin's real voice; without it the browser's robotic voice is used) |
| `TYPESAFE_API_KEY` | console.typesafe.ai/keys (JEV, may be early access) | Optional (faster reflex layer; Nemotron Nano is used without it) |

Leave the `MODEL_*` lines as they are (they are the verified Crusoe model ids).

### Web app (Node)
```bash
cd ../web
npm install
```

---

## 1. Check the keys work (1 minute)

From `server/`:
```bash
.venv/bin/python scripts/smoke_crusoe.py     # lists Nemotron models, one reply per model, timings, tool calling OK
.venv/bin/python scripts/smoke_tts.py        # only if NVIDIA_API_KEY is set: writes out.wav, play it to hear Twin's voice
```
If `smoke_crusoe.py` fails: wrong key type (must be an Intelligence Foundry inference key) or no credits.

---

## 2. Start it (every time)

**Terminal 1, server:**
```bash
cd server
.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
```
Check: open http://localhost:8000/health in Chrome; it should say ok. Debug view of a room: http://localhost:8000/state?room=demo

**Terminal 2, web app:**
```bash
cd web
npm run dev
```
Then open **http://localhost:5173** in **Chrome**.

1. Type your name, allow the microphone (and camera) when Chrome asks, click **Join**.
2. You land in `/room/demo`. Twin's tile is in the call, labeled AI.
3. **Put on headphones** (otherwise Twin hears itself through your mic).

---

## 3. Test it: the M1 script (say these out loud)

| # | Say this | What should happen |
|---|---|---|
| 1 | "So our web app talks to an auth service, and auth stores users in Postgres." | Within a few seconds three boxes (web app, auth, Postgres) and two arrows appear on the whiteboard, one by one. |
| 2 | (pause about 2 seconds) | Twin mentions what it drew at the pause ("I sketched web, auth and Postgres..."). |
| 3 | "Actually auth also uses Redis for sessions." | A Redis box and arrow appear. Auth is NOT duplicated. |
| 4 | "Twin, does that look right?" | Twin answers out loud in one or two short sentences. |
| 5 | Start talking while Twin is mid-sentence. | Twin stops immediately. |
| 6 | "Twin, walk me through the login sequence." | A Mermaid sequence diagram appears. |
| 7 | "Our p95 latency went from 420 to 180 milliseconds after the cache." | A chart appears. |
| 8 | "What would the new login screen look like?" | A screen mockup card appears. |
| 9 | "OK, different topic: let's talk about billing." | The board starts a new topic area and pans to it. |
| 10 | "Not now, Twin." | Twin's tile shows a raised hand and it stays quiet for a while. |
| 11 | Simulate a scheduled standup: `curl -X POST "http://localhost:8000/simulate/calendar_join?room=demo&meeting=Daily%20standup"` (or the Connections tab > Calendar > Simulate). | Twin joins, shows its pre-meeting brief, and says hi. |
| 12 | Open the side panel > Twin activity. | The live agent tree shows Listener, Conductor, Diagrammer jobs nesting and finishing. |

Server latency for each turn is logged to `server/learning/runs/latency.jsonl` (use real numbers in the pitch).

---

## 4. Test without a server or without talking

- **Mock mode (no server, no keys):** http://localhost:5173/room/demo?mock=1 . A fake server plays a scripted meeting: captions, Twin states, a diagram building itself, rich shapes. Use this to check the UI.
- **Board-only page:** http://localhost:5173/board-dev.html . The whiteboard alone, for testing shapes.
- **Typing instead of talking:** if the mic is flaky, use push-to-talk (hold space) or the text box in the UI if present; or send text from a terminal with the e2e script below.

---

## 5. Automated tests

```bash
cd server && .venv/bin/python -m pytest -q          # all server tests (offline, fake model calls, a few seconds)
cd web && npm run build                             # type-checks the whole web app and builds it
cd web && npm run lint                              # lint
```
Live end-to-end test through the real server and Crusoe (no browser, no mic): see `server/scripts/e2e_text.py` once it exists (task T-1 in TASKS.md). It sends the M1 script as text over the WebSocket and checks that draws, transcript and audio messages come back.

---

## 6. Two laptops in the same call

1. Laptop A runs the server and web app (steps above).
2. On laptop A, start the web app with `npm run dev -- --host` (otherwise it only listens on localhost). Find laptop A's address: `ipconfig getifaddr en0` (Mac). On laptop B open `http://<that-address>:5173/room/demo`.
3. If the venue Wi-Fi blocks laptop-to-laptop traffic, on laptop A run a tunnel:
   `cloudflared tunnel --url http://localhost:8000` (install: `brew install cloudflared`). Then start the web app on laptop B with
   `VITE_SERVER_URL=wss://<the-tunnel-host> npm run dev` and open http://localhost:5173 on laptop B.
4. Both people join the same room name. Only ONE laptop should play Twin's voice out loud, and that one should use headphones.

---

## 7. Troubleshooting

| Problem | Fix |
|---|---|
| Chrome never asks for the mic / captions stay empty | Use Chrome (not Safari). Click the lock icon in the address bar > allow Microphone. Must be `localhost` or `https`. |
| Twin keeps interrupting itself or transcribes its own voice | Headphones. The echo guard helps but headphones fix it. |
| Twin's voice sounds robotic | No `NVIDIA_API_KEY`, so the browser voice is used. Add the key, restart the server. |
| Replies sometimes slow or fall back | The fast Lightning model can return "capacity unavailable"; Twin automatically falls back to Nano. Normal. |
| Drawing takes long | Check the server log and `latency.jsonl`; drawing should be one model call per burst. |
| `address already in use` | Something else is on port 8000 or 5173: `lsof -i :8000` then `kill <pid>`. |
| Web shows "disconnected" | Server not running, or wrong `VITE_SERVER_URL`. Check http://localhost:8000/health. |
| `ModuleNotFoundError` | Run commands from `server/` with `.venv/bin/...`, and re-run `pip install -r requirements.txt` after pulling. |
| After `git pull`, web errors | Re-run `npm install` in `web/`. |
