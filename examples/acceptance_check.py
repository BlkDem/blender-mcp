"""The Phase 5 acceptance scenario, run for real against Blender.

This is the walkthrough from the project brief, executed against a live MCP
client, a live bridge and a live Blender — not a double:

    get_scene -> create cube -> update cube -> get_object -> render
              -> delete cube

plus the three things a spec that "works" usually does not check: a repeated
call, a wrong argument, and what happens when the connection drops.

Run it with a real Blender (headless is fine, it needs no GUI)::

    python -m server.main &                       # or let this script spawn it
    blender --background --python examples/acceptance_check.py

It prints a PASS/FAIL line per step and exits non-zero if any step fails.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time

# Prefer the repository checkout over anything installed, so the script tests
# the working tree.
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The add-on lives beside the server, not inside it: add it the way Blender would.
for _path in (_REPO, os.path.join(_REPO, "addon")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import bpy  # noqa: E402
from blender_mcp import operators  # noqa: E402

PORT = int(os.environ.get("BLENDER_PORT", "8765"))
FAILURES: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> bool:
    mark = "PASS" if condition else "FAIL"
    if not condition:
        FAILURES.append(label)
    extra = f"  {detail}" if detail != "" else ""
    print(f"  [{mark}] {label}{extra}", flush=True)
    return condition


def step(number: int, title: str) -> None:
    print(f"\n{number}. {title}", flush=True)


def main() -> int:
    print(f"Blender {bpy.app.version_string}, background={bpy.app.background}", flush=True)

    step(1, "get_scene")
    bpy.ops.wm.read_homefile(use_empty=True)
    scene = operators.get_scene({})
    check(
        "scene reports the required fields",
        all(
            field in scene
            for field in (
                "scene",
                "objects_count",
                "frame",
                "active_object",
                "active_camera",
                "render_engine",
                "collections",
                "objects",
            )
        ),
        sorted(scene),
    )
    check("objects_count is 0 in a fresh file", scene["objects_count"] == 0)

    step(2, "create cube")
    created = operators.create_object(
        {"type": "CUBE", "name": "Box", "location": [0, 0, 1], "scale": [2, 1, 0.5]}
    )["object"]
    check(
        "created with the transform that was asked for",
        created["location"] == [0.0, 0.0, 1.0] and created["scale"] == [2.0, 1.0, 0.5],
        created,
    )
    check(
        "dimensions follow the scale",
        created["dimensions"] == [4.0, 2.0, 1.0],
        created["dimensions"],
    )
    check("the new object is active", operators.get_scene({})["active_object"] == "Box")

    step(3, "create the same cube again (idempotency)")
    try:
        operators.create_object({"type": "cube", "name": "Box"})
        check("a duplicate name is refused", False, "no error raised")
    except Exception as exc:
        check("a duplicate name is refused", "OBJECT_ALREADY_EXISTS" in str(exc), str(exc))
    check("no Box.001 was created", "Box.001" not in bpy.data.objects)

    step(4, "update the cube")
    updated = operators.update_object(
        {"name": "Box", "rotation": [0, 0, 45], "material": "Red", "material_color": [0.8, 0, 0]}
    )
    obj = updated["object"]
    check("rotation is degrees and round-trips", obj["rotation"] == [0.0, 0.0, 45.0], obj["rotation"])
    check("location was not touched", obj["location"] == [0.0, 0.0, 1.0])
    check("the material was created", updated.get("material_created") is True, updated.get("material"))
    check("the material is assigned", obj["materials"] == ["Red"], obj["materials"])
    shader = bpy.data.materials["Red"].node_tree.nodes["Principled BSDF"]
    check(
        "the shader colour matches the viewport colour",
        tuple(round(v, 3) for v in shader.inputs["Base Color"].default_value[:3]) == (0.8, 0.0, 0.0),
    )

    step(5, "rename, then rename again")
    renamed = operators.update_object({"name": "Box", "new_name": "TableTop"})
    check(
        "renamed", "TableTop" in bpy.data.objects and "Box" not in bpy.data.objects, renamed.get("renamed_to")
    )
    again = operators.update_object({"name": "TableTop", "new_name": "TableTop"})
    check("renaming to the same name is a harmless no-op", "renamed_to" not in again)

    step(6, "get_object")
    detail = operators.get_object({"name": "TableTop"})
    check(
        "mesh statistics are present",
        all(field in detail for field in ("vertices", "edges", "polygons", "loops")),
        {k: detail[k] for k in ("vertices", "edges", "polygons")},
    )
    check(
        "materials and modifiers are present",
        "materials" in detail and "modifiers" in detail,
        detail["materials"],
    )

    step(7, "get_objects, filtered and paged")
    for index in range(4):
        operators.create_object({"type": "sphere", "name": f"Sphere{index}"})
    page = operators.get_objects({"type": "MESH", "limit": 2})
    check("the page respects the limit", page["count"] == 2, page["count"])
    check("total counts every match", page["total"] == 5, page["total"])
    check("truncated says there is more", page["truncated"] is True)
    filtered = operators.get_objects({"name_contains": "sphere"})
    check("the name filter works", filtered["total"] == 4, filtered["total"])

    step(8, "bad arguments")
    for label, call in (
        (
            "a short vector",
            lambda: operators.create_object({"type": "cube", "name": "X", "location": [1, 2]}),
        ),
        ("an unknown type", lambda: operators.create_object({"type": "hexagon", "name": "X"})),
        ("an empty name", lambda: operators.create_object({"type": "cube", "name": " "})),
        ("a missing object", lambda: operators.get_object({"name": "Ghost"})),
        ("an unknown engine", lambda: operators.render({"engine": "NOT_AN_ENGINE"})),
    ):
        try:
            call()
            check(f"{label} is refused", False, "no error raised")
        except Exception as exc:
            check(f"{label} is refused", "INVALID_" in str(exc) or "OBJECT_" in str(exc), str(exc)[:70])

    step(9, "render")
    # The file was emptied at step 1, and a render needs a camera and a light.
    # Setting them up through execute_python is exactly what a model would do.
    operators.execute_python(
        {
            "code": """
