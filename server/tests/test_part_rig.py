"""POST /cad/part-rig: fake vision model, cache, failure -> empty."""
import asyncio

import pytest

from cad import part_rig

VIEWS = {"front": "data:image/jpeg;base64,AAAA", "top": "data:image/jpeg;base64,BBBB"}
REPLY = '{"parts":[{"name":"rotor","kind":"spin","axis":"z","boxes":{"front":[0.3,0.1,0.7,0.5],"top":[0.3,0.4,0.7,0.6]}},{"name":"bad","kind":"spin","boxes":{"front":[0,0,0,0]}}]}'


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CAD_LIBRARY_DIR", str(tmp_path / "library"))
    part_rig._memory.clear()


def test_rig_parses_and_caches(monkeypatch):
    calls = []

    async def fake(model, messages, **kw):
        calls.append(model)
        return REPLY
    monkeypatch.setattr(part_rig.llm, "chat", fake)
    first = asyncio.run(part_rig.rig("/cad/x.glb", "wind turbine", VIEWS))
    assert [p["name"] for p in first["parts"]] == ["rotor"]
    assert first["parts"][0]["kind"] == "spin" and first["parts"][0]["axis"] == "z"
    part_rig._memory.clear()  # disk cache survives a restart
    second = asyncio.run(part_rig.rig("/cad/x.glb?v=2", "wind turbine", VIEWS))
    assert second["cached"] and second["parts"] == first["parts"]
    assert len(calls) == 1


def test_failure_returns_empty_and_is_not_cached(monkeypatch):
    async def boom(model, messages, **kw):
        raise RuntimeError("down")
    monkeypatch.setattr(part_rig.llm, "chat", boom)
    assert asyncio.run(part_rig.rig("/cad/y.glb", "car", VIEWS)) == {"parts": []}
    assert part_rig._read(part_rig._key("/cad/y.glb")) is None


def test_missing_views_returns_empty():
    assert asyncio.run(part_rig.rig("/cad/z.glb", "car", {})) == {"parts": []}


def test_endpoint_wired():
    from fastapi.testclient import TestClient
    import app as app_mod
    r = TestClient(app_mod.app).post("/cad/part-rig", json={})
    assert r.status_code == 200 and r.json() == {"parts": []}
