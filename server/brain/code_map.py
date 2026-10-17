"""Deterministic, bounded Python code map for live impact questions.

No model or database is involved. Paths exposed by this module are always
relative to ``root`` so specialists can cite them in a meeting.
"""

from __future__ import annotations

import ast
from collections import deque
import os
from pathlib import Path
import subprocess
from typing import Any


_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build"}


class CodeMap:
    """Index Python files and answer reverse-import impact queries.

    ``files`` maps POSIX repo-relative paths to JSON-ready records with
    path/module/symbols/imports/imported_by/owner/owners/summary/is_test.
    ``impact`` adds distance and relation to those records; the queried source
    is first, followed by files that depend on it directly or transitively.
    """

    def __init__(self, root: str | Path, *, max_files: int = 5000,
                 max_file_bytes: int = 256_000) -> None:
        if max_files < 1 or max_file_bytes < 1:
            raise ValueError("code map limits must be positive")
        self.root = Path(root).expanduser().resolve()
        self.max_files = max_files
        self.max_file_bytes = max_file_bytes
        self.files: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _module(path: str) -> str:
        parts = list(Path(path).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        return ".".join(parts)

    @staticmethod
    def _import_names(tree: ast.AST, module: str, is_package: bool) -> set[str]:
        names: set[str] = set()
        package = module if is_package else module.rpartition(".")[0]
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    parts = package.split(".") if package else []
                    keep = len(parts) - node.level + 1
                    base = ".".join(parts[:max(keep, 0)])
                    target = ".".join(filter(None, (base, node.module or "")))
                else:
                    target = node.module or ""
                if target:
                    names.add(target)
                for alias in node.names:
                    if alias.name != "*":
                        names.add(".".join(filter(None, (target, alias.name))))
        return names

    def _git_owners(self) -> dict[str, list[str]]:
        """One git invocation gives newest-first authors for all indexed files."""
        try:
            prefix = subprocess.run(
                ["git", "-C", str(self.root), "rev-parse", "--show-prefix"],
                capture_output=True, text=True, check=True, timeout=3,
            ).stdout.strip("\n")
            log = subprocess.run(
                ["git", "-C", str(self.root), "log", "-n", "200", "--format=AUTHOR:%an", "--name-only", "--", "."],
                capture_output=True, text=True, check=True, timeout=10,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return {}
        owners: dict[str, list[str]] = {}
        author = ""
        for line in log.splitlines():
            if line.startswith("AUTHOR:"):
                author = line[len("AUTHOR:"):]
            elif line and author:
                path = line.removeprefix(prefix) if prefix and line.startswith(prefix) else line
                if path in self.files:
                    authors = owners.setdefault(path, [])
                    if author not in authors:
                        authors.append(author)
        return owners

    def build(self) -> CodeMap:
        if not self.root.is_dir():
            raise ValueError(f"code root is not a directory: {self.root}")
        self.files = {}
        pending_imports: dict[str, set[str]] = {}
        paths: list[Path] = []
        for directory, subdirs, filenames in os.walk(self.root, followlinks=False):
            subdirs[:] = sorted(name for name in subdirs
                                if name not in _SKIP_DIRS and not name.startswith(".")
                                and not (Path(directory) / name).is_symlink())
            for filename in sorted(filenames):
                if filename.endswith(".py"):
                    paths.append(Path(directory) / filename)
                    if len(paths) >= self.max_files:
                        break
            if len(paths) >= self.max_files:
                break
        for path in sorted(paths):
            if path.is_symlink() or not path.is_file() or path.stat().st_size > self.max_file_bytes:
                continue
            rel = path.relative_to(self.root).as_posix()
            module = self._module(rel)
            source = path.read_text(encoding="utf-8", errors="replace")
            try:
                tree = ast.parse(source, filename=rel)
                symbols = sorted(node.name for node in tree.body
                                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
                summary = (ast.get_docstring(tree) or "").splitlines()[:1]
                pending_imports[rel] = self._import_names(tree, module, path.name == "__init__.py")
            except SyntaxError:
                symbols, summary = [], []
                pending_imports[rel] = set()
            self.files[rel] = {
                "path": rel,
                "module": module,
                "symbols": symbols,
                "imports": [],
                "imported_by": [],
                "owner": None,
                "owners": [],
                "summary": (summary[0].strip()[:180] if summary else f"Python module {module or rel}"),
                "is_test": "tests" in Path(rel).parts or Path(rel).name.startswith("test_"),
            }
        by_module = {record["module"]: path for path, record in self.files.items() if record["module"]}
        for path, names in pending_imports.items():
            imports: set[str] = set()
            for name in names:
                # The longest matching indexed prefix is the actual local
                # module; `from auth import login` also matches auth.login.
                segments = name.split(".")
                for length in range(len(segments), 0, -1):
                    target = by_module.get(".".join(segments[:length]))
                    if target:
                        if target != path:
                            imports.add(target)
                        break
            self.files[path]["imports"] = sorted(imports)
            for target in imports:
                self.files[target]["imported_by"].append(path)
        owners = self._git_owners()
        for path, record in self.files.items():
            record["imported_by"].sort()
            record["owners"] = owners.get(path, [])
            record["owner"] = record["owners"][0] if record["owners"] else None
        return self

    def resolve(self, query: str) -> list[str]:
        """Resolve a path, module, symbol, or short topic to indexed files."""
        if not isinstance(query, str) or not (query := query.strip()):
            return []
        query = query.replace("\\", "/").removeprefix("./")
        if query in self.files:
            return [query]
        lowered = query.lower()
        exact_modules = [path for path, record in self.files.items()
                         if record["module"].lower() == lowered]
        package_files = [path for path, record in self.files.items()
                         if record["module"].lower().startswith(lowered + ".")]
        if package_files:
            return sorted(set(exact_modules + package_files))
        if exact_modules:
            return sorted(exact_modules)
        stem_or_symbol = [path for path, record in self.files.items()
                          if Path(path).stem.lower() == lowered
                          or lowered in (symbol.lower() for symbol in record["symbols"])]
        if stem_or_symbol:
            return sorted(stem_or_symbol)
        matches = [path for path, record in self.files.items()
                   if lowered in path.lower() or lowered in record["summary"].lower()]
        return sorted(matches)[:20]

    def impact(self, path_or_symbol: str) -> list[dict[str, Any]]:
        """Return source files plus all transitive reverse-import dependents."""
        seeds = self.resolve(path_or_symbol)
        distance = {path: 0 for path in seeds}
        queue = deque(seeds)
        while queue:
            path = queue.popleft()
            for dependent in self.files[path]["imported_by"]:
                next_distance = distance[path] + 1
                if dependent not in distance or next_distance < distance[dependent]:
                    distance[dependent] = next_distance
                    queue.append(dependent)
        return [dict(self.files[path], distance=distance[path],
                     relation="source" if distance[path] == 0 else "dependent")
                for path in sorted(distance, key=lambda item: (distance[item], item))]

    def read_file(self, path: str, max_chars: int = 4000) -> str:
        """Read only indexed source, capped at 16,000 characters."""
        if path not in self.files:
            raise ValueError(f"file is not indexed: {path}")
        if isinstance(max_chars, bool) or not isinstance(max_chars, int) or not 1 <= max_chars <= 16_000:
            raise ValueError("max_chars must be from 1 to 16000")
        source_path = self.root / path
        if source_path.is_symlink() or not source_path.resolve().is_relative_to(self.root):
            raise ValueError("source path escapes the code root")
        with source_path.open("r", encoding="utf-8", errors="replace") as source:
            return source.read(max_chars)
