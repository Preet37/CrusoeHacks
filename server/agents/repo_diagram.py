"""Repository architecture drawn only from the session's indexed files/imports."""

from __future__ import annotations

import copy
import re
from collections import defaultdict
from pathlib import PurePosixPath
from uuid import uuid4


_REPO = re.compile(r"\b(?:repo(?:sitory)?|codebase|code base|project)\b", re.I)
_DRAW = re.compile(r"\b(?:draw|sketch|show|map|visuali[sz]e|diagram|architecture|structure|workflow|looks? like|walk me through|how (?:it |this |the (?:repo|repository) )?works)\b", re.I)


def repo_architecture_request(moments: list[dict], source_lines: list[dict] | None = None) -> str | None:
    """Recognize a repository overview without interpreting implementation claims."""
    for item in reversed(source_lines or moments):
        text = str(item.get("text") or "")
        if _REPO.search(text) and _DRAW.search(text):
            return text
    return None


def _selected(files: dict, limit: int = 20) -> list[str]:
    """Balance directories first, then retain their most connected indexed files."""
    paths = sorted(path for path, record in files.items() if isinstance(path, str)
                   and isinstance(record, dict))
    ordinary = [path for path in paths if not files[path].get("is_test")]
    if ordinary:
        paths = ordinary
    groups = defaultdict(list)
    for path in paths:
        groups[str(PurePosixPath(path).parent)].append(path)
    def rank(path):
        record = files[path]
        return (-len(record.get("imports", [])) - len(record.get("imported_by", [])), path)
    for members in groups.values():
        members.sort(key=rank)
    ordered = sorted(groups, key=lambda group: (rank(groups[group][0]), group))
    selected = []
    while ordered and len(selected) < limit:
        for group in list(ordered):
            selected.append(groups[group].pop(0))
            if not groups[group]:
                ordered.remove(group)
            if len(selected) >= limit:
                break
    return selected


def repo_ops(code_map, board, topic_id: str | None) -> list[dict]:
    """Build a bounded file graph; arrows mean indexed imports, never guessed flow."""
    files = getattr(code_map, "files", None)
    if not isinstance(files, dict) or not files:
        return []
    selected = _selected(files)
    if not selected:
        return []
    prefix = f"repo_{uuid4().hex[:8]}"
    mapped = {path: f"{prefix}_n{i}" for i, path in enumerate(selected)}
    # Five files per row keep long repo-relative labels readable at board scale.
    rows = [selected[i:i + 5] for i in range(0, len(selected), 5)]
    height = 880 // len(rows)
    ops = []
    if not topic_id:
        # Deterministic overview routing need not create a conversation topic.
        # Keep all rows inside one overview so the browser fits the full graph.
        topic_id = f"{prefix}_overview"
        ops.append({"op": "frame", "id": topic_id, "title": "Repository architecture",
                    "x": 0, "y": 0, "w": 1000, "h": 1000})
    for row, members in enumerate(rows):
        frame = f"{prefix}_r{row}"
        directories = list(dict.fromkeys(str(PurePosixPath(path).parent) for path in members))
        title = " / ".join(directories)[:120]
        ops.append({"op": "frame", "id": frame, "title": title,
                    "x": 20, "y": 40 + row * height, "w": 960, "h": height - 12,
                    **({"frame": topic_id} if topic_id else {})})
        for column, path in enumerate(members):
            owner = files[path].get("owner")
            label = path + (f"\nOwner: {owner}" if isinstance(owner, str) and owner else "")
            ops.append({"op": "add_box", "id": mapped[path], "label": label,
                        "kind": "service", "x": round(50 + column * 780 / max(1, len(members) - 1)),
                        "y": 380, "frame": frame})
    edges = [(path, target) for path in selected
             for target in files[path].get("imports", [])
             if isinstance(target, str) and target in mapped and path != target]
    for index, (source, target) in enumerate(sorted(set(edges))[:32]):
        ops.append({"op": "connect", "id": f"{prefix}_e{index}",
                    "from_id": mapped[source], "to_id": mapped[target], "label": "imports",
                    **({"frame": topic_id} if topic_id else {})})
    ops.append({"op": "add_text", "id": f"{prefix}_legend",
                "text": f"Repository map: {len(selected)} of {len(files)} indexed files. Arrows = imports; runtime / PR workflow is not inferred.",
                "x": 30, "y": 950, "w": 940, "h": 40, "size": "s",
                **({"frame": topic_id} if topic_id else {})})
    if topic_id:
        ops.append({"op": "focus", "id": topic_id})
    staged = copy.deepcopy(board)
    for op in ops:
        staged.apply(op)
    return ops


async def draw_repo(session, board, topic_id, publish, expected_revision) -> list[dict]:
    """Validate the complete graph before publishing; respect concurrent clears."""
    from agents.diagrammer import StaleBoardError
    code_map = getattr(session, "code_map", None)
    ops = repo_ops(code_map, board, topic_id)
    draws = []
    for op in ops:
        if session.board is not board or board.revision != expected_revision:
            raise StaleBoardError("board changed during repository diagram")
        if getattr(session, "code_map", None) is not code_map:
            raise StaleBoardError("repository changed during repository diagram")
        draw = board.apply(op)
        expected_revision = board.revision
        draws.append(draw)
        await publish(session, board, draw, expected_revision)
    return draws
