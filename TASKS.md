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
| PC-1 | done | Create `server/` skeleton: `requirements.txt` (fastapi, uvicorn[standard], openai, httpx, pydantic, python-dotenv), `.env.example`, `app.py` with `/health` and `/ws`, `config.py` stub reading env (PREET-CODEX fills model ids) | - | `uvicorn app:app --port 8000` starts; `curl localhost:8000/health` returns ok |
| PC-2 | done | `scripts/smoke_crusoe.py`: list models (`GET /v1/models`), print Nemotron ids, one chat call to each role model with timing; test tool calling on Super with a dummy tool and print whether `tool_calls` came back | PC-1 | Prints model list, replies, latency in ms, and "tool calling: OK/NOT OK". Post results in chat. |
| PC-3 | blocked | `scripts/smoke_tts.py`: one Magpie call, write `out.wav`, print latency | PC-1 | `out.wav` plays and sounds right |
| PC-4 | done | `protocol.py`: pydantic models for every message in `PROTOCOL.md` + a `parse(msg)` and `send(ws, model)` helper | PC-1 | Unit test round-trips every example JSON in PROTOCOL.md |
| PC-5 | done | `session.py`: Session per room: connected sockets, transcript history, speaking state, current `say_id`, work-update queue, running task handles; broadcast helper; send `board_snapshot` on connect | PC-4 | Two browser tabs connect to same room, both receive `status` and `transcript` |
| PC-6 | done | `voice/tts.py`: async Magpie client, sentence splitter for a token stream (split on . ? ! with min 4 words), per-`say_id` cancellation, `data:null` fallback on error | PC-3 | Feeding a fake token stream produces ordered `audio` messages; cancel stops further chunks |
| PC-7 | done | `agents/responder.py` + `prompts/responder.md`: streaming reply on `MODEL_RESPONDER` using persona prompt, last 20 transcript lines, board summary, pending work updates; pipe into tts.py | PC-5, PC-6, PX-1 | Saying "Twin, what do you think?" gets a spoken 1 to 2 sentence answer |
| PC-8 | done | `turn_taking.py`: should_respond rules from ARCHITECTURE.md section 7 (name mention, push-to-talk, work update after 1.2 s silence, max 1 interjection per 30 s, "not now" -> hand_raised). Values in a dict the Coach can edit later | PC-5 | Unit tests for each rule |
| PC-9 | done | Barge-in: on `barge_in`, cancel responder + TTS, send `stop_audio`, status listening | PC-7 | Talking over Twin stops it; no stale audio arrives after |
| PC-10 | done | Work-update queue: Diagrammer (PREET-CODEX) calls `session.push_update(text)`; responder delivers per turn_taking | PC-8 | After a diagram change and a pause, Twin mentions it once |
| PC-11 | done | Latency logging per turn (ARCHITECTURE.md section 6) to console + `server/learning/runs/latency.jsonl` | PC-7 | File has rows with the 4 timings |
| PC-12 | doing | M2: wire Fact Checker and Code Analyst results into work updates; `task` messages | M1 done | Planted false claim gets a spoken correction with source |
| PC-13 | doing | **Newest wins** (ARCH 1E.5): job registry in session keyed by topic_id + source line; cancel stale Diagrammer/specialist jobs when the topic changes; cancel an un-started Responder turn when a newer human final arrives | PC-5 | Rapid topic switch: no drawing or speech about the old topic arrives after the switch |
| PC-14 | done | **Echo guard, server side** (ARCH 1E.3): drop final user_text that closely matches Twin's last ~3 spoken sentences | PC-7 | Playing Twin through speakers does not create fake human lines |
| PC-15 | done | `participants` + `topic` messages (PROTOCOL v2): track who is connected and speaking (from interim user_text) and broadcast; relay topic changes from PREET-CODEX | PC-5 | Two browsers see each other's speaking state |
| PC-16 | done | Two-laptop access: CORS + bind 0.0.0.0; doc in README how to run `cloudflared tunnel --url http://localhost:8000`; web reads `VITE_SERVER_URL` | PC-1 | Second laptop joins the same room |
| PC-17 | done | `draw_error` relay to the Diagrammer; `calendar_join` -> Twin joins, sends `brief` (predictions from PREET-CODEX Prep, or a stub), says hi; demo calendar file `server/demo_calendar.json` | PC-15 | Simulate button makes Twin join and greet with a brief |

