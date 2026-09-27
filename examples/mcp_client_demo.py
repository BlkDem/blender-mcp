"""Plays the AI client: drives a running Blender through the full MCP stack.

    this script  --stdio-->  server.main  --WebSocket-->  add-on  -->  bpy

Nothing here is specific to Blender: it is the same conversation an AI client
would have, written out. Pair it with ``examples/gui_blender_demo.py``, which
runs inside Blender and takes a screenshot when this script says the scene is
ready.

Usage::

    python -m server.main &                       # or let this script spawn it
    python examples/mcp_client_demo.py \
        --blender "blender --python examples/gui_blender_demo.py -- --out /tmp/mcp-demo"

    # where quoting is awkward (a Windows path with spaces), one flag per argument;
    # the demo's own options travel in the environment, so nothing else needs quoting
    python examples/mcp_client_demo.py --out /tmp/mcp-demo \
        --blender-arg=blender \
        --blender-arg=--python \
        --blender-arg=examples/gui_blender_demo.py \
        --keep-open

Windows paths work as-is; quote the Blender executable if it has spaces.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import subprocess
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="mcp_client_demo")
    parser.add_argument("--out", default="/tmp/mcp-demo", help="shared folder for the screenshot flags")
    parser.add_argument(
        "--blender",
        default="",
        help="command that launches the Blender GUI demo, as one shell-style string",
    )
    parser.add_argument(
        "--blender-arg",
        action="append",
        default=[],
        metavar="ARG",
        help=(
            "one argument of the Blender command, as --blender-arg=VALUE. Repeat it "
            "instead of --blender to dodge quoting; the = form is required for values "
            "that start with a dash."
        ),
    )
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--python", default=sys.executable, help="interpreter that runs server.main")
    parser.add_argument("--no-launch", action="store_true", help="talk to an already running Blender")
    parser.add_argument(
        "--keep-open",
        action="store_true",
        help="keep the server and Blender alive after the screenshot, so a human can look at it",
    )
    return parser.parse_args()


class Client:
    """The AI-client half. Every call here is one a model would make."""

    def __init__(self, session: ClientSession) -> None:
        self.session = session

    async def call(self, name: str, arguments: dict | None = None) -> dict:
        result = await self.session.call_tool(name, arguments or {})
        if result.is_error:
            text = result.content[0].text
            _, _, tail = text.partition(": ")
            raise RuntimeError(f"{name} failed: {json.dumps(json.loads(tail), indent=2)}")
        return result.structured_content

    async def blocks(self, name: str, arguments: dict | None = None) -> list:
        """For the tools that answer with content blocks, not one JSON object."""
        result = await self.session.call_tool(name, arguments or {})
        if result.is_error:
            text = result.content[0].text
            _, _, tail = text.partition(": ")
            raise RuntimeError(f"{name} failed: {json.dumps(json.loads(tail), indent=2)}")
        return result.content

    async def build_the_table(self) -> None:
        """Example 4 from the README, as a chain of tool calls."""
        legs = {"LegFL": (-0.9, -0.4), "LegFR": (0.9, -0.4), "LegBL": (-0.9, 0.4), "LegBR": (0.9, 0.4)}

        scene = await self.call("blender.get_scene")
        log(
            f"scene starts with {scene['objects_count']} objects "
            f"(showing {scene['objects_shown']}, truncated={scene['objects_truncated']})"
        )

        await self.call("blender.begin_transaction")
        await self.call(
            "blender.create_object",
            {"type": "plane", "name": "Floor", "location": [0, 0, 0], "scale": [8, 8, 1]},
        )
        await self.call(
            "blender.create_object",
            {"type": "cube", "name": "TableTop", "location": [0, 0, 0.75], "scale": [1.6, 0.8, 0.05]},
        )
        for name, (x, y) in legs.items():
            await self.call(
                "blender.create_object",
                {"type": "cube", "name": name, "location": [x, y, 0.35], "scale": [0.08, 0.08, 0.7]},
            )
        await self.call(
            "blender.create_object",
            {"type": "sphere", "name": "Ball", "location": [-0.4, 0, 0.98], "scale": [0.25, 0.25, 0.25]},
        )
        await self.call(
            "blender.create_object",
            {"type": "torus", "name": "Ring", "location": [0.45, 0, 0.86], "scale": [0.2, 0.2, 0.2]},
        )
        committed = await self.call("blender.commit_transaction")
        log(f"transaction committed, {committed['undo_steps']} undo steps recorded")

        outcome = await self.call("blender.execute_python", {"code": MATERIAL_AND_LIGHTS})
        log(f"execute_python -> {outcome['result']['result']}")

        final = await self.call("blender.get_scene")
        names = sorted(obj["name"] for obj in final["objects"])
        log(f"scene now: {final['objects_count']} objects: {', '.join(names)}")

        await self.show_the_new_tools(names)

    async def show_the_new_tools(self, names: list[str]) -> None:
        """The things this build added on top of the README's example.

        Each one is worth a look in a real window: the picture, the registry
        entry, and a rollback you can see happen.
        """
        instances = await self.call("blender.get_instances")
        active = instances["active"]
        log(
            f"instance {active['id']} (pid {active['pid']}, Blender {active['blender_version']}) "
            f"on {active['address']}; takeover_allowed={instances['takeover_allowed']}"
        )

        still = await self.blocks("blender.wait_for_change", {"timeout": 0.5})
        log(f"wait_for_change on a quiet scene -> {json.loads(still[0].text)['changed']}")

        preview = await self.blocks("blender.render_preview", {"max_edge": 640})
        summary = json.loads(preview[0].text)
        image = [block for block in preview if getattr(block, "type", None) == "image"]
        log(
            f"render_preview -> {summary['output_path']} in {summary['render_time']}s, "
            f"{len(image)} image block(s), {len(image[0].data) if image else 0} base64 chars"
        )

        # Two rollbacks, because the difference between them is the point: a
        # checkpoint discards what came after it, the transaction start discards
        # the lot. The first leaves the spare cube alone, the second removes it.
        await self.call("blender.begin_transaction", {"label": "demo"})
        await self.call("blender.create_object", {"type": "cube", "name": "Spare", "location": [2, 2, 2]})
        await self.call("blender.checkpoint", {"label": "spare"})
        await self.call("blender.update_object", {"name": "Ball", "location": [9, 9, 9]})
        log(f"Ball moved to {[9, 9, 9]}")

        rolled = await self.call("blender.rollback_transaction", {"to": "spare"})
        log(
            f"rolled back to '{rolled['checkpoint']}' -> restored {rolled['restored']} object(s), "
            f"transaction {rolled['transaction']}, unrecoverable={rolled['unrecoverable']}"
        )
        ball = await self.call("blender.get_object", {"name": "Ball"})  # the object itself
        after_stage = await self.call("blender.get_objects", {"limit": 50})
        stage_names = sorted(obj["name"] for obj in after_stage["objects"])
        log(
            f"Ball back at {ball['location']}; the cube made before the checkpoint "
            f"stayed: {'Spare' in stage_names}"
        )

        closed = await self.call("blender.rollback_transaction", {})
        log(
            f"rolled back to the start -> {closed['restored']} object(s) restored, "
            f"transaction {closed['transaction']}"
        )
        final_stage = await self.call("blender.get_objects", {"limit": 50})
        end_names = sorted(obj["name"] for obj in final_stage["objects"])
        log(f"Spare gone: {'Spare' not in end_names} -> {end_names}")


#: Materials, lights and a camera, in one snippet — the part of a build that the
#: object tools deliberately do not cover.
MATERIAL_AND_LIGHTS = """
import bpy, math

