"""Ticket Agent: after a ticket lands in Jira, a coding agent works it on the team's behalf.

Pipeline (each step is saved on the ticket as ticket["agent"], so the dashboard shows live progress):
  1. reading_jira     - the Jira issue (description, assignee, reporter, status) + its attachments:
                        <KEY>-agent-brief.md, <KEY>-diagram-N.mmd and any images people attached.
  2. cloning          - shallow, read-only clone of the workspace repo (repo_connector._clone: no credentials,
                        no hooks, size-capped). Repository code is NEVER executed.
  3. reading_diagrams - images go to Nemotron Super-VL, which describes them in text for the coder.
  4. writing_code     - Nemotron Super gets the brief, the issue, the diagrams and the relevant files and returns
                        full new contents for at most MAX_FILES files (JSON). One repair retry on bad JSON.
  5. checking         - changes are written into the clone; `git diff` builds the patch; .py files are
                        syntax-checked with compile() and .json files parsed (nothing is run).
  6. delivering       - Jira comment (summary, plan, files, checks) + <KEY>-agent.patch attached, issue moved to
                        In Review / In Progress. If GITHUB_TOKEN is set, a branch + pull request is opened too.
All model calls go to NVIDIA Nemotron on Crusoe (llm.chat).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

import config
import llm

log = logging.getLogger("twin.ticket_agent")

MAX_FILES = 4               # files the agent may change per ticket
MAX_FILE_BYTES = 16_000     # per file shown to the model
MAX_CONTEXT_FILES = 6
MAX_PATCH_BYTES = 200_000
CODE_TIMEOUT_S = float(os.getenv("TICKET_AGENT_TIMEOUT_S", "240"))
_SEM = asyncio.Semaphore(int(os.getenv("TICKET_AGENT_CONCURRENCY", "2")))
_RUNNING: dict[str, asyncio.Task] = {}
_TEXT_EXT = {".md", ".mmd", ".txt", ".patch", ".diff", ".json", ".yaml", ".yml"}
_IMAGE_MIME = {"image/png", "image/jpeg", "image/webp", "image/gif"}
_SKIP_DIRS = {".git", "node_modules", "dist", "build", ".venv", "venv", "__pycache__", ".next"}
_CODE_EXT = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".kt", ".rb", ".css", ".html",
             ".json", ".yaml", ".yml", ".toml", ".md", ".sql", ".sh"}
_PATH = re.compile(r"[\w.-]+(?:/[\w.-]+)+\.[A-Za-z0-9]{1,5}")

SYSTEM = """You are Twin's Ticket Agent, a senior engineer finishing a Jira ticket that came out of a team meeting.
You get: the ticket (decision, discussion, Twin's analysis, diagrams, acceptance criteria), the repository file list,
and the full text of the most relevant files. Make the smallest correct change that satisfies EVERY acceptance criterion.
Rules:
- Only modify files that exist in the file list, or create new files when clearly needed. At most 4 files.
- Return the COMPLETE new content of every file you change (not a diff). Keep unrelated code exactly as it is.
- Follow the existing style. Add or update a test when the repo has tests for that area.
- Never invent APIs that are not in the files you were shown. If the ticket is too vague to implement safely,
  return no changes and explain what is missing in "notes".
Return ONLY a JSON object:
{"summary": "one paragraph", "plan": ["step", ...],
 "changes": [{"path": "relative/path", "action": "modify" | "create", "content": "full file content"}],
 "tests": ["how to verify", ...], "notes": "risks / follow-ups"}"""


# ---------------------------------------------------------------- state on the ticket

def _now() -> int:
    return int(time.time() * 1000)


def _update(ticket_id: str, workspace: str | None, room: str | None, ticket_fields: dict | None = None,
            **fields) -> dict | None:
    """Merge fields into ticket["agent"] (and ticket_fields into the ticket) and persist.

    Saved meetings are re-read from SQLite on every lookup, so every change goes through here."""
    import integrations
    try:
        ticket, session, meeting = integrations.find_ticket(ticket_id, workspace, room)
    except integrations.TicketError:
        return None
    for k, v in (ticket_fields or {}).items():
        ticket[k] = {**ticket[k], **v} if isinstance(v, dict) and isinstance(ticket.get(k), dict) else v
    agent = dict(ticket.get("agent") or {})
    agent.update(fields)
    agent["updated_at"] = _now()
    ticket["agent"] = agent
    try:
        integrations._persist(session, meeting)
    except Exception:  # noqa: BLE001 - progress is best effort
        log.exception("could not persist agent progress for %s", ticket_id)
    return ticket


def is_running(ticket_id: str) -> bool:
    task = _RUNNING.get(ticket_id)
    return bool(task and not task.done())


def start(ticket_id: str, workspace: str | None, room: str | None = None) -> bool:
    """Spawn the agent for one ticket (no-op if it's already running). Returns True if started."""
    if is_running(ticket_id):
        return False
    _update(ticket_id, workspace, room, status="queued", step="queued", started_at=_now(), error=None)
    task = asyncio.create_task(_run_guarded(ticket_id, workspace, room))
    _RUNNING[ticket_id] = task
    task.add_done_callback(lambda _t, tid=ticket_id: _RUNNING.pop(tid, None))
    return True


def auto_enabled() -> bool:
    """TICKET_AGENT=auto (default) | manual. Under pytest it is manual unless a test sets TICKET_AGENT."""
    default = "manual" if "PYTEST_CURRENT_TEST" in os.environ else "auto"
    return os.getenv("TICKET_AGENT", default).strip().lower() == "auto"


async def _run_guarded(ticket_id: str, workspace: str | None, room: str | None) -> None:
    async with _SEM:
        try:
            await run(ticket_id, workspace, room)
        except Exception as e:  # noqa: BLE001 - surface every failure on the ticket
            log.exception("ticket agent failed for %s", ticket_id)
            _update(ticket_id, workspace, room, status="failed", step="failed",
                    error=str(e)[:400] or type(e).__name__, finished_at=_now())


# ---------------------------------------------------------------- the pipeline

async def run(ticket_id: str, workspace: str | None, room: str | None = None) -> dict:
    import integrations
    from integrations import jira
    from brain import repo_connector

    def step(name: str, **extra) -> None:
        _update(ticket_id, workspace, room, status="running", step=name, **extra)

    ticket, session, meeting = integrations.find_ticket(ticket_id, workspace, room)
    key = ticket.get("jira_key")

    # 1. Jira issue + attachments
    step("reading_jira")
    issue, texts, images = None, [], []
    if key and jira.configured():
        issue = await jira.get_issue(key)
        _update(ticket_id, workspace, room, ticket_fields={"jira": {k: issue.get(k) for k in
                                                                    ("assignee", "reporter", "status", "url")}})
        for att in issue.get("attachments") or []:
            name, mime, size = str(att.get("filename") or ""), str(att.get("mime") or ""), int(att.get("size") or 0)
            if name.endswith("-agent.patch") or not att.get("content"):
                continue
            try:
                if Path(name).suffix.lower() in _TEXT_EXT and size <= 200_000:
                    texts.append((name, (await jira.download(att["content"], 200_000)).decode("utf-8", "replace")))
                elif mime in _IMAGE_MIME and size <= 1_500_000 and len(images) < 3:
                    images.append((name, mime, await jira.download(att["content"], 1_500_000)))
            except jira.JiraError as e:
                log.info("skipping attachment %s: %s", name, e)
    if not any(n.endswith("agent-brief.md") for n, _ in texts):
        texts.insert(0, ("agent-brief.md", integrations.agent_brief_markdown(ticket, session, meeting, workspace,
                                                                             {"key": key, "url": (issue or {}).get("url")})))
    description = jira.adf_text(issue.get("description")) if issue else ""

    # 2. repository
    repo_url = integrations._repo_url(session, meeting, workspace)
    if not repo_url:
        raise RuntimeError("No repository connected for this workspace. Connect one on the dashboard, then retry.")
    step("cloning", repo_url=repo_url)
    root = Path(tempfile.mkdtemp(prefix="twin-agent-"))
    try:
        await repo_connector._clone(repo_connector.canonical_url(repo_url), root / "repo")
        repo = root / "repo"
        files = _list_files(repo)

        # 3. diagrams / images
        diagram_notes = ""
        if images:
            step("reading_diagrams", images=[n for n, _, _ in images])
            diagram_notes = await _describe_images(images, ticket)

        # 4. code
        step("writing_code", model=config.MODEL_SMART)
        context = "\n\n".join(f"===== {name} =====\n{text[:40_000]}" for name, text in texts)
        relevant = _pick_files(repo, files, " ".join([context, description, str(ticket.get("title") or "")]))
        result = await _write_code(ticket, issue, description, context, diagram_notes, files, relevant, repo)

        # 5. apply + check
        step("checking")
        changed, checks = _apply(repo, result.get("changes") or [])
        patch = await _git_diff(repo) if changed else ""
        if len(patch.encode()) > MAX_PATCH_BYTES:
            patch = patch.encode()[:MAX_PATCH_BYTES].decode("utf-8", "ignore") + "\n# (patch truncated)\n"
        ok = bool(changed) and all(c["ok"] for c in checks)

        # 6. deliver
        step("delivering")
        pr_url, pr_error, jira_status = None, None, None
        if changed and os.getenv("GITHUB_TOKEN"):
            try:
                pr_url = await _open_pr(repo_url, key or ticket_id, ticket, result, repo, changed)
            except Exception as e:  # noqa: BLE001 - the patch is still delivered via Jira
                pr_error = str(e)[:300]
                log.info("PR not opened for %s: %s", ticket_id, e)
        if key and jira.configured():
            files_att = [(f"{key}-agent.patch", patch.encode(), "text/x-diff")] if patch else []
            if files_att:
                await jira.attach_files(key, files_att)
            await jira.add_comment(key, _comment(result, changed, checks, pr_url, key, bool(patch)))
            if changed:
                jira_status = await jira.transition(key, ("review", "progress"))
        final = {
            "status": "done" if ok else ("needs_review" if changed else "no_change"),
            "step": "done", "finished_at": _now(), "summary": str(result.get("summary") or "")[:1500],
            "plan": [str(p)[:300] for p in (result.get("plan") or [])][:10],
            "tests": [str(t)[:300] for t in (result.get("tests") or [])][:8],
            "notes": str(result.get("notes") or "")[:1000], "files": changed, "checks": checks,
            "patch": patch, "pr_url": pr_url, "pr_error": pr_error,
        }
        _update(ticket_id, workspace, room, ticket_fields={"jira": {"status": jira_status}} if jira_status else None,
                **final)
        return final
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------- helpers

def _list_files(repo: Path) -> list[str]:
    out = []
    for base, dirs, names in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for n in names:
            p = Path(base) / n
            if p.suffix.lower() in _CODE_EXT and not p.is_symlink():
                out.append(str(p.relative_to(repo)))
    return sorted(out)[:3000]


def _pick_files(repo: Path, files: list[str], text: str) -> list[str]:
    """Files named in the brief first, then the best keyword matches on path names."""
    named = [p for p in dict.fromkeys(_PATH.findall(text)) if p in files]
    words = {w for w in re.findall(r"[a-z][a-z0-9_]{2,}", text.lower())} - {
        "the", "and", "for", "with", "this", "that", "from", "twin", "ticket", "meeting", "should", "will"}
    scored = sorted(((sum(w in f.lower() for w in words), f) for f in files if f not in named), reverse=True)
    picked = named + [f for s, f in scored if s > 0]
    return picked[:MAX_CONTEXT_FILES]


def _read(repo: Path, rel: str) -> str:
    try:
        data = (repo / rel).read_text("utf-8", errors="replace")
    except OSError:
        return ""
    return data if len(data) <= MAX_FILE_BYTES else data[:MAX_FILE_BYTES] + "\n... (truncated)"


async def _describe_images(images: list[tuple[str, str, bytes]], ticket: dict) -> str:
    parts: list[dict] = [{"type": "text", "text":
                          f"These images are attached to the ticket '{ticket.get('title')}'. Describe every component, "
                          "arrow, label and annotation precisely, as a developer would need to implement it."}]
    for name, mime, data in images:
        parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(data).decode()}"}})
    try:
        return await llm.chat(config.MODEL_COACH, [{"role": "user", "content": parts}], max_tokens=1500, timeout=90)
    except Exception as e:  # noqa: BLE001 - images are optional context
        log.info("image description failed: %s", e)
        return ""


def _parse_json(raw: str) -> dict:
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("not an object")
    return data


async def _write_code(ticket, issue, description, context, diagram_notes, files, relevant, repo) -> dict:
    user = [f"# Ticket {ticket.get('jira_key') or ticket.get('id')}: {ticket.get('title')}"]
    if issue:
        user.append(f"Assignee: {issue.get('assignee')} | Reporter: {issue.get('reporter')} | Status: {issue.get('status')}")
    if description:
        user.append("## Jira description\n" + description[:12_000])
    user.append("## Attached context\n" + context[:40_000])
    if diagram_notes:
        user.append("## What the attached images show (from Nemotron Super-VL)\n" + diagram_notes[:4000])
    user.append("## Repository files\n" + "\n".join(files[:600]))
    for rel in relevant:
        user.append(f"## File: {rel}\n```\n{_read(repo, rel)}\n```")
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "\n\n".join(user)}]
    kw = {"max_tokens": 12_000, "timeout": CODE_TIMEOUT_S, "response_format": {"type": "json_object"}}
    raw = await llm.chat(config.MODEL_SMART, messages, **kw)
    try:
        return _parse_json(raw)
    except (ValueError, json.JSONDecodeError):
        repair = messages + [{"role": "assistant", "content": raw[:6000]},
                             {"role": "user", "content": "That was not valid JSON. Return ONLY the JSON object described."}]
        return _parse_json(await llm.chat(config.MODEL_SMART, repair, **kw))


