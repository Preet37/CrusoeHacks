"""Authoritative state and validation for PROTOCOL v1/v2 whiteboard operations."""

from __future__ import annotations

import copy
import math
import re
from collections.abc import Mapping
from typing import Any


_ID = re.compile(r"[a-z][a-z0-9]*(?:[_-][a-z0-9]+)*\Z")
_KINDS = frozenset({"service", "datastore", "client", "external", "note"})
_STICKY_COLORS = frozenset({"yellow", "blue", "green", "pink", "orange"})
_SKETCH_COLORS = frozenset({"black", "red", "blue", "green", "orange", "violet"})
_TEXT_SIZES = frozenset({"s", "m", "l"})
_CHART_KINDS = frozenset({"bar", "line", "pie", "scatter"})
_UNSAFE_HTML = re.compile(
    r"<\s*/?\s*(?:script|iframe|object|embed|link|meta|form|base)\b"
    r"|\bon[a-z]+\s*=|\b(?:src|href|action|srcdoc)\s*="
    r"|javascript\s*:|\burl\s*\(|@import\b",
    re.IGNORECASE,
)
_FIELDS = {
    "add_box": (frozenset({"id", "label", "x", "y", "kind"}), frozenset({"frame"})),
    "connect": (frozenset({"id", "from_id", "to_id"}), frozenset({"label", "frame"})),
    "rename": (frozenset({"id", "label"}), frozenset()),
    "remove": (frozenset({"id"}), frozenset()),
    "highlight": (frozenset({"ids", "on"}), frozenset()),
    "clear": (frozenset(), frozenset()),
    "frame": (frozenset({"id", "title", "x", "y"}), frozenset({"w", "h", "frame"})),
    "focus": (frozenset({"id"}), frozenset()),
    "add_sticky": (frozenset({"id", "text", "color", "x", "y"}), frozenset({"w", "h", "frame"})),
    "add_text": (frozenset({"id", "text", "x", "y"}), frozenset({"size", "w", "h", "frame"})),
    "add_mermaid": (frozenset({"id", "code", "x", "y"}), frozenset({"w", "h", "frame"})),
    "add_chart": (frozenset({"id", "chart", "title", "series", "x", "y"}), frozenset({"labels", "w", "h", "frame"})),
    "add_table": (frozenset({"id", "columns", "rows", "x", "y"}), frozenset({"w", "h", "frame"})),
    "add_code": (frozenset({"id", "language", "code", "x", "y"}), frozenset({"w", "h", "frame"})),
    "add_mock": (frozenset({"id", "title", "html", "x", "y"}), frozenset({"w", "h", "frame"})),
    "sketch": (frozenset({"id", "strokes", "color"}), frozenset({"frame"})),
    "update": (frozenset({"id"}), frozenset()),  # Dynamic editable fields of the target shape.
}
_RICH_CREATE = frozenset({
    "frame", "add_sticky", "add_text", "add_mermaid", "add_chart",
    "add_table", "add_code", "add_mock", "sketch",
})
_RENAME_FIELD = {
    "frame": "title", "add_sticky": "text", "add_text": "text",
    "add_chart": "title", "add_mock": "title",
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


def _grid_number(value: Any, name: str, *, positive: bool = False) -> int | float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not (0 < value <= 1000 if positive else 0 <= value <= 1000)
            or not math.isfinite(value)):
        limit = "greater than 0 and at most 1000" if positive else "from 0 to 1000"
        raise ValueError(f"{name} must be a finite number {limit}")
    return value


def _bounded_text(value: Any, name: str, limit: int, *, empty_ok: bool = False) -> str:
    if not isinstance(value, str) or (not empty_ok and not value.strip()) or len(value) > limit:
        raise ValueError(f"{name} must be a string of at most {limit} characters")
    return value


def _choice(value: Any, name: str, choices: frozenset[str]) -> str:
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"{name} must be one of {', '.join(sorted(choices))}")
    return value


