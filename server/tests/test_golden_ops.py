"""Shared v2/v2.1 draw fixtures are valid wire messages and board input."""

from __future__ import annotations

import json
from pathlib import Path

from board import BoardState
from protocol import dump, parse


FIXTURE = Path(__file__).with_name("golden_ops.json")
GOLDEN_OPS = {
    "add_box", "connect", "rename", "remove", "highlight", "clear",
    "frame", "focus", "add_sticky", "add_text", "add_mermaid",
    "add_chart", "add_table", "add_code", "add_mock", "sketch", "update", "add_3d",
}


def test_golden_draw_operations_roundtrip_and_replay() -> None:
    fixture = json.loads(FIXTURE.read_text())
    assert fixture["fixture_version"] == "v2.1"
    operations = fixture["operations"]
    assert {operation["op"] for operation in operations} == GOLDEN_OPS
    assert all(operation["type"] == "draw" for operation in operations)

    board = BoardState()
    for index, operation in enumerate(operations, start=1):
        # The same serialized objects are sent to the browser. Check the
        # parser/model serializer, not the dict passthrough in protocol.dump.
        wire = json.dumps(operation)
        parsed = parse(wire)
        assert dump(parsed) == operation
        assert board.apply(dump(parsed)) == operation
        assert board.revision == index

    assert board.snapshot() == fixture["expected_snapshot"]
    assert dump(parse(json.dumps(board.snapshot()))) == fixture["expected_snapshot"]
    assert sorted(board.highlighted) == fixture["expected_highlighted"]
    assert board.revision == fixture["expected_revision"] == len(operations)
