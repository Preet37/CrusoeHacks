"""FIXED Blender script (written for Twin): render a quick PNG preview of a GLB, for debugging quality.

    blender --background --factory-startup --python blender_preview.py -- <in.glb> <out.png>
Paths arrive via argv. Uses Workbench (fast, no GPU needed).
"""
import sys

import addon_utils
import bpy
from mathutils import Vector

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
if len(argv) != 2:
    print("usage: -- <in.glb> <out.png>", file=sys.stderr)
    sys.exit(2)
in_glb, out_png = argv

bpy.ops.wm.read_factory_settings(use_empty=True)
try:
    addon_utils.enable("io_scene_gltf2", default_set=False, persistent=False)
except Exception:  # noqa: BLE001
    pass
bpy.ops.import_scene.gltf(filepath=in_glb)
scene = bpy.context.scene
meshes = [o for o in scene.objects if o.type == "MESH"]
lo = Vector((1e9, 1e9, 1e9))
hi = Vector((-1e9, -1e9, -1e9))
for o in meshes:
    for v in o.bound_box:
        w = o.matrix_world @ Vector(v)
        lo = Vector(map(min, lo, w))
        hi = Vector(map(max, hi, w))
center = (lo + hi) / 2
radius = max((hi - lo).length / 2, 1e-3)

cam_data = bpy.data.cameras.new("cam")
cam = bpy.data.objects.new("cam", cam_data)
scene.collection.objects.link(cam)
direction = Vector((1.0, -1.4, 0.9)).normalized()
cam.location = center + direction * radius * 3.0
cam.rotation_euler = (center - cam.location).to_track_quat("-Z", "Y").to_euler()
cam_data.lens = 50
scene.camera = cam

scene.render.engine = "BLENDER_WORKBENCH"
scene.display.shading.light = "STUDIO"
scene.display.shading.color_type = "MATERIAL"
scene.display.shading.show_cavity = True
scene.render.resolution_x = 800
scene.render.resolution_y = 600
scene.render.image_settings.file_format = "PNG"
scene.render.filepath = out_png
bpy.ops.render.render(write_still=True)