def material(name, color, metallic=0.0, roughness=0.5):
    mat = bpy.data.materials.new(name)
    # diffuse_color alone only colours the solid viewport; the Principled BSDF
    # stays at its default grey, so a real render comes out white. Set both.
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
bpy.data.objects["Ball"].data.materials.append(material("Red", (0.72, 0.05, 0.05), roughness=0.35))
bpy.data.objects["Ring"].data.materials.append(material("Brass", (0.72, 0.55, 0.2), metallic=0.9))
bpy.data.objects["Floor"].data.materials.append(material("Ground", (0.5, 0.48, 0.45), roughness=0.8))
for name in ("TableTop", "LegFL", "LegFR", "LegBL", "LegBR"):
    bpy.data.objects[name].data.materials.append(wood)

world = bpy.data.worlds.new("World")
bpy.context.scene.world = world
world.use_nodes = True
background = world.node_tree.nodes["Background"]
background.inputs[0].default_value = (0.05, 0.06, 0.08, 1)
background.inputs[1].default_value = 2.0

key = bpy.data.objects.new("Key", bpy.data.lights.new("Key", type='AREA'))
key.data.energy = 400
key.data.size = 2.5
key.location = (2.0, -2.2, 3.2)
key.rotation_euler = (math.radians(42), 0, math.radians(42))
bpy.context.scene.collection.objects.link(key)

fill = bpy.data.objects.new("Fill", bpy.data.lights.new("Fill", type='AREA'))
fill.data.energy = 90
fill.location = (-2.5, -1.0, 1.8)
fill.rotation_euler = (math.radians(75), 0, math.radians(-65))
bpy.context.scene.collection.objects.link(fill)

