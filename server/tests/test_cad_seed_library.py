"""Seed model library (Loomin's .model-library shipped in server/cad/seed_library)."""
import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cad import library as L  # noqa: E402
from cad import pipeline  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("CAD_LIBRARY_DIR", str(tmp_path / "lib"))
    monkeypatch.delenv("CAD_SEED_LIBRARY", raising=False)
    monkeypatch.setattr(pipeline, "DISK_DIR", tmp_path)
    L.clear_library()
    yield
    L.clear_library()


def test_seed_files_exist_and_index_is_consistent():
    index = json.loads((L.SEED_DIR / "index.json").read_text())
    entries = index["entries"]
    assert len(entries) >= 21
    for entry in entries.values():
        glb = L.SEED_DIR / entry["glbFile"]
        assert glb.is_file() and glb.read_bytes()[:4] == b"glTF", entry["key"]
    assert "wind turbine" in L.seed_topics()


@pytest.mark.parametrize("request_text, key_prefix", [
    ("wind turbine", "photoreal-wind-turbine"),
    ("draw a 3D wind turbine", "photoreal-wind-turbine"),
    ("Wind turbine. Can you show me a wind turbine with its parts?", "photoreal-wind-turbine"),
    ("windmill", "photoreal-wind-turbine"),
    ("f1 car", "photoreal-formula-one"),
    ("banana", "photoreal-banana"),
    ("a phone", "photoreal-iphone"),
    ("Photoreal airplane", "photoreal-airplane"),
    ("pulley", "pulley-system"),
])
def test_topics_resolve_to_seed_entries(request_text, key_prefix):
    hit = L.lookup_model(request_text)
    assert hit is not None
    entry, blob = hit
    assert entry["key"].startswith(key_prefix) and entry["seed"]
    assert blob[:4] == b"glTF"


@pytest.mark.parametrize("request_text", ["quantum flux widget", "industrial robot arm", "turbine", "bicycle"])
def test_unknown_topic_returns_none(request_text):
    assert L.lookup_model(request_text) is None


def test_request_detection_needs_a_draw_verb_or_3d_cue():
    assert L.seed_for_request("draw a wind turbine with its parts")["key"].startswith("photoreal-wind-turbine")
    assert L.seed_for_request("show me an F1 car")["key"].startswith("photoreal-formula-one")
    assert L.seed_for_request("make car rentals service talk to Postgres") is None
    assert L.seed_for_request("the wind turbine team ships Friday") is None
    assert L.seed_for_request("remove the wind turbine") is None


def test_generate_model_replays_seed_instantly_with_a_stable_url(monkeypatch):
    async def no_llm(*a, **k):
        raise AssertionError("library hit must not call the model")

    monkeypatch.setattr(pipeline, "_ask", no_llm)
    first = asyncio.run(pipeline.generate_model("Wind turbine. Can you draw a 3D wind turbine?"))
    again = asyncio.run(pipeline.generate_model("draw a 3D wind turbine"))
    assert first["cached"] and first["seed"] and first["generator"] == "library"
    assert first["url"] == again["url"] and first["url"].endswith(f"/cad/{first['id']}.glb")
    assert pipeline.get_glb(first["id"])[:4] == b"glTF"


def test_rejecting_a_seed_match_stops_replaying_it():
    assert L.lookup_model("banana") is not None
    L.reject_model("banana")
    assert L.lookup_model("banana") is None


def test_seed_can_be_disabled(monkeypatch):
    monkeypatch.setenv("CAD_SEED_LIBRARY", "0")
    assert L.lookup_model("wind turbine") is None


def test_board_accepts_own_cad_url_but_not_other_http(monkeypatch):
    from board import BoardState
    monkeypatch.delenv("PUBLIC_SERVER_URL", raising=False)
    board = BoardState()
    own = {"gltf": "http://localhost:8000/cad/0123456789ab.glb", "autoRotate": True}
    op = {"op": "add_3d", "id": "wt", "title": "Wind turbine", "x": 500, "y": 100, "scene": own}
    assert board.apply(op)["scene"] == own
    with pytest.raises(ValueError):
        board.apply({**op, "id": "x", "scene": {"gltf": "http://example.com/cad/0123456789ab.glb"}})


def test_diagrammer_places_library_model_without_an_llm_call():
    from agents.diagrammer import _library_3d_op
    lines = [{"text": "can you draw a 3D wind turbine for us"}]
    op, topic = _library_3d_op(lines, lines, "add_3d", False)
    assert topic == "wind turbine" and op["op"] == "add_3d" and op["title"] == "Wind turbine"
    assert op["scene"]["gltf"].endswith(".glb") and op["scene"]["autoRotate"] is True
    assert _library_3d_op(lines, lines, "add_chart", False) is None
    other = [{"text": "draw a 3D robot arm with three joints"}]
    assert _library_3d_op(other, other, "add_3d", False) is None
