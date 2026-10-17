"""Authoritative state and validation for PROTOCOL v1 whiteboard operations."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


_ID = re.compile(r"[a-z][a-z0-9]*(?:[_-][a-z0-9]+)*\Z")
_KINDS = frozenset({"service", "datastore", "client", "external", "note"})
_FIELDS = {
    "add_box": (frozenset({"id", "label", "x", "y", "kind"}), frozenset()),
    "connect": (frozenset({"id", "from_id", "to_id"}), frozenset({"label"})),
    "rename": (frozenset({"id", "label"}), frozenset()),
    "remove": (frozenset({"id"}), frozenset()),
    "highlight": (frozenset({"ids", "on"}), frozenset()),
    "clear": (frozenset(), frozenset()),
}


def _valid_id(value: Any) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ValueError(f"invalid board id: {value!r}")
    return value


def _label(value: Any, *, empty_ok: bool = False) -> str:
    if not isinstance(value, str) or (not empty_ok and not value.strip()):
        raise ValueError("label must be a nonempty string" if not empty_ok else "label must be a string")
    return value


def _coordinate(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1000:
        raise ValueError(f"{name} must be an integer from 0 to 1000")
    return value


class BoardState:
    """Apply one validated draw operation at a time to a room's whiteboard.

    ``apply`` returns a new broadcast-ready draw message. A rejected operation
    raises ValueError without changing boxes, edges, or highlights.
    """

    def __init__(self) -> None:
        self.boxes: dict[str, dict[str, Any]] = {}
        self.edges: dict[str, dict[str, str]] = {}
        self.highlighted: set[str] = set()

    def apply(self, op: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(op, Mapping):
            raise ValueError("draw operation must be an object")
        if "type" in op and op["type"] != "draw":
            raise ValueError("message type must be draw")
        name = op.get("op")
        if not isinstance(name, str) or name not in _FIELDS:
            raise ValueError(f"unknown draw operation: {name!r}")

        required, optional = _FIELDS[name]
        keys = set(op) - {"type", "op"}
        if missing := required - keys:
            raise ValueError(f"missing {name} fields: {', '.join(sorted(missing))}")
        if extra := keys - required - optional:
            raise ValueError(f"unexpected {name} fields: {', '.join(sorted(extra))}")

        # Build a fresh protocol message before touching the authoritative state.
        draw: dict[str, Any] = {"type": "draw", "op": name}
        if name == "add_box":
            box_id = _valid_id(op["id"])
            if box_id in self.boxes or box_id in self.edges:
                raise ValueError(f"board id already exists: {box_id}")
            box = {
                "id": box_id,
                "label": _label(op["label"]),
                "x": _coordinate(op["x"], "x"),
                "y": _coordinate(op["y"], "y"),
                "kind": op["kind"],
            }
            if not isinstance(box["kind"], str) or box["kind"] not in _KINDS:
                raise ValueError(f"invalid box kind: {box['kind']!r}")
            self.boxes[box_id] = box
            draw.update(box)

        elif name == "connect":
            edge_id = _valid_id(op["id"])
            source = _valid_id(op["from_id"])
            target = _valid_id(op["to_id"])
            if edge_id in self.boxes or edge_id in self.edges:
                raise ValueError(f"board id already exists: {edge_id}")
            if source not in self.boxes or target not in self.boxes:
                raise ValueError("edge endpoints must be existing boxes")
            edge = {
                "id": edge_id,
                "from_id": source,
                "to_id": target,
                "label": _label(op.get("label", ""), empty_ok=True),
            }
            self.edges[edge_id] = edge
            draw.update(edge)

        elif name == "rename":
            box_id = _valid_id(op["id"])
            if box_id not in self.boxes:
                raise ValueError(f"box does not exist: {box_id}")
            label = _label(op["label"])
            self.boxes[box_id]["label"] = label
            draw.update({"id": box_id, "label": label})

        elif name == "remove":
            item_id = _valid_id(op["id"])
            if item_id in self.boxes:
                del self.boxes[item_id]
                removed_edges = {
                    edge_id for edge_id, edge in self.edges.items()
                    if item_id in (edge["from_id"], edge["to_id"])
                }
                for edge_id in removed_edges:
                    del self.edges[edge_id]
                self.highlighted.difference_update(removed_edges | {item_id})
            elif item_id in self.edges:
                del self.edges[item_id]
                self.highlighted.discard(item_id)
            else:
                raise ValueError(f"board id does not exist: {item_id}")
            draw["id"] = item_id

        elif name == "highlight":
            ids = op["ids"]
            on = op["on"]
            if not isinstance(ids, list):
                raise ValueError("highlight ids must be a list")
            if not isinstance(on, bool):
                raise ValueError("highlight on must be a boolean")
            checked = [_valid_id(item_id) for item_id in ids]
            if missing := set(checked) - self.boxes.keys() - self.edges.keys():
                raise ValueError(f"board ids do not exist: {', '.join(sorted(missing))}")
            if on:
                self.highlighted.update(checked)
            else:
                self.highlighted.difference_update(checked)
            draw.update({"ids": checked, "on": on})

        else:  # clear
            self.boxes.clear()
            self.edges.clear()
            self.highlighted.clear()

        return draw

    def summary(self) -> str:
        """Compact, deterministic description for agent prompts."""
        if not self.boxes:
            return "Board empty."
        boxes = ", ".join(
            f"{box_id}={box['label']} ({box['kind']}) at {box['x']},{box['y']}"
            for box_id, box in sorted(self.boxes.items())
        )
        edges = ", ".join(
            f"{edge['from_id']}->{edge['to_id']}" + (f" ({edge['label']})" if edge["label"] else "")
            for _, edge in sorted(self.edges.items())
        )
        parts = [f"Boxes: {boxes}", f"Edges: {edges or 'none'}"]
        if self.highlighted:
            parts.append(f"Highlighted: {', '.join(sorted(self.highlighted))}")
        return "; ".join(parts) + "."

    def snapshot(self) -> dict[str, Any]:
        """Return an independent PROTOCOL v1 board_snapshot message."""
        return {
            "type": "board_snapshot",
            "boxes": [box.copy() for _, box in sorted(self.boxes.items())],
            "edges": [edge.copy() for _, edge in sorted(self.edges.items())],
        }
