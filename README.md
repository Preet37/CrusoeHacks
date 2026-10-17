# Twin

**A teammate that joins your meeting, talks, draws, knows your code, and gets better every time.**

Twin is not a note-taker. Note-takers remember your meetings. Twin takes part in them. It joins the call with its own tile and voice, sketches the architecture while you talk, checks what people say against the real repo, turns decisions into tickets, and after every meeting a Coach proposes changes to Twin and keeps only the ones that score better on meetings it has never seen.

Built in one day at the **Recursive Agents Hackathon** (Cerebral Valley x Crusoe, Shack15 SF, Oct 17 2026).
**Track: Version N+1** (Twin improves itself, measured). Also touches Shared Brain (one memory of code and decisions for the whole team).
Every thinking call runs on **open NVIDIA Nemotron models via Crusoe Managed Inference**.

| | |
|---|---|
| Talks | Real voice (NVIDIA Magpie TTS). Waits its turn, stops when you talk over it |
| Draws | Boxes and arrows, Mermaid sequence diagrams, charts, tables, UI mockups, 3D scenes, live on a shared whiteboard |
| Knows your code | Connect a GitHub repo once per company. Twin maps files, owners and impact |
| Checks | Flags claims the code contradicts, citing the file and lines |
| Ships work | Meeting end: summary, decisions, to-dos and tickets on the dashboard |
| Learns | Coach edits Twin's prompts and turn-taking rules, scores them on held-out meetings, keeps only wins |

---

## How it works

```mermaid
flowchart LR
  subgraph Browser["Meeting room (Chrome)"]
    MIC[Mic + Web Speech] --> WS
    BOARD[Whiteboard<br/>tldraw + Mermaid + charts + Three.js]
    SCREEN[Screen share<br/>+ annotations]
    TILE[Twin tile + 3D orb]
  end
  WS((WebSocket)) --> REFLEX
  subgraph Server["Twin server (FastAPI)"]
    REFLEX[Reflex check<br/>for Twin? needs a visual? new topic?]
    REFLEX --> MOUTH[Mouth<br/>Responder streams a short reply]
    REFLEX --> MIND
    subgraph MIND["Mind (agent tree)"]
      L[Listener] --> C[Conductor]
      C --> D[Diagrammer]
      C --> F[Fact Checker]
      C --> A[Code Analyst]
      C --> T[Task Planner]
    end
    BRAIN[(Brain<br/>code map + sourced memory<br/>SQLite per workspace)]
    MIND <--> BRAIN
  end
  MOUTH --> TTS[Magpie TTS] --> TILE
  D --> BOARD
  F --> MOUTH
  T --> DASH[Dashboard<br/>summaries + tickets]
  subgraph Crusoe["Crusoe Managed Inference"]
    N1[Nemotron 3.5 Lightning]
    N2[Nemotron 3 Nano]
    N3[Nemotron 3 Super 120B]
    N4[Nemotron 3.5 Super VL]
  end
  MOUTH -.-> N1
  L -.-> N2
  D -.-> N3
  COACH[Coach] -.-> N4
```

**Mouth and mind.** The mouth never waits for the mind. You get a short spoken reply in about a second while the mind draws, checks the code and writes tasks in parallel. The mind's results are spoken at the next natural pause ("I sketched that, does it look right?").

### From your sentence to a diagram

```mermaid
sequenceDiagram
  participant You
  participant Reflex as Reflex check
  participant Board
  participant Nano as Nemotron Nano
  participant Super as Nemotron Super
  You->>Reflex: "Auth stores users in Postgres."
  Reflex-->>Board: ~0.1 s: needs a visual
  Board-->>You: ~0.3 s: "sketching" card + "Got it, sketching that now"
  Reflex->>Nano: extract parts and links (~1 s)
  Nano-->>Board: rough diagram, shapes stream in (~2 s warm)
  Nano->>Super: refine (stable ids)
  Super-->>Board: refined diagram updates in place
  You->>Board: rename "Auth" to "Identity"
  Board-->>Reflex: human edit is authoritative
  Reflex-->>You: Twin acknowledges and redraws around it
```

Speed rules: one model call per drawing burst, newest sentence wins (stale work is cancelled), instant acknowledgement, rough first then refine.

### Where the recursion is (Version N+1)

```mermaid
flowchart TB
  subgraph Live["In the meeting: agents that spawn agents"]
    M[One moment] --> CON[Conductor]
    CON --> J1[Diagrammer]
    CON --> J2[Fact Checker]
    CON --> J3[Code Analyst]
    J3 --> J4[find owner]
    J3 --> J5[impact]
  end
  subgraph After["After the meeting: the Coach"]
    R[Replay recorded meetings] --> E[Propose ONE edit<br/>prompt / playbook / turn-taking value]
    E --> S[Score on validation meetings]
    S -->|better| K[Keep: new version]
    S -->|worse| X[Reject and log]
    K --> LT[Report on locked test set]
  end
  Live -. transcripts .-> After
  After -. new playbook .-> Live
```

- Run time: the Conductor splits a moment into jobs, jobs can spawn sub-jobs (depth 3, max 12). The tree is visible live in the side panel.
- Learning: 12 scripted meetings (8 train, 2 validation, 2 locked test). Every Coach edit is logged with its score, kept or rejected.
- First real Coach run today: candidate edit scored F1 0.40 vs v1 0.60 on validation, so it was **rejected automatically**. Evidence: `server/learning/results/coach_prep_iteration_2026-10-17.json`. We report rejections; we do not claim gains we did not measure.

---

## Testing and benchmarks

