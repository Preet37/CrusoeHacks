# Twin: Architecture and Build Guide

Single source of truth for the Recursive Agents Hackathon (Cerebral Valley x Crusoe, Shack15 SF, Oct 17 2026).
Every human and every coding agent (Claude Code, Codex) reads this file before doing anything.

Companion files (read all of them):

| File | What it is | Who edits it |
|---|---|---|
| `ARCHITECTURE.md` | This file. The plan, the stack, the rules. | Humans only (agents propose changes in `AGENT_CHAT.txt`) |
| `PROTOCOL.md` | The exact WebSocket messages between browser and server. The contract. | Only with agreement in `AGENT_CHAT.txt` from both sides |
| `TASKS.md` | Task board: who owns what, status, acceptance checks. | Every agent updates its own rows |
| `AGENT_CHAT.txt` | Append-only chat log between all agents. | Every agent appends, never edits others' lines |
| `KICKOFF_PROMPTS.md` | The first prompt to paste into each of the 4 coding agents. | Humans |
| `AGENTS.md` / `CLAUDE.md` | Short rules auto-loaded by Codex / Claude Code. Point here. | Humans |

---

## 0. Hard rules (read these first)

1. **The repo is PUBLIC.** Never commit API keys, `.env`, tokens, or personal data. Keys live only in `server/.env` (gitignored). If you see a key in a diff, stop and remove it before committing.
2. **New work only.** Everything here is written today, Oct 17 2026. Do not copy code from other projects. Borrowing *ideas and patterns* is fine. `open-dot` has no license: ideas only, no code. `coucou` brand, name, icons, character and art are reserved: copy the *visual style* only.
3. **Nothing from any employer.** No Cisco code, names, docs, repos, or internal details. Demo data is fake.
4. **All agent reasoning runs on NVIDIA Nemotron via Crusoe Managed Inference.** That is a hackathon requirement.
5. **Stay in your lane.** Each agent owns specific folders (section 9). Do not edit another agent's files. If you need a change there, ask in `AGENT_CHAT.txt`.
6. **Talk through `AGENT_CHAT.txt`.** Read it before starting any task. Append when you start, finish, get blocked, or change anything shared.
7. **Small commits, pull before push.** `git pull --rebase` before every push. Commit messages start with your agent id, e.g. `[PREET-CLAUDE] server: websocket session`.
8. **Never break `main`.** If you push code that breaks the app, fix it or revert it within 10 minutes and say so in the chat.
9. **Twin always identifies as an AI** if asked, and its tile in the UI is labeled AI. It should sound natural, never pretend to be human.

---

## 1. What we are building

**Twin** is a personal agent that joins your meetings as a real participant. It knows your codebase, your projects and you. It speaks up, draws on the whiteboard, checks claims, and turns decisions into tasks. After every meeting it reviews how it did and rewrites its own playbook, so it measurably improves.

**It is not a note-taker.** A note-taker remembers what was said. Twin understands what it means for your code and your plans, and acts on it during the meeting.

One-liner for the pitch: *Note-takers remember your meetings. Twin understands them, knows your project better than you do, and gets better every meeting.*

### What judges care about most (told to us by organizers)

Is it recursive? Is it really learning? How does it adapt? Then: technical difficulty (40%), creativity (25%), live demo (20%), future AI impact (15%).

- **Recursive:** the Conductor splits a live moment into sub-jobs; specialists can split again. After each meeting the Coach rewrites the team's own prompts and playbook.
- **Learning:** changes are kept only if they score better on held-out scripted meetings it never trained on.
- **Adapting:** facts get retired when they change; speaking rules change when people say "not now".

### Track

Version N+1 (self-improvement, before/after score on unseen tasks). Memory acts as a Shared Brain. Claim checking borrows from Done Means Done.

---

## 1A. Hackathon context (everything an agent needs to know)

**Event:** The Recursive Agents Hackathon, hosted by Cerebral Valley and Crusoe, with NVIDIA and You.com as partners. Saturday Oct 17 2026, Shack15, 1 Ferry Building Suite 201, San Francisco.

**Schedule:** 10:45 AM hacking starts · 1:00 PM lunch · **5:00 PM submissions due** · 5:00 to 6:45 PM first-round judging (about 3 min live demo + 1 to 2 min Q&A per team, in judging rooms) · 7:00 to 8:00 PM final round (top 6 teams demo on stage, equal weighting across criteria) · 8:15 PM winners.

**Submission:** public repo + a 1-minute demo video showing only what we built today + all team members added on the submission page.

**Rules that can disqualify us:**
- Repos must be public.
- Max 4 people per team (we are 2).
- The demo must clearly show only what was built during the hackathon. Presenting existing work = disqualification.
- No code, data, or assets we don't have rights to.
- Banned project types: AI mental health advisor, basic RAG apps, Streamlit apps, image analyzers, "AI for Education" chatbots, AI job screeners, AI nutrition coaches, personality analyzers, anything giving medical advice, **any project where a dashboard is the main feature**, sports analyzers or coaches. (Our scoreboard and agent tree are proof views, never the main feature. The main feature is Twin acting live in a meeting.)

**The three problem statements (all must be an agent team on Crusoe Managed Inference):**
1. **Done Means Done:** agents write their own definition of done, check their work with tests or independent verifier agents, catch drifting or looping, and decide to continue, re-plan, stop, or rarely ping a human.
2. **Shared Brain:** a shared memory layer every agent reads before working and updates after; kept current, lean and conflict-free; must show measurable gains in output quality.
3. **Version N+1:** the team improves itself over time: after each run it reviews what worked, rewrites its own prompts, playbooks, tools or team structure, and keeps a change only if it measurably helps; prove it with a before-and-after score on tasks it hasn't seen.

