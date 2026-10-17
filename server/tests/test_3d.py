"""PROTOCOL v2.1 3D scenes validate atomically and survive snapshots/updates."""

from __future__ import annotations

import copy
import math

import pytest

from agents.draw_tools import DRAW_TOOLS
from board import BoardState
from protocol import dump, parse


SCENE = {
    "autoRotate": True,
    "objects": [
        {"id": "base", "shape": "cylinder", "pos": [0, 0, 0], "rot": [0, 0, 0],
         "scale": [1, 0.3, 1], "color": "#8a8f98", "label": "Base"},
        {"id": "joint", "shape": "sphere", "pos": [0.45, 1.8, 0],
         "scale": [0.25, 0.25, 0.25], "color": "#ffb020"},
    ],
}
ADD = {"op": "add_3d", "id": "robot", "title": "Robot arm", "x": 100, "y": 200,
       "w": 400, "h": 350, "scene": SCENE}


def _unchanged(board: BoardState, operation: dict) -> None:
    snapshot, highlighted, revision = copy.deepcopy(board.snapshot()), board.highlighted.copy(), board.revision
    with pytest.raises(ValueError):
        board.apply(operation)
    assert board.snapshot() == snapshot
    assert board.highlighted == highlighted
    assert board.revision == revision


def test_3d_create_update_rename_highlight_snapshot_and_frame_cascade() -> None:
    board = BoardState()
    assert board.snapshot() == {"type": "board_snapshot", "boxes": [], "edges": []}
    board.apply({"op": "frame", "id": "topic", "title": "Robotics", "x": 0, "y": 0})
    created = board.apply({**ADD, "frame": "topic"})
    assert created == {"type": "draw", **ADD, "frame": "topic"}
    assert dump(parse(created)) == created
    assert next(shape for shape in board.snapshot()["shapes"] if shape["id"] == "robot") == created

    new_scene = {"objects": [{"id": "panel", "shape": "plane", "pos": [0, 0, 0],
                              "scale": [2, 1, 0.1], "color": "#ffffff"}]}
    assert board.apply({"op": "update", "id": "robot", "scene": new_scene}) == {
        "type": "draw", "op": "update", "id": "robot", "scene": new_scene,
    }
    board.apply({"op": "rename", "id": "robot", "label": "Robot overview"})
    board.apply({"op": "highlight", "ids": ["robot"], "on": True})
    assert board.shapes["robot"]["title"] == "Robot overview"
    assert board.shapes["robot"]["scene"] == new_scene
    assert board.highlighted == {"robot"}
    board.apply({"op": "remove", "id": "topic"})
    assert board.snapshot() == {"type": "board_snapshot", "boxes": [], "edges": []}
    assert board.highlighted == set()


@pytest.mark.parametrize("bad_scene", [
    {}, {"autoRotate": True}, {"objects": []}, {"objects": [{}]},
    {"objects": SCENE["objects"] * 31},
    {"objects": [SCENE["objects"][0], SCENE["objects"][0]]},
    {"objects": [{**SCENE["objects"][0], "shape": "pyramid"}]},
    {"objects": [{**SCENE["objects"][0], "color": "red"}]},
    {"objects": [{**SCENE["objects"][0], "pos": [0, math.nan, 0]}]},
    {"objects": [{**SCENE["objects"][0], "pos": [True, 0, 0]}]},
    {"objects": [{**SCENE["objects"][0], "pos": [101, 0, 0]}]},
    {"objects": [{**SCENE["objects"][0], "rot": [0, math.inf, 0]}]},
    {"objects": [{**SCENE["objects"][0], "rot": [0, 9 * math.pi, 0]}]},
    {"objects": [{**SCENE["objects"][0], "scale": [1, 0, 1]}]},
    {"objects": [{**SCENE["objects"][0], "scale": [1, 101, 1]}]},
    {"objects": [{**SCENE["objects"][0], "extra": "ignored?"}]},
    {**SCENE, "autoRotate": "yes"}, {**SCENE, "extra": 1},
    {"gltf": "http://example.com/model.glb"},
    {"gltf": "https://user:pass@example.com/model.glb"},
])
def test_3d_invalid_scenes_reject_without_mutation(bad_scene: dict) -> None:
    board = BoardState()
    board.apply(ADD)
    _unchanged(board, {"op": "update", "id": "robot", "scene": bad_scene})
    _unchanged(board, {**ADD, "id": "other", "scene": bad_scene})


def test_3d_gltf_https_and_each_primitive_are_supported() -> None:
    board = BoardState()
    for index, kind in enumerate(("box", "cylinder", "sphere", "cone", "torus", "plane")):
        item = {"id": f"p{index}", "shape": kind, "pos": [0, 0, 0],
                "scale": [1, 1, 1], "color": "#abcdef"}
        board.apply({**ADD, "id": f"r{index}", "scene": {"objects": [item]}})
    gltf = {"gltf": "https://example.com/model.glb", "autoRotate": False}
    assert board.apply({**ADD, "id": "uploaded", "scene": gltf})["scene"] == gltf
    assert board.revision == 7
    assert len(board.snapshot()["shapes"]) == 7


def test_3d_accepts_exactly_sixty_unique_objects() -> None:
    board = BoardState()
    primitive = {"shape": "box", "pos": [0, 0, 0], "scale": [1, 1, 1], "color": "#123abc"}
    objects = [{"id": f"p{index}", **primitive} for index in range(60)]
    board.apply({**ADD, "scene": {"objects": objects}})
    assert len(board.shapes["robot"]["scene"]["objects"]) == 60
    _unchanged(board, {"op": "update", "id": "robot", "scene": {
        "objects": objects + [{"id": "p60", **primitive}],
    }})


def test_3d_draw_tool_exposes_scene_and_update_field() -> None:
    tools = {item["function"]["name"]: item["function"]["parameters"] for item in DRAW_TOOLS}
    assert "add_3d" in tools
    assert {"id", "title", "scene", "x", "y"} <= set(tools["add_3d"]["required"])
    assert tools["add_3d"]["properties"]["scene"]["properties"]["objects"]["maxItems"] == 60
    assert "scene" in tools["update"]["properties"]