def _apply(repo: Path, changes: list) -> tuple[list[dict], list[dict]]:
    changed, checks = [], []
    root = repo.resolve()
    for ch in changes[:MAX_FILES]:
        if not isinstance(ch, dict) or not isinstance(ch.get("content"), str):
            continue
        rel = str(ch.get("path") or "").strip().lstrip("/")
        target = (repo / rel).resolve()
        if not rel or ".git" in Path(rel).parts or root not in target.parents:
            checks.append({"path": rel, "ok": False, "detail": "rejected: path outside the repository"})
            continue
        existed = target.exists()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(ch["content"], "utf-8")
        changed.append({"path": rel, "action": "modify" if existed else "create"})
        checks.append(_check(rel, ch["content"]))
    return changed, checks


def _check(rel: str, content: str) -> dict:
    """Syntax checks only: nothing from the repository is executed."""
    suffix = Path(rel).suffix.lower()
    try:
        if suffix == ".py":
            compile(content, rel, "exec")
            return {"path": rel, "ok": True, "detail": "Python syntax OK"}
        if suffix == ".json":
            json.loads(content)
            return {"path": rel, "ok": True, "detail": "valid JSON"}
    except SyntaxError as e:
        return {"path": rel, "ok": False, "detail": f"SyntaxError line {e.lineno}: {e.msg}"}
    except ValueError as e:
        return {"path": rel, "ok": False, "detail": f"invalid JSON: {e}"}
    return {"path": rel, "ok": True, "detail": "written (no automatic check for this file type)"}