**Our track: Version N+1 (primary).** We also use a Shared Brain (Twin's memory, M2) and Done Means Done ideas (the Fact Checker and the locked test decide truth, not the model's own word). We submit under Version N+1.

**Judging, round one:** Technicality 40% · Creativity and originality 25% · Live demo 20% · Future potential and AI impact 15%. Organizers told us in person that the biggest question is: **is it recursive, is it really learning, and how does it adapt?**

**Prizes:** 1st $14,000 + $1,000 You.com credits · 2nd $8,000 + $500 · 3rd $3,000 + $250 · **Best Use of Nemotron: an NVIDIA RTX 5090** · Best Use of NemoClaw and OpenShell: $100 Brev credits per person.

**Resources we have:** $50 Crusoe Managed Inference credits (claim form in the participant guide), NVIDIA Nemotron models, NVIDIA build.nvidia.com free endpoints (Magpie TTS, Nemotron ASR), You.com search API (optional, not planned), Discord channels #crusoe, #nvidia, #questions.

---

## 1B. Where Crusoe and NVIDIA come in

**Crusoe** is an AI cloud company that builds data centers where energy is cheap and clean (for example Iceland, Norway, a solar-powered site in Nevada) and rents out GPUs. We use **Crusoe Managed Inference** (their "Intelligence Foundry"): they host the models on their GPUs and we call them over an **OpenAI-compatible API** at `https://api.inference.crusoecloud.com/v1`. Code written for the OpenAI SDK works by changing `base_url` and `api_key`. Billing is per token from our $50 credits. Their inference engine (MemoryAlloy) caches shared prompt prefixes across requests, so **keeping stable system prompts at the start of every call makes repeated calls faster and cheaper**. We take advantage of that: every agent's prompt file is a fixed prefix, and the changing transcript comes after it.

**Every model call in Twin goes to Crusoe.** The only non-Crusoe services are the browser's own speech recognition and NVIDIA's free Magpie voice for text-to-speech (also NVIDIA).

**NVIDIA Nemotron models hosted on Crusoe** (verify ids with `GET /v1/models` in `scripts/smoke_crusoe.py`):

**VERIFIED live on our account at 13:22 by PREET-CLAUDE (smoke_crusoe.py). These ids win over anything else in this file.**

| Model id on Crusoe (verified) | What it is, in plain words | Where Twin uses it |
|---|---|---|
| `nvidia/Nemotron-3.5-Lightning-30B-A3B` | Fast instruct model; no-think flag works | **Responder (the mouth)**. Note: sometimes returns 503 capacity_unavailable, so the Responder falls back to Nano |
| `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` | Small, cheap, fast (about 0.7 s) | **Listener**, first-pass filters, Librarian, fallback Responder. Reasoning eats tokens: give it 1000+ max tokens |
| `nvidia/Nemotron-3-Nano-Omni-Reasoning-30B-A3B` | Small model that takes images and audio, answers in text | Optional: transcribe audio, read a whiteboard photo or screenshot |
| `nvidia/NVIDIA-Nemotron-3-Super-120B-A12B` | Mid-size reasoning model; tool calling and json_object both work | **Conductor, Diagrammer, Code Analyst, Fact Checker, Task Planner, Prep** |
| `nvidia/NVIDIA-Nemotron-3.5-Super-VL` | Strongest model available to us, also sees images | **Coach** (stand-in: there is NO Ultra on our Crusoe account) |
| `nvidia/Nemotron-3-VoiceChat` | Speech-to-speech | Not used: request format undocumented on Crusoe |

Crusoe also hosts non-NVIDIA models (DeepSeek, Gemma, GLM, others). **We do not use them**: all-Nemotron is part of the pitch and our shot at the Best Use of Nemotron prize.

**Why this is a strong "Best Use of Nemotron" story:** Twin uses the whole family as a team, each sized to its job: Lightning for instant speech, Nano for constant cheap listening and filtering, Omni for seeing and hearing, Super for reasoning with tools, Ultra for slow offline self-improvement. Say this in the pitch.

---

## 1C. Where the recursion is (say this to judges, point at it in the demo)

1. **Recursive task splitting (live, M2):** the Conductor turns one moment ("let's switch login to sessions") into sub-jobs: find impact, check against past decisions, update the diagram. The Code Analyst splits "find impact" into one job per affected module, and each can split again if a module is large. Results flow back up and merge into one spoken answer and one diagram change. Depth limit 3, max 12 jobs per moment, a token budget per moment. Every job emits an `agent_event` with a `parent_id`, so the UI shows the tree growing and collapsing live.
2. **Recursive self-improvement (after each meeting, M3):** the Coach reads the team's own traces, rewrites the team's own prompt files, playbook and turn-taking values, then tests the new version on meetings it never trained on, and keeps it only if it scores better. Version N becomes Version N+1. The Coach can also change team structure: move a job to a smaller model, add a checker step, or drop a useless step.
3. **Recursive memory (M2):** facts are built from facts (a decision supersedes an earlier claim, which retires an older fact), and the Librarian keeps the chain sourced.

---

## 1D. Fast first pass for huge codebases (M2, design now, build if time)

**Problem:** a real codebase can be 500k+ lines. No model reads that per question. Stuffing it into the context window is slow, expensive, and makes answers worse.

**Solution: narrow first, think last.** Three passes, cheapest first:
1. **Deterministic pass (code, milliseconds):** `brain/code_map.py` builds the map once, ahead of the meeting: files, symbols, imports, reverse imports ("who depends on me"), owners from git. A question like "what does changing login touch?" becomes a graph walk, not a model call. This alone handles most impact questions on any size codebase.
2. **Fast filter pass:** for fuzzy questions, **JEV** (see 1F) answers a batch of yes/no relevance questions over candidate file summaries and facts in one call and keeps the top few dozen out of thousands. Fallback when JEV is unavailable: Nemotron Nano scoring the same summaries (each file gets a one-line summary when the map is built), plus plain keyword search.
3. **Reasoning pass:** Nemotron Super reads only the shortlisted files and facts and answers, citing sources.

