# PROTOCOL.md: Browser <-> Server contract

One WebSocket: `ws://localhost:8000/ws?room=demo&user=<name>`.
Every message is one JSON object with a `type` field. Times are milliseconds since epoch (`ts`).
Server mirror: `server/protocol.py` (pydantic). Web mirror: `web/src/lib/protocol.ts` (TypeScript).
**Changing this file requires agreement from both PREET-CLAUDE and PARSH-CODEX in `AGENT_CHAT.txt`.** Bump `PROTOCOL_VERSION` when you change it.

PROTOCOL_VERSION = 1

---

## Browser -> Server

### `hello`
Sent once after connecting.
```json
{"type":"hello","protocol":1,"user":"Preet","room":"demo"}
```

### `user_text`
Speech recognition output. Send interim updates (optional, for captions) and every final sentence.
```json
{"type":"user_text","speaker":"Preet","text":"so the app talks to an auth service","final":true,"ts":1760731200000}
```
- `final=false`: interim, server only uses it for captions and to detect that someone is talking.
- `final=true`: committed sentence, goes into history and to the agents.

### `barge_in`
Sent the instant the browser detects a human starting to speak while Twin audio is playing (browser has ALREADY stopped playback locally).
```json
{"type":"barge_in","speaker":"Preet","ts":1760731200500}
```

### `push_to_talk`
Optional explicit "Twin, respond to this" signal (button held while speaking).
```json
{"type":"push_to_talk","active":true,"ts":1760731200000}
```

### `control`
```json
{"type":"control","action":"mute_twin"}      // mute_twin | unmute_twin | clear_board | reset_session
```

---

## Server -> Browser

### `status`
Drives the Twin tile.
```json
{"type":"status","state":"listening"}         // listening | thinking | speaking | hand_raised
```

### `transcript`
Committed line for captions (includes Twin's own lines, speaker "Twin").
```json
{"type":"transcript","speaker":"Twin","text":"I put Redis next to auth.","ts":1760731201000}
```

### `audio`
One chunk of Twin speech. One `say_id` per Twin turn; `seq` orders chunks (usually one chunk per sentence). Play in `seq` order.
```json
{"type":"audio","say_id":"s_17","seq":0,"format":"wav","sample_rate":44100,"text":"I put Redis next to auth.","data":"<base64 WAV>","last":false}
```
- `last=true` on the final chunk of that turn.
- If audio fails, server sends the chunk with `"data":null`; browser speaks `text` with `speechSynthesis`.

### `stop_audio`
Stop and drop everything queued for `say_id` (or all if omitted).
```json
{"type":"stop_audio","say_id":"s_17"}
```

### `draw`
One whiteboard operation on a 0 to 1000 grid (both axes). Browser scales to the canvas. Ids are stable lowercase slugs.
```json
{"type":"draw","op":"add_box","id":"auth","label":"Auth service","x":420,"y":300,"kind":"service"}
{"type":"draw","op":"connect","id":"e_web_auth","from_id":"web","to_id":"auth","label":"login"}
{"type":"draw","op":"rename","id":"auth","label":"Auth (sessions)"}
{"type":"draw","op":"remove","id":"redis"}
{"type":"draw","op":"highlight","ids":["auth","db"],"on":true}
{"type":"draw","op":"clear"}
```
- `kind`: `service | datastore | client | external | note`. Suggested styling: datastore = cylinder-ish or distinct color, client = rounded, note = sticky.
- Removing a box removes its edges (browser should handle both a separate edge remove and the implicit one).

### `board_snapshot`
Full board state, sent on connect and after `clear` or reconnect. Browser replaces its board with this.
```json
{"type":"board_snapshot","boxes":[{"id":"web","label":"Web app","x":120,"y":300,"kind":"client"}],"edges":[{"id":"e_web_auth","from_id":"web","to_id":"auth","label":"login"}]}
```

### `agent_event`
For the live agent tree (M2/M4). `parent_id` lets the UI nest spawned jobs.
```json
{"type":"agent_event","event_id":"ev_42","parent_id":"ev_40","agent":"Code Analyst","model":"Nemotron-3-Super","state":"started","detail":"impact of changing auth/login.py","ts":1760731202000}
```
- `state`: `started | done | failed | cancelled`.

### `task`
(M2) A task created from a decision.
```json
{"type":"task","id":"t_3","title":"Move session storage to Redis","owner":"Priya","source":"meeting 2026-10-17 @ 12:14"}
```

### `metric`
(M3) Scoreboard rows.
```json
{"type":"metric","version":"v1","split":"locked_test","name":"prediction_score","value":0.42}
```

### `error`
```json
{"type":"error","message":"Crusoe call failed: timeout","recoverable":true}
```

---

## Sequences

**Normal turn:** `user_text(final)` -> `status:thinking` -> `transcript(Twin)` + `audio seq 0..n` (status `speaking`) -> `audio last=true` -> `status:listening`.

**Barge-in:** browser stops playback, sends `barge_in` -> server cancels generation and TTS -> `stop_audio` -> `status:listening`.

**Drawing:** `user_text(final)` -> (background) one or more `draw` messages -> later a Twin turn mentions the change.
