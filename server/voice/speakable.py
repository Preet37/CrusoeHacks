"""Turn code references into how a teammate would say them out loud.

"app/api/auth/[auth0]/route.ts:18-24" -> "the auth0 route file"
"lib/specSheet.ts" -> "the spec sheet file"
Captions keep this spoken form too; the exact path is highlighted on the board instead.
"""
from __future__ import annotations

import re

# a path-ish token: has a slash or a known code extension, optional :line or :a-b
_EXTS = r"(?:py|ts|tsx|js|jsx|mjs|cjs|go|rs|java|rb|json|md|yml|yaml|toml|sql|css|html)"
_SEG = r"[\w\-\[\]()@]+(?:\.[\w\-\[\]()@]+)*"  # dots only BETWEEN word chars, so a sentence's final period is not eaten
_PATH = re.compile(
    r"(?<![\w/:.])`?(?P<path>(?:" + _SEG + r"/)+" + _SEG + r"|[\w\-\[\]]+\." + _EXTS + r")\b"
    r"(?::\d+(?:-\d+)?)?`?"
)
_URL = re.compile(r"https?://\S+")
_GENERIC = {"route", "index", "page", "layout", "main", "__init__", "init", "mod", "app", "server", "client", "utils", "util", "handler", "api"}
_EXT = re.compile(r"\.(py|ts|tsx|js|jsx|mjs|cjs|go|rs|java|rb|json|md|yml|yaml|toml|sql|css|html)$", re.I)


def _words(s: str) -> str:
    s = s.strip("[]()@_").replace("_", " ").replace("-", " ")
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s)  # specSheet -> spec Sheet
    return re.sub(r"\s+", " ", s).strip().lower()


def spoken_name(path: str) -> str:
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    if not parts:
        return path
    leaf = parts[-1]
    has_ext = bool(_EXT.search(leaf))
    stem = _words(_EXT.sub("", leaf))
    if not has_ext:  # a directory reference
        return f"the {stem} folder" if stem else path
    if stem in _GENERIC and len(parts) > 1:
        parent = _words(parts[-2])
        return f"the {parent} {stem} file".replace("  ", " ")
    return f"the {stem} file"


def speakable(text: str) -> str:
    urls: list[str] = []

    def keep_url(m: re.Match) -> str:
        urls.append(m.group(0))
        return f"\x00{len(urls) - 1}\x00"

    out = _URL.sub(keep_url, text or "")

    def sub(m: re.Match) -> str:
        p = m.group("path")
        if "/" not in p and not _EXT.search(p):
            return m.group(0)
        name = spoken_name(p)
        rest = out_holder[0][m.end():m.end() + 12].lower()
        if rest.lstrip().startswith(("file", "folder", "module")):
            name = re.sub(r" (file|folder)$", "", name)  # "the session.py file" -> "the session file"
        return name

    out_holder = [out]
    out = _PATH.sub(sub, out)
    out = re.sub("\x00(\\d+)\x00", lambda m: urls[int(m.group(1))], out)
    out = re.sub(r"\bthe the\b", "the", out, flags=re.I)
    out = re.sub(r"\b(in|from|see|check|at)\s+the\s+the\b", r"\1 the", out, flags=re.I)
    return out


def code_refs(text: str) -> list[str]:
    """Paths mentioned in text (for highlighting the matching board boxes)."""
    return [m.group("path") for m in _PATH.finditer(_URL.sub(" ", text or ""))
            if "/" in m.group("path") or _EXT.search(m.group("path"))]
