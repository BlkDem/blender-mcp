"""Build a line-up of humans through the add-on, so the human tools can be judged by eye.

Needs MPFB installed in this Blender (Get Extensions -> MPFB) and, for skins
and eyes, its MakeHuman system asset pack. Run it with a real Blender::

    blender --background --python examples/human_check.py -- --out ~/human-check

MPFB is switched on for this run only if it is installed but not enabled --
preferences are not saved, so the person's own settings stay as they were.

Writes ``report.json``, ``humans.blend``, ``lineup.png`` and ``head.png`` into
the output folder. Everything goes through ``BlenderConnection.run_action``, the
same seam the MCP server's requests take, transactions included.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "addon"))
# An installed copy of the add-on is already imported when Blender starts with it
# enabled; this script checks the checkout it lives in, so that copy is dropped.
for _name in [name for name in sys.modules if name == "blender_mcp" or name.startswith("blender_mcp.")]:
    del sys.modules[_name]

import addon_utils  # noqa: E402
import bpy  # noqa: E402
import mathutils  # noqa: E402
from blender_mcp.connection import BlenderConnection  # noqa: E402
from blender_mcp.protocol import ActionError  # noqa: E402

#: Descriptions a model would plausibly write, with the values the tool guide
#: suggests for them. The render is how those suggestions get checked.
LINEUP: list[dict[str, Any]] = [
    {
        "name": "Athlete",
        "gender": 1,
        "age_years": 30,
        "height_m": 1.82,
        "muscle": 0.75,
        "weight": 0.5,
        "hair": "short02",
    },
    {
        "name": "Skinny",
        "gender": 0,
        "age_years": 22,
        "height_m": 1.65,
        "muscle": 0.35,
        "weight": 0.15,
        "hair": "long01",
    },
    {"name": "Heavy", "gender": 1, "age_years": 50, "height_m": 1.75, "muscle": 0.4, "weight": 0.85},
    {"name": "Bodybuilder", "gender": 1, "age_years": 35, "height_m": 1.85, "muscle": 1.0, "weight": 0.65},
    {"name": "Elder", "gender": 0, "age_years": 78, "height_m": 1.6, "hair": "bob01"},
]


def output_dir() -> str:
    args = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    out = args[args.index("--out") + 1] if "--out" in args else os.path.join(os.getcwd(), "human-check")
    os.makedirs(out, exist_ok=True)
    return out


def call(bridge: BlenderConnection, action: str, params: dict[str, Any]) -> dict[str, Any]:
    """Run an action; an expected refusal comes back as its code, not a crash."""
    try:
        return bridge.run_action(action, params)
    except ActionError as exc:
        return {"error": exc.code, "message": exc.message, "details": exc.details}


def enable_mpfb() -> bool:
    if any(name.endswith("mpfb.services.humanservice") for name in sys.modules):
        return True
    module = next((mod.__name__ for mod in addon_utils.modules() if mod.__name__.endswith(".mpfb")), None)
    if module is None:
        return False
    bpy.context.preferences.use_preferences_save = False  # never write the person's preferences
    addon_utils.enable(module, default_set=True)
    return any(name.endswith("mpfb.services.humanservice") for name in sys.modules)


def placement() -> dict[str, Any]:
    """How far each human's parts sit from where they belong, in metres.

    Every part's bounding-box centre should be over the body: within a few
    centimetres sideways (x) and front-to-back (y). A part fitted to the wrong
    place -- the bug where parts landed at twice the body's offset -- shows up
    here as a number, before anyone has to spot it in a render.
    """
    bpy.context.view_layer.update()
    found: dict[str, Any] = {}
    for spec in LINEUP:
        body = bpy.data.objects.get(spec["name"])
        if body is None:
            continue
        offsets = {}
        for child in body.children_recursive:
            corners = [child.matrix_world @ mathutils.Vector(corner) for corner in child.bound_box]
            centre_x = sum(corner.x for corner in corners) / 8
            offsets[child.name] = round(centre_x - body.matrix_world.translation.x, 3)
        found[spec["name"]] = {
            "x_offsets": offsets,
            "ok": all(abs(value) < 0.05 for value in offsets.values()),
        }
    return found


def render(
    path: str, camera: Any, location: tuple[float, float, float], lens: float, size: tuple[int, int]
) -> None:
    scene = bpy.context.scene
    camera.location = location
    camera.rotation_euler = (math.radians(90), 0, 0)
    camera.data.lens = lens
    scene.render.resolution_x, scene.render.resolution_y = size
    scene.render.filepath = path
    bpy.ops.render.render(write_still=True)


def main() -> None:
    out = output_dir()
    report: dict[str, Any] = {"blender": bpy.app.version_string}
    try:
        check(out, report)
    finally:
        # Written whatever happened: a half-finished report is how a failure is read.
        with open(os.path.join(out, "report.json"), "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, default=str)


def check(out: str, report: dict[str, Any]) -> None:
    bpy.ops.wm.read_homefile(use_empty=True)
    bridge = BlenderConnection()

    # Before MPFB is on: the refusal has to be explicit, not a traceback.
    report["without_mpfb"] = {
        "options": call(bridge, "get_human_options", {}),
        "create": call(bridge, "create_human", {"name": "Nobody"}),
    }
    if not enable_mpfb():
        report["mpfb"] = "not installed"
        return

    report["options"] = call(bridge, "get_human_options", {"section": "nose"})
    report["refusals"] = {
        "bad_target": call(bridge, "create_human", {"name": "Typo", "targets": {"nose-size": 0.5}}),
        "typo_left_behind": bpy.data.objects.get("Typo") is not None,
        "bad_skin": call(bridge, "create_human", {"name": "Skinless", "skin": "no_such_skin"}),
        "skin_left_behind": bpy.data.objects.get("Skinless") is not None,
    }

    built: dict[str, Any] = {}
    for index, spec in enumerate(LINEUP):
        started = time.perf_counter()
        result = call(bridge, "create_human", {**spec, "location": [(index - 2) * 1.1, 0, 0]})
        built[spec["name"]] = {"seconds": round(time.perf_counter() - started, 2), **result}
    report["created"] = built
    report["placement"] = placement()
    report["duplicate"] = call(bridge, "create_human", {"name": "Athlete"})

    started = time.perf_counter()
    report["update"] = call(
        bridge,
        "update_human",
        {
            "name": "Athlete",
            "age_years": 45,
            "targets": {"nose-volume-decr-incr": 0.6, "chin-prominent-decr-incr": 0.5},
        },
    )
    report["update"]["seconds"] = round(time.perf_counter() - started, 2)
    eyes = (built["Athlete"].get("human", {}).get("parts", {}).get("eyes") or ["?"])[0]
    report["not_a_human"] = call(bridge, "update_human", {"name": eyes, "muscle": 1})

    # A transaction around a throwaway human: the rollback must take its eyes,
    # brows and LOD copy with it.
    bridge.run_action("begin_transaction", {"label": "temp"})
    call(bridge, "create_human", {"name": "Temp", "location": [0, 6, 0]})
    call(bridge, "add_human_lod", {"name": "Temp", "low_ratios": [0.5]})
    before = sorted(obj.name for obj in bpy.data.objects if obj.name.startswith("Temp"))
    rollback = bridge.run_action("rollback_transaction", {})
    after = sorted(obj.name for obj in bpy.data.objects if obj.name.startswith("Temp"))
    report["rollback"] = {"before": before, "after": after, "result": rollback}

    started = time.perf_counter()
    report["lod"] = call(
        bridge, "add_human_lod", {"name": "Elder", "subdivision_levels": 1, "low_ratios": [0.5, 0.2]}
    )
    report["lod"]["seconds"] = round(time.perf_counter() - started, 2)

    scene = bpy.context.scene
    scene.render.engine = "BLENDER_EEVEE"
    world = scene.world or bpy.data.worlds.new("World")
    scene.world = world
    world.color = (0.05, 0.05, 0.055)
    camera = bpy.data.objects.new("Camera", bpy.data.cameras.new("Camera"))
    scene.collection.objects.link(camera)
    scene.camera = camera
    sun = bpy.data.objects.new("Sun", bpy.data.lights.new("Sun", "SUN"))
    sun.data.energy = 4
    sun.rotation_euler = (math.radians(50), 0, math.radians(30))
    scene.collection.objects.link(sun)
    for name in ("Elder_LOD1", "Elder_LOD2"):
        if name in bpy.data.objects:
            bpy.data.objects[name].hide_render = True  # the line-up is about bodies, not LODs

    render(os.path.join(out, "lineup.png"), camera, (0, -8.5, 1.0), 50, (1600, 900))
    athlete = bpy.data.objects["Athlete"]
    bpy.context.view_layer.update()
    head_z = athlete.location.z + athlete.dimensions.z * 0.93
    render(os.path.join(out, "head.png"), camera, (athlete.location.x, -0.9, head_z), 85, (900, 900))

    bpy.ops.wm.save_as_mainfile(filepath=os.path.join(out, "humans.blend"))
    print("HUMAN_CHECK_DONE", out)


main()