## PREET-CODEX (the mind: Crusoe client, agents, brain, learning)

| id | status | task | needs | check (done when) |
|---|---|---|---|---|
| PX-1 | done | `config.py` model ids from env with defaults from ARCHITECTURE.md section 4; `llm.py`: async OpenAI client to Crusoe, `chat()`, `stream_chat()`, `run_tools()` (section 8), JSON-ops fallback, retries with backoff, token usage logging | PC-1 | Small script calls each function successfully |
| PX-2 | done | `board.py`: BoardState (boxes, edges), validate + apply each draw op, `summary()` compact text for prompts, `snapshot()` for `board_snapshot` | - | Unit tests: add, connect, rename, remove (removes edges), highlight, clear, rejects bad ids and out-of-range coords |
| PX-3 | done | `agents/listener.py` + `prompts/listener.md`: transcript lines -> moments JSON on MODEL_FAST; debounced, one call in flight | PX-1 | Given "the app talks to an auth service which uses Postgres" returns system_fact moments with entities |
| PX-4 | done | `agents/diagrammer.py` + `prompts/diagrammer.md`: tool loop on MODEL_SMART with draw tools; applies via board.py; broadcasts `draw` via session; pushes one work update after changes | PX-2, PX-3, PC-5 | Speaking a system description draws it; a follow-up change renames or adds without duplicates |
| PX-5 | blocked | `agents/conductor.py`: M1 plain routing (system_fact -> diagrammer). Emit `agent_event` for every job (started/done) with parent ids | PX-4 | UI agent tree shows listener -> conductor -> diagrammer events |
| PX-6 | todo | M2: `demo_project/` fake app (web, auth JWT, billing, db Postgres, notifications; 10 to 15 files) + `scripts/fake_history.sh` creating commits by 3 fake authors | M1 done | `git log` in demo_project shows 3 authors |
| PX-7 | todo | M2: `brain/code_map.py` (ast: symbols, imports, reverse imports, owners, one-line summary per file) + `impact()` | PX-6 | `impact("auth/login.py")` returns the right dependents |
| PX-8 | todo | M2: `brain/memory.py` sourced facts + Librarian supersede logic; two-pass retrieval (Nano filter, Super read). Leave a clear hook for the JEV first-pass filter (ARCHITECTURE.md 1D) | PX-1 | Contradicting fact supersedes old one with reason; nothing deleted |
| PX-9 | todo | M2: `code_analyst.py`, `fact_checker.py`, `task_planner.py`; Conductor becomes LLM router on MODEL_SMART that spawns specialists in parallel with depth limit 3 and merges results (the recursion) | PX-7, PX-8 | One moment spawns a visible tree of jobs; answer cites files |
| PX-10 | todo | M3: 12 scripted meetings JSON with ground truth (8 train, 2 validation, 2 locked test) about demo_project | PX-6 | Files exist, ground truth filled |
| PX-11 | todo | M3: replay harness + `evaluate.py` metrics (ARCHITECTURE.md section 11) | PX-9, PX-10 | Prints scores for v1 on all splits |
| PX-12 | todo | M3: `prep.py` predictions + `coach.py` (MODEL_COACH = Nemotron-3.5-Super-VL, no Ultra on Crusoe): one edit at a time to prompt files / playbook / turn-taking values, validate, keep or reject, log to `coach_log.jsonl` | PX-11 | At least 3 Coach iterations logged; version latest vs v1 on locked test |
| PX-13 | doing | **PRIORITY: one call per drawing burst** (ARCH 1E.5 rule 3): Diagrammer emits all ops in ONE call (parallel tool calls or validated JSON-ops batch), then applies them progressively | PX-4 | 5-op burst under ~4 s instead of ~26 s |
| PX-14 | doing | **Topic tracking** in Listener: every batch returns `topic_id`, `title`, `topic_changed`; Conductor tags jobs with topic; Diagrammer draws into that topic's `frame` and emits `focus` on switch | PX-3 | Switching topics creates a new frame and pans; returning pans back |
| PX-15 | doing | **Rich drawing**: extend BoardState + Diagrammer tools with v2 ops (frame, focus, add_sticky, add_text, add_mermaid, add_chart, add_table, add_code, add_mock, sketch, update) + the choosing rule in ARCH 1E.4; fix Mermaid once on `draw_error` | PX-13 | "Walk me through the login sequence" draws a Mermaid sequence diagram; numbers produce a chart; "what would the screen look like" produces a mock |
| PX-17 | doing | **JEV fast path** (ARCH 1F): `server/agents/fast_decide.py` with `decide(state, questions)`; JEV backend (`typesafe-sdk`, `TYPESAFE_API_KEY`) + Nemotron Nano fallback; one call per final sentence with addressed/kind/topic_changed/wants_visual/is_echo; gate Listener, topic switch, Diagrammer tool choice, echo drop; log latency. Add `TYPESAFE_API_KEY=` to `.env.example` | PX-3 | Small-talk lines skip the Listener; topic switch detected in under ~0.5 s with JEV |
| PX-16 | todo | M2: `connect_repo`: build the code map from a public GitHub URL (shallow clone, size cap) | PX-7 | Pasting the demo repo URL builds the map |

