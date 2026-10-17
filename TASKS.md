# TASKS.md: Task board

How to use:
- Find your agent id. Take the next `todo` task in YOUR section whose `needs` are `done`.
- Change its status to `doing` and commit right away (so others see it), then announce it in `AGENT_CHAT.txt`.
- When the check passes, set `done`, commit, and post in `AGENT_CHAT.txt` what you built and how to use it.
- `blocked`: write why in the chat with `@AGENT-ID` of who can unblock you.
- Only edit rows in your own section. To add a task for someone else, post it in the chat.

Statuses: `todo` | `doing` | `done` | `blocked`

---

## PREET-CLAUDE (server core: WebSocket, session, the mouth, voice)

| id | status | task | needs | check (done when) |
|---|---|---|---|---|
| PC-1 | todo | Create `server/` skeleton: `requirements.txt` (fastapi, uvicorn[standard], openai, httpx, pydantic, python-dotenv), `.env.example`, `app.py` with `/health` and `/ws`, `config.py` stub reading env (PREET-CODEX fills model ids) | - | `uvicorn app:app --port 8000` starts; `curl localhost:8000/health` returns ok |
| PC-2 | todo | `scripts/smoke_crusoe.py`: list models (`GET /v1/models`), print Nemotron ids, one chat call to each role model with timing; test tool calling on Super with a dummy tool and print whether `tool_calls` came back | PC-1 | Prints model list, replies, latency in ms, and "tool calling: OK/NOT OK". Post results in chat. |
| PC-3 | todo | `scripts/smoke_tts.py`: one Magpie call, write `out.wav`, print latency | PC-1 | `out.wav` plays and sounds right |
| PC-4 | todo | `protocol.py`: pydantic models for every message in `PROTOCOL.md` + a `parse(msg)` and `send(ws, model)` helper | PC-1 | Unit test round-trips every example JSON in PROTOCOL.md |
| PC-5 | todo | `session.py`: Session per room: connected sockets, transcript history, speaking state, current `say_id`, work-update queue, running task handles; broadcast helper; send `board_snapshot` on connect | PC-4 | Two browser tabs connect to same room, both receive `status` and `transcript` |
| PC-6 | todo | `voice/tts.py`: async Magpie client, sentence splitter for a token stream (split on . ? ! with min 4 words), per-`say_id` cancellation, `data:null` fallback on error | PC-3 | Feeding a fake token stream produces ordered `audio` messages; cancel stops further chunks |
| PC-7 | todo | `agents/responder.py` + `prompts/responder.md`: streaming reply on `MODEL_RESPONDER` using persona prompt, last 20 transcript lines, board summary, pending work updates; pipe into tts.py | PC-5, PC-6, PX-1 | Saying "Twin, what do you think?" gets a spoken 1 to 2 sentence answer |
| PC-8 | todo | `turn_taking.py`: should_respond rules from ARCHITECTURE.md section 7 (name mention, push-to-talk, work update after 1.2 s silence, max 1 interjection per 30 s, "not now" -> hand_raised). Values in a dict the Coach can edit later | PC-5 | Unit tests for each rule |
| PC-9 | todo | Barge-in: on `barge_in`, cancel responder + TTS, send `stop_audio`, status listening | PC-7 | Talking over Twin stops it; no stale audio arrives after |
| PC-10 | todo | Work-update queue: Diagrammer (PREET-CODEX) calls `session.push_update(text)`; responder delivers per turn_taking | PC-8 | After a diagram change and a pause, Twin mentions it once |
| PC-11 | todo | Latency logging per turn (ARCHITECTURE.md section 6) to console + `server/learning/runs/latency.jsonl` | PC-7 | File has rows with the 4 timings |
| PC-12 | todo | M2: wire Fact Checker and Code Analyst results into work updates; `task` messages | M1 done | Planted false claim gets a spoken correction with source |

## PREET-CODEX (the mind: Crusoe client, agents, brain, learning)

| id | status | task | needs | check (done when) |
|---|---|---|---|---|
| PX-1 | todo | `config.py` model ids from env with defaults from ARCHITECTURE.md section 4; `llm.py`: async OpenAI client to Crusoe, `chat()`, `stream_chat()`, `run_tools()` (section 8), JSON-ops fallback, retries with backoff, token usage logging | PC-1 | Small script calls each function successfully |
| PX-2 | todo | `board.py`: BoardState (boxes, edges), validate + apply each draw op, `summary()` compact text for prompts, `snapshot()` for `board_snapshot` | - | Unit tests: add, connect, rename, remove (removes edges), highlight, clear, rejects bad ids and out-of-range coords |
| PX-3 | todo | `agents/listener.py` + `prompts/listener.md`: transcript lines -> moments JSON on MODEL_FAST; debounced, one call in flight | PX-1 | Given "the app talks to an auth service which uses Postgres" returns system_fact moments with entities |
| PX-4 | todo | `agents/diagrammer.py` + `prompts/diagrammer.md`: tool loop on MODEL_SMART with draw tools; applies via board.py; broadcasts `draw` via session; pushes one work update after changes | PX-2, PX-3, PC-5 | Speaking a system description draws it; a follow-up change renames or adds without duplicates |
| PX-5 | todo | `agents/conductor.py`: M1 plain routing (system_fact -> diagrammer). Emit `agent_event` for every job (started/done) with parent ids | PX-4 | UI agent tree shows listener -> conductor -> diagrammer events |
| PX-6 | todo | M2: `demo_project/` fake app (web, auth JWT, billing, db Postgres, notifications; 10 to 15 files) + `scripts/fake_history.sh` creating commits by 3 fake authors | M1 done | `git log` in demo_project shows 3 authors |
| PX-7 | todo | M2: `brain/code_map.py` (ast: symbols, imports, reverse imports, owners, one-line summary per file) + `impact()` | PX-6 | `impact("auth/login.py")` returns the right dependents |
| PX-8 | todo | M2: `brain/memory.py` sourced facts + Librarian supersede logic; two-pass retrieval (Nano filter, Super read). Leave a clear hook for the JEV first-pass filter (ARCHITECTURE.md 1D) | PX-1 | Contradicting fact supersedes old one with reason; nothing deleted |
| PX-9 | todo | M2: `code_analyst.py`, `fact_checker.py`, `task_planner.py`; Conductor becomes LLM router on MODEL_SMART that spawns specialists in parallel with depth limit 3 and merges results (the recursion) | PX-7, PX-8 | One moment spawns a visible tree of jobs; answer cites files |
| PX-10 | todo | M3: 12 scripted meetings JSON with ground truth (8 train, 2 validation, 2 locked test) about demo_project | PX-6 | Files exist, ground truth filled |
| PX-11 | todo | M3: replay harness + `evaluate.py` metrics (ARCHITECTURE.md section 11) | PX-9, PX-10 | Prints scores for v1 on all splits |
| PX-12 | todo | M3: `prep.py` predictions + `coach.py` (Ultra): one edit at a time to prompt files / playbook / turn-taking values, validate, keep or reject, log to `coach_log.jsonl` | PX-11 | At least 3 Coach iterations logged; version latest vs v1 on locked test |

