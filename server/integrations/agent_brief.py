"""Agent brief: one Markdown file a coding agent can take as full context for a ticket.

Built from everything Twin knows: the decision, the meeting discussion, Twin's take, the board
(boxes/arrows converted to a Mermaid flowchart, plus any Mermaid/table/code/sticky shapes), the
relevant repo files as GitHub links, acceptance checks and the agent prompt.

Works for a live session or for a saved meeting (meeting["board"], meeting["contribution"]),
so pushing a ticket from the dashboard after the meeting ended still carries the diagram.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

_KIND_SHAPE = {
    "service": ('["', '"]'),
    "datastore": ('[("', '")]'),
    "client": ('(["', '"])'),
    "external": ('{{"', '"}}'),
    "note": ('>"', '"]'),
}


def _clean(text: object, limit: int = 120) -> str:
    """Text safe inside a quoted Mermaid label."""
    return " ".join(str(text or "").replace('"', "'").split())[:limit]


def _node_id(raw: object) -> str:
    ident = re.sub(r"[^A-Za-z0-9_]", "_", str(raw or "n"))
    return ident if ident and not ident[0].isdigit() else f"n_{ident}"


def board_snapshot(session=None, meeting: dict | None = None) -> dict | None:
    """Live board snapshot, else the one saved with the meeting."""
    board = getattr(session, "board", None) if session is not None else None
    if board is not None:
        try:
            return board.snapshot()
        except Exception:  # noqa: BLE001 - best effort
            pass
    snap = (meeting or {}).get("board")
    return snap if isinstance(snap, dict) else None


def board_flowchart(snap: dict | None, frame: str | None = None) -> str | None:
    """Boxes + arrows on the board (optionally one topic frame) as a Mermaid flowchart."""
    if not isinstance(snap, dict):
        return None
    boxes = [b for b in snap.get("boxes") or [] if isinstance(b, dict) and b.get("id")]
    edges = [e for e in snap.get("edges") or [] if isinstance(e, dict)]
    if frame and any(b.get("frame") == frame for b in boxes):
        boxes = [b for b in boxes if b.get("frame") == frame]
    if not boxes:
        return None
    ids = {str(b["id"]) for b in boxes}
    lines = ["flowchart LR"]
    for b in boxes:
        left, right = _KIND_SHAPE.get(str(b.get("kind") or "service"), _KIND_SHAPE["service"])
        lines.append(f"  {_node_id(b['id'])}{left}{_clean(b.get('label') or b['id'])}{right}")
    for e in edges:
        a, z = str(e.get("from_id") or ""), str(e.get("to_id") or "")
        if a in ids and z in ids:
            label = _clean(e.get("label"), 60)
            arrow = f"-->|\"{label}\"|" if label else "-->"
            lines.append(f"  {_node_id(a)} {arrow} {_node_id(z)}")
    return "\n".join(lines)


def _shapes(snap: dict | None, frame: str | None) -> list[dict]:
    shapes = [s for s in (snap or {}).get("shapes") or [] if isinstance(s, dict)]
    if frame and any(s.get("frame") == frame for s in shapes):
        shapes = [s for s in shapes if s.get("frame") == frame]
    return shapes


def diagrams(snap: dict | None, frame: str | None = None) -> list[tuple[str, str]]:
    """[(title, mermaid code)]: the board flowchart first, then any Mermaid shapes."""
    out: list[tuple[str, str]] = []
    flow = board_flowchart(snap, frame)
    if flow:
        out.append(("Architecture (from the whiteboard)", flow))
    for s in _shapes(snap, frame):
        if s.get("op") == "add_mermaid" and isinstance(s.get("code"), str) and s["code"].strip():
            out.append((str(s.get("title") or s.get("id") or "Diagram"), s["code"].strip()))
    return out[:4]


def _board_extras(snap: dict | None, frame: str | None) -> list[str]:
    """Stickies, tables and code snippets from the board, as Markdown."""
    md: list[str] = []
    for s in _shapes(snap, frame):
        op = s.get("op")
        if op == "add_sticky" and s.get("text"):
            md.append(f"- Note: {_clean(s['text'], 300)}")
        elif op == "add_text" and s.get("text"):
            md.append(f"- {_clean(s['text'], 300)}")
        elif op == "add_table" and isinstance(s.get("columns"), list):
            cols = [_clean(c, 40) for c in s["columns"]][:8]
            rows = [[_clean(c, 60) for c in r][: len(cols)] for r in (s.get("rows") or [])[:12] if isinstance(r, list)]
            md += ["", "| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
            md += ["| " + " | ".join(r + [""] * (len(cols) - len(r))) + " |" for r in rows]
            md.append("")
        elif op == "add_code" and isinstance(s.get("code"), str):
            md += ["", f"```{s.get('language') or ''}", s["code"][:2000], "```", ""]
    return md


def _github_base(repo_url: str | None) -> str | None:
    url = str(repo_url or "").strip().rstrip("/")
    if not re.match(r"^https?://(www\.)?github\.com/[\w.-]+/[\w.-]+$", url):
        return None
    return url.removesuffix(".git")


def build_brief(ticket: dict, *, session=None, meeting: dict | None = None, repo_url: str | None = None,
                contribution: dict | None = None, context: list[dict] | None = None,
                files: list[tuple[str, int | None, int | None]] | None = None,
                jira_key: str | None = None, jira_url: str | None = None) -> str:
    """The full Markdown brief for one ticket."""
    title = str(ticket.get("title") or "Ticket from Twin").strip()
    frame = ticket.get("topic") or None
    snap = board_snapshot(session, meeting)
    if not frame and isinstance(snap, dict):
        frame = snap.get("active_frame")
    contribution = contribution or (meeting or {}).get("contribution")
    body = str(ticket.get("body") or "")
    decision = body.splitlines()[0].removeprefix("Decision: ") if body.startswith("Decision: ") else title
    room = getattr(session, "room", None) or (meeting or {}).get("room") or ""
    base = _github_base(repo_url)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    md = [f"# {jira_key + ': ' if jira_key else ''}{title}", ""]
    meta = [("Jira", f"[{jira_key}]({jira_url})" if jira_key and jira_url else None),
            ("Owner", ticket.get("owner")), ("Status", ticket.get("status")),
            ("Meeting", room or None), ("Repository", repo_url), ("Ticket id", ticket.get("id")),
            ("Generated", f"{now} by Twin")]
    md += [f"- **{k}:** {v}" for k, v in meta if v] + [""]

    md += ["## Goal", "", decision, ""]
    summary = (meeting or {}).get("summary")
    if summary:
        md += ["## Meeting summary", "", str(summary), ""]

    if context:
        md += ["## Discussion (around the decision)", ""]
        md += [f"> **{str(l.get('speaker') or 'Participant')}:** {str(l.get('text') or '').strip()}" for l in context]
        md.append("")

    if isinstance(contribution, dict) and any(contribution.get(k) for k in ("current", "impact", "risks", "question")):
        md += ["## Twin's analysis", ""]
        if contribution.get("current"):
            md += [f"**Current state:** {contribution['current']}", ""]
        if contribution.get("impact"):
            md += ["**If we change it:**"] + [f"- {x}" for x in contribution["impact"]] + [""]
        if contribution.get("risks"):
            md += ["**Risks:**"] + [f"- {x}" for x in contribution["risks"]] + [""]
        if contribution.get("question"):
            md += [f"**Open question:** {contribution['question']}", ""]

    found = diagrams(snap, frame)
    if found:
        md += ["## Diagrams", ""]
        for name, code in found:
            md += [f"### {name}", "", "```mermaid", code, "```", ""]
    extras = _board_extras(snap, frame)
    if extras:
        md += ["## Whiteboard notes", ""] + extras + [""]

    if files:
        md += ["## Relevant files", ""]
        for path, a, b in files:
            label = f"{path}:{a}-{b}" if a else path
            link = f"{base}/blob/HEAD/{path.lstrip('/')}" + (f"#L{a}-L{b}" if a else "") if base else None
            md.append(f"- [`{label}`]({link})" if link else f"- `{label}`")
        md.append("")

    acceptance = ticket.get("acceptance")
    checks = acceptance if isinstance(acceptance, list) else \
        [p.strip() for p in re.split(r"\n+|(?<=[.!?])\s+(?=[A-Z])", str(acceptance or "")) if p.strip()]
    if checks:
        md += ["## Acceptance criteria", ""] + [f"- [ ] {c}" for c in checks] + [""]

    md += ["## How to work on this", ""]
    if repo_url:
        md.append(f"1. Clone `{repo_url}` and create a branch `twin/{(jira_key or ticket.get('id') or 'ticket').lower()}`.")
    md += ["1. Read the relevant files above and their tests before editing; do not assume the decision is already implemented.",
           "1. Make the smallest change that satisfies every acceptance criterion; add or update tests.",
           "1. Open a PR referencing " + (jira_key or "this ticket") + " and summarise what changed and how it was verified.", ""]
    if ticket.get("agent_prompt"):
        md += ["## Agent prompt", "", "```text", str(ticket["agent_prompt"]).strip(), "```", ""]
    return "\n".join(md).strip() + "\n"