## PARSH-CLAUDE (meeting page, look, mic, speech, audio)

| id | status | task | needs | check (done when) |
|---|---|---|---|---|
| RC-1 | done | `MeetingRoom.tsx` layout per ARCH 1E.1 (Meet/Zoom-like): tile grid (humans + Twin labeled "AI"), whiteboard as main stage when it has content (Whiteboard component from PREET-CLAUDE), captions, control bar, side panel | RX-1 | Page renders with mock data |
| RC-2 | done | `styles/theme.css`: Mac-native look per ARCHITECTURE.md section 4 (style only, no coucou assets). Dark and light | RC-1 | Looks like a native Mac app, not a web template |
| RC-3 | done | `lib/ws.ts`: connect `ws://localhost:8000/ws?room=demo&user=<name>`, auto-reconnect, typed send/receive using `protocol.ts`, event emitter | RX-1 | Connects to server; logs `status` messages |
| RC-4 | done | `lib/speech.ts`: Web Speech API wrapper: continuous, interim + final; emits `speechstart` the instant a user starts talking; auto-restarts when Chrome stops it; send `user_text` | RC-3 | Talking shows live captions; finals reach server |
| RC-5 | done | `lib/player.ts`: audio queue by `say_id`/`seq`, decode base64 WAV, play in order, `stopAll()` instantly; `data:null` -> `speechSynthesis` fallback; report playback start time for latency logging | RC-3 | Plays server chunks smoothly; stop is instant |
| RC-6 | done | Barge-in: on speechstart while playing -> `player.stopAll()` then send `barge_in`; handle `stop_audio` | RC-4, RC-5 | Talking over Twin silences it immediately |
| RC-7 | done | `ParticipantTile.tsx`: Twin tile states from `status` (listening, thinking, speaking glow, hand raised); `Captions.tsx` showing transcript with speaker names | RC-3 | States visibly change during a turn |
| RC-8 | done | Push-to-talk button + keyboard shortcut (hold space) sending `push_to_talk` | RC-3 | Holding it makes Twin respond without saying its name |
| RC-9 | todo | M4: polish pass, demo flow, record 1-minute video | M3 done | Video recorded |
| RC-10 | done | **Join screen** (`JoinScreen.tsx`): name, mic + camera preview, Join; room from URL `/room/demo` | RX-1 | Feels like joining a Meet call |
| RC-11 | done | **Camera + tiles** (`media.ts`, `ParticipantTile.tsx`): local webcam via getUserMedia; remote humans as avatar tiles; active-speaker ring from `participants`; Twin tile with avatar, AI label, voice ring animation | RC-3 | Speaking person's tile lights up |
| RC-12 | done | **Control bar + side panel** (`ControlBar.tsx`, `SidePanel.tsx`): mic, camera, present/whiteboard, captions, raise hand, people, Twin activity (AgentTree), tasks, connections, leave | RC-1 | All buttons work or are clearly disabled |
| RC-13 | done | **Echo guard, browser side** (`echoGuard.ts`, ARCH 1E.3): while Twin plays, ignore interim speech that matches Twin's current sentence; only barge in on non-matching words. Show a "use headphones" hint | RC-6 | Twin through speakers does not interrupt itself |
| RC-14 | done | **Connections tab** (`ConnectionsPanel.tsx`, ARCH 1E.6): GitHub card sends `connect_repo` (real); Calendar/Slack/Jira/Zoom/Meet cards marked demo; Calendar card has "Simulate standup" sending `calendar_join` | RC-12 | Simulate makes Twin join and show its brief |
| RC-15 | done | `VITE_SERVER_URL` support for two-laptop demo | RC-3 | Second laptop joins via tunnel URL |