## PARSH-CLAUDE (meeting page, look, mic, speech, audio)

| id | status | task | needs | check (done when) |
|---|---|---|---|---|
| RC-1 | todo | `MeetingRoom.tsx` layout: participant tiles row (2 humans + Twin, Twin labeled "AI"), big whiteboard area (Whiteboard component from PARSH-CODEX), captions strip, controls (mic, push-to-talk, mute Twin) | RX-1 | Page renders with mock data |
| RC-2 | todo | `styles/theme.css`: Mac-native look per ARCHITECTURE.md section 4 (style only, no coucou assets). Dark and light | RC-1 | Looks like a native Mac app, not a web template |
| RC-3 | todo | `lib/ws.ts`: connect `ws://localhost:8000/ws?room=demo&user=<name>`, auto-reconnect, typed send/receive using `protocol.ts`, event emitter | RX-1 | Connects to server; logs `status` messages |
| RC-4 | todo | `lib/speech.ts`: Web Speech API wrapper: continuous, interim + final; emits `speechstart` the instant a user starts talking; auto-restarts when Chrome stops it; send `user_text` | RC-3 | Talking shows live captions; finals reach server |
| RC-5 | todo | `lib/player.ts`: audio queue by `say_id`/`seq`, decode base64 WAV, play in order, `stopAll()` instantly; `data:null` -> `speechSynthesis` fallback; report playback start time for latency logging | RC-3 | Plays server chunks smoothly; stop is instant |
| RC-6 | todo | Barge-in: on speechstart while playing -> `player.stopAll()` then send `barge_in`; handle `stop_audio` | RC-4, RC-5 | Talking over Twin silences it immediately |
| RC-7 | todo | `ParticipantTile.tsx`: Twin tile states from `status` (listening, thinking, speaking glow, hand raised); `Captions.tsx` showing transcript with speaker names | RC-3 | States visibly change during a turn |
| RC-8 | todo | Push-to-talk button + keyboard shortcut (hold space) sending `push_to_talk` | RC-3 | Holding it makes Twin respond without saying its name |
| RC-9 | todo | M4: polish pass, demo flow, record 1-minute video | M3 done | Video recorded |

## PARSH-CODEX (whiteboard, protocol types, mock server, agent tree, scoreboard)

| id | status | task | needs | check (done when) |
|---|---|---|---|---|
| RX-1 | todo | Scaffold `web/` with Vite + React + TS; install `tldraw`; `lib/protocol.ts` TypeScript types for every message in PROTOCOL.md | - | `npm run dev` serves on 5173; types compile |
| RX-2 | todo | `lib/mockServer.ts`: fake message stream (status changes, transcript lines, a sequence of draw ops building a 5-box architecture, a rename, a highlight) toggled by `?mock=1` | RX-1 | UI works fully without backend |
| RX-3 | todo | `Whiteboard.tsx`: tldraw editor; map 0-1000 grid to canvas; apply `add_box`, `connect` (arrow bound to shapes), `rename`, `remove`, `highlight`, `clear`, `board_snapshot`; keep a map id -> tldraw shape id; style by `kind`; animate new shapes in | RX-1 | Mock sequence builds the diagram correctly; rename edits in place; remove deletes edges too |
| RX-4 | todo | Auto-fit and tidy: zoom to fit after changes; nudge boxes that overlap | RX-3 | No overlapping boxes in the mock sequence |
| RX-5 | todo | `AgentTree.tsx`: live tree from `agent_event` (nest by `parent_id`, states started/done/failed, model name badge) | RX-1 | Mock events render as a growing/collapsing tree |
| RX-6 | todo | M2: task list panel from `task` messages | RX-1 | Tasks appear with owner |
| RX-7 | todo | M3: `Scoreboard.tsx` from `metric` messages: v1 vs latest on locked test, rejected changes listed | RX-1 | Renders mock metrics |

---

## Milestone gates (humans check these)
- [ ] M0 setup: server + web start, smoke_crusoe and smoke_tts pass
- [ ] M1 talks and draws live (ARCHITECTURE.md section 2 checks 1 to 5)
- [ ] M2 knows the code
- [ ] M3 learns, scoreboard shows v1 vs latest
- [ ] M4 demo rehearsed twice, video recorded
- [ ] Submitted by 5:00 PM
