"""The acceptance scenario over the full MCP stack, in a live Blender.

Same checks as ``examples/acceptance_check.py``, but driven through the real
chain — an MCP client on stdio, the WebSocket bridge, the add-on, and a real
Blender — so it exercises the transport and the tool surface as well as the
operators. ``acceptance_check.py`` is the fast, in-process one; this is the one
that would catch a broken wire.

    python examples/mcp_acceptance.py --blender "blender --background"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILURES: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> bool:
    if not condition:
        FAILURES.append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}{f'  {detail}' if detail != '' else ''}", flush=True)
    return condition


class Client:
    def __init__(self, session: ClientSession) -> None:
        self.session = session

    async def call(self, name: str, arguments: dict | None = None) -> dict:
        result = await self.session.call_tool(name, arguments or {})
        if result.is_error:
            _, _, tail = result.content[0].text.partition(": ")
            raise RuntimeError(f"{name} -> {tail}")
        return result.structured_content

    async def call_blocks(self, name: str, arguments: dict | None = None) -> list:
        """For the tools that answer with content blocks rather than one JSON object.

        ``render_preview`` and ``wait_for_change`` return a JSON summary plus
        whatever images they have, which is not a single structured object.
        """
        result = await self.session.call_tool(name, arguments or {})
        if result.is_error:
            _, _, tail = result.content[0].text.partition(": ")
            raise RuntimeError(f"{name} -> {tail}")
        return result.content

    async def read_resource(self, uri: str):
        return await self.session.read_resource(uri)

    async def expect_error(self, name: str, arguments: dict | None = None) -> str:
        result = await self.session.call_tool(name, arguments or {})
        if not result.is_error:
            raise AssertionError(f"{name} unexpectedly succeeded")
        _, _, tail = result.content[0].text.partition(": ")
        return json.loads(tail)["error"]["code"]


async def run(options: argparse.Namespace) -> None:
    parameters = StdioServerParameters(
        command=options.python,
        args=["-m", "server.main"],
        cwd=REPO,
        env={
            **os.environ,
            "BLENDER_PORT": str(options.port),
            "ALLOW_PYTHON_EXECUTION": "true",
            "LOG_LEVEL": "WARNING",
        },
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            client = Client(session)

            print("\n1. the tool surface", flush=True)
            names = {tool.name for tool in (await session.list_tools()).tools}
            required = {
                "blender.get_scene",
                "blender.get_objects",
                "blender.get_object",
                "blender.create_object",
                "blender.update_object",
                "blender.delete_object",
                "blender.render",
                "blender.execute_python",
            }
            check(
                "every required tool is registered",
                required <= names,
                sorted(required - names),
            )
            uris = {str(r.uri) for r in (await session.list_resources()).resources}
            check(
                "every resource is registered",
                uris == {"blender://scene", "blender://objects", "blender://render/latest"},
                sorted(uris),
            )

            print("\n2. waiting for Blender", flush=True)
            for _ in range(240):
                probe = await session.call_tool("blender.get_scene", {})
                if not probe.is_error:
                    break
                await asyncio.sleep(0.5)
            else:
                raise TimeoutError("no Blender attached to the bridge")

            # Start from a known-empty scene. bpy.ops.wm.read_homefile is on the
            # policy blocklist on purpose, so clear the objects directly.
            await client.call(
                "blender.execute_python",
                {
                    "code": (
                        "import bpy\n"
                        "for obj in list(bpy.data.objects):\n"
                        "    bpy.data.objects.remove(obj, do_unlink=True)\n"
                        "result = {'objects': len(bpy.data.objects)}\n"
                    )
                },
            )

            print("\n3. get_scene", flush=True)
            scene = await client.call("blender.get_scene")
            check(
                "reports the required fields",
                all(
                    f in scene
                    for f in (
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
            )
            check("an empty file has no objects", scene["objects_count"] == 0)

            print("\n4. create_object", flush=True)
            created = await client.call(
                "blender.create_object",
                {"type": "CUBE", "name": "Box", "location": [0, 0, 1], "scale": [2, 1, 0.5]},
            )
            check("uppercase type accepted", created["object"]["name"] == "Box")
            check(
                "dimensions follow the scale",
                created["object"]["dimensions"] == [4.0, 2.0, 1.0],
                created["object"]["dimensions"],
            )

            print("\n5. idempotency", flush=True)
            check(
                "a duplicate name is refused",
                await client.expect_error("blender.create_object", {"type": "cube", "name": "Box"})
                == "OBJECT_ALREADY_EXISTS",
            )
            listing = await client.call("blender.get_objects")
            check("no Box.001 appeared", listing["total"] == 1, listing["total"])

            print("\n6. update_object", flush=True)
            updated = await client.call(
                "blender.update_object",
                {"name": "Box", "rotation": [0, 0, 45], "material": "Red", "material_color": [0.8, 0.0, 0.0]},
            )
            check(
                "rotation round-trips in degrees",
                updated["object"]["rotation"] == [0.0, 0.0, 45.0],
                updated["object"]["rotation"],
            )
            check("location untouched", updated["object"]["location"] == [0.0, 0.0, 1.0])
            check(
                "material created and assigned",
                updated["material"] == "Red" and updated["material_created"] is True,
            )
            renamed = await client.call("blender.update_object", {"name": "Box", "new_name": "TableTop"})
            check(
                "renamed",
                renamed.get("renamed_to") == "Box -> TableTop",
                renamed.get("renamed_to"),
            )

            print("\n7. get_object", flush=True)
            detail = await client.call("blender.get_object", {"name": "TableTop"})
            check(
                "mesh statistics present",
                all(f in detail for f in ("vertices", "edges", "polygons", "loops")),
                {k: detail[k] for k in ("vertices", "polygons")},
            )
            check("materials reported", detail["materials"] == ["Red"], detail["materials"])

            print("\n8. get_objects paging", flush=True)
            for index in range(4):
                await client.call("blender.create_object", {"type": "sphere", "name": f"Sphere{index}"})
            page = await client.call("blender.get_objects", {"type": "MESH", "limit": 2})
            check("page respects the limit", page["count"] == 2)
            check("total counts every match", page["total"] == 5, page["total"])
            check("truncated is set", page["truncated"] is True)
            check(
                "filter by name",
                (await client.call("blender.get_objects", {"name_contains": "sphere"}))["total"] == 4,
            )

            print("\n9. errors", flush=True)
            check(
                "a missing object is OBJECT_NOT_FOUND",
                await client.expect_error("blender.get_object", {"name": "Ghost"}) == "OBJECT_NOT_FOUND",
            )
            check(
                "a short vector is INVALID_PARAMETER",
                await client.expect_error(
                    "blender.create_object", {"type": "cube", "name": "X", "location": [1, 2]}
                )
                == "INVALID_PARAMETER",
            )
            check(
                "a bad colour is INVALID_PARAMETER",
                await client.expect_error(
                    "blender.update_object",
                    {"name": "TableTop", "material": "R", "material_color": [9, 0, 0]},
                )
                == "INVALID_PARAMETER",
            )
            check(
                "an unknown type is INVALID_OBJECT_TYPE",
                await client.expect_error("blender.create_object", {"type": "hexagon", "name": "X"})
                == "INVALID_OBJECT_TYPE",
            )
            check(
                "a repeated create is refused",
                await client.expect_error("blender.create_object", {"type": "cube", "name": "Sphere0"})
                == "OBJECT_ALREADY_EXISTS",
            )

            print("\n10. resources", flush=True)
            scene_resource = json.loads((await session.read_resource("blender://scene")).contents[0].text)
            objects_resource = json.loads((await session.read_resource("blender://objects")).contents[0].text)
            check(
                "blender://scene is current",
                scene_resource["objects_count"] == 5,
                scene_resource["objects_count"],
            )
            check("blender://objects lists the scene", len(objects_resource["objects"]) == 5)

            print("\n11. execute_python", flush=True)
            outcome = await client.call(
                "blender.execute_python", {"code": "import bpy\nresult = {'objects': len(bpy.data.objects)}"}
            )
            check(
                "returns the result variable",
                outcome["result"]["result"] == {"objects": 5},
                outcome["result"]["result"],
            )
            check(
                "blocked code is rejected",
                await client.expect_error("blender.execute_python", {"code": "import os\nos.system('ls')"})
                == "VALIDATION_ERROR",
            )

            print("\n12. render", flush=True)
            # The scene was emptied at step 3 and a render needs a camera and a
            # light; setting them up is exactly what a model would reach for
            # execute_python to do.
            await client.call(
                "blender.execute_python",
                {
                    "code": (
                        "import bpy, math\n"
                        "light = bpy.data.objects.new('Key', bpy.data.lights.new('Key', type='SUN'))\n"
                        "light.data.energy = 3.0\n"
                        "light.rotation_euler = (math.radians(30), 0, math.radians(30))\n"
                        "bpy.context.scene.collection.objects.link(light)\n"
                        "cam = bpy.data.objects.new('Cam', bpy.data.cameras.new('CamData'))\n"
                        "cam.location = (4, -6, 4)\n"
                        "cam.rotation_euler = (math.radians(60), 0, math.radians(35))\n"
                        "bpy.context.scene.collection.objects.link(cam)\n"
                        "bpy.context.scene.camera = cam\n"
                        "bpy.context.scene.cycles.device = 'CPU'\n"
                        "result = {'camera': cam.name}\n"
                    )
                },
            )
            output = os.path.join(tempfile.gettempdir(), "mcp_acceptance.png")
            render = await client.call(
                "blender.render",
                {
                    "engine": "CYCLES",
                    "resolution_x": 48,
                    "resolution_y": 32,
                    "samples": 1,
                    "output_path": output,
                },
            )
            check(
                "an image was written",
                os.path.exists(output) and os.path.getsize(output) > 0,
                f"{render['render_time']}s -> {render['output_path']}",
            )

            print("\n13. delete_object", flush=True)
            deleted = await client.call("blender.delete_object", {"name": "TableTop"})
            check("deleted", deleted["deleted"] == "TableTop")
            check(
                "deleting twice is refused",
                await client.expect_error("blender.delete_object", {"name": "TableTop"})
                == "OBJECT_NOT_FOUND",
            )

            print("\n14. render_preview returns a picture", flush=True)
            preview = await client.call_blocks(
                "blender.render_preview", {"max_edge": 64, "engine": "CYCLES", "samples": 1}
            )
            check(
                "the preview is an image block",
                any(getattr(block, "type", None) == "image" for block in preview),
                [getattr(b, "type", None) for b in preview],
            )
            latest = await client.read_resource("blender://render/latest")
            check("the last render is served as bytes", bool(latest.contents[0].blob))

            print("\n15. waiting for a change", flush=True)
            still = await client.call_blocks("blender.wait_for_change", {"timeout": 0.5})
            check("a quiet scene answers no", json.loads(still[0].text)["changed"] is False)

            print("\n16. a transaction is undoable", flush=True)
            await client.call("blender.begin_transaction")
            for index in range(3):
                await client.call("blender.create_object", {"type": "cylinder", "name": f"Leg{index}"})
            committed = await client.call("blender.commit_transaction")
            check("undo steps were recorded", committed["undo_steps"] == 3, committed)

            print("\n17. a checkpoint unwinds one stage", flush=True)
            await client.call("blender.begin_transaction", {"label": "layout"})
            await client.call("blender.create_object", {"type": "cube", "name": "Shell"})
            marked = await client.call("blender.checkpoint", {"label": "shell"})
            check("the checkpoint is named", marked["checkpoints"] == ["layout", "shell"], marked)
            await client.call("blender.create_object", {"type": "cube", "name": "Detail"})
            rolled = await client.call("blender.rollback_transaction", {"to": "shell"})
            after = await client.call("blender.get_objects", {"limit": 50})
            names = {entry["name"] for entry in after["objects"]}
            check("the detail is gone", "Detail" not in names, sorted(names))
            check("the shell stayed", "Shell" in names, sorted(names))
            check("the transaction is still open", rolled["transaction"] == "open", rolled)
            await client.call("blender.commit_transaction")

            print("\n18. the instances registry", flush=True)
            listed = await client.call("blender.get_instances", {})
            check("one instance is active", listed["active"]["status"] == "active", listed["active"])
            check("takeover is off by default", listed["takeover_allowed"] is False)


def main() -> int:
    parser = argparse.ArgumentParser(prog="mcp_acceptance")
    parser.add_argument("--python", default=sys.executable, help="interpreter for server.main")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--blender-arg",
        action="append",
        default=[],
        metavar="ARG",
        help=(
            "one argument of the Blender command, as --blender-arg=VALUE; repeat to launch "
            "a headless Blender with examples/blender_attach.py. Omit to use a Blender that "
            "is already attached."
        ),
    )
    parser.add_argument("--blender-seconds", type=float, default=300.0, help="headless Blender lifetime")
    options = parser.parse_args()

    blender = None
    if options.blender_arg:
        blender = subprocess.Popen(
            options.blender_arg,
            env={
                **os.environ,
                # An existing value wins: a Blender on the other side of a
                # boundary — a Windows Blender reaching a server in WSL, say —
                # cannot use loopback, and the operator is the one who knows.
                "BLENDER_ATTACH_HOST": os.environ.get("BLENDER_ATTACH_HOST", "127.0.0.1"),
                "BLENDER_ATTACH_PORT": os.environ.get("BLENDER_ATTACH_PORT", str(options.port)),
                "BLENDER_ATTACH_SECONDS": str(options.blender_seconds),
            },
        )
        print(f"launched: {' '.join(options.blender_arg)}", flush=True)
    try:
        asyncio.run(run(options))
    finally:
        if blender is not None and blender.poll() is None:
            blender.terminate()
            try:
                blender.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover
                blender.kill()
    print("", flush=True)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {', '.join(FAILURES)}", flush=True)
        return 1
    print("All acceptance checks passed.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
