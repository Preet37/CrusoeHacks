# Adapted from LoominPhysicsSandbox (Preet37, pre-existing project) render-worker/server.js + render-worker/binPaths.js, ported to Python for Twin.
"""OpenSCAD source -> binary STL (openscad CLI) -> GLB bytes (headless Blender, fixed script).

Safety:
  * Only OpenSCAD source compiled by cad.plan from validated numbers reaches here.
    As defence in depth, source containing import(/include/use (file access) is rejected.
  * Blender only ever runs the fixed blender_stl_to_glb.py shipped next to this file;
    file paths are passed via argv. Model-written Python is never executed.
  * Subprocesses run without a shell, cwd = a fresh temp dir that is always removed.
Binaries: OPENSCAD_BIN / BLENDER_BIN env override, else PATH, else macOS app bundles.
Loomin's render-worker queued jobs one at a time; here render_glb is sync and the
caller (cad.pipeline) runs it in a thread with a per-room cap of one job.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONVERT_SCRIPT = HERE / "blender_stl_to_glb.py"
PREVIEW_SCRIPT = HERE / "blender_preview.py"
OPENSCAD_TIMEOUT = 40
BLENDER_TIMEOUT = 60
MIN_GLB_BYTES = 60

_MAC_BLENDER = "/Applications/Blender.app/Contents/MacOS/Blender"
_MAC_OPENSCAD = ("/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD",
                 "/Applications/OpenSCAD-2021.01.app/Contents/MacOS/OpenSCAD")
_FILE_ACCESS = re.compile(r"\b(?:import|include|use|surface)\s*[(<]")


class RenderError(RuntimeError):
    pass


def openscad_bin() -> str | None:
    return _resolve("OPENSCAD_BIN", "openscad", _MAC_OPENSCAD)


def blender_bin() -> str | None:
    return _resolve("BLENDER_BIN", "blender", (_MAC_BLENDER,))


def _resolve(env: str, name: str, fallbacks: tuple[str, ...]) -> str | None:
    if os.getenv(env):
        return os.environ[env]
    found = shutil.which(name)
    if found:
        return found
    return next((p for p in fallbacks if os.path.exists(p)), None)


def check_scad_source(src: str) -> None:
    """Reject empty source or any statement that could read files."""
    if not src or not src.strip():
        raise RenderError("Empty OpenSCAD script")
    # Strip comments first so a part name like "use" in a comment is harmless.
    code = re.sub(r"//[^\n]*|/\*.*?\*/", "", src, flags=re.S)
    if _FILE_ACCESS.search(code):
        raise RenderError("OpenSCAD source uses import/include/use (file access is not allowed)")


def _run(cmd: list[str], cwd: str, timeout: int, what: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, cwd=cwd, timeout=timeout, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, check=False)
    except subprocess.TimeoutExpired as e:
        raise RenderError(f"{what} timed out after {timeout}s") from e
    except OSError as e:
        raise RenderError(f"{what} could not start: {e}") from e


def scad_to_stl(scad_source: str, workdir: str) -> Path:
    """Compile scad -> binary STL inside workdir. Returns the STL path."""
    check_scad_source(scad_source)
    exe = openscad_bin()
    if not exe:
        raise RenderError("openscad binary not found (set OPENSCAD_BIN)")
    scad = Path(workdir, "model.scad")
    scad.write_text(scad_source, encoding="utf-8")
    stl = Path(workdir, "out.stl")
    proc = _run([exe, "-o", str(stl), "--export-format", "binstl", str(scad)], workdir, OPENSCAD_TIMEOUT, "OpenSCAD")
    if proc.returncode != 0:
        raise RenderError(f"OpenSCAD failed (code {proc.returncode}): {proc.stderr[-600:]}")
    if not stl.exists() or stl.stat().st_size <= 84:  # 80-byte header + count = empty mesh
        raise RenderError("OpenSCAD produced no STL — the script renders nothing")
    return stl


def stl_to_glb(stl: Path, workdir: str) -> bytes:
    exe = blender_bin()
    if not exe:
        raise RenderError("blender binary not found (set BLENDER_BIN)")
    glb = Path(workdir, "out.glb")
    proc = _run([exe, "--background", "--factory-startup", "--python", str(CONVERT_SCRIPT), "--", str(stl), str(glb)],
                workdir, BLENDER_TIMEOUT, "Blender")
    # Blender can exit non-zero after the GLB is already written (Loomin saw this headless),
    # so the file, not the exit code, decides success.
    data = glb.read_bytes() if glb.exists() else b""
    if len(data) < MIN_GLB_BYTES or data[:4] != b"glTF":
        raise RenderError(f"Blender produced no GLB (code {proc.returncode}): {(proc.stderr or proc.stdout)[-600:]}")
    return data


def render_glb(scad_source: str) -> bytes:
    """OpenSCAD source -> GLB bytes. Blocking; call via asyncio.to_thread."""
    workdir = tempfile.mkdtemp(prefix="twin-cad-")
    try:
        stl = scad_to_stl(scad_source, workdir)
        return stl_to_glb(stl, workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def render_preview_png(glb: bytes) -> bytes:
    """Debug helper: GLB -> 800x600 PNG via the fixed preview script."""
    exe = blender_bin()
    if not exe:
        raise RenderError("blender binary not found (set BLENDER_BIN)")
    workdir = tempfile.mkdtemp(prefix="twin-cad-preview-")
    try:
        src, out = Path(workdir, "in.glb"), Path(workdir, "out.png")
        src.write_bytes(glb)
        _run([exe, "--background", "--factory-startup", "--python", str(PREVIEW_SCRIPT), "--", str(src), str(out)],
             workdir, BLENDER_TIMEOUT, "Blender preview")
        if not out.exists():
            raise RenderError("preview render produced no PNG")
        return out.read_bytes()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