Pitch line: the library can be any size; what lands on the model's desk stays small. We can show that on the scoreboard (tokens per answer stays flat as the codebase grows).

### 1F. JEV: the fast "System One" decision layer (added 13:20)

**What it is, in plain words:** JEV (by TypeSafe AI) is a model that does not chat or write. You give it some text (the "state") and a set of typed questions, and it answers each one with a typed value plus a calibrated probability: a **choice** from options you define, a **score** on a scale you define, or a **noul** (a 0 to 1 yes/no probability). Claimed latency 70 to 500 ms end to end, input about $0.042 per million tokens, output free. Many questions run in parallel in ONE call against the same state. Think: a reflex, not a thought. Nemotron does the thinking; JEV does the instant gut checks.

**API (from docs.typesafe.ai quickstart):** `POST https://api.typesafe.ai/v1/systemone`, header `Authorization: Bearer $TYPESAFE_API_KEY`, body `{"model":"jev-latest","state":"<text or JSON>","questions":{"<id>":{"type":"choice"|"score"|"noul","instructions":"...","criteria":...}}}`. Response `answers.<id>` has `choice`/`score`/`noul`, `confidence`, `probabilities`. Python SDK: `pip install typesafe-sdk`, `from typesafe_sdk import Choice, Noul, Score, TypeSafeClient`, `TypeSafeClient(model="jev-latest").system_one(state=..., questions={...})`. Key from console.typesafe.ai/keys. **Access:** TypeSafe's launch post said early access with a waitlist; confirm our key works before building on it.

**Where Twin uses JEV (the live fast path, in front of Nemotron):** on every final human sentence, ONE JEV call with several questions:
- `addressed` (noul): is this sentence directed at Twin?
- `kind` (choice): system_fact | decision | claim | question | task | small_talk | none
- `topic_changed` (noul): did the conversation move to a new topic vs the current topic title?
- `wants_visual` (choice): none | architecture | sequence | chart | mockup | table | code
- `is_echo` (noul): is this just Twin's own last sentence picked up by the mic? (echo guard)
Results gate everything: `kind=none/small_talk` -> skip the Listener entirely (saves Nemotron calls); `topic_changed` -> new frame + cancel stale jobs (speed rule 1); `addressed` -> Responder starts immediately; `wants_visual` -> tells the Diagrammer which tool to use; `is_echo` -> drop the line.
Also: **first-pass relevance** over code-map summaries and memory facts (1D step 2), and **pre-checks for the Fact Checker** (does this claim contradict fact X? only send likely contradictions to Super).

**Rules:**
- JEV never writes anything the user sees or hears; Nemotron on Crusoe does all reasoning and language. This keeps us inside the hackathon rule (agent team on Crusoe) and the Best Use of Nemotron story.
- One interface, two backends: `server/agents/fast_decide.py` exposes `decide(state, questions) -> answers`. Backend = JEV when `TYPESAFE_API_KEY` is set and a health check passes; otherwise Nemotron Nano with a strict JSON prompt. The rest of the code never knows which one ran.
- Log JEV latency next to the Nemotron latency in `latency.jsonl`. Demo line: "the reflex layer answers in about X ms" (real measured number only).

---

## 1E. Product decisions added at 13:07 (all agents read this)

### 1. Our own meeting app that looks and feels like Google Meet / Zoom
The live demo runs on OUR meeting page, built to feel like a real call app. Real Meet/Zoom comes later (see 5).
- **Join screen:** name field, mic and camera preview, "Join now" button. Room from the URL (`/room/demo`).
- **Call view:** grid of participant tiles. Each human tile shows their webcam (`getUserMedia`, local camera only on each laptop; remote humans show as an avatar tile with initials until real video exists). Active-speaker ring on whoever is talking (driven by `user_text` interim messages and the `participants` message). **Twin's tile** has its own avatar, an "AI" label, and states: listening, thinking, speaking (animated voice ring), hand raised.
- **Stage:** when the whiteboard has content, it takes the main stage like a screen share ("Twin is presenting"), with tiles moved to a strip at the side.
- **Bottom control bar, centered, pill buttons:** mic, camera, whiteboard/present, captions on/off, raise hand, people, Twin activity, leave (red). Hold-space push-to-talk.
- **Side panel (toggleable tabs):** People, Twin activity (the agent tree), Tasks (M2), Connections.
- **Captions:** live, at the bottom of the stage, speaker name in bold.
- Look: Mac-native (section 4) but laid out like Meet.

### 2. Two laptops in the same call
- One laptop runs the server. The other connects over the network. Both browsers join the same room; each does its own speech recognition and sends `user_text` with its speaker name. The server broadcasts Twin's audio, transcript and drawings to everyone in the room.
- Venue Wi-Fi may block laptop-to-laptop traffic. Fallback: a free tunnel (`cloudflared tunnel --url http://localhost:8000`, or ngrok) gives a public URL; the web app reads the server URL from `VITE_SERVER_URL`.
- Real video between humans (WebRTC via a hosted service such as LiveKit or Daily) is a stretch goal only.

### 3. Echo guard (very important for a live demo)
Twin's voice comes out of the laptop speaker and goes back into the mic, so speech recognition hears Twin and thinks a human is talking, which triggers barge-in and garbage transcripts.
- **Demo with headphones** on the laptop that plays Twin's voice.
- Browser: while Twin audio plays, ignore interim speech whose words mostly match the sentence Twin is currently saying (similarity check); only barge in on words that do not match.
- Server: drop any `final` user_text that closely matches Twin's last spoken sentences.

