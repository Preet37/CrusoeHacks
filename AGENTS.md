# AGENTS.md (auto-loaded by Codex)

You are one of three coding agents (PREET-CLAUDE, PREET-CODEX, PARSH-CLAUDE) building **Twin** at the Recursive Agents Hackathon (Crusoe x Cerebral Valley, Oct 17 2026). Deadline: 5:00 PM.

## Before doing anything
1. Read `ARCHITECTURE.md` (the plan; sections 0, 1A, 1B, 1C, 1E, 2, 6, 9 at minimum), `PROTOCOL.md` (the contract), `TASKS.md` (the board), and all of `AGENT_CHAT.txt` (the team chat).
2. Your human told you your agent id in the first prompt: `PREET-CODEX` (the only Codex agent). If not, ask.
3. Only edit files your id owns (ARCHITECTURE.md section 9). Ask in `AGENT_CHAT.txt` for anything else.

## Working loop
1. `git pull --rebase`.
2. Re-read the bottom of `AGENT_CHAT.txt` for messages addressed to you (`@YOUR-ID`) and answer them first.
3. Take your next `todo` task in `TASKS.md` whose needs are done. Mark it `doing`, commit, push, and post `START` in the chat.
4. Build it. Run its check. Keep commits small: `[YOUR-ID] area: what`.
5. Mark it `done`, post `DONE` in the chat with how to run or test it. Push.
6. Repeat. If blocked, post `BLOCKED` with `@ID` of who can help, then take another unblocked task.

## Non-negotiables
- Public repo: never commit keys or `.env`. Keys are read from `server/.env`.
- All model calls go to NVIDIA Nemotron on Crusoe (`server/llm.py`, model ids only in `server/config.py`).
- New code only, written today. No copying from other projects (ideas are fine). Nothing from any employer.
- Follow `PROTOCOL.md` exactly. Propose changes in the chat; never change it alone.
- Do not break `main`. Fix or revert within 10 minutes.
