"""server/cad: plan validation, OpenSCAD compile/render, and the add_3d -> GLB meeting hook."""
import asyncio
import copy

import pytest

from cad import hook as cad_hook
from cad import pipeline
from cad import plan as P
from cad import render as R

AIRPLANE = {
    "referenceProduct": "Cessna 172", "summary": "high-wing single-engine plane",
    "parts": [
        {"name": "fuselage", "primitive": "cylinder", "size": [7000, 1200, 1200], "position": [0, 0, 1500], "axis": "+x"},
        {"name": "nose cone", "primitive": "cone", "size": [900, 1200, 1200], "position": [3900, 0, 1500], "axis": "+x"},
        {"name": "wing", "primitive": "box", "size": [1500, 5000, 120], "position": [500, 2600, 1900], "mirror": "y"},
        {"name": "horizontal stabilizer", "primitive": "box", "size": [800, 1600, 80], "position": [-3200, 900, 1600], "mirror": "y"},
        {"name": "tail fin", "primitive": "box", "size": [900, 80, 1300], "position": [-3200, 0, 2500]},
        {"name": "propeller hub", "primitive": "sphere", "size": [300, 300, 300], "position": [4400, 0, 1500]},
        {"name": "propeller blade", "primitive": "box", "size": [60, 1800, 150], "position": [4450, 0, 1500]},
        {"name": "wheel", "primitive": "cylinder", "size": [500, 180, 500], "position": [600, 700, 650], "axis": "+y", "mirror": "y"},
    ],
}


def _plan(raw=AIRPLANE):
    return P.coerce_geometry_plan(copy.deepcopy(raw), "airplane")


def test_good_airplane_plan_validates():
    v = P.validate_geometry_plan(_plan())
    assert v.valid, v.errors
    assert all(c["pass"] for c in v.checks)


def test_floating_part_is_rejected():
    raw = copy.deepcopy(AIRPLANE)
    raw["parts"].append({"name": "lost engine", "primitive": "box", "size": [300, 300, 300], "position": [0, 0, 40000]})
    v = P.validate_geometry_plan(_plan(raw))
    assert not v.valid
    assert any("float disconnected" in e and "lost engine" in e for e in v.errors)


def test_blobs_and_empty_plans_are_rejected():
    blob = {"parts": [{"name": f"ball{i}", "primitive": "sphere", "size": [100, 100, 100], "position": [i * 50, 0, 0]}
                      for i in range(5)]}
    assert any("blob" in e for e in P.validate_geometry_plan(_plan(blob)).errors)
    assert P.coerce_geometry_plan({"parts": []}, "x") is None
    assert not P.validate_geometry_plan(None).valid
    few = {"parts": AIRPLANE["parts"][:2]}
    assert any("at least" in e for e in P.validate_geometry_plan(_plan(few)).errors)


def test_coercion_synonyms_and_mirror_duplicates():
    raw = {"parts": [
        {"name": "body", "type": "pipe", "dimensions": ["100", 20, 20], "position": [0, 0, 0], "axis": "x"},
        {"name": "left wing", "shape": "plate", "size": [20, 50, 2], "position": [0, 30, 0], "mirror": "y"},
        {"name": "right wing", "shape": "plate", "size": [20, 50, 2], "position": [0, -30, 0]},
        {"name": "bad", "primitive": "box", "size": [0, 1, 1], "position": [0, 0, 0]},
    ]}
    plan = _plan(raw)
    assert [p.name for p in plan.parts] == ["body", "left wing"]  # explicit twin + zero-size part dropped
    assert plan.parts[0].primitive == "cylinder" and plan.parts[0].axis == "+x"


def test_rotation_aware_bounds():
    part = P.PlanPart("rod", "cylinder", (10, 10, 100), (0, 0, 0), (0, 90, 0))
    lo, hi = P.part_aabb(part)
    assert hi[0] - lo[0] == pytest.approx(100) and hi[2] - lo[2] == pytest.approx(10)


def test_compiler_output_is_safe_and_normalised():
    raw = copy.deepcopy(AIRPLANE)
    raw["parts"][0]["name"] = "fuselage\ninclude </etc/passwd>"
    scad = P.plan_to_openscad(_plan(raw))
    R.check_scad_source(scad)  # the newline-smuggled include stays inside a // comment
    assert "mirror([0, 1, 0])" in scad and "rotate([0, 90, 0])" in scad
    for bad in ("import(\"x.stl\");", "include <a.scad>", "use <b.scad>"):
        with pytest.raises(R.RenderError):
            R.check_scad_source(scad + bad)


