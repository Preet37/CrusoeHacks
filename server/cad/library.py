# Adapted from LoominPhysicsSandbox (Preet37, pre-existing project) lib/modelLibrary.ts, ported to Python for Twin.
"""Model library: an on-disk cache of accepted CAD models keyed by topic.

Building a model costs research calls plus an OpenSCAD/Blender round trip, and the
result for "tennis racket" is the same every session. Once rendered and NOT rejected
it is replayed instantly. A rejection evicts the entry (the next request re-renders)
and the rejection count survives eviction so a topic that keeps failing is visible.

Storage: GLB files + index.json under server/learning/runs/cad/library (gitignored;
override with CAD_LIBRARY_DIR). If the filesystem is unavailable it degrades to memory.
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


def lookup_model(topic: str) -> tuple[dict[str, Any], bytes] | None:
    """A previously accepted model for this topic, or None."""
    key = topic_key(topic)
    with _lock:
        index = _read_index()
        entry = index["entries"].get(key)
        if not entry:
            return None
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
    """Thumbs down: evict so the next request re-renders; keep the rejection count."""
    key = topic_key(topic)
    with _lock:
        index = _read_index()
        rejections = index["rejections"].get(key, 0) + 1
        index["rejections"][key] = rejections
        entry = index["entries"].pop(key, None)
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
