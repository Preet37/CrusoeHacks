# Adapted from LoominPhysicsSandbox (Preet37, pre-existing project) render-worker/server.js (importPy), ported to Python for Twin.
"""FIXED Blender conversion script: STL -> centred, normalised, smooth-shaded GLB.

Run by server/cad/render.py as:
    blender --background --factory-startup --python blender_stl_to_glb.py -- <in.stl> <out.glb>
Paths arrive via argv (never string-interpolated into source). This file is the
only Python Blender ever runs for Twin; model-written scripts are never executed.
Loomin's thumbnail render is skipped (slow); see blender_preview.py for a PNG.
"""
import sys

import addon_utils
import bpy

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
if len(argv) != 2:
    print("usage: -- <in.stl> <out.glb>", file=sys.stderr)
    sys.exit(2)
out_stl, out_glb = argv

bpy.ops.wm.read_factory_settings(use_empty=True)
# read_factory_settings clears all addons — re-enable the glTF exporter.
try:
    addon_utils.enable("io_scene_gltf2", default_set=False, persistent=False)
except Exception as e:  # noqa: BLE001
    print("io_scene_gltf2 enable:", e, file=sys.stderr)

# Blender 4.x+ uses wm.stl_import; 3.x used import_mesh.stl.
try:
    bpy.ops.wm.stl_import(filepath=out_stl)
except AttributeError:
    bpy.ops.import_mesh.stl(filepath=out_stl)
objs = list(bpy.context.selected_objects) or [o for o in bpy.data.objects if o.type == "MESH"]

if objs:
    lo = [1e18] * 3
    hi = [-1e18] * 3
    for o in objs:
        for v in o.bound_box:
            for i in range(3):
                lo[i] = min(lo[i], v[i])
                hi[i] = max(hi[i], v[i])
    c = [(lo[i] + hi[i]) / 2 for i in range(3)]
    size = max(hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2], 1e-6)
    s = 2.3 / size
    for o in objs:
        o.location.x -= c[0]
        o.location.y -= c[1]
        o.location.z -= c[2]
        o.scale = (o.scale[0] * s, o.scale[1] * s, o.scale[2] * s)

    # Twin: smooth curved faces but keep hard edges (STL import is flat-shaded).
    for o in objs:
        bpy.context.view_layer.objects.active = o
        o.select_set(True)
    try:
        bpy.ops.object.shade_smooth_by_angle(angle=0.35)
    except Exception:  # noqa: BLE001 - older Blender
        try:
            bpy.ops.object.shade_auto_smooth(angle=0.35)
        except Exception as e:  # noqa: BLE001
            print("smooth shading skipped:", e, file=sys.stderr)

    mat = bpy.data.materials.new(name="Mat")
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf:
        bsdf.inputs["Base Color"].default_value = (0.35, 0.5, 0.9, 1)
        bsdf.inputs["Roughness"].default_value = 0.45
        bsdf.inputs["Metallic"].default_value = 0.15
    for o in objs:
        if len(o.data.materials) == 0:
            o.data.materials.append(mat)
        else:
            o.data.materials[0] = mat

try:
    bpy.ops.export_scene.gltf(filepath=out_glb, export_format="GLB", use_selection=False)
except Exception as e1:  # noqa: BLE001
    bpy.ops.export_scene.gltf(filepath=out_glb, export_format="GLB")