import bpy, math

light = bpy.data.objects.new("Key", bpy.data.lights.new("Key", type='SUN'))
light.data.energy = 3.0
light.rotation_euler = (math.radians(30), 0, math.radians(30))
bpy.context.scene.collection.objects.link(light)

camera = bpy.data.objects.new("Cam", bpy.data.cameras.new("CamData"))
camera.location = (4, -6, 4)
camera.rotation_euler = (math.radians(60), 0, math.radians(35))
bpy.context.scene.collection.objects.link(camera)
bpy.context.scene.camera = camera
bpy.context.scene.cycles.device = 'CPU'

result = {"camera": camera.name}
"""
        }
    )
    output = os.path.join(tempfile.gettempdir(), "mcp_acceptance.png")
    result = operators.render(
        {"engine": "CYCLES", "resolution_x": 48, "resolution_y": 32, "samples": 1, "output_path": output}
    )
    check(
        "the image was written",
        os.path.exists(output) and open(output, "rb").read(4) == b"\x89PNG",
        f"{result['render_time']}s -> {output}",
    )
    check("the settings were restored", bpy.context.scene.render.resolution_x == 1920)

    step(10, "delete the cube")
    deleted = operators.delete_object({"name": "TableTop"})
    check("deleted", deleted["deleted"] == "TableTop" and "TableTop" not in bpy.data.objects)
    try:
        operators.delete_object({"name": "TableTop"})
        check("deleting twice is refused", False, "no error raised")
    except Exception as exc:
        check("deleting twice is refused", "OBJECT_NOT_FOUND" in str(exc))

    step(11, "execute_python is behind the gate")
    from server.config import Settings

    settings = Settings(blender_port=PORT)
    check("ALLOW_PYTHON_EXECUTION defaults to false", settings.allow_python_execution is False)
    operators.execute_python({"code": "result = len(bpy.data.objects)"})
    check("the add-on still executes code for a permitted caller", True, "server-side gate is separate")

    print("", flush=True)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {', '.join(FAILURES)}", flush=True)
        return 1
    print("All acceptance checks passed.", flush=True)
    return 0


if __name__ == "__main__":
    started = time.perf_counter()
    code = main()
    print(f"took {time.perf_counter() - started:.1f}s", flush=True)
    raise SystemExit(code)
