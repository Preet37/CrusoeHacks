"""The deterministic code map supports a real meeting impact question."""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from brain.code_map import CodeMap


DEMO = Path(__file__).resolve().parents[2] / "demo_project"


def test_login_impact_follows_reverse_imports_to_routes_and_tests() -> None:
    code_map = CodeMap(DEMO).build()
    login = code_map.files["auth/login.py"]
    assert {"login", "password_digest", "LoginError"} <= set(login["symbols"])
    assert {"auth/jwt_tokens.py", "db/models.py"} <= set(login["imports"])
    assert "web/routes.py" in login["imported_by"]
    assert login["summary"].startswith("Current login path")
    assert code_map.resolve("login") == ["auth/login.py"]

    impacted = {item["path"]: item for item in code_map.impact("auth/login.py")}
    assert impacted["auth/login.py"]["relation"] == "source"
    assert impacted["auth/login.py"]["distance"] == 0
    assert impacted["web/routes.py"]["distance"] == 1
    assert impacted["tests/test_auth.py"]["is_test"] is True
    assert impacted["tests/test_auth.py"]["distance"] == 1
    jwt_impact = {item["path"]: item["distance"] for item in code_map.impact("auth/jwt_tokens.py")}
    assert jwt_impact["auth/permissions.py"] == 1
    assert jwt_impact["billing/invoices.py"] == 2
    assert jwt_impact["web/routes.py"] == 2  # Also depends on auth/login.py.
    assert code_map.impact("not_a_real_symbol") == []


def test_read_is_indexed_and_bounded() -> None:
    code_map = CodeMap(DEMO).build()
    assert code_map.read_file("auth/login.py", max_chars=20) == '"""Current login pat'
    with pytest.raises(ValueError):
        code_map.read_file("../AGENTS.md")
    with pytest.raises(ValueError):
        code_map.read_file("auth/login.py", max_chars=16_001)


def test_index_skips_symlinked_source_and_enforces_file_limit(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def a(): pass\n")
    (tmp_path / "b.py").write_text("def b(): pass\n")
    (tmp_path / "secret.py").symlink_to(DEMO / "auth/login.py")
    code_map = CodeMap(tmp_path, max_files=2).build()
    assert set(code_map.files) == {"a.py", "b.py"}
    assert "secret.py" not in code_map.files


def test_fake_history_has_three_owners_without_touching_checkout(tmp_path: Path) -> None:
    target = tmp_path / "ledgerlane"
    result = subprocess.run(["bash", str(DEMO / "fake_history.sh"), str(target)],
                            capture_output=True, text=True, check=True)
    assert result.stdout.strip() == str(target)
    code_map = CodeMap(target).build()
    assert code_map.files["auth/login.py"]["owner"] == "Avery Chen"
    assert code_map.files["billing/invoices.py"]["owner"] == "Morgan Patel"
    assert code_map.files["web/routes.py"]["owner"] == "Riley Okafor"
    assert {record["owner"] for record in code_map.files.values()} == {
        "Avery Chen", "Morgan Patel", "Riley Okafor",
    }
    assert not (DEMO / ".git").exists()


def test_typescript_javascript_literal_relative_imports_and_impact(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "parts").mkdir()
    (tmp_path / "src" / "parts" / "index.ts").write_text(
        "export interface Part { id: string }\nexport const wing = 1;\n")
    (tmp_path / "src" / "engine.ts").write_text("export function buildEngine() {}\n")
    (tmp_path / "src" / "plane.tsx").write_text(
        "import { wing } from './parts';\n"
        "import { buildEngine } from './engine.js';\n"
        "// import fake from './missing';\n"
        "const stringOnly = \"require('./also_missing')\";\n"
        "export function Plane() { return wing; }\n")
    (tmp_path / "src" / "entry.jsx").write_text(
        "export { Plane } from './plane';\nconst engine = require('./engine');\n")
    (tmp_path / "src" / "plane.test.ts").write_text("import './plane';\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "bad.ts").write_text("export const bad = 1\n")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "bad.js").write_text("export const bad = 1\n")
    code_map = CodeMap(tmp_path).build()
    assert "src/parts/index.ts" in code_map.files
    assert "Plane" in code_map.files["src/plane.tsx"]["symbols"]
    assert set(code_map.files["src/plane.tsx"]["imports"]) == {
        "src/parts/index.ts", "src/engine.ts"}
    assert set(code_map.files["src/entry.jsx"]["imports"]) == {
        "src/plane.tsx", "src/engine.ts"}
    assert code_map.files["src/plane.test.ts"]["is_test"] is True
    assert "node_modules/bad.ts" not in code_map.files
    assert "build/bad.js" not in code_map.files
    impacted = {item["path"]: item["distance"] for item in code_map.impact("src/engine.ts")}
    assert impacted["src/plane.tsx"] == 1
    assert impacted["src/entry.jsx"] == 1
    assert impacted["src/plane.test.ts"] == 2
    assert "buildEngine" in code_map.resolve("buildEngine") or code_map.resolve("buildEngine") == ["src/engine.ts"]
    assert "export function Plane" in code_map.read_file("src/plane.tsx", max_chars=1000)


def test_js_import_resolution_skips_ambiguous_or_nonliteral_targets(tmp_path: Path) -> None:
    (tmp_path / "a.ts").write_text("export const a = 1\n")
    (tmp_path / "a.js").write_text("export const a = 2\n")
    (tmp_path / "caller.ts").write_text(
        "import { a } from './a';\n"
        "const dynamic = import(variable);\n"
        "import external from 'package';\n"
        "/* import hidden from './a.js'; */\n")
    code_map = CodeMap(tmp_path).build()
    assert code_map.files["caller.ts"]["imports"] == []
