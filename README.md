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
