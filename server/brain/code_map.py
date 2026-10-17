"""Deterministic, bounded Python code map for live impact questions.

No model or database is involved. Paths exposed by this module are always
relative to ``root`` so specialists can cite them in a meeting.
"""

from __future__ import annotations

import ast
from collections import deque
import os
from pathlib import Path
import posixpath
import re
import subprocess
from typing import Any


_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build"}
_JS_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx")
_JS_SYMBOL = re.compile(r"\b(?:async\s+)?(?:function|class|interface|type|enum|const|let|var)\s+([A-Za-z_$][\w$]*)")


def _js_tokens(source: str) -> list[tuple[str, str]]:
    """Lex enough JS/TS to distinguish literal imports from comments/strings."""
    tokens = []
    i = 0
    while i < len(source):
        char = source[i]
        if char.isspace():
            i += 1
        elif source.startswith("//", i):
            end = source.find("\n", i + 2)
            i = len(source) if end < 0 else end + 1
        elif source.startswith("/*", i):
            end = source.find("*/", i + 2)
            i = len(source) if end < 0 else end + 2
        elif char in "\"'`":
            quote = char
            start = i + 1
            i = start
            while i < len(source):
                if source[i] == "\\":
                    i += 2
                elif source[i] == quote:
                    break
                else:
                    i += 1
            value = source[start:i]
            tokens.append(("string" if quote != "`" and "\\" not in value else "other", value))
            i += 1
        elif char.isalpha() or char in "_$":
            start = i
            i += 1
            while i < len(source) and (source[i].isalnum() or source[i] in "_$"):
                i += 1
            tokens.append(("name", source[start:i]))
        else:
            tokens.append(("punct", char))
            i += 1
    return tokens


def _js_info(source: str) -> tuple[list[str], set[str]]:
    tokens = _js_tokens(source)
    imports: set[str] = set()
    for index, (kind, value) in enumerate(tokens):
        if kind != "name" or value not in {"import", "export", "require"}:
            continue
        if index and tokens[index - 1] == ("punct", "."):
            continue
        if value == "require" or (value == "import" and index + 1 < len(tokens)
                                   and tokens[index + 1] == ("punct", "(")):
            if (index + 2 < len(tokens) and tokens[index + 1] == ("punct", "(")
                    and tokens[index + 2][0] == "string"):
                imports.add(tokens[index + 2][1])
            continue
        if value == "import" and index + 1 < len(tokens) and tokens[index + 1][0] == "string":
            imports.add(tokens[index + 1][1])
            continue
        for position in range(index + 1, min(index + 25, len(tokens))):
            candidate = tokens[position]
            if candidate == ("punct", ";"):
                break
            if candidate == ("name", "from"):
                if position + 1 < len(tokens) and tokens[position + 1][0] == "string":
                    imports.add(tokens[position + 1][1])
                break
    # Symbol extraction uses only lexical names and punctuation, so words in
    # comments or string literals cannot become fabricated symbols.
    code = " ".join(value if kind != "string" else " " for kind, value in tokens)
    symbols = sorted(set(_JS_SYMBOL.findall(code)))
    return symbols, {name for name in imports if name.startswith(".")}


def _resolve_js_import(source: str, literal: str, indexed: set[str]) -> str | None:
    if not literal.startswith(".") or "\\" in literal or "?" in literal or "#" in literal:
        return None
    parent = posixpath.dirname(source)
    base = posixpath.normpath(posixpath.join(parent, literal))
    if base == ".." or base.startswith("../") or base.startswith("/"):
        return None
    extension = posixpath.splitext(base)[1]
    if extension in _JS_EXTENSIONS:
        if base in indexed:
            return base
        # TypeScript sources often write the emitted .js import path.
        candidates = [base[:-3] + suffix for suffix in (".ts", ".tsx")]
    elif extension:
        return None
    else:
        candidates = [base + suffix for suffix in _JS_EXTENSIONS]
        candidates += [base + "/index" + suffix for suffix in _JS_EXTENSIONS]
    matches = [candidate for candidate in candidates if candidate in indexed]
    return matches[0] if len(matches) == 1 else None


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
        pending_js_imports: dict[str, set[str]] = {}
        paths: list[Path] = []
        for directory, subdirs, filenames in os.walk(self.root, followlinks=False):
            subdirs[:] = sorted(name for name in subdirs
                                if name not in _SKIP_DIRS and not name.startswith(".")
                                and not (Path(directory) / name).is_symlink())
            for filename in sorted(filenames):
                if filename.endswith((".py",) + _JS_EXTENSIONS):
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
            if path.suffix == ".py":
                try:
                    tree = ast.parse(source, filename=rel)
                    symbols = sorted(node.name for node in tree.body
                                     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
                    summary = (ast.get_docstring(tree) or "").splitlines()[:1]
                    pending_imports[rel] = self._import_names(tree, module, path.name == "__init__.py")
                except SyntaxError:
                    symbols, summary = [], []
                    pending_imports[rel] = set()
            else:
                symbols, names = _js_info(source)
                pending_js_imports[rel] = names
                summary = [f"{'TypeScript' if path.suffix in ('.ts', '.tsx') else 'JavaScript'} source {rel}"]
            self.files[rel] = {
                "path": rel,
                "module": module,
                "symbols": symbols,
                "imports": [],
                "imported_by": [],
                "owner": None,
                "owners": [],
                "summary": (summary[0].strip()[:180] if summary else f"Python module {module or rel}"),
                "is_test": ("tests" in Path(rel).parts or Path(rel).name.startswith("test_")
                            or Path(rel).name.endswith((".test.ts", ".test.tsx", ".test.js",
                                                        ".test.jsx", ".spec.ts", ".spec.tsx",
                                                        ".spec.js", ".spec.jsx"))),
            }
        by_module = {record["module"]: path for path, record in self.files.items()
                     if record["module"] and path.endswith(".py")}
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
        indexed = set(self.files)
        for path, names in pending_js_imports.items():
            imports = {target for literal in names
                       if (target := _resolve_js_import(path, literal, indexed)) is not None
                       and target != path}
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