How hard we tested the models and the system (numbers from today's live runs and logs):

| What | Result |
|---|---|
| Automated server tests | 436 passing at the 3:12 freeze (`cd server && .venv/bin/pytest -q`): protocol round-trips of every PROTOCOL.md example, turn-taking, barge-in, echo guard, TTS streaming, workspace auth, human edits, golden draw ops |
| Live end-to-end spoken script (`server/scripts/e2e_text.py --url ws://localhost:8000 --strict`) | real meeting over WebSocket against live Nemotron: 12/12 in the 14:08 strict run, 11/12 in later runs |
| Browser tests | `npm run test:e2e` (Playwright mock smoke + live dashboard flow); `npx tsc -b` type-checks the app |
| First sound after Twin is asked | 16 to 18 ms (was 2.7 s) |
| Stop when interrupted | 24 to 103 ms across runs |
| Understanding each sentence (Listener) | median 965 ms over 5 calls (was 9 to 12 s) |
| Drawing appears after you speak | sketch card 0.31 s; first shapes 2 to 10 s depending on Crusoe load (2.09 s best warm; was 7 to 24 s) |
| Reflex check per sentence | ~0.1 s warm |
| Per-turn latency log | `server/learning/runs/latency.jsonl` (final, first token, first sentence, audio) |
| Contract tests | every draw op has a golden JSON shared by server and web (`server/tests/golden_ops.json`, replayed at `web/golden-dev.html`) |

Per-model benchmarks (accuracy, p50/p95 latency, tokens per task, raw JSON) live in **`BENCHMARKS.md`** and `server/bench/` (run: `cd server && .venv/bin/python bench/run_bench.py`).

---

## You stay in control

Built after judge feedback this afternoon; check `TASKS.md` and `AGENT_CHAT.txt` for what landed before the deadline.

Built after judge feedback this afternoon; check `TASKS.md` and `AGENT_CHAT.txt` for what landed before the deadline.

- **Modes:** Off, On request (only when named), Proactive. The board stays hidden until it has content.
- **Permissions per company:** internet off, repo read-only, tickets and screen reading ask first, speaking unprompted off. The server refuses tools that are switched off.
- **Edit it yourself:** draw, rename, annotate on the board or on a shared screen. Human edits win; Twin reacts to them.
- **Echo guard:** Twin never treats its own voice as a person speaking.

## Companies, dashboard, memory

- **Login:** company workspace + invite code (set in `server/.env` as `WORKSPACES=name:code`). Each company only sees its own rooms.
- **Dashboard** (`/dashboard`): today's meetings with Join and auto-join, GitHub repo connected once and remembered, past meetings with summary, decisions, to-dos and tickets, and Twin's learning scores.
- **Memory:** SQLite per workspace (gitignored). Decisions are stored with who said them and when; newer decisions replace older ones.
- **Install as an app:** Chrome "Install Twin" opens it in its own window (PWA).

## Models

| Job | Model on Crusoe |
|---|---|
| Spoken reply | `nvidia/Nemotron-3.5-Lightning-30B-A3B` (fallback Nano) |
| Listen to every line, reflex decisions | `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` |
| Draw, route, analyze code, refine | `nvidia/NVIDIA-Nemotron-3-Super-120B-A12B` |
| Coach, read the shared screen | `nvidia/NVIDIA-Nemotron-3.5-Super-VL` |
| Voice | NVIDIA Magpie TTS (multilingual) |

Model ids live only in `server/config.py`. All calls go through `server/llm.py`.

---

## Run it

```bash
# server
cd server
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env        # add CRUSOE_API_KEY, NVIDIA_API_KEY (+ optional WORKSPACES). Never commit .env
.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000

# web (second terminal)
cd web && npm install && npm run dev
```
Open **http://localhost:5173/dashboard** in Chrome, log in, click Join. Use headphones. Full guide and troubleshooting: `RUNNING.md`. No keys? `http://localhost:5173/room/demo?mock=1` plays a scripted meeting.

**Public link:** `cloudflared tunnel --url http://localhost:8000`, deploy `web/` to Vercel with `VITE_SERVER_URL=wss://<tunnel>` (or append `?server=wss://<tunnel>` to the link).

**Two laptops:** same Wi-Fi: `npm run dev -- --host` on laptop A, laptop B opens `http://<A-ip>:5173`. Otherwise use the tunnel.

## Repo map

```
server/            FastAPI app, WebSocket session, mouth, turn-taking, voice
  agents/          listener, conductor, diagrammer, fact checker, code analyst, task planner, responder, reflex
  brain/           code map, sourced memory, GitHub repo connector
  learning/        replay harness, evaluate, Coach, results (committed evidence)
  scripts/         smoke tests, live end-to-end script
  tests/           pytest suite + golden draw ops
web/               React + Vite meeting room, whiteboard, dashboard, login
demo_project/      small sample codebase Twin reasons about in the demo
ARCHITECTURE.md    full design    PROTOCOL.md  browser <-> server contract
TASKS.md, AGENT_CHAT.txt          how three coding agents built this in parallel
```

## How we built it

Two humans and three coding agents (two Claude, one Codex) worked in parallel on one repo, coordinating through `TASKS.md` and an append-only `AGENT_CHAT.txt`, with a shared contract in `PROTOCOL.md` and code ownership by area. The 3D part planning borrows the architecture of Preet's earlier project [LoominPhysicsSandbox](https://github.com/Preet37/LoominPhysicsSandbox) (structural part plans with real sizes, deterministic validation before drawing).

Team: Preet Karia, Parsh [need: Parsh's last name].
