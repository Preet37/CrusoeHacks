"""Behavioral checks for the authoritative whiteboard state."""

import math
import unittest

from server.board import BoardState


def box(box_id: str, x: int = 100, y: int = 200, kind: str = "service") -> dict:
    return {"op": "add_box", "id": box_id, "label": box_id.title(), "x": x, "y": y, "kind": kind}


class BoardStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.board = BoardState()

    def assert_rejected_without_change(self, op: dict) -> None:
        before = self.board.snapshot()
        highlights = self.board.highlighted.copy()
        revision = self.board.revision
        with self.assertRaises(ValueError):
            self.board.apply(op)
        self.assertEqual(self.board.snapshot(), before)
        self.assertEqual(self.board.highlighted, highlights)
        self.assertEqual(self.board.revision, revision)

    def test_revision_advances_for_every_accepted_operation(self) -> None:
        self.assertEqual(self.board.revision, 0)
        operations = [
            {"op": "clear"},  # Even an empty clear invalidates old work.
            box("auth"),
            box("db"),
            {"op": "connect", "id": "e_auth_db", "from_id": "auth", "to_id": "db"},
            {"op": "rename", "id": "auth", "label": "Auth service"},
            {"op": "highlight", "ids": [], "on": True},  # A valid no-op still advances.
            {"op": "remove", "id": "e_auth_db"},
            {"op": "remove", "id": "db"},
            {"op": "clear"},
        ]
        for expected_revision, operation in enumerate(operations, start=1):
            self.board.apply(operation)
            self.assertEqual(self.board.revision, expected_revision)
        self.assert_rejected_without_change({"op": "remove", "id": "auth"})
        self.assertEqual(self.board.revision, len(operations))
        self.board.apply({"op": "clear"})
        self.assertEqual(self.board.revision, len(operations) + 1)
        self.assertEqual(set(self.board.snapshot()), {"type", "boxes", "edges"})

    def test_add_connect_snapshot_and_summary(self) -> None:
        self.assertEqual(self.board.summary(), "Board empty.")
        self.assertEqual(self.board.apply(box("web", 0, 1000, "client"))["type"], "draw")
        self.board.apply(box("auth"))
        self.assertEqual(
            self.board.apply({"type": "draw", "op": "connect", "id": "e_web_auth", "from_id": "web", "to_id": "auth", "label": "login"}),
            {"type": "draw", "op": "connect", "id": "e_web_auth", "from_id": "web", "to_id": "auth", "label": "login"},
        )
        snapshot = self.board.snapshot()
        self.assertEqual(set(snapshot), {"type", "boxes", "edges"})
        self.assertEqual(snapshot["type"], "board_snapshot")
        self.assertEqual(snapshot["boxes"][1], {"id": "web", "label": "Web", "x": 0, "y": 1000, "kind": "client"})
        self.assertEqual(snapshot["edges"][0]["label"], "login")
        self.assertIn("web->auth (login)", self.board.summary())
        snapshot["boxes"][0]["label"] = "Tampered"
        self.assertEqual(self.board.boxes["auth"]["label"], "Auth")

    def test_invalid_ids_coordinates_and_fields_are_atomic(self) -> None:
        self.board.apply(box("auth"))
        for invalid_id in ("Auth", "auth service", "1auth", "e!", "", True, None):
            with self.subTest(id=invalid_id):
                self.assert_rejected_without_change({**box("new"), "id": invalid_id})
        for bad_coord in (-1, 1001, True, False, math.nan, math.inf, 5.5, "42"):
            with self.subTest(coord=bad_coord):
                self.assert_rejected_without_change({**box("new"), "x": bad_coord})
                self.assert_rejected_without_change({**box("new"), "y": bad_coord})
        self.assert_rejected_without_change(box("auth"))
        self.assert_rejected_without_change({**box("new"), "kind": "unknown"})
        self.assert_rejected_without_change({**box("new"), "label": "   "})
        self.assert_rejected_without_change({**box("new"), "unexpected": 1})
        self.assert_rejected_without_change({"type": "board_snapshot", **box("new")})

    def test_connect_rejects_missing_endpoints_and_reused_ids(self) -> None:
        self.board.apply(box("auth"))
        connect = {"op": "connect", "id": "e_auth_db", "from_id": "auth", "to_id": "db"}
        self.assert_rejected_without_change(connect)
        self.board.apply(box("db", kind="datastore"))
        self.board.apply(connect)
        self.assertEqual(self.board.edges["e_auth_db"]["label"], "")
        self.assert_rejected_without_change(connect)
        self.assert_rejected_without_change({**connect, "id": "auth"})
        self.assert_rejected_without_change(box("e_auth_db"))
        self.assert_rejected_without_change({**connect, "id": "e_other", "from_id": "missing"})
        self.assert_rejected_without_change({**connect, "id": "e_other", "label": None})

    def test_rename_remove_box_and_remove_edge(self) -> None:
        for name in ("web", "auth", "db"):
            self.board.apply(box(name))
        self.board.apply({"op": "connect", "id": "e_web_auth", "from_id": "web", "to_id": "auth"})
        self.board.apply({"op": "connect", "id": "e_auth_db", "from_id": "auth", "to_id": "db"})
        self.board.apply({"op": "connect", "id": "e_web_db", "from_id": "web", "to_id": "db"})
        self.board.apply({"op": "rename", "id": "auth", "label": "Auth (sessions)"})
        self.assertEqual(self.board.boxes["auth"]["label"], "Auth (sessions)")
        self.assert_rejected_without_change({"op": "rename", "id": "e_web_db", "label": "No"})
        self.assert_rejected_without_change({"op": "rename", "id": "missing", "label": "No"})
        self.board.apply({"op": "remove", "id": "auth"})
        self.assertEqual(set(self.board.edges), {"e_web_db"})
        self.assertEqual(set(self.board.boxes), {"web", "db"})
        self.board.apply({"op": "remove", "id": "e_web_db"})
        self.assertEqual(self.board.edges, {})
        self.assert_rejected_without_change({"op": "remove", "id": "auth"})

    def test_highlight_validates_whole_request_and_clear(self) -> None:
        self.board.apply(box("auth"))
        self.board.apply(box("db"))
        self.board.apply({"op": "connect", "id": "e_auth_db", "from_id": "auth", "to_id": "db"})
        self.board.apply({"op": "highlight", "ids": ["auth", "e_auth_db"], "on": True})
        self.assertEqual(self.board.highlighted, {"auth", "e_auth_db"})
        self.assertIn("Highlighted: auth, e_auth_db", self.board.summary())
        self.assert_rejected_without_change({"op": "highlight", "ids": ["db", "missing"], "on": True})
        self.assert_rejected_without_change({"op": "highlight", "ids": ["auth", "db"], "on": 1})
        self.assert_rejected_without_change({"op": "highlight", "ids": "auth", "on": False})
        self.board.apply({"op": "highlight", "ids": ["auth"], "on": False})
        self.assertEqual(self.board.highlighted, {"e_auth_db"})
        self.board.apply({"op": "remove", "id": "auth"})
        self.assertEqual(self.board.highlighted, set())
        self.board.apply({"op": "clear"})
        self.assertEqual(self.board.snapshot(), {"type": "board_snapshot", "boxes": [], "edges": []})
        self.assertEqual(self.board.summary(), "Board empty.")


if __name__ == "__main__":
    unittest.main()
