"""Build a test scene in Blender and save it, so you can look at the result.

Run it with a real Blender::

    blender --background --python examples/test_scene.py
    blender --background --python examples/test_scene.py -- --out ~/blender-mcp-demo

Or, if you have the ``bpy`` Python module instead of the Blender app::

    python -m bpy --background --python examples/test_scene.py

Writes ``<out>/test_scene.blend`` and a Cycles render next to it. Everything
goes through the add-on's own operator layer, so what it exercises is exactly
what the MCP tools call — if this script produces a scene, ``blender.create_object``
works.

The scene is a small still life: a table, a sphere on it, a key light, a camera.
It exists to answer "is the bridge talking to Blender?", so it uses every
primitive type and both mutation styles (tools, then execute_python).
"""

from __future__ import annotations

import math
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "addon"))

import bpy  # noqa: E402
from blender_mcp import operators  # noqa: E402
from blender_mcp.connection import BlenderConnection  # noqa: E402

LEGS = {"LegFL": (-0.9, -0.4), "LegFR": (0.9, -0.4), "LegBL": (-0.9, 0.4), "LegBR": (0.9, 0.4)}


def output_dir() -> str:
    """``--out <dir>`` after the Blender ``--`` separator, else a default."""
    argv = sys.argv
    if "--" in argv and "--out" in argv:
        index = argv.index("--out")
        if index + 1 < len(argv):
            return os.path.abspath(argv[index + 1])
    return os.path.join(os.path.expanduser("~"), "blender-mcp-demo")


def build() -> dict:
    """Create the scene through the add-on's operator layer."""
    bpy.ops.wm.read_homefile(use_empty=True)
    scene = bpy.context.scene
    scene.name = "TestScene"

    operators.create_object({"type": "plane", "name": "Floor", "location": [0, 0, 0], "scale": [8, 8, 1]})
    operators.create_object(
        {"type": "cube", "name": "TableTop", "location": [0, 0, 0.75], "scale": [1.6, 0.8, 0.05]}
    )
    for name, (x, y) in LEGS.items():
        operators.create_object(
            {"type": "cube", "name": name, "location": [x, y, 0.35], "scale": [0.08, 0.08, 0.7]}
        )

    # The rest of the primitive set, on a shelf behind the table, so one render
    # proves all six build paths.
    operators.create_object(
        {"type": "sphere", "name": "Ball", "location": [-0.4, 0, 0.98], "scale": [0.25, 0.25, 0.25]}
    )
    operators.create_object(
        {"type": "cylinder", "name": "Cup", "location": [0.3, 0.1, 0.93], "scale": [0.12, 0.12, 0.18]}
    )
    operators.create_object(
        {"type": "cone", "name": "Cone", "location": [0.55, -0.1, 0.93], "scale": [0.12, 0.12, 0.18]}
    )
    operators.create_object(
        {"type": "torus", "name": "Ring", "location": [-0.75, 0.2, 0.86], "scale": [0.2, 0.2, 0.2]}
    )

    # A transaction, so a mistake here could have been rolled back in one call.
    # Transactions live in the connection layer because they are undo bookkeeping
    # rather than a bpy operation; a BlenderConnection can run one without a
    # socket, which is what makes this script possible.
    bridge = BlenderConnection()
    bridge.run_action("begin_transaction", {})
    for name, (x, y) in LEGS.items():
        bridge.run_action("update_object", {"name": name, "rotation": [0, 0, math.degrees(math.atan2(y, x))]})
    print("  transaction:", bridge.run_action("commit_transaction", {}))

    operators.execute_python(
        {
            "code": """
import bpy, math

def material(name, color, metallic=0.0, roughness=0.5):
    mat = bpy.data.materials.new(name)
    # diffuse_color alone only colours the solid viewport; the Principled BSDF
    # stays at its default grey, so any real render comes out white. Set both.
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = (*color, 1.0)
    bsdf.inputs["Metallic"].default_value = metallic
    bsdf.inputs["Roughness"].default_value = roughness
    mat.diffuse_color = (*color, 1.0)
    mat.metallic = metallic
    mat.roughness = roughness
    return mat

wood = material("Wood", (0.32, 0.19, 0.07), roughness=0.65)
red = material("Red", (0.72, 0.05, 0.05), roughness=0.35)
white = material("Porcelain", (0.85, 0.85, 0.82), roughness=0.25)
metal = material("Brass", (0.72, 0.55, 0.20), metallic=0.9, roughness=0.3)

for name in ("TableTop", "LegFL", "LegFR", "LegBL", "LegBR"):
    bpy.data.objects[name].data.materials.append(wood)
bpy.data.objects["Ball"].data.materials.append(red)
bpy.data.objects["Cup"].data.materials.append(white)
bpy.data.objects["Cone"].data.materials.append(white)
bpy.data.objects["Ring"].data.materials.append(metal)
bpy.data.objects["Floor"].data.materials.append(material("Floor", (0.55, 0.53, 0.5), roughness=0.8))

world = bpy.context.scene.world
if world is None:
    world = bpy.data.worlds.new("World")
    bpy.context.scene.world = world
world.use_nodes = True
world.node_tree.nodes["Background"].inputs[0].default_value = (0.05, 0.06, 0.08, 1.0)
world.node_tree.nodes["Background"].inputs[1].default_value = 2.0

key_data = bpy.data.lights.new("Key", type='AREA')
key_data.energy = 220
key_data.size = 2.0
key = bpy.data.objects.new("Key", key_data)
key.location = (2.0, -2.2, 3.2)
key.rotation_euler = (math.radians(42), 0, math.radians(42))
bpy.context.scene.collection.objects.link(key)

fill_data = bpy.data.lights.new("Fill", type='AREA')
fill_data.energy = 40
fill = bpy.data.objects.new("Fill", fill_data)
fill.location = (-2.5, -1.0, 1.8)
fill.rotation_euler = (math.radians(75), 0, math.radians(-65))
bpy.context.scene.collection.objects.link(fill)

camera = bpy.data.objects.new("Camera", bpy.data.cameras.new("CameraData"))
camera.location = (2.4, -3.2, 1.7)
camera.rotation_euler = (math.radians(74), 0, math.radians(37))
bpy.context.scene.collection.objects.link(camera)
bpy.context.scene.camera = camera

result = {"materials": len(bpy.data.materials), "lights": 2, "objects": len(bpy.data.objects)}
"""
        }
    )
    return operators.get_scene({"include_details": True})


def main() -> int:
    out = output_dir()
    os.makedirs(out, exist_ok=True)

    started = time.perf_counter()
    scene = build()
    print(f"Scene built in {time.perf_counter() - started:.2f}s: {scene['objects_total']} objects")
    for obj in scene["objects"]:
        print(f"  {obj['name']:<10} {obj['type']:<6} {obj['dimensions']}")

    blend = os.path.join(out, "test_scene.blend")
    bpy.ops.wm.save_as_mainfile(filepath=blend)
    print(f"Saved {blend}")

    bpy.context.scene.render.engine = "CYCLES"
    bpy.context.scene.cycles.device = "CPU"
    result = operators.render(
        {
            "engine": "CYCLES",
            "resolution_x": 640,
            "resolution_y": 400,
            "samples": 32,
            "output_path": os.path.join(out, "test_scene.png"),
        }
    )
    print(f"Rendered {result['output_path']} in {result['render_time']}s -> {result['used']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