def _finite_number(value: Any, name: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError(f"{name} must be a finite number")
    return value


def _table_cell(value: Any) -> str | int | float | bool:
    if isinstance(value, str):
        return _bounded_text(value, "table cell", 500, empty_ok=True)
    if isinstance(value, bool):
        return value
    return _finite_number(value, "table cell")


class BoardState:
    """Apply one validated draw operation at a time to a room's whiteboard.

    ``apply`` returns a new broadcast-ready draw message. A rejected operation
    raises ValueError without changing boxes, edges, or highlights.
    """

    def __init__(self) -> None:
        self.boxes: dict[str, dict[str, Any]] = {}
        self.edges: dict[str, dict[str, str]] = {}
        self.shapes: dict[str, dict[str, Any]] = {}
        self.active_frame: str | None = None
        self.highlighted: set[str] = set()
        # Not part of the wire snapshot: used to reject model output based on
        # a board that changed while the model was thinking.
        self.revision = 0

    def _all_ids(self) -> set[str]:
        return self.boxes.keys() | self.edges.keys() | self.shapes.keys()

    def _frame_id(self, value: Any, item_id: str | None = None) -> str:
        frame_id = _valid_id(value)
        if frame_id not in self.shapes or self.shapes[frame_id]["op"] != "frame":
            raise ValueError(f"frame does not exist: {frame_id}")
        # A frame can move into a parent, but not into itself or a descendant.
        current = frame_id
        while current is not None:
            if current == item_id:
                raise ValueError("frame membership would create a cycle")
            current = self.shapes[current].get("frame")
        return frame_id

    def _validated_shape(self, source: Mapping[str, Any]) -> dict[str, Any]:
        """Return an independent canonical v2 creation op, with no side effects."""
        name = source["op"]
        required, optional = _FIELDS[name]
        fields = set(source) - {"type", "op"}
        if missing := required - fields:
            raise ValueError(f"missing {name} fields: {', '.join(sorted(missing))}")
        if extra := fields - required - optional:
            raise ValueError(f"unexpected {name} fields: {', '.join(sorted(extra))}")
        item_id = _valid_id(source["id"])
        shape: dict[str, Any] = {"type": "draw", "op": name, "id": item_id}
        if "frame" in source:
            shape["frame"] = self._frame_id(source["frame"], item_id)
        if name != "sketch":
            shape["x"] = _grid_number(source["x"], "x")
            shape["y"] = _grid_number(source["y"], "y")
            for size in ("w", "h"):
                if size in source:
                    shape[size] = _grid_number(source[size], size, positive=True)

        if name == "frame":
            shape["title"] = _bounded_text(source["title"], "title", 200)
        elif name == "add_sticky":
            shape["text"] = _bounded_text(source["text"], "text", 2000)
            shape["color"] = _choice(source["color"], "color", _STICKY_COLORS)
        elif name == "add_text":
            shape["text"] = _bounded_text(source["text"], "text", 4000)
            if "size" in source:
                shape["size"] = _choice(source["size"], "size", _TEXT_SIZES)
        elif name == "add_mermaid":
            shape["code"] = _bounded_text(source["code"], "code", 16000)
        elif name == "add_chart":
            shape["chart"] = _choice(source["chart"], "chart", _CHART_KINDS)
            shape["title"] = _bounded_text(source["title"], "title", 200)
            labels = source.get("labels", [])
            minimum_labels = 0 if shape["chart"] == "scatter" else 1
            if not isinstance(labels, list) or not minimum_labels <= len(labels) <= 64:
                raise ValueError(f"labels must contain {minimum_labels} to 64 strings")
            shape["labels"] = [_bounded_text(label, "label", 200) for label in labels]
            series = source["series"]
            if not isinstance(series, list) or not 1 <= len(series) <= 16:
                raise ValueError("series must contain 1 to 16 data sets")
            checked_series = []
            for entry in series:
                if not isinstance(entry, dict) or set(entry) != {"name", "values"}:
                    raise ValueError("each series needs only name and values")
                values = entry["values"]
                if not isinstance(values, list):
                    raise ValueError("series values must be a list")
                if shape["chart"] == "scatter":
                    if not 1 <= len(values) <= 64:
                        raise ValueError("scatter series must contain 1 to 64 points")
                    if labels and len(values) != len(labels):
                        raise ValueError("labeled scatter series must have one point per label")
                    checked_values = []
                    for pair in values:
                        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                            raise ValueError("scatter values must be [x, y] pairs")
                        checked_values.append([_finite_number(v, "scatter value") for v in pair])
                else:
                    if len(values) != len(labels):
                        raise ValueError("each series must have one value per label")
                    checked_values = [_finite_number(v, "chart value") for v in values]
                checked_series.append({"name": _bounded_text(entry["name"], "series name", 100),
                                       "values": checked_values})
            shape["series"] = checked_series
        elif name == "add_table":
            columns = source["columns"]
            rows = source["rows"]
            if not isinstance(columns, list) or not 1 <= len(columns) <= 20:
                raise ValueError("columns must contain 1 to 20 strings")
            if not isinstance(rows, list) or len(rows) > 100:
                raise ValueError("rows must be a list of at most 100 rows")
            shape["columns"] = [_bounded_text(column, "column", 200) for column in columns]
            checked_rows = []
            for row in rows:
                if not isinstance(row, list) or len(row) != len(columns):
                    raise ValueError("every row must match the number of columns")
                checked_rows.append([_table_cell(cell) for cell in row])
            shape["rows"] = checked_rows
        elif name == "add_code":
            shape["language"] = _bounded_text(source["language"], "language", 40)
            shape["code"] = _bounded_text(source["code"], "code", 16000)
        elif name == "add_mock":
            shape["title"] = _bounded_text(source["title"], "title", 200)
            html = _bounded_text(source["html"], "html", 4096)
            if len(html.encode("utf-8")) > 4096:
                raise ValueError("html must be at most 4096 UTF-8 bytes")
            if _UNSAFE_HTML.search(html):
                raise ValueError("html contains active or external content")
            shape["html"] = html
        elif name == "sketch":
            shape["color"] = _choice(source["color"], "color", _SKETCH_COLORS)
            strokes = source["strokes"]
            if not isinstance(strokes, list) or not 1 <= len(strokes) <= 32:
                raise ValueError("strokes must contain 1 to 32 paths")
            checked_strokes = []
            for stroke in strokes:
                if not isinstance(stroke, list) or not 1 <= len(stroke) <= 500:
                    raise ValueError("each stroke must contain 1 to 500 points")
                checked_points = []
                for point in stroke:
                    if not isinstance(point, (list, tuple)) or len(point) != 2:
                        raise ValueError("each point must be an [x, y] pair")
                    checked_points.append([_grid_number(point[0], "point x"),
                                           _grid_number(point[1], "point y")])
                checked_strokes.append(checked_points)
            shape["strokes"] = checked_strokes
        return shape

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
        if name != "update" and (extra := keys - required - optional):
            raise ValueError(f"unexpected {name} fields: {', '.join(sorted(extra))}")

        # Build a fresh protocol message before touching the authoritative state.
        draw: dict[str, Any] = {"type": "draw", "op": name}
        if name == "add_box":
            box_id = _valid_id(op["id"])
            if box_id in self._all_ids():
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
            if "frame" in op:
                box["frame"] = self._frame_id(op["frame"], box_id)
            self.boxes[box_id] = box
            draw.update(box)

        elif name == "connect":
            edge_id = _valid_id(op["id"])
            source = _valid_id(op["from_id"])
            target = _valid_id(op["to_id"])
            if edge_id in self._all_ids():
                raise ValueError(f"board id already exists: {edge_id}")
            if source not in self.boxes or target not in self.boxes:
                raise ValueError("edge endpoints must be existing boxes")
            edge = {
                "id": edge_id,
                "from_id": source,
                "to_id": target,
                "label": _label(op.get("label", ""), empty_ok=True),
            }
            if "frame" in op:
                edge["frame"] = self._frame_id(op["frame"], edge_id)
            self.edges[edge_id] = edge
            draw.update(edge)

        elif name == "rename":
            item_id = _valid_id(op["id"])
            label = _label(op["label"])
            if item_id in self.boxes:
                self.boxes[item_id]["label"] = label
            elif item_id in self.shapes and (field := _RENAME_FIELD.get(self.shapes[item_id]["op"])):
                candidate = {**self.shapes[item_id], field: label}
                self.shapes[item_id] = self._validated_shape(candidate)
            else:
                raise ValueError(f"text-bearing shape does not exist: {item_id}")
            draw.update({"id": item_id, "label": label})

        elif name == "remove":
            item_id = _valid_id(op["id"])
            if item_id not in self._all_ids():
                raise ValueError(f"board id does not exist: {item_id}")
            removed = {item_id}
            # Frames may contain other frames. Collect descendants before any
            # mutation, then remove incident edges wherever they live.
            if item_id in self.shapes and self.shapes[item_id]["op"] == "frame":
                while True:
                    members = {
                        child_id for child_id, child in (
                            list(self.boxes.items()) + list(self.edges.items()) + list(self.shapes.items())
                        ) if child.get("frame") in removed
                    }
                    if members <= removed:
                        break
                    removed.update(members)
            removed.update(
                edge_id for edge_id, edge in self.edges.items()
                if edge["from_id"] in removed or edge["to_id"] in removed
            )
            for removed_id in removed:
                self.boxes.pop(removed_id, None)
                self.edges.pop(removed_id, None)
                self.shapes.pop(removed_id, None)
            self.highlighted.difference_update(removed)
            if self.active_frame in removed:
                self.active_frame = None
            draw["id"] = item_id

        elif name == "highlight":
            ids = op["ids"]
            on = op["on"]
            if not isinstance(ids, list):
                raise ValueError("highlight ids must be a list")
            if not isinstance(on, bool):
                raise ValueError("highlight on must be a boolean")
            checked = [_valid_id(item_id) for item_id in ids]
            if missing := set(checked) - self._all_ids():
                raise ValueError(f"board ids do not exist: {', '.join(sorted(missing))}")
            if on:
                self.highlighted.update(checked)
            else:
                self.highlighted.difference_update(checked)
            draw.update({"ids": checked, "on": on})

        elif name == "focus":
            frame_id = self._frame_id(op["id"])
            self.active_frame = frame_id
            draw["id"] = frame_id

        elif name in _RICH_CREATE:
            item_id = _valid_id(op["id"])
            if item_id in self._all_ids():
                raise ValueError(f"board id already exists: {item_id}")
            shape = self._validated_shape(op)
            self.shapes[item_id] = shape
            draw = copy.deepcopy(shape)

        elif name == "update":
            item_id = _valid_id(op["id"])
            if item_id not in self.shapes:
                raise ValueError(f"rich shape does not exist: {item_id}")
            patch = {k: v for k, v in op.items() if k not in {"type", "op", "id"}}
            if not patch:
                raise ValueError("update needs at least one field")
            previous = self.shapes[item_id]
            allowed = (_FIELDS[previous["op"]][0] | _FIELDS[previous["op"]][1]) - {"id"}
            if extra := patch.keys() - allowed:
                raise ValueError(f"fields cannot update this shape: {', '.join(sorted(extra))}")
            candidate = {**previous, **patch}
            if patch.get("frame", object()) is None:
                candidate.pop("frame", None)
            revised = self._validated_shape(candidate)
            self.shapes[item_id] = revised
            draw.update({"id": item_id, **{key: copy.deepcopy(revised.get(key)) for key in patch}})

        else:  # clear
            self.boxes.clear()
            self.edges.clear()
            self.shapes.clear()
            self.active_frame = None
            self.highlighted.clear()

        self.revision += 1
        return draw

    def summary(self) -> str:
        """Compact, deterministic description for agent prompts."""
        if not self.boxes and not self.shapes:
            return "Board empty."
        boxes = ", ".join(
            f"{box_id}={box['label']} ({box['kind']}) at {box['x']},{box['y']}"
            for box_id, box in sorted(self.boxes.items())
        )
        edges = ", ".join(
            f"{edge['from_id']}->{edge['to_id']}" + (f" ({edge['label']})" if edge["label"] else "")
            for _, edge in sorted(self.edges.items())
        )
        parts = [f"Boxes: {boxes or 'none'}", f"Edges: {edges or 'none'}"]
        if self.shapes:
            shapes = ", ".join(
                f"{shape_id}={shape['op']}"
                + (f" ({str(shape.get('title', shape.get('text', '')))[:80]})"
                   if shape.get("title") or shape.get("text") else "")
                + (f" in {shape['frame']}" if shape.get("frame") else "")
                for shape_id, shape in sorted(self.shapes.items())
            )
            parts.append(f"Shapes: {shapes}")
        if self.active_frame:
            parts.append(f"Focus: {self.active_frame}")
        if self.highlighted:
            parts.append(f"Highlighted: {', '.join(sorted(self.highlighted))}")
        return "; ".join(parts) + "."

    def snapshot(self) -> dict[str, Any]:
        """Return a detached snapshot; omit v2 fields for legacy-only boards."""
        snapshot = {
            "type": "board_snapshot",
            "boxes": [copy.deepcopy(box) for _, box in sorted(self.boxes.items())],
            "edges": [copy.deepcopy(edge) for _, edge in sorted(self.edges.items())],
        }
        if self.shapes:
            snapshot["shapes"] = [copy.deepcopy(shape) for _, shape in sorted(self.shapes.items())]
            snapshot["active_frame"] = self.active_frame
        return snapshot