### 4. Rich drawing: the board can draw much more than boxes
The Diagrammer chooses the right visual for what is being discussed. The board is **tldraw** (open-source whiteboard inside our page). New draw ops (PROTOCOL.md v2):
- **Live boxes and arrows** (`add_box`, `connect`, ...): architectures that change during the conversation. Stays the default for system diagrams because each piece can be renamed or highlighted live.
- **Mermaid diagrams** (`add_mermaid`): sequence diagrams, flowcharts, state machines, ER/database diagrams, Gantt timelines, class diagrams. The model writes Mermaid code; the browser renders it with mermaid.js to SVG and places it on the board as an image. If the Mermaid code fails to render, the browser sends `draw_error` so the Diagrammer can fix it once.
- **Charts** (`add_chart`): bar, line, pie, scatter from numbers mentioned or pulled from data. Rendered in the browser to SVG.
- **Screen mockups** (`add_mock`): the model writes a small self-contained HTML snippet; the browser shows it in a sandboxed frame on the board (no scripts, no network).
- **Sticky notes** (`add_sticky`), **text** (`add_text`), **tables** (`add_table`), **code snippets** (`add_code`).
- **Freehand sketch** (`sketch`): simple strokes (lists of points on the 0-1000 grid) for rough drawings, arrows, circles around things.
- **Topic areas** (`frame`, `focus`): every topic gets its own area (frame) on the board. When the conversation switches topic, Twin starts a new frame and the view pans there; when an old topic comes back, the view pans back.
Choosing rule for the Diagrammer prompt: architecture or "how things connect" -> boxes/arrows; a process over time or between parties -> Mermaid sequence/flow; data or numbers -> chart; "what would the screen look like" -> mock; ideas/decisions/risks -> stickies; comparison -> table; a specific function -> code.

### 5. Speed rules (humans switch topics fast; Twin must keep up)
1. **Newest wins.** Every job is tagged with the topic and the transcript line that caused it. When a newer line arrives that changes the topic or contradicts the job's input, cancel the stale job. The Responder generation for an old turn is cancelled when a new human final arrives before Twin started speaking.
2. **Topic tracking.** The Listener returns a `topic_id` and `topic_changed` flag with every batch. The server keeps the active topic, sends a `topic` message, and the Diagrammer draws into that topic's frame.
3. **One model call per drawing burst.** The Diagrammer must emit ALL ops for a burst in ONE call (parallel tool calls or JSON-ops batch), then apply them one by one with a tiny delay so they appear progressively. Measured problem at 13:40: one round trip per op took about 26 s for 5 ops. Target: under about 3 to 4 s per burst.
4. **Fast models for live work.** Lightning/Nano for mouth and Listener, Super for drawing, Super-VL only offline.
5. **Instant acknowledgment.** When a slow answer or drawing is coming, Twin says a 2 to 4 word ack ("yeah, sketching it") right away.
6. **Prep before the meeting** (M2/M3): load code map and memory so live answers are lookups.
7. **Measure.** Every stage logs latency (already in `latency.jsonl`); show the numbers in the demo.

### 6. Connections page and calendar auto-join
- **Connections tab:** cards for GitHub (REAL: paste a public repo URL, Twin builds its code map from it; M2), Google Calendar, Slack, Jira, Zoom, Google Meet (DEMO cards, clearly marked "demo data" or "coming soon"; never claim they are live).
- **Calendar auto-join (demo):** a demo calendar file lists meetings (e.g. "Daily standup, 1:30 PM"). At meeting time (or on a "simulate" button), Twin automatically joins our meeting room, posts its pre-meeting brief and predictions, and says hi. This shows the "it detects your standup and joins" behavior honestly.
- Must never become a dashboard-first product (banned). Connections is a settings tab; the main feature is Twin in the call.

### 7. Real Google Meet / Zoom (stretch, only after M3)
How note-takers do it: a bot (a hidden browser logged into its own account) joins the meeting link, gets admitted by the host, captures the call audio, and, to speak, plays audio through a fake microphone. Building that ourselves today is too risky. Option to RENT: a meeting-bot service (for example Recall.ai). PARSH evaluates in 10 minutes: can its bot SPEAK into the call (not only listen), what does it cost, is there a free trial, how fast is the live transcript. Only start if all are yes and M3 is done. Pitch line either way: "Twin joins Zoom, Meet and Teams through a meeting bot, the same way note-takers do."

---

## 2. Milestones and timeline

Hackathon: building until **5:00 PM**. Submissions due 5:00 PM (public repo + 1-minute video). Judging 5:00 to 6:45 PM.

| Milestone | Target time | Definition of done (all must pass) |
|---|---|---|
| **M0: Setup** | 12:55 | Repo skeleton exists. `server` starts. `web` starts. `scripts/smoke_crusoe.py` lists models and gets one reply. Magpie TTS returns audio. |
| **M1: Talks and draws live** | 2:15 | (1) You speak in the browser, Twin answers out loud when addressed. (2) You can interrupt Twin and it stops within a fraction of a second. (3) Describing a system ("the app talks to auth, auth uses Postgres") draws boxes and arrows on the whiteboard. (4) A change ("auth also uses Redis") updates the board without redrawing everything. (5) Twin mentions what it drew at a natural pause. |
| **M2: Knows the code** | 3:15 | Demo codebase + code map exist. Asking "what does changing login touch?" gets a spoken answer and a highlighted diagram of the real affected files. A planted false claim gets corrected with a source. Decisions become tasks with owners. |
| **M3: Learns** | 4:00 | 12 scripted meetings exist (8 train, 4 locked test). Prep agent predictions are scored. Coach rewrites prompt files; a change is kept only if the validation score improves. Scoreboard: version 1 vs latest on the locked meetings. |
| **M4: Demo ready** | 4:40 | Agent tree view, polish, demo rehearsed twice, 1-minute video recorded. |
| **Submit** | 5:00 | Repo public, README updated, video uploaded, all teammates added. |