## RX tasks: the board, OWNER PREET-CLAUDE (FINAL 14:15)

RX-1 was done by PARSH-CLAUDE. RX-2 to RX-9 are PREET-CLAUDE's. ALL RC tasks (including RC-4, RC-5, RC-6, RC-13 audio) are PARSH-CLAUDE's. PARSH-CODEX does not exist.

| id | status | task | needs | check (done when) |
|---|---|---|---|---|
| RX-1 | done | (PARSH-CLAUDE) Scaffold `web/` with Vite + React + TS + tldraw, `lib/protocol.ts` v1 types. **Follow-up for PREET-CLAUDE: add the PROTOCOL v2 types to `protocol.ts`** | - | `npm run dev` serves on 5173 |
| RX-2 | done | `lib/mockServer.ts`: fake message stream (status changes, transcript lines, a sequence of draw ops building a 5-box architecture, a rename, a highlight) toggled by `?mock=1` | RX-1 | UI works fully without backend |
| RX-3 | done | `Whiteboard.tsx`: tldraw editor; map 0-1000 grid to canvas; apply `add_box`, `connect` (arrow bound to shapes), `rename`, `remove`, `highlight`, `clear`, `board_snapshot`; keep a map id -> tldraw shape id; style by `kind`; animate new shapes in | RX-1 | Mock sequence builds the diagram correctly; rename edits in place; remove deletes edges too |
| RX-4 | done | Auto-fit and tidy: zoom to fit after changes; nudge boxes that overlap | RX-3 | No overlapping boxes in the mock sequence |
| RX-5 | done | `AgentTree.tsx`: live tree from `agent_event` (nest by `parent_id`, states started/done/failed, model name badge) | RX-1 | Mock events render as a growing/collapsing tree |
| RX-6 | done | M2: task list panel from `task` messages | RX-1 | Tasks appear with owner |
| RX-7 | done | M3: `Scoreboard.tsx` from `metric` messages: v1 vs latest on locked test, rejected changes listed | RX-1 | Renders mock metrics |
| RX-8 | done | **Rich shapes** in `web/src/components/board/` (ARCH 1E.4, PROTOCOL v2): frame + focus (pan/zoom), sticky, text, Mermaid (mermaid.js -> SVG image; on failure send `draw_error`), chart (render SVG), table, code, mock (sandboxed iframe custom shape), sketch (draw strokes), update | RX-3 | Mock server sequence shows every shape type correctly |
| RX-9 | done | Progressive appearance: animate each new shape in; stage mode ("Twin is presenting") when content arrives | RX-8 | Shapes appear one by one, smoothly |

---

## Humans
| id | owner | task |
|---|---|---|
| H-1 | PARSH | Get the free NVIDIA key (build.nvidia.com, Magpie TTS) into `server/.env` (PC-3 is blocked on it) |
| H-2 | PARSH | 10-minute check: can a rented meeting bot (e.g. Recall.ai) SPEAK into Google Meet/Zoom, price, free trial, transcript speed. Post findings in AGENT_CHAT.txt. Build only after M3 |
| H-3 | PREET | DONE: JEV = TypeSafe AI System One model (ARCH 1F) |
| H-4 | PREET | Get a TypeSafe API key at console.typesafe.ai/keys (may be early access/waitlist). Put it in `server/.env` as `TYPESAFE_API_KEY`. If no access today, PX-17 runs on the Nano fallback |

## Milestone gates (humans check these)
- [ ] M0 setup: server + web start, smoke_crusoe and smoke_tts pass
- [ ] M1 talks and draws live (ARCHITECTURE.md section 2 checks 1 to 5)
- [ ] M2 knows the code
- [ ] M3 learns, scoreboard shows v1 vs latest
- [ ] M4 demo rehearsed twice, video recorded
- [ ] Submitted by 5:00 PM
