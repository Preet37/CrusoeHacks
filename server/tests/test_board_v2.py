"""Protocol v2 rich board state: validation, snapshots, editing and frame removal."""

from __future__ import annotations

import copy
import math

import pytest

from board import BoardState


def _shape(snapshot: dict, item_id: str) -> dict:
    return next(shape for shape in snapshot["shapes"] if shape["id"] == item_id)


def _unchanged(board: BoardState, op: dict) -> None:
    before = copy.deepcopy(board.snapshot())
    highlighted = board.highlighted.copy()
    revision = board.revision
    with pytest.raises(ValueError):
        board.apply(op)
    assert board.snapshot() == before
    assert board.highlighted == highlighted
    assert board.revision == revision


def test_every_rich_shape_snapshots_and_focus() -> None:
    board = BoardState()
    assert board.snapshot() == {"type": "board_snapshot", "boxes": [], "edges": []}
    operations = [
        {"op": "frame", "id": "t_auth", "title": "Auth redesign", "x": 0, "y": 0, "w": 1000, "h": 1000},
        {"op": "add_sticky", "id": "s1", "text": "Risk: migration", "color": "yellow", "x": 700, "y": 120, "frame": "t_auth"},
        {"op": "add_text", "id": "x1", "text": "Login today", "x": 60, "y": 40, "size": "l", "frame": "t_auth"},
        {"op": "add_mermaid", "id": "m1", "code": "sequenceDiagram\nBrowser->>Auth: login", "x": 100, "y": 200, "w": 600, "h": 400, "frame": "t_auth"},
        {"op": "add_chart", "id": "c1", "chart": "bar", "title": "p95 latency", "labels": ["v1", "v2"], "series": [{"name": "p95", "values": [420, 180]}], "x": 600, "y": 500, "w": 350, "h": 250, "frame": "t_auth"},
        {"op": "add_table", "id": "tb1", "columns": ["Option", "Cost"], "rows": [["JWT", "low"], ["Sessions", 42]], "x": 100, "y": 650, "frame": "t_auth"},
        {"op": "add_code", "id": "k1", "language": "python", "code": "def login():\n    pass", "x": 550, "y": 150, "w": 400, "h": 220, "frame": "t_auth"},
        {"op": "add_mock", "id": "u1", "title": "New login screen", "html": "<div><input placeholder='Email'/><button>Continue</button></div>", "x": 100, "y": 100, "w": 300, "h": 400, "frame": "t_auth"},
        {"op": "sketch", "id": "sk1", "strokes": [[[100, 100], [200.5, 120], [260, 200]]], "color": "red", "frame": "t_auth"},
    ]
    for index, op in enumerate(operations, 1):
        emitted = board.apply(op)
        assert emitted["type"] == "draw" and emitted["op"] == op["op"]
        assert board.revision == index
    board.apply({"op": "focus", "id": "t_auth"})
    snapshot = board.snapshot()
    assert snapshot["active_frame"] == "t_auth"
    assert len(snapshot["shapes"]) == len(operations)
    assert _shape(snapshot, "m1")["code"].startswith("sequenceDiagram")
    assert "Shapes:" in board.summary() and "Focus: t_auth" in board.summary()
    snapshot["shapes"][0]["id"] = "tampered"
    assert "tampered" not in board.shapes


def test_generic_update_and_rename_validate_before_replacing() -> None:
    board = BoardState()
    board.apply({"op": "frame", "id": "topic", "title": "Original", "x": 0, "y": 0})
    board.apply({"op": "add_mermaid", "id": "m1", "code": "flowchart LR\nA-->B", "x": 100, "y": 200, "frame": "topic"})
    board.apply({"op": "add_chart", "id": "c1", "chart": "line", "title": "Trend", "labels": ["Jan"], "series": [{"name": "p95", "values": [100]}], "x": 200, "y": 200})
    board.apply({"op": "add_table", "id": "tb1", "columns": ["A"], "rows": [["old"]], "x": 10, "y": 10})
    board.apply({"op": "add_mock", "id": "u1", "title": "Screen", "html": "<div>Old</div>", "x": 10, "y": 20})
    board.apply({"op": "update", "id": "m1", "code": "sequenceDiagram\nA->>B: hi", "x": 120})
    board.apply({"op": "update", "id": "c1", "labels": ["Jan", "Feb"], "series": [{"name": "p95", "values": [100, 90]}]})
    board.apply({"op": "update", "id": "tb1", "rows": [["new"]]})
    board.apply({"op": "update", "id": "u1", "html": "<div>New</div>"})
    board.apply({"op": "rename", "id": "topic", "label": "Renamed topic"})
    board.apply({"op": "rename", "id": "c1", "label": "New trend"})
    snapshot = board.snapshot()
    assert _shape(snapshot, "m1")["x"] == 120
    assert _shape(snapshot, "m1")["code"].startswith("sequenceDiagram")
    assert _shape(snapshot, "c1")["title"] == "New trend"
    assert _shape(snapshot, "tb1")["rows"] == [["new"]]
    assert _shape(snapshot, "u1")["html"] == "<div>New</div>"
    assert _shape(snapshot, "topic")["title"] == "Renamed topic"
    _unchanged(board, {"op": "update", "id": "c1", "series": [{"name": "p95", "values": [1]}]})
    _unchanged(board, {"op": "update", "id": "u1", "html": "<script>alert(1)</script>"})
    _unchanged(board, {"op": "update", "id": "m1", "id2": "bad"})
    _unchanged(board, {"op": "update", "id": "m1"})