async def _git(repo: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
    proc = await asyncio.create_subprocess_exec("git", "-C", str(repo), "-c", "core.hooksPath=/dev/null", *args,
                                                env=env, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode:
        raise RuntimeError(f"git {args[0]} failed: {err.decode()[:200]}")
    return out.decode("utf-8", "replace")


async def _git_diff(repo: Path) -> str:
    await _git(repo, "add", "-A", "--intent-to-add")
    return await _git(repo, "diff", "--no-color", "--no-ext-diff")


def _comment(result: dict, changed: list[dict], checks: list[dict], pr_url: str | None, key: str, has_patch: bool):
    from integrations import jira
    content = [jira.heading("Twin ticket agent"),
               jira.paragraph(jira._text(str(result.get("summary") or "No summary.")[:2000]))]
    if result.get("plan"):
        content += [jira.heading("Plan"), jira.bullet_list([[jira._text(str(p)[:400])] for p in result["plan"][:10]])]
    if changed:
        content += [jira.heading("Files changed"),
                    jira.bullet_list([[jira._text(f"{c['action']}: {c['path']}")] for c in changed])]
        content += [jira.heading("Checks"),
                    jira.bullet_list([[jira._text(f"{'OK' if c['ok'] else 'FAIL'} {c['path']}: {c['detail']}")]
                                      for c in checks])]
    if result.get("tests"):
        content += [jira.heading("How to verify"),
                    jira.bullet_list([[jira._text(str(t)[:400])] for t in result["tests"][:8]])]
    if result.get("notes"):
        content += [jira.heading("Notes"), jira.paragraph(jira._text(str(result["notes"])[:1500]))]
    if pr_url:
        content.append(jira.paragraph(jira._text("Pull request: ", None), jira._text(pr_url, pr_url)))
    elif has_patch:
        content.append(jira.paragraph(jira._text(
            f"Patch attached as {key}-agent.patch (apply with: git apply {key}-agent.patch).")))
    else:
        content.append(jira.paragraph(jira._text("No code change was made; see notes.")))
    return content


async def _open_pr(repo_url: str, key: str, ticket: dict, result: dict, repo: Path, changed: list[dict]) -> str:
    """Branch + commit (GitHub contents API) + pull request. Needs GITHUB_TOKEN with repo write access."""
    import httpx
    m = re.match(r"https://github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", repo_url)
    if not m:
        raise RuntimeError("not a GitHub repository URL")
    owner, name = m.groups()
    api = f"https://api.github.com/repos/{owner}/{name}"
    headers = {"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}", "Accept": "application/vnd.github+json"}
    branch = f"twin/{re.sub(r'[^A-Za-z0-9._-]+', '-', key.lower())}-{int(time.time())}"
    async with httpx.AsyncClient(headers=headers, timeout=30) as gh:
        meta = (await gh.get(api)).raise_for_status().json()
        base = meta["default_branch"]
        sha = (await gh.get(f"{api}/git/ref/heads/{base}")).raise_for_status().json()["object"]["sha"]
        (await gh.post(f"{api}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": sha})).raise_for_status()
        for c in changed:
            path = c["path"]
            body = {"message": f"{key}: {ticket.get('title')}"[:200], "branch": branch,
                    "content": base64.b64encode((repo / path).read_bytes()).decode()}
            current = await gh.get(f"{api}/contents/{path}", params={"ref": branch})
            if current.status_code == 200:
                body["sha"] = current.json()["sha"]
            (await gh.put(f"{api}/contents/{path}", json=body)).raise_for_status()
        pr = (await gh.post(f"{api}/pulls", json={
            "title": f"{key}: {ticket.get('title')}"[:250], "head": branch, "base": base,
            "body": f"{result.get('summary') or ''}\n\nOpened by Twin's ticket agent for {key}."})).raise_for_status().json()
    return pr["html_url"]