camera = bpy.data.objects.new("Camera", bpy.data.cameras.new("CameraData"))
camera.location = (2.4, -3.2, 1.7)
camera.rotation_euler = (math.radians(74), 0, math.radians(37))
bpy.context.scene.collection.objects.link(camera)
bpy.context.scene.camera = camera
bpy.context.scene.render.engine = 'BLENDER_EEVEE' if 'BLENDER_EEVEE' in [
    item.identifier for item in
    bpy.types.RenderSettings.bl_rna.properties['engine'].enum_items
] else 'BLENDER_EEVEE_NEXT'

# Hide the camera gizmo from the viewport only. Only the camera: hide_set() on a
# light also removes its contribution to the viewport, which turns the scene
# black.
bpy.data.objects["Camera"].hide_set(True)

result = {"materials": len(bpy.data.materials), "objects": len(bpy.data.objects)}
"""


def log(message: str) -> None:
    print(f"[client] {message}", flush=True)


def split_command(command: str) -> list[str]:
    """Split a shell-style command line, keeping Windows paths intact.

    ``shlex.split(posix=True)`` eats backslashes, so ``C:\\out`` becomes
    ``C:out``; ``posix=False`` keeps them but leaves the quotes around a path
    with spaces, which ``CreateProcess`` then rejects. So: parse without POSIX
    rules and strip the quotes afterwards.
    """
    if os.name != "nt":
        return shlex.split(command)
    return [
        part[1:-1] if len(part) > 1 and part.startswith('"') and part.endswith('"') else part
        for part in shlex.split(command, posix=False)
    ]


def blender_environment(options: argparse.Namespace) -> dict[str, str]:
    """Hand the demo its configuration through the environment.

    Keeps the Blender command line down to the executable and the script path,
    which is the only part that genuinely has to be quoted on Windows.
    """
    return {
        **os.environ,
        "MCP_DEMO_OUT": options.out,
        "MCP_DEMO_HOST": "127.0.0.1",
        "MCP_DEMO_PORT": str(options.port),
        "MCP_DEMO_KEEP_OPEN": "1" if options.keep_open else "0",
    }


async def wait_for_gui(log_path: str, process: subprocess.Popen | None, timeout: float = 90.0) -> None:
    """Wait until the GUI has registered its timer and is ready for requests."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError(f"Blender exited early with {process.returncode}")
        try:
            with open(log_path, encoding="utf-8") as handle:
                if "waiting for the client" in handle.read():
                    return
        except FileNotFoundError:
            pass
        await asyncio.sleep(0.5)
    raise TimeoutError("the Blender GUI never reported ready")


async def main() -> int:
    options = arguments()
    os.makedirs(options.out, exist_ok=True)
    log_path = os.path.join(options.out, "gui.log")
    flag = os.path.join(options.out, "shoot.flag")
    done = os.path.join(options.out, "done.flag")
    shot = os.path.join(options.out, "screenshot.png")
    for stale in (flag, done, shot, log_path):
        if os.path.exists(stale):
            os.remove(stale)

    blender = None
    if not options.no_launch:
        command = options.blender_arg or ([options.blender] if options.blender else [])
        if not command:
            raise SystemExit("pass --blender, --blender-arg, or --no-launch")
        blender = subprocess.Popen(command, env=blender_environment(options))
        log(f"launched the Blender GUI: {' '.join(command)}")

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
    try:
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                log(f"MCP server up on stdio, bridge on 127.0.0.1:{options.port}")

                if blender is not None:
                    await wait_for_gui(log_path, blender)
                    log("Blender is ready; waiting for the add-on to connect")

                for _ in range(120):
                    probe = await session.call_tool("blender.get_scene", {})
                    if not probe.is_error:
                        break
                    await asyncio.sleep(0.5)
                else:
                    raise TimeoutError("the add-on never connected to the bridge")
                log("add-on connected")

                await Client(session).build_the_table()

                if blender is None:
                    log("no GUI to photograph; done")
                    return 0

                with open(flag, "w", encoding="utf-8") as handle:
                    handle.write("shoot")
                # Wait for the done flag, not just the file to appear: Blender is
                # still flushing the PNG when the first byte lands, and killing
                # it then truncates the image.
                for _ in range(240):
                    if os.path.exists(done):
                        break
                    await asyncio.sleep(0.5)
                else:
                    raise TimeoutError("Blender never finished writing the screenshot")
                await asyncio.sleep(2.0)
                log(f"screenshot: {shot} ({os.path.getsize(shot)} bytes)")

                if options.keep_open:
                    # Hold the stdio server open: the bridge lives in the same
                    # process, so quitting here would disconnect the add-on and
                    # the panel would start retrying.
                    log("--keep-open: holding the bridge open, Ctrl-C to stop")
                    while True:
                        await asyncio.sleep(3600)
    finally:
        if blender is not None and blender.poll() is None:
            if options.keep_open:
                log("leaving Blender running (--keep-open)")
            else:
                blender.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
