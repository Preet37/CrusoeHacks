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


# ── spec sheet / templates / library / audit / orchestration ────────────────

from cad import audit as A  # noqa: E402
from cad import library as L  # noqa: E402
from cad import spec as S  # noqa: E402
from cad import templates as T  # noqa: E402


def test_spec_units_normalise():
    assert S.lookup_unit("in")[2] == pytest.approx(25.4)
    assert S.lookup_unit("sq in") == ("area", "mm2", pytest.approx(645.16))
    assert S.lookup_unit("in²")[0] == "area" and S.lookup_unit("mm^2")[2] == 1
    assert S.lookup_unit("lbs")[1] == "g" and S.lookup_unit("feet")[2] == pytest.approx(304.8)
    assert S.lookup_unit("furlong")[0] == "unknown"


def test_spec_validation_catches_hallucinated_numbers():
    raw = {"referenceProduct": "Babolat Pure Drive", "dimensions": [
        {"key": "overall_length", "value": 27, "unit": "in"},
        {"key": "head_width", "value": 25, "unit": "cm"},
        {"key": "head_height", "value": 33, "unit": "cm"},
        {"key": "head_area", "value": 100, "unit": "sq in"},
    ]}
    spec = S.coerce_spec_sheet(raw, "tennis racket")
    assert spec.dimensions[0].value == pytest.approx(685.8)
    assert S.validate_spec_sheet(spec).valid  # 100 in² ≈ ellipse of 250×330 mm
    raw["dimensions"][1]["value"] = 1.2  # a 12 mm wide head
    bad = S.validate_spec_sheet(S.coerce_spec_sheet(raw, "tennis racket"))
    assert not bad.valid and any("inconsistent" in e for e in bad.errors)
    huge = S.coerce_spec_sheet({"dimensions": [{"key": "l", "value": "3 km"}] * 3}, "racket")
    assert any("plausible" in e for e in S.validate_spec_sheet(huge).errors)
    assert "VERIFIED REAL DIMENSIONS" in S.format_spec_for_plan(spec)


def test_spec_research_retries_and_caches():
    S.clear_spec_cache()
    answers = ['{"dimensions": []}',
               '{"referenceProduct": "Herman Miller Aeron", "dimensions": ['
               '{"key": "height", "value": 1.1, "unit": "m"}, {"key": "width", "value": 68, "unit": "cm"},'
               '{"key": "seat_height", "value": 450, "unit": "mm"}]}']
    calls = []

    async def ask(prompt):
        calls.append(prompt)
        return answers[len(calls) - 1], "fake"

    async def run():
        first = await S.research_spec_sheet("office chair", ask)
        second = await S.research_spec_sheet("office  chair", ask)
        return first, second

    (spec, v), (spec2, _) = asyncio.run(run())
    assert v.valid and spec.reference_product == "Herman Miller Aeron" and spec2 is spec
    assert len(calls) == 2  # one retry, then served from cache
    S.clear_spec_cache()


def test_classify_topic_uses_word_boundaries():
    assert T.classify_topic("a cargo ship") == "ship"
    assert T.classify_topic("sports car with spoiler") == "car"
    assert T.classify_topic("relevant business mission search") == "generic"
    assert T.classify_topic("server rack layout") == "generic"


def test_every_template_is_safe_source():
    for cat in ["car", "aircraft", "racket", *T._STATIC]:
        src = T.template_for("x\ninclude <evil.scad>", None, cat)
        R.check_scad_source(src)
    assert T.template_for("a floor plan", None) is None  # no blob template for unknowns


def test_spec_grounded_template_uses_spec_numbers():
    spec = S.coerce_spec_sheet({"referenceProduct": "Mini", "dimensions": [
        {"key": "overall_length", "value": 3000, "unit": "mm"},
        {"key": "overall_width", "value": 3000, "unit": "mm"},
        {"key": "wheelbase", "value": 2000, "unit": "mm"}]}, "car")
    wide, default = T.openscad_car_from_spec("car", spec), T.openscad_car_from_spec("car")
    assert "// Mini" in wide and wide != default
    assert "cube([4.600, 4.600" in wide  # width == length after scaling


@needs_openscad
def test_fast_templates_compile_with_openscad(tmp_path):
    for cat in ("building", "bottle", "ship", "satellite"):
        assert R.scad_to_stl(T.template_for("x", None, cat), str(tmp_path)).stat().st_size > 1000