**Rule:** do not start M(n+1) work until M(n) passes, except for tasks explicitly marked as parallel in `TASKS.md`.

---

## 3. System overview

Four layers. Purple are agents on Nemotron. Teal are interfaces and data.

```
+---------------------------------------------------------------+
| IN THE MEETING (browser, /web)                                |
|   Meeting page | Twin tile + captions | Whiteboard (tldraw)    |
|   Mic -> Web Speech API (speech to text in the browser)        |
|   Speaker <- audio chunks from server (Magpie TTS)             |
+-------------------------------^-------------------------------+
                  one WebSocket | (PROTOCOL.md)
+-------------------------------v-------------------------------+
| SERVER (/server, Python FastAPI)                               |
|   Session: history, board state, speaking state, work queue    |
|                                                                |
|   LIVE AGENT TEAM (Nemotron on Crusoe)                         |
|     Responder (the mouth)  - fast replies, persona, short      |
|     Listener               - finds moments in transcript       |
|     Conductor              - decides what to do, spawns jobs   |
|     Diagrammer             - draw tools on a 0-1000 grid       |
|     Code Analyst (M2)      - impact of a change, from code map |
|     Fact Checker (M2)      - claims vs sourced memory          |
|     Task Planner (M2)      - decisions to owned tasks          |
+-------------------------------^-------------------------------+
                                | read / write
+-------------------------------v-------------------------------+
| BRAIN (/server/brain, M2)                                       |
|   Code map (files, imports, owners) | Memory (sourced facts)   |
|   Profile (what the user owns and cares about)                 |
+-------------------------------^-------------------------------+
                                | after each meeting
+-------------------------------v-------------------------------+
| LEARNING (/server/learning, M3)                                 |
|   Prep agent (predictions) | Coach (rewrites prompts/playbook)  |
|   Scripted meetings: 8 train, 4 locked test | Scoreboard        |
+---------------------------------------------------------------+
```

### The core idea for M1: a mouth and a mind

Humans talk while thinking in the background. Twin does the same.

