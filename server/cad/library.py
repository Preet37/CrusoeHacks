# Adapted from LoominPhysicsSandbox (Preet37, pre-existing project) lib/modelLibrary.ts, ported to Python for Twin.
"""Model library: an on-disk cache of accepted CAD models keyed by topic.

Building a model costs research calls plus an OpenSCAD/Blender round trip, and the
result for "tennis racket" is the same every session. Once rendered and NOT rejected
it is replayed instantly. A rejection evicts the entry (the next request re-renders)
and the rejection count survives eviction so a topic that keeps failing is visible.

Storage: GLB files + index.json under server/learning/runs/cad/library (gitignored;
override with CAD_LIBRARY_DIR). If the filesystem is unavailable it degrades to memory.

Seed library (Twin addition): server/cad/seed_library/ ships Loomin's .model-library
(photoreal + OpenSCAD GLBs, same index.json format) plus Loomin's public/models/wind_turbine.glb,
read-only. lookup_model falls back to it with a fuzzy topic match ("draw a 3D windmill" ->
photoreal wind turbine), preferring photoreal entries. CAD_SEED_LIBRARY=0 disables it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("twin.cad")
MIN_GLB_BYTES = 60

_lock = threading.Lock()
_memory_blobs: dict[str, bytes] = {}
_memory_index: dict[str, Any] | None = None


def library_dir() -> Path:
    env = os.getenv("CAD_LIBRARY_DIR")
    return Path(env) if env else Path(__file__).resolve().parents[1] / "learning" / "runs" / "cad" / "library"


def normalize_topic(topic: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", str(topic or "").lower()).strip())


def topic_key(topic: str) -> str:
    normalized = normalize_topic(topic)
    slug = normalized.replace(" ", "-")[:48] or "untitled"
    digest = hashlib.sha1(normalized.encode()).hexdigest()[:8]
    return f"{slug}-{digest}"


def _empty() -> dict[str, Any]:
    return {"version": 1, "entries": {}, "rejections": {}}


def _ensure_dir() -> bool:
    try:
        library_dir().mkdir(parents=True, exist_ok=True)
        return True
    except OSError:
        log.warning("model library: filesystem unavailable, using memory only")
        return False


def _read_index() -> dict[str, Any]:
    global _memory_index
    if not _ensure_dir():
        if _memory_index is None:
            _memory_index = _empty()
        return _memory_index
    try:
        parsed = json.loads((library_dir() / "index.json").read_text("utf-8"))
        if not isinstance(parsed, dict) or not isinstance(parsed.get("entries"), dict):
            return _empty()
        parsed.setdefault("rejections", {})
        return parsed
    except (OSError, ValueError):
        return _memory_index if _memory_index is not None else _empty()


def _write_index(index: dict[str, Any]) -> None:
    global _memory_index
    _memory_index = index
    if not _ensure_dir():
        return
    try:
        tmp = library_dir() / "index.json.tmp"
        tmp.write_text(json.dumps(index, indent=2), "utf-8")
        tmp.replace(library_dir() / "index.json")
    except OSError as e:
        log.warning("model library: could not persist index: %s", e)


def _read_blob(entry: dict[str, Any]) -> bytes | None:
    mem = _memory_blobs.get(entry["key"])
    if mem:
        return mem
    try:
        data = (library_dir() / entry["glbFile"]).read_bytes()
    except OSError:
        return None
    return data if len(data) >= MIN_GLB_BYTES else None


def lookup_model(topic: str, *, use_seed: bool = True) -> tuple[dict[str, Any], bytes] | None:
    """A previously accepted model for this topic (exact), else a fuzzy seed-library match, or None."""
    key = topic_key(topic)
    with _lock:
        index = _read_index()
        entry = index["entries"].get(key)
        if not entry:
            if not use_seed:
                return None
            rejected = set(index["rejections"])
            seed = match_seed(topic)
            if seed is None or seed["key"] in rejected:
                return None
            blob = _read_seed_blob(seed)
            return (dict(seed), blob) if blob is not None else None
        blob = _read_blob(entry)
        if blob is None:  # index and files drifted apart
            index["entries"].pop(key, None)
            _write_index(index)
            return None
        entry["hits"] = entry.get("hits", 0) + 1
        _write_index(index)
        return dict(entry), blob


def save_model(topic: str, glb: bytes, generator: str, reference_product: str | None = None,
               score: float | None = None, extra: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if not topic or not glb or len(glb) < MIN_GLB_BYTES:
        return None
    key = topic_key(topic)
    with _lock:
        index = _read_index()
        entry = {
            "key": key, "topic": topic[:400], "normalizedTopic": normalize_topic(topic),
            "generator": generator, "score": score, "referenceProduct": reference_product,
            "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "hits": 0,
            "verified": False, "rejections": index["rejections"].get(key, 0),
            "glbFile": f"{key}.glb", "thumbFile": None, "bytes": len(glb), **(extra or {}),
        }
        _memory_blobs[key] = glb
        if _ensure_dir():
            try:
                (library_dir() / entry["glbFile"]).write_bytes(glb)
            except OSError as e:
                log.warning("model library: could not write model: %s", e)
        index["entries"][key] = entry
        _write_index(index)
    log.info("model library: saved %r (%s, %d KB)", topic[:60], generator, len(glb) // 1024)
    return entry


def verify_model(topic: str) -> dict[str, Any] | None:
    """Thumbs up: mark the stored model as confirmed-good."""
    key = topic_key(topic)
    with _lock:
        index = _read_index()
        entry = index["entries"].get(key)
        if not entry:
            return None
        entry["verified"] = True
        _write_index(index)
        return dict(entry)


def reject_model(topic: str) -> dict[str, Any]:
    """Thumbs down: evict so the next request re-renders; keep the rejection count.

    A rejected seed match is remembered (by seed key) so that topic stops replaying the seed."""
    key = topic_key(topic)
    with _lock:
        index = _read_index()
        rejections = index["rejections"].get(key, 0) + 1
        index["rejections"][key] = rejections
        entry = index["entries"].pop(key, None)
        seed = match_seed(topic) if entry is None else None
        if seed is not None:
            index["rejections"][seed["key"]] = index["rejections"].get(seed["key"], 0) + 1
        if entry:
            _memory_blobs.pop(key, None)
            try:
                (library_dir() / entry["glbFile"]).unlink(missing_ok=True)
            except OSError:
                pass
        _write_index(index)
    return {"evicted": entry is not None, "rejections": rejections}


def list_models() -> list[dict[str, Any]]:
    with _lock:
        return sorted(_read_index()["entries"].values(), key=lambda e: -e.get("hits", 0))


def library_stats() -> dict[str, Any]:
    entries = list_models()
    with _lock:
        rejected = len(_read_index()["rejections"])
    return {"count": len(entries), "totalBytes": sum(e.get("bytes", 0) for e in entries),
            "totalHits": sum(e.get("hits", 0) for e in entries),
            "verified": sum(1 for e in entries if e.get("verified")), "rejectedTopics": rejected,
            "dir": str(library_dir())}


def clear_library() -> None:
    """Test helper: wipe memory state (and the index file)."""
    global _memory_index
    with _lock:
        _memory_blobs.clear()
        _memory_index = _empty()
        try:
            (library_dir() / "index.json").unlink(missing_ok=True)
        except OSError:
            pass


# ── Seed library (read-only, shipped in the repo) ────────────────────────────

SEED_DIR = Path(__file__).resolve().parent / "seed_library"
_seed_cache: dict[str, Any] | None = None
_seed_blobs: dict[str, bytes] = {}

# Whole-phrase rewrites applied to the normalized request before tokenizing.
_PHRASES = [
    (r"\bformula (?:1|one)\b|\bf ?1\b", "formula one"),
    (r"\bwind ?mills?\b|\bwind ?generators?\b|\bwind ?power turbines?\b", "wind turbine"),
    (r"\bhour ?glass(?:es)?\b|\bsand ?timers?\b|\begg ?timers?\b", "hour glass"),
    (r"\b(?:aero ?planes?|air ?planes?|planes?|jets?|jetliners?|airliners?|aircraft|boeing)\b", "airplane"),
    (r"\b(?:smart ?phones?|cell ?phones?|mobile phones?|phones?|i ?phones?)\b", "iphone"),
    (r"\b(?:race ?cars?|racecars?)\b", "race car"),
    (r"\b(?:cars?|automobiles?|sports cars?|miata)\b", "car"),
    (r"\b(?:pulleys?|block and tackle)\b", "pulley"),
    (r"\b(?:benches|bench)\b", "bench"),
    (r"\bcatapults?\b", "trebuchet"),
    (r"\b(?:the )?moon (?:and|around|orbiting|circling) (?:the )?earth\b|\bearth (?:and|with) (?:the )?moon\b", "moon orbiting earth"),
]
_NOISE = {"photoreal", "photorealistic", "realistic"}
# Present in some entry topics but not needed for a match ("pulley" -> "pulley system").
_OPTIONAL = {"system", "race", "orbiting", "garden", "bicycle"}


def _singular(tok: str) -> str:
    if len(tok) > 3 and tok.endswith("ies"):
        return tok[:-3] + "y"
    if len(tok) > 3 and tok.endswith("s") and not tok.endswith(("ss", "us", "is")):
        return tok[:-1]
    return tok


def seed_tokens(text: str) -> list[str]:
    """Normalized, synonym-folded, singular tokens of a request or an entry topic."""
    norm = normalize_topic(text)
    for pattern, repl in _PHRASES:
        norm = re.sub(pattern, repl, norm)
    return [_singular(t) for t in norm.split() if t not in _NOISE]


def seed_enabled() -> bool:
    return os.getenv("CAD_SEED_LIBRARY", "1").strip().lower() not in ("0", "false", "off", "no")


def _seed_index() -> dict[str, Any]:
    global _seed_cache
    if _seed_cache is None:
        try:
            parsed = json.loads((SEED_DIR / "index.json").read_text("utf-8"))
            entries = parsed.get("entries") if isinstance(parsed, dict) else None
            _seed_cache = {k: dict(v, seed=True) for k, v in (entries or {}).items()
                           if isinstance(v, dict) and v.get("glbFile") and (SEED_DIR / v["glbFile"]).is_file()}
        except (OSError, ValueError):
            log.warning("model library: seed library unavailable at %s", SEED_DIR)
            _seed_cache = {}
    return _seed_cache


def seed_models() -> list[dict[str, Any]]:
    return [dict(e) for e in _seed_index().values()] if seed_enabled() else []


def seed_topics() -> list[str]:
    """Distinct human topics available instantly from the seed library (for prompts)."""
    out: list[str] = []
    for entry in seed_models():
        topic = " ".join(seed_tokens(entry.get("topic", "")))
        if topic and topic not in out:
            out.append(topic)
    return sorted(out)


def match_seed(topic: str) -> dict[str, Any] | None:
    """Best seed entry for a free-text request, or None. Every core word of the entry's topic
    must appear in the request (2/3 of them for 3+ word topics); photoreal wins ties."""
    if not topic or not seed_enabled():
        return None
    query = set(seed_tokens(topic))
    if not query:
        return None
    best: tuple | None = None
    best_entry = None
    for key, entry in sorted(_seed_index().items()):
        toks = seed_tokens(entry.get("topic", ""))
        core = [t for t in toks if t not in _OPTIONAL] or toks
        if not core:
            continue
        hit = sum(1 for t in core if t in query)
        need = len(core) if len(core) <= 2 else -(-2 * len(core) // 3)
        if hit < need:
            continue
        extra = sum(1 for t in toks if t in _OPTIONAL and t in query)
        score = (hit + extra, hit / len(core), entry.get("generator") == "photoreal",
                 bool(entry.get("verified")))
        if best is None or score > best:
            best, best_entry = score, entry
    return dict(best_entry) if best_entry else None


def _read_seed_blob(entry: dict[str, Any]) -> bytes | None:
    key = entry["key"]
    if key not in _seed_blobs:
        try:
            data = (SEED_DIR / entry["glbFile"]).read_bytes()
        except OSError:
            return None
        if len(data) < MIN_GLB_BYTES:
            return None
        _seed_blobs[key] = data
    return _seed_blobs[key]


_STRONG_3D = re.compile(r"\b3d\b|three[- ]dimensional|\bmodel of\b|\bphoto ?real|\brender\b|\bcad\b", re.I)
_REQUEST = re.compile(
    r"\b(?:draw|show|build|create|make|render|display|visuali[sz]e|model|sketch|pull up|bring up|put up)\s+"
    r"(?:me\s+|us\s+)?(?:a\s+|an\s+|the\s+|our\s+|this\s+|that\s+|some\s+)?(.{1,60})", re.I)
_WINDOW_STOP = {"with", "and", "for", "that", "which", "so", "please", "on", "in", "to", "showing", "where",
                "while", "from", "here", "now", "next", "there", "it", "its", "spinning", "rotating", "turning"}


def seed_for_request(text: str) -> dict[str, Any] | None:
    """The seed entry a spoken/typed request is asking to see, or None.

    With an explicit 3D cue ("3D", "model of", "render") any seed topic in the sentence counts.
    Otherwise the topic must directly follow a show/draw verb, so "make car rentals talk to
    Postgres" is not a request for a car model."""
    if not text or not seed_enabled():
        return None
    if re.search(r"\b(?:remove|delete|clear|hide|don't|do not|no longer)\b", text, re.I):
        return None
    if _STRONG_3D.search(text):
        hit = match_seed(text)
        if hit is not None:
            return hit
    for m in _REQUEST.finditer(text):
        window: list[str] = []
        for tok in normalize_topic(m.group(1)).split():
            if tok in _WINDOW_STOP:
                break
            window.append(tok)
        phrase = " ".join(window)
        hit = match_seed(phrase) if phrase else None
        if hit is None:
            continue
        toks = [t for t in seed_tokens(phrase) if t not in {"3d", "model", "of", "photo", "real"}]
        if len(toks) <= len(seed_tokens(hit.get("topic", ""))) + 1:
            return hit
    return None