def test_model_library_save_lookup_reject(monkeypatch, tmp_path):
    monkeypatch.setenv("CAD_LIBRARY_DIR", str(tmp_path))
    L.clear_library()
    glb = b"glTF" + b"\0" * 100
    assert L.lookup_model("Tennis Racket") is None
    L.save_model("Tennis Racket", glb, "plan", "Babolat")
    entry, blob = L.lookup_model("tennis  racket!")
    assert blob == glb and entry["hits"] == 1
    assert L.verify_model("tennis racket")["verified"]
    assert L.reject_model("tennis racket") == {"evicted": True, "rejections": 1}
    assert L.lookup_model("tennis racket") is None
    L.save_model("tennis racket", glb, "plan")
    assert L.list_models()[0]["rejections"] == 1  # count survives eviction
    L.clear_library()


def test_audit_flags_lego_models_but_not_racks():
    lego = P.coerce_geometry_plan({"parts": [
        {"name": f"seg{i}", "primitive": "box", "size": [100, 100, 100], "position": [i * 90, 0, 0]}
        for i in range(6)] + [{"name": "knob", "primitive": "sphere", "size": [50, 50, 50], "position": [0, 0, 70]}]},
        "teapot")
    assert any("LEGO" in i for i in A.smoothness_audit(lego, "teapot"))
    assert not any("LEGO" in i for i in A.smoothness_audit(lego, "server rack row"))
    src, fixes = A.polish_openscad("sphere(r=1, $fn=8); cylinder(h=1, r=1, $fn = 64);")
    assert "$fn=32" in src and "$fn = 64" in src and len(fixes) == 1


ROBOT_PLAN = """{"referenceProduct": "KUKA KR 6", "summary": "industrial arm", "parts": [
 {"name": "pedestal", "primitive": "cylinder", "size": [400, 400, 300], "position": [0, 0, 150], "axis": "+z"},
 {"name": "turret", "primitive": "cylinder", "size": [300, 300, 200], "position": [0, 0, 400], "axis": "+z"},
 {"name": "shoulder joint", "primitive": "sphere", "size": [220, 220, 220], "position": [0, 0, 520]},
 {"name": "upper arm", "primitive": "box", "size": [120, 120, 600], "position": [0, 0, 820]},
 {"name": "elbow joint", "primitive": "cylinder", "size": [160, 200, 160], "position": [0, 0, 1120], "axis": "+y"},
 {"name": "forearm", "primitive": "cylinder", "size": [500, 100, 100], "position": [250, 0, 1120], "axis": "+x"},
 {"name": "gripper", "primitive": "cone", "size": [120, 120, 120], "position": [540, 0, 1120], "axis": "+x"}]}"""


def _fake_llm(monkeypatch, answers):
    calls = []

    async def fake_ask(prompt, timeout=0):
        calls.append(prompt)
        return (answers.pop(0) if answers else ""), "fake-nemotron"

    monkeypatch.setattr(pipeline, "_ask", fake_ask)
    monkeypatch.setattr(R, "render_glb", lambda scad: b"glTF" + scad.encode()[:200])
    return calls


def test_generate_model_plan_path_then_library_hit(monkeypatch, tmp_path):
    monkeypatch.setenv("CAD_LIBRARY_DIR", str(tmp_path / "lib"))
    monkeypatch.setattr(pipeline, "DISK_DIR", tmp_path)
    L.clear_library()
    S.clear_spec_cache()
    calls = _fake_llm(monkeypatch, ['{"dimensions": []}', '{"dimensions": []}', ROBOT_PLAN])
    out = asyncio.run(pipeline.generate_model("industrial robot arm"))
    assert out["generator"] == "plan" and out["parts"] == 7 and out["url"].endswith(f"/cad/{out['id']}.glb")
    assert pipeline.get_glb(out["id"]).startswith(b"glTF")
    assert len(calls) == 3  # spec, spec retry, plan
    again = asyncio.run(pipeline.generate_model("Industrial robot arm"))
    assert again["cached"] and again["generator"] == "library" and len(calls) == 3
    L.clear_library()


def test_generate_model_falls_back_to_template_then_fails_loudly(monkeypatch, tmp_path):
    monkeypatch.setenv("CAD_LIBRARY_DIR", str(tmp_path / "lib"))
    monkeypatch.setattr(pipeline, "DISK_DIR", tmp_path)
    L.clear_library()
    _fake_llm(monkeypatch, [])  # the model returns nothing usable
    out = asyncio.run(pipeline.generate_model("a cargo ship", use_spec=False))
    assert out["generator"] == "template:ship"
    with pytest.raises(pipeline.CadError, match="looks like"):
        asyncio.run(pipeline.generate_model("a quantum flux widget", use_spec=False))
    L.clear_library()