- **The mouth (Responder):** a fast model that only produces short spoken replies. It never waits for slow work.
- **The mind (Listener, Conductor, Diagrammer, later specialists):** slower, smarter work that runs in the background on the transcript.
- When the mind finishes something, it puts a **work update** in a queue ("added Redis next to auth"). The mouth says it at the next natural pause, never mid-sentence. (Pattern borrowed as an idea from open-dot's "voice front, worker back".)

---

## 4. Tech stack (decided, do not relitigate)

### Web (`/web`)
- **Vite + React + TypeScript.** Run with `npm run dev` on port 5173.
- **tldraw** for the whiteboard (`npm i tldraw`). Draw shapes from code via the tldraw `Editor` API.
- **Speech to text:** browser **Web Speech API** (`window.SpeechRecognition || window.webkitSpeechRecognition`), `continuous = true`, `interimResults = true`, `lang = 'en-US'`. Use **Chrome** for the demo (best support).
- **Audio out:** Web Audio API. Play WAV chunks from the server in order through a queue. Must be able to stop instantly.
- **Fallback TTS:** `window.speechSynthesis` if server audio fails.
- **Style:** Mac-native look inspired by coucou's design language (style only, no assets): `font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, sans-serif`; small tight type (12 to 14px, weights 500/600); frosted panels `backdrop-filter: blur(20px) saturate(180%)`; near-black cards (`#141518`) with 1px white borders at 3 to 8% opacity in dark mode; pill buttons (`border-radius: 999px`); continuous rounded corners (12 to 16px); spring-like motion (`cubic-bezier(0.34, 1.56, 0.64, 1)`, 250 to 450ms). Twin tile glows softly when speaking.

### Server (`/server`)
- **Python 3.11+, FastAPI, uvicorn** (`uvicorn app:app --reload --port 8000`). WebSocket at `ws://localhost:8000/ws`.
- **openai** Python SDK pointed at Crusoe: `base_url="https://api.inference.crusoecloud.com/v1"`, `api_key=CRUSOE_API_KEY`.
- **httpx** for Magpie TTS (async).
- **pydantic** models for every protocol message (mirror of `PROTOCOL.md`).
- **python-dotenv** for `server/.env`.
- Concurrency: `asyncio`. Every background job is an `asyncio.Task` that can be cancelled.

### Models (Crusoe Managed Inference, OpenAI-compatible)

Verify exact ids first with `scripts/smoke_crusoe.py` (`GET /v1/models`). Put them in `server/config.py`, never hardcode elsewhere.

| Role | Model id (verify) | Why |
|---|---|---|
| Responder (mouth) | `nvidia/nemotron-3.5-lightning-30b-a3b` with `extra_body={"chat_template_kwargs":{"enable_thinking": False}}` | Fastest replies. If the flag is rejected, drop `extra_body`. Fallback: `nvidia/Nemotron-3-Nano-30B-A3B` |
| Listener | `nvidia/Nemotron-3-Nano-30B-A3B` | Cheap, runs often |
| Conductor, Diagrammer, Code Analyst, Fact Checker, Task Planner, Prep | `nvidia/Nemotron-3-Super-120B-A12B` | Good tool calling and reasoning |
| Coach (M3, offline only) | `nvidia/Nemotron-3-Ultra-550B` | Best reasoning, used rarely |
| Optional: transcribe audio or read images | `nvidia/Nemotron-3-Nano-Omni-Reasoning-30B-A3B` | Takes audio/image, returns text. Untested on Crusoe with base64 audio; keep `max_tokens >= 1024` (reasoning model) |

**Nemotron VoiceChat** (`nvidia/Nemotron-3-VoiceChat`) is listed by Crusoe but its request format is undocumented. Do NOT build on it unless Crusoe gives us a working endpoint. Ask once in Discord #crusoe; max 10 minutes.

### Text to speech: NVIDIA Magpie (free key from build.nvidia.com)

```
POST https://877104f7-e885-42b9-8de8-f6e4c6303969.invocation.api.nvcf.nvidia.com/v1/audio/synthesize
Header: Authorization: Bearer $NVIDIA_API_KEY
Multipart form fields:
  text=<sentence>
  language=en-US
  voice=Magpie-Multilingual.EN-US.Aria     (list voices: /v1/audio/list_voices)
  encoding=LINEAR_PCM
  sample_rate_hz=44100
Returns: WAV bytes
```

The server calls Magpie (key stays on the server) and streams base64 WAV chunks to the browser, one per sentence.

### Environment variables (`server/.env`, gitignored; copy from `server/.env.example`)

```
CRUSOE_API_KEY=
CRUSOE_BASE_URL=https://api.inference.crusoecloud.com/v1
NVIDIA_API_KEY=
MODEL_RESPONDER=nvidia/nemotron-3.5-lightning-30b-a3b
MODEL_FAST=nvidia/Nemotron-3-Nano-30B-A3B
MODEL_SMART=nvidia/Nemotron-3-Super-120B-A12B
MODEL_COACH=nvidia/Nemotron-3-Ultra-550B
TWIN_NAME=Twin
```

---

## 5. Folder layout

```
/
  ARCHITECTURE.md  PROTOCOL.md  TASKS.md  AGENT_CHAT.txt  KICKOFF_PROMPTS.md
  AGENTS.md  CLAUDE.md  README.md  .gitignore
  /server
    app.py                 FastAPI app, /ws endpoint, /health
    config.py              env + model ids (only place model ids live)
    protocol.py            pydantic models mirroring PROTOCOL.md
    session.py             Session: state, history, board, speaking, queues
    llm.py                 Crusoe client: chat(), stream_chat(), run_tools()
    board.py               BoardState: authoritative diagram, validates draw ops
    turn_taking.py         when Twin speaks (rules the Coach can rewrite)
    /voice
      tts.py               Magpie client + sentence splitter + cancellation
    /agents
      responder.py         the mouth
      listener.py          transcript -> moments
      conductor.py         moments -> jobs (M1: route to diagrammer)
      diagrammer.py        tool-calling loop -> draw ops
      code_analyst.py      (M2)
      fact_checker.py      (M2)
      task_planner.py      (M2)
      /prompts             one .md file per agent prompt (the Coach edits these)
        responder.md listener.md conductor.md diagrammer.md ...
      playbook.md          learned rules (M3, Coach writes it)
    /brain                 (M2)
      code_map.py          build map of /demo_project (files, imports, owners)
      memory.py            sourced facts store (JSON file), librarian logic
      profile.py
    /learning              (M3)
      meetings/            12 scripted meetings (JSON)
      prep.py              predictions before a meeting
      coach.py             review, propose prompt edits, test, keep or reject
      evaluate.py          scoring on train/validation/locked test
      runs/                results per version (JSON)
    /scripts
      smoke_crusoe.py      list models + one chat call + timing
      smoke_tts.py         one Magpie call, writes out.wav
    /tests
    requirements.txt
    .env.example
  /web
    index.html  package.json  vite.config.ts
    /src
      main.tsx  App.tsx
      /lib
        protocol.ts        TS types mirroring PROTOCOL.md
        ws.ts              WebSocket client with reconnect
        speech.ts          Web Speech API wrapper (interim + final, speech start)
        player.ts          audio queue, play/stop instantly
        mockServer.ts      fake server messages for UI testing without backend
      /components
        MeetingRoom.tsx    layout
        ParticipantTile.tsx  humans + Twin (AI label, speaking glow, hand raised)
        Captions.tsx
        Whiteboard.tsx     tldraw + applies draw ops
        AgentTree.tsx      live tree of agent events (M2/M4)
        Scoreboard.tsx     before/after results (M3)
      /styles
        theme.css
  /demo_project            (M2) small fake codebase Twin knows about
```

---

## 6. How M1 works, step by step

1. **User speaks.** `speech.ts` emits interim and final text. On the first sound of user speech while Twin is talking, the browser **immediately stops playback** (`player.stopAll()`) and sends `barge_in`.
2. **Browser sends** `user_text` messages (`final=false` for interim, `final=true` for finished sentences) with the speaker name.
3. **Server Session** appends final text to history, broadcasts a `transcript` message (for captions), and does two things in parallel:
   - **Mouth path:** `turn_taking.should_respond()` decides if Twin answers now. M1 rule: answer if the utterance contains the name "Twin" (case-insensitive) or the push-to-talk button was held, or a work update is waiting and there has been 1.2 seconds of silence.
   - **Mind path:** send the final text to the **Listener** (debounced: batch every final sentence, at most one call in flight; if a new sentence arrives during a call, run again after).
4. **Listener** (Nano) returns structured "moments" JSON: `{type: "system_fact"|"decision"|"question"|"claim"|"task"|"none", text, entities:[...]}`.
5. **Conductor** (M1: plain code routing, becomes an LLM in M2) sends any `system_fact` moments to the **Diagrammer**.
6. **Diagrammer** (Super, tool calling) gets the current board state (as compact JSON) + the new moments and calls draw tools: `add_box`, `connect`, `rename`, `remove`, `highlight`, `clear`. `board.py` validates every op (ids exist, coordinates 0 to 1000, no duplicate ids), applies it to the authoritative state, and broadcasts a `draw` message.
7. After a Diagrammer run that changed the board, it pushes a **work update** into the Session's queue: one short sentence, e.g. "I added Redis next to auth."
8. **Responder** (mouth) builds a reply from: persona prompt + last ~20 transcript lines + current board summary + pending work updates. It streams tokens. A **sentence splitter** cuts the stream at `.`, `?`, `!` (min 4 words). Each sentence goes to Magpie TTS immediately and is sent as `audio` chunks with a `say_id` and `seq`. The first sentence starts playing while later ones are still generating.
9. On `barge_in`, the server cancels the Responder task and all pending TTS for that `say_id`, sends `stop_audio`, and sets status to `listening`.
10. Status messages (`listening`, `thinking`, `speaking`, `hand_raised`) drive the Twin tile animation.

### Latency budget (targets, measure and log real numbers)

Log these per turn in the server console and in `server/learning/runs/latency.jsonl`:
- user final text received -> first Responder token
- first token -> first sentence complete
- sentence -> Magpie audio returned
- audio sent -> playback started (browser logs this)

Tricks: keep Responder prompts short; send only recent history; start TTS per sentence; say a short acknowledgment ("Yeah, one sec") when a slow answer is coming.

---

## 7. Agent prompts (behavior specs)

Prompts live in `server/agents/prompts/*.md`, loaded at call time (so the Coach can rewrite them in M3). Keep each under ~60 lines.

### Responder (the mouth)
- You are Twin, a senior engineer on this team, in a live voice call.
- Speak in 1 or 2 short sentences. Casual, direct, warm. Contractions are fine.
- Never use lists, markdown, headings, emojis, or "Great question". Never summarize the whole meeting.
- If you are unsure, ask one short question back.
- Never invent facts, file names, or results. Only mention what is in the transcript, the board summary, work updates, or (M2) cited memory.
- If asked whether you are an AI, say yes plainly.
- When a work update is provided, weave it in naturally ("I put Redis next to auth, does that match what you meant?").
- Output plain spoken text only. No stage directions.

### Listener
- Input: the last few final transcript lines. Output ONLY JSON: a list of moments.
- Extract system architecture facts (components and how they connect), decisions, questions, claims, tasks.
- Return `[]` if nothing new. Do not repeat moments already listed in "previous moments".

### Diagrammer
- You maintain one architecture diagram. You get the current board (boxes with ids, labels, positions; edges) and new facts.
- Use the tools only. Reuse existing ids. Prefer `rename` over remove+add. Never duplicate a component.
- Layout: grid 0 to 1000 in both axes. Place new boxes near what they connect to, avoid overlaps (boxes are about 160 x 70 grid units), flow left to right for request paths, data stores at the bottom.
- Ids are short lowercase slugs: `web`, `auth`, `db`, `redis`.
- At most 8 tool calls per run.

### Turn-taking rules (`turn_taking.py`, and later `playbook.md`)
- Speak when addressed by name or push-to-talk.
- Deliver a work update only after 1.2 s of silence and only if no human started talking.
- Max one unprompted interjection per 30 seconds.
- If a human says "not now", "hold on", or "wait", stop and set `hand_raised` instead of speaking for the next 30 seconds.
These numbers are starting values the Coach is allowed to change in M3.

---

## 8. Tool calling with Nemotron on Crusoe

Use OpenAI-style `tools=[{"type":"function","function":{...}}]` with `tool_choice="auto"` in `llm.run_tools()`:

```python
async def run_tools(model, messages, tools, handlers, max_steps=8):
    for _ in range(max_steps):
        r = await client.chat.completions.create(model=model, messages=messages, tools=tools, tool_choice="auto")
        msg = r.choices[0].message
        if not msg.tool_calls:
            return msg.content
        messages.append(msg)
        for call in msg.tool_calls:
            result = await handlers[call.function.name](**json.loads(call.function.arguments))
            messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)})
    return None
```

**First thing to verify (M0):** that Crusoe returns `tool_calls` for Super. If tool calling is unreliable, fall back to: ask the model for a JSON array of ops (`response_format={"type":"json_object"}` if supported, otherwise strict instructions + `json.loads` with one retry), then apply the ops in code. Same `board.py` validation either way.

Draw tool schemas (also in `PROTOCOL.md` as draw ops):

| Tool | Args |
|---|---|
| `add_box` | `id: str, label: str, x: int (0-1000), y: int (0-1000), kind: "service"|"datastore"|"client"|"external"|"note"` |
| `connect` | `id: str, from_id: str, to_id: str, label: str = ""` |
| `rename` | `id: str, label: str` |
| `remove` | `id: str` (box or edge; removing a box removes its edges) |
| `highlight` | `ids: list[str], on: bool` |
| `clear` | none |

---

## 9. Who does what (3 coding agents, 2 humans)

**FINAL split (14:15, set by Preet; overrides every earlier version and chat message). Three agents, split by AREA so nobody waits on anybody: Parsh's agent = the whole call (UI + audio + video). Preet's Claude = the server + the whole whiteboard. Preet's Codex = the mind.**

| Agent id | Human | Tool | Owns | Tasks |
|---|---|---|---|---|
| `PARSH-CLAUDE` | Parsh | Claude Code | Everything in `web/` EXCEPT the board files listed for PREET-CLAUDE: scaffold, `App.tsx`, `JoinScreen.tsx`, `MeetingRoom.tsx`, `ParticipantTile.tsx`, `ControlBar.tsx`, `SidePanel.tsx`, `Captions.tsx`, `ConnectionsPanel.tsx`, `web/src/lib/ws.ts`, `media.ts`, `speech.ts`, `player.ts`, `bargeIn.ts`, `echoGuard.ts`, `web/src/styles/` | All RC tasks (RC-1 to RC-15), RX-1 (done) |
| `PREET-CLAUDE` | Preet | Claude Code | Server core (`server/app.py`, `session.py`, `mouth.py`, `protocol.py`, `turn_taking.py`, `server/voice/`, `server/agents/responder.py` + prompt, `server/scripts/`) and the whole board: `web/src/components/Whiteboard.tsx`, `web/src/components/board/`, `AgentTree.tsx`, `TaskPanel.tsx`, `Scoreboard.tsx`, `web/src/lib/mockServer.ts`, v2 types in `web/src/lib/protocol.ts` | PC tasks, RX-2 to RX-9 |
| `PREET-CODEX` | Preet | Codex | `server/llm.py`, `config.py`, `board.py`, `server/agents/` (except responder), `server/brain/`, `server/learning/`, `demo_project/` | PX tasks |

**Integration contract (from PARSH-CLAUDE's 14:08 message, keep it):** MeetingRoom renders `<Whiteboard client={ws} onHasContent={(b)=>void} />`, `<AgentTree client={ws}/>`, `<TaskPanel client={ws}/>`. `ws.ts` exports `createClient({room,user})` -> `{ send(msg), on(type, cb) -> unsubscribe, onAny(cb), state }`. With `?mock=1`, ws.ts uses `startMockServer(emit, onClientMessage?) -> stop()` from `lib/mockServer.ts`.

Shared files: `PROTOCOL.md` (agreement from PREET-CLAUDE and PARSH-CLAUDE), `requirements.txt` (PREET-CLAUDE), `web/package.json` (PARSH-CLAUDE; PREET-CLAUDE may add `mermaid` and a chart library and posts a CHANGE).

### Humans
- **Parsh:** gets all API keys and credits working first (Crusoe org + API key, $50 credit form, NVIDIA build.nvidia.com key for Magpie), shares keys with Preet privately (NOT in the repo, NOT in chat files), asks Crusoe in Discord #crusoe about VoiceChat once. Then drives the web agents.
- **Preet:** drives the server agents, writes the demo pitch, owns the scripted meetings content in M3 (with PREET-CODEX).

---

## 10. M2: Knows the code (outline)

- `demo_project/`: a small fake app (about 10 to 15 Python files): `web/`, `auth/` (JWT login), `billing/`, `db/` (Postgres models), `notifications/`. Fake git history via a script that commits as different fake authors so owners exist. Plant: auth currently uses JWT; a "decision" in meeting 3 says "we moved sessions to Redis".
- `brain/code_map.py`: parse with Python `ast`: modules, functions, imports, reverse-import graph ("who depends on me"), owners from `git log`. Query: `impact(path_or_symbol) -> list of files + tests`.
- `brain/memory.py`: facts `{id, claim, source, status: current|superseded|retired, seen_at, scope}` in `brain/memory.json`. Librarian: when a new fact contradicts an old one (same subject), mark old `superseded` with `superseded_by` and a reason. Never delete.
- Retrieval: Nano filters candidate facts by subject, Super reads the short list (two-pass).
- Code Analyst: answers "what does changing X touch?" with the code map (code decides), then highlights those boxes on the board.
- Fact Checker: compares Listener `claim` moments to memory; if contradicted by a `current` fact with a source, push a work update with the correction and the source.
- Task Planner: `decision` moments become tasks assigned to the owner from the code map; shown in a task list panel.
- Conductor becomes an LLM router (Super) that can spawn several specialists in parallel and merge results (this is the recursion; emit `agent_event` messages for the tree view).

## 11. M3: Learns (outline)

- `learning/meetings/*.json`: 12 scripted meetings over fake "weeks" about `demo_project`, each a list of `{speaker, text}` lines plus ground truth: `{decisions, false_claims, impacted_files, topics}`. Split: 1 to 8 train, 9 and 10 validation, 11 and 12 locked test. Locked meetings are never shown to the Coach.
- Replay harness: feeds meetings as text through the same Session pipeline (no audio needed), collects what Twin said, drew, flagged, predicted.
- Metrics: prediction score (topics and decisions predicted by Prep vs ground truth), false claims caught, false alarms, impact accuracy (files vs ground truth), helpful-interruption rate (judged against ground truth moments), response latency, cost per meeting (tokens).
- Coach (Ultra): reads failures from train runs, proposes ONE edit at a time to a prompt file, `playbook.md`, or `turn_taking` values, as a diff. Run validation. Keep if better and nothing worse; otherwise reject. Log every proposal, with result and reason, in `learning/runs/coach_log.jsonl`.
- Scoreboard: version 1 vs latest on the locked meetings. Show rejected changes too.

---

## 12. Demo script (3 minutes)

1. Hook: "Three weeks into my internship I still didn't know what half my team's projects were."
2. Pre-meeting brief with Twin's predictions.
3. Live: describe the system and Twin draws it; propose a change and Twin names the impacted files and highlights them; a teammate says something false and Twin corrects it with a source; a decision becomes a task.
4. After: Twin scores its predictions; the Coach shows what it changed and why.
5. Proof: version 1 vs latest on the locked meetings.
Judge tests: ask anything about the codebase; say something false; change a fact and ask again; say "not now" and watch it wait.

---

## 13. Fallbacks

| If this fails | Do this |
|---|---|
| Web Speech API flaky | Push-to-talk button + MediaRecorder WAV sent to Nano Omni "Transcribe this audio." (untested path) |
| Magpie TTS fails or is slow | `window.speechSynthesis` in the browser |
| Tool calling unreliable | JSON ops mode (section 8) |
| Lightning model unavailable | `MODEL_FAST` (Nano) for the Responder |
| Credits running low | Nano everywhere except Coach; cache Listener results; shorter histories |
| tldraw API trouble | Render boxes and arrows as plain SVG from the same board state |