needs_openscad = pytest.mark.skipif(R.openscad_bin() is None, reason="openscad not installed")
needs_blender = pytest.mark.skipif(R.blender_bin() is None, reason="blender not installed")


@needs_openscad
def test_openscad_renders_compiled_plan(tmp_path):
    stl = R.scad_to_stl(P.plan_to_openscad(_plan()), str(tmp_path))
    assert stl.stat().st_size > 1000


@needs_openscad
@needs_blender
def test_full_render_to_glb():
    glb = R.render_glb(P.plan_to_openscad(_plan()))
    assert glb[:4] == b"glTF" and len(glb) > 1000


def test_glb_store_rejects_bad_ids(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, "DISK_DIR", tmp_path)
    pipeline.put_glb("abcdef012345", b"glTF-test")
    assert pipeline.get_glb("abcdef012345") == b"glTF-test"
    assert pipeline.get_glb("../../etc/passwd") is None
    assert pipeline.get_glb("nope") is None
    pipeline._store.clear()
    assert pipeline.get_glb("abcdef012345") == b"glTF-test"  # survives the LRU via disk


class _Board:
    def __init__(self):
        self.applied = []

    def apply(self, op):
        self.applied.append(op)
        raise ValueError("gltf must be an HTTPS URL")  # like the real board with an http URL


class _Session:
    room = "t"

    def __init__(self):
        self.board = _Board()
        self.history = [{"speaker": "Preet", "text": "let's design a small plane", "ts": 1}]
        self.sent = []
        self._broadcast_observers = []
        self.tasks = {}

    def spawn(self, name, coro):
        task = asyncio.ensure_future(coro)
        self.tasks[name] = task
        return task

    async def broadcast(self, msg):
        for cb in list(self._broadcast_observers):
            cb(msg)
        self.sent.append(msg)


def test_observer_runs_one_job_per_add_3d_and_updates_in_place(monkeypatch):
    calls = []

    async def fake_generate(description):
        calls.append(description)
        await asyncio.sleep(0)
        return {"id": "abc", "url": "http://localhost:8000/cad/abc.glb", "parts": 9, "ms": {"total": 1}}

    monkeypatch.setattr(pipeline, "generate_model", fake_generate)

    async def run():
        s = _Session()
        hook = cad_hook.attach(s, force=True)
        assert cad_hook.attach(s, force=True) is hook and len(s._broadcast_observers) == 1
        add = {"type": "draw", "op": "add_3d", "id": "plane", "title": "Airplane",
               "scene": {"objects": [{"id": "b", "shape": "box"}]}}
        await s.broadcast(add)
        await s.broadcast(dict(add))  # duplicate id: no second job
        await s.broadcast({"type": "draw", "op": "add_3d", "id": "g", "title": "x", "scene": {"gltf": "https://a/b.glb"}})
        await asyncio.gather(*s.tasks.values())
        return s, hook

    s, hook = asyncio.run(run())
    assert calls == ["Airplane. let's design a small plane"]
    assert hook.jobs_started == 1
    updates = [m for m in s.sent if m.get("op") == "update"]
    assert updates == [{"type": "draw", "op": "update", "id": "plane",
                        "scene": {"gltf": "http://localhost:8000/cad/abc.glb", "autoRotate": True},
                        "title": "Airplane"}]
    assert s.board.applied and s.board.applied[0]["op"] == "update"  # tried, rejection tolerated


def test_observer_failure_keeps_primitives_and_caps_concurrency(monkeypatch):
    started = []
    gate = None

    async def fake_generate(description):
        started.append(description)
        await gate.wait()
        raise RuntimeError("no capacity")

    monkeypatch.setattr(pipeline, "generate_model", fake_generate)

    async def run():
        nonlocal gate
        gate = asyncio.Event()
        s = _Session()
        s.history = []
        cad_hook.attach(s, force=True)
        for i in range(3):
            await s.broadcast({"type": "draw", "op": "add_3d", "id": f"m{i}", "title": f"model {i}", "scene": {}})
        await asyncio.sleep(0)
        assert started == ["model 0"]  # one running job per room
        gate.set()
        for _ in range(3):
            await asyncio.gather(*list(s.tasks.values()))
            await asyncio.sleep(0)
        return s

    s = asyncio.run(run())
    assert started == ["model 0", "model 2"]  # m1 was superseded by the newer m2 while waiting
    assert not [m for m in s.sent if m.get("op") == "update"]


def test_attach_is_off_under_pytest_unless_forced():
    s = _Session()
    assert cad_hook.attach(s) is None and not s._broadcast_observers