def test_frame_membership_cascade_edges_and_all_shape_highlights() -> None:
    board = BoardState()
    board.apply({"op": "frame", "id": "outer", "title": "Outer", "x": 0, "y": 0})
    board.apply({"op": "frame", "id": "inner", "title": "Inner", "x": 10, "y": 10, "frame": "outer"})
    board.apply({"op": "add_box", "id": "auth", "label": "Auth", "x": 100, "y": 100, "kind": "service", "frame": "inner"})
    board.apply({"op": "add_box", "id": "web", "label": "Web", "x": 10, "y": 100, "kind": "client"})
    board.apply({"op": "connect", "id": "e_web_auth", "from_id": "web", "to_id": "auth"})
    board.apply({"op": "add_text", "id": "note", "text": "Info", "x": 10, "y": 10, "frame": "inner"})
    board.apply({"op": "focus", "id": "inner"})
    board.apply({"op": "highlight", "ids": ["outer", "inner", "auth", "e_web_auth", "note"], "on": True})
    _unchanged(board, {"op": "highlight", "ids": ["web", "missing"], "on": True})
    _unchanged(board, {"op": "frame", "id": "bad", "title": "Bad", "x": 0, "y": 0, "frame": "missing"})
    _unchanged(board, {"op": "update", "id": "outer", "frame": "inner"})
    board.apply({"op": "remove", "id": "outer"})
    assert set(board.boxes) == {"web"}
    assert board.edges == {} and board.shapes == {}
    assert board.active_frame is None and board.highlighted == set()
    assert board.snapshot() == {"type": "board_snapshot", "boxes": [{"id": "web", "label": "Web", "x": 10, "y": 100, "kind": "client"}], "edges": []}


@pytest.mark.parametrize("bad", [True, math.nan, math.inf, -1, 1001, 10**1000])
def test_rich_coordinates_and_sizes_reject_invalid_numbers(bad) -> None:
    board = BoardState()
    good = {"op": "add_text", "id": "x1", "text": "Hello", "x": 10, "y": 10, "w": 100}
    for field in ("x", "y", "w"):
        _unchanged(board, {**good, field: bad})
    _unchanged(board, {**good, "h": 0})
    board.apply({**good, "h": 50.5})
    assert board.shapes["x1"]["h"] == 50.5


def test_chart_table_sketch_and_mock_validation_are_atomic() -> None:
    board = BoardState()
    chart = {"op": "add_chart", "id": "c1", "chart": "scatter", "title": "Samples", "labels": ["a"], "series": [{"name": "points", "values": [[1, 2]]}], "x": 1, "y": 2}
    board.apply(chart)
    board.apply({**chart, "id": "c_unlabeled", "labels": [],
                 "series": [{"name": "points", "values": [[1, 2], [3, 4]]}]})
    without_labels = {key: value for key, value in chart.items() if key != "labels"}
    board.apply({**without_labels, "id": "c_no_labels"})
    _unchanged(board, {**chart, "id": "c2", "series": [{"name": "points", "values": [[1, math.nan]]}]})
    _unchanged(board, {**chart, "id": "c2", "series": [{"name": "points", "values": [1]}]})
    _unchanged(board, {**chart, "id": "c2", "series": [{"name": "points", "values": [[1, 2], [3, 4]]}]})
    _unchanged(board, {**chart, "id": "c2", "chart": "unknown"})
    _unchanged(board, {"op": "add_table", "id": "t1", "columns": ["A", "B"], "rows": [["one"]], "x": 1, "y": 2})
    _unchanged(board, {"op": "add_table", "id": "t1", "columns": ["A"], "rows": [[math.inf]], "x": 1, "y": 2})
    _unchanged(board, {"op": "sketch", "id": "sk1", "strokes": [[[0, 0], [math.nan, 1]]], "color": "red"})
    _unchanged(board, {"op": "sketch", "id": "sk1", "strokes": [[[0, 0]]], "color": "purple"})
    mock = {"op": "add_mock", "id": "u1", "title": "Login", "html": "<div>Safe</div>", "x": 1, "y": 2}
    for html in ("<script>alert(1)</script>", "<img src='https://example.com/x'>", "<p onload='run()'>x</p>", "é" * 2049):
        _unchanged(board, {**mock, "html": html})
    board.apply({**mock, "html": "é" * 2048})
    assert len(board.shapes["u1"]["html"].encode("utf-8")) == 4096
    assert board.revision == 4