def _chair(topic="office chair"):
    return P.coerce_geometry_plan({"parts": [
        {"name": "seat", "primitive": "box", "size": [500, 500, 80], "position": [0, 0, 450]},
        {"name": "backrest", "primitive": "box", "size": [60, 480, 600], "position": [-400, 0, 900]},
        {"name": "post", "primitive": "cylinder", "size": [60, 60, 350], "position": [0, 0, 230], "axis": "+z"},
        {"name": "leg", "primitive": "box", "size": [350, 40, 40], "position": [175, 0, 60]},
        {"name": "caster", "primitive": "sphere", "size": [60, 60, 60], "position": [600, 900, 30], "mirror": "y"},
        {"name": "headrest", "primitive": "wedge", "size": [500, 500, 200], "position": [0, 0, 1400]}]}, topic)


def test_snap_floating_moves_detached_groups_onto_main_body():
    plan = _chair()
    assert not P.validate_geometry_plan(plan).valid
    assert any("move it" in line for line in P.floating_report(plan))
    snapped = P.snap_floating(plan)
    assert P.validate_geometry_plan(snapped).valid
    by = {p.name: p.position for p in snapped.parts}
    assert by["seat"] == (0, 0, 450) and by["post"] == (0, 0, 230)  # main body untouched


def test_all_box_plans_ok_for_rectilinear_topics_and_wedge_compiles():
    layout = {"parts": [{"name": "floor", "primitive": "box", "size": [6000, 3000, 100], "position": [0, 0, 50]}] + [
        {"name": f"rack {i}", "primitive": "box", "size": [600, 1000, 2000], "position": [-2000 + i * 800, 0, 1100]}
        for i in range(4)] + [{"name": "roof", "primitive": "wedge", "size": [6000, 3000, 800], "position": [0, 0, 2500]}]}
    assert P.validate_geometry_plan(P.coerce_geometry_plan(layout, "server room layout")).valid
    blob = P.validate_geometry_plan(P.coerce_geometry_plan(
        {"parts": [p for p in layout["parts"] if p["primitive"] == "box"]}, "teapot"))
    assert any("blob" in e for e in blob.errors)
    scad = P.plan_to_openscad(P.coerce_geometry_plan(layout, "server room"))
    assert "linear_extrude" in scad and "polygon(" in scad


def test_detached_cluster_is_rejected_and_radial_copies_expand():
    parts = [{"name": "floor", "primitive": "box", "size": [3000, 3000, 100], "position": [0, 0, 50]},
             {"name": "rack", "primitive": "box", "size": [600, 1000, 2000], "attach": {"to": "floor", "face": "+z"}},
             {"name": "tile", "primitive": "box", "size": [600, 600, 20], "position": [6000, 0, 10],
              "array": {"count": 4, "step": [620, 0, 0]}}]
    plan = _plan({"parts": parts})
    v = P.validate_geometry_plan(plan)
    assert any("separate group" in e for e in v.errors)
    snapped = P.snap_floating(plan)
    assert not [e for e in P.validate_geometry_plan(snapped).errors if "float" in e or "group" in e]
    assert snapped.parts[0].position == (0, 0, 50)  # the main body stays put
    star = _plan({"parts": [
        {"name": "hub", "primitive": "cylinder", "size": [120, 120, 40], "axis": "+z", "position": [0, 0, 100]},
        {"name": "leg", "primitive": "box", "size": [330, 50, 30], "radial": {"count": 5, "axis": "z"},
         "attach": {"to": "hub", "face": "+x", "inset": 20}},
        {"name": "post", "primitive": "cylinder", "size": [60, 60, 300], "axis": "+z", "attach": {"to": "hub", "face": "+z"}},
        {"name": "seat", "primitive": "box", "size": [480, 460, 60], "attach": {"to": "post", "face": "+z", "offset": [9999, 0]}}]})
    assert [p.name for p in star.parts][:5] == ["hub", "leg", "leg 2", "leg 3", "leg 4"]
    assert P.validate_geometry_plan(star).valid  # huge offset is clamped so the seat still touches the post
    assert "rotate(a=72" in P.plan_to_openscad(star)
