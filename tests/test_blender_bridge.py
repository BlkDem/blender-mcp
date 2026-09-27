"""The full stack against a real Blender: MCP client -> server -> add-on -> bpy.

Skipped unless the real ``bpy`` module is installed (see
:mod:`tests.test_blender_integration`). This is the closest thing to the
acceptance list in the README that can run without a desktop: a real MCP
session, a real WebSocket bridge, the add-on's own client, and real ``bpy``
mutating a real scene.

One thing it cannot cover headlessly: Blender's timer scheduler. The add-on
normally runs its actions from ``bpy.app.timers``, which needs Blender's event
loop. These tests call :meth:`BlenderConnection.drain` directly instead — the same
function the timer calls, on the same main thread, reading the same queue — so the
dispatch path is exercised but the scheduler registration is not.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import pytest

from tests.support.bpy_gate import require_bpy

bpy = require_bpy()

from blender_mcp.connection import BlenderConnection  # noqa: E402
from mcp.client.session import ClientSession  # noqa: E402
from mcp.shared.memory import create_client_server_memory_streams  # noqa: E402
from mcp_types import CallToolResult  # noqa: E402

from server.config import Settings  # noqa: E402
from server.mcp.server import create_server  # noqa: E402

pytestmark = pytest.mark.anyio

_PORT_BASE = 27000 + random.randint(0, 2000)
_next_port = iter(range(_PORT_BASE, _PORT_BASE + 500))


def free_port() -> int:
    return next(_next_port)


def payload_of(result: CallToolResult) -> dict:
    assert result.structured_content is not None, result.content
    return result.structured_content


def error_of(result: CallToolResult) -> dict:
    assert result.is_error, result.content
    _, _, tail = result.content[0].text.partition(": ")
    return json.loads(tail)["error"]


@asynccontextmanager
async def stack(**overrides) -> AsyncIterator[tuple[ClientSession, BlenderConnection]]:
    """A live MCP server with the real add-on attached to it."""
    port = free_port()
    values: dict = {
        "blender_host": "127.0.0.1",
        "blender_port": port,
        "blender_request_timeout": 20.0,
        "allow_python_execution": True,
    }
    values.update(overrides)
    server = create_server(Settings(**values))
    add_on = BlenderConnection("127.0.0.1", port)

    bpy.ops.wm.read_homefile(use_empty=True)
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(
                server._lowlevel_server.run,  # noqa: SLF001
                server_streams[0],
                server_streams[1],
                server._lowlevel_server.create_initialization_options(),  # noqa: SLF001
            )
            async with ClientSession(client_streams[0], client_streams[1]) as session:
                await session.initialize()
                add_on.connect()

                with anyio.fail_after(10.0):
                    while not add_on.is_connected:
                        await anyio.sleep(0.02)

                # Stand in for bpy.app.timers: same function, same thread, same queue.
                pumping = True

                async def pump() -> None:
                    while pumping:
                        add_on.drain()
                        await anyio.sleep(0.01)

                async with anyio.create_task_group() as pump_group:
                    pump_group.start_soon(pump)
                    try:
                        yield session, add_on
                    finally:
                        pumping = False
            task_group.cancel_scope.cancel()
    add_on.disconnect()


async def test_the_addon_reports_a_working_bridge() -> None:
    async with stack() as (session, add_on):
        assert add_on.is_connected
        result = payload_of(await session.call_tool("blender.get_scene", {}))
        assert result["scene"] == bpy.context.scene.name
        assert result["objects_count"] == len(bpy.data.objects)


async def test_create_object_reaches_real_bpy() -> None:
    async with stack() as (session, _):
        created = payload_of(
            await session.call_tool(
                "blender.create_object",
                {"type": "cube", "name": "TableTop", "location": [0, 0, 0.75], "scale": [2, 1, 0.05]},
            )
        )
    assert created["object"]["dimensions"] == [4.0, 2.0, 0.1]
    assert "TableTop" in bpy.data.objects
    assert bpy.data.objects["TableTop"].location.z == pytest.approx(0.75)


async def test_the_full_acceptance_walkthrough() -> None:
    async with stack() as (session, _):
        scene = payload_of(await session.call_tool("blender.get_scene", {}))
        assert scene["objects_count"] == 0

        payload_of(await session.call_tool("blender.create_object", {"type": "cube", "name": "Box"}))
        fetched = payload_of(await session.call_tool("blender.get_object", {"name": "Box"}))
        assert fetched["name"] == "Box"
        assert fetched["dimensions"] == [2.0, 2.0, 2.0]

        updated = payload_of(
            await session.call_tool("blender.update_object", {"name": "Box", "dimensions": [4, 1, 1]})
        )
        assert updated["object"]["dimensions"] == [4.0, 1.0, 1.0]

        assert error_of(await session.call_tool("blender.get_object", {"name": "Ghost"}))["code"] == (
            "OBJECT_NOT_FOUND"
        )
        assert (
            error_of(await session.call_tool("blender.create_object", {"type": "cube", "name": "Box"}))[
                "code"
            ]
            == "OBJECT_ALREADY_EXISTS"
        )

        payload_of(await session.call_tool("blender.delete_object", {"name": "Box"}))
        assert "Box" not in bpy.data.objects
        assert error_of(await session.call_tool("blender.delete_object", {"name": "Box"}))["code"] == (
            "OBJECT_NOT_FOUND"
        )


async def test_execute_python_runs_in_blender() -> None:
    code = "import bpy\nresult = {'objects': sorted(o.name for o in bpy.data.objects)}"
    async with stack() as (session, _):
        payload_of(await session.call_tool("blender.create_object", {"type": "sphere", "name": "Ball"}))
        outcome = payload_of(await session.call_tool("blender.execute_python", {"code": code}))
    assert outcome["result"]["result"] == {"objects": ["Ball"]}
    assert outcome["validation"]["screened"] is True


async def test_execute_python_is_refused_when_disabled() -> None:
    async with stack(allow_python_execution=False) as (session, _):
        result = await session.call_tool("blender.execute_python", {"code": "import bpy"})
    assert error_of(result)["code"] == "PYTHON_EXECUTION_DISABLED"


async def test_blocked_code_never_reaches_blender() -> None:
    async with stack() as (session, _):
        result = await session.call_tool("blender.execute_python", {"code": "import os\nos.system('ls')"})
    assert error_of(result)["code"] == "VALIDATION_ERROR"


async def test_a_transaction_is_undoable_in_real_blender() -> None:
    async with stack() as (session, _):
        payload_of(await session.call_tool("blender.create_object", {"type": "cube", "name": "Before"}))
        payload_of(await session.call_tool("blender.begin_transaction", {}))
        for index in range(3):
            payload_of(
                await session.call_tool("blender.create_object", {"type": "cylinder", "name": f"Leg{index}"})
            )
        committed = payload_of(await session.call_tool("blender.commit_transaction", {}))
    assert committed["undo_steps"] == 3
    assert {"Before", "Leg0", "Leg1", "Leg2"} <= set(bpy.data.objects.keys())


async def test_rollback_removes_the_objects_it_recorded() -> None:
    async with stack() as (session, _):
        payload_of(await session.call_tool("blender.create_object", {"type": "cube", "name": "Before"}))
        payload_of(await session.call_tool("blender.begin_transaction", {}))
        for index in range(3):
            payload_of(
                await session.call_tool("blender.create_object", {"type": "cylinder", "name": f"Leg{index}"})
            )
        rolled = payload_of(await session.call_tool("blender.rollback_transaction", {}))
    assert rolled["restored"] == 3
    assert rolled["transaction"] == "closed"
    assert rolled["checkpoint"] == "begin"
    assert set(bpy.data.objects.keys()) == {"Before"}


async def test_a_checkpoint_rolls_back_only_the_stage_after_it() -> None:
    async with stack() as (session, _):
        await session.call_tool("blender.create_object", {"type": "cube", "name": "Before"})
        await session.call_tool("blender.begin_transaction", {"label": "layout"})
        await session.call_tool("blender.create_object", {"type": "cube", "name": "Shell"})
        marked = payload_of(await session.call_tool("blender.checkpoint", {"label": "shell"}))
        assert marked["checkpoints"] == ["layout", "shell"]
        await session.call_tool("blender.create_object", {"type": "cube", "name": "Detail"})

        rolled = payload_of(await session.call_tool("blender.rollback_transaction", {"to": "shell"}))
        assert rolled["checkpoints"] == ["layout", "shell"]
        assert rolled["transaction"] == "open"
        assert set(bpy.data.objects.keys()) == {"Before", "Shell"}
        await session.call_tool("blender.commit_transaction", {})


async def test_waiting_reports_no_change_when_the_scene_is_still() -> None:
    async with stack() as (session, _):
        blocks = await session.call_tool("blender.wait_for_change", {"timeout": 0.2})
    payload = json.loads(blocks.content[0].text)
    assert payload["changed"] is False
    assert payload["changes"] == 0
    assert payload["waited"] < 2.0


async def test_waiting_returns_as_soon_as_something_changes() -> None:
    async with stack() as (session, _):
        await session.call_tool("blender.create_object", {"type": "cube", "name": "Later"})

        async def change_soon() -> None:
            await asyncio.sleep(0.15)
            await session.call_tool("blender.create_object", {"type": "cube", "name": "Trigger"})

        asyncio.create_task(change_soon())
        blocks = await session.call_tool("blender.wait_for_change", {"timeout": 5})
    payload = json.loads(blocks.content[0].text)
    assert payload["changed"] is True
    assert "Trigger" in payload["changed_objects"]
    assert payload["waited"] < 4.0


async def test_get_instances_reports_the_attached_blender() -> None:
    async with stack() as (session, _):
        with anyio.fail_after(10.0):
            # The socket is up before the identity handshake finishes, so the
            # first answers can legitimately report nothing yet.
            while True:
                payload = payload_of(await session.call_tool("blender.get_instances", {}))
                if payload["active"] is not None:
                    break
                await anyio.sleep(0.02)
    assert payload["takeover_allowed"] is False
    assert payload["active"]["status"] == "active"
    assert payload["active"]["pid"] > 0
    assert payload["active"]["blender_version"]


async def test_resources_read_the_real_scene() -> None:
    async with stack() as (session, _):
        for name in ("A", "B"):
            await session.call_tool("blender.create_object", {"type": "cube", "name": name})
        scene = json.loads((await session.read_resource("blender://scene")).contents[0].text)
        objects = json.loads((await session.read_resource("blender://objects")).contents[0].text)
    assert scene["objects_count"] == 2
    assert sorted(obj["name"] for obj in objects["objects"]) == ["A", "B"]


async def add_camera(session: ClientSession) -> None:
    """An empty file has no camera, and Blender refuses to render without one.

    Cycles on the CPU, because the default engine is EEVEE and EEVEE needs a
    GPU context that the ``bpy`` module does not have: asking for it aborts the
    process rather than raising.
    """
    payload_of(
        await session.call_tool(
            "blender.execute_python",
            {
                "code": (
                    "import bpy\n"
                    "bpy.context.scene.render.engine = 'CYCLES'\n"
                    "bpy.context.scene.cycles.device = 'CPU'\n"
                    "data = bpy.data\n"
                    "camera = data.cameras.new('PreviewCamera')\n"
                    "obj = data.objects.new('PreviewCamera', camera)\n"
                    "bpy.context.scene.collection.objects.link(obj)\n"
                    "obj.location = (0, -6, 2)\n"
                    "obj.rotation_euler = (1.2, 0, 0)\n"
                    "bpy.context.scene.camera = obj\n"
                    "light = data.lights.new('PreviewSun', 'SUN')\n"
                    "sun = data.objects.new('PreviewSun', light)\n"
                    "bpy.context.scene.collection.objects.link(sun)\n"
                    "sun.rotation_euler = (0.9, 0.2, 0.4)\n"
                    "result = 'ready'"
                )
            },
        )
    )


async def test_render_preview_comes_back_as_an_image() -> None:
    """The point of the preview: a picture, not a path the model cannot open."""
    async with stack() as (session, _):
        await add_camera(session)
        await session.call_tool("blender.create_object", {"type": "cube", "name": "Subject"})
        blocks = await session.call_tool(
            "blender.render_preview", {"max_edge": 64, "engine": "CYCLES", "samples": 1}
        )

    summary = json.loads(blocks.content[0].text)
    assert summary["success"] is True
    assert [block.type for block in blocks.content] == ["text", "image"]
    image = blocks.content[1]
    assert image.mime_type == "image/png"
    assert base64.b64decode(image.data).startswith(b"\x89PNG")
    assert os.path.isfile(summary["output_path"])


async def test_the_last_render_resource_serves_that_image() -> None:
    async with stack() as (session, _):
        await add_camera(session)
        rendered = payload_of(
            await session.call_tool(
                "blender.render",
                {
                    "resolution_x": 64,
                    "resolution_y": 64,
                    "engine": "CYCLES",
                    "samples": 1,
                },
            )
        )
        latest = await session.read_resource("blender://render/latest")
    assert latest.contents[0].mime_type == "image/png"
    assert base64.b64decode(latest.contents[0].blob) == Path(rendered["output_path"]).read_bytes()


async def test_opening_another_file_drops_the_remembered_render(tmp_path) -> None:
    """A render belongs to the scene it was made in.

    Keeping it would mean serving one file's picture as the "latest render" of
    another, which is worse than saying there is none.
    """
    async with stack() as (session, _):
        await add_camera(session)
        first_file = str(tmp_path / "first.blend")
        bpy.ops.wm.save_as_mainfile(filepath=first_file)
        await session.call_tool("blender.render", {"engine": "CYCLES", "samples": 1})
        assert (await session.read_resource("blender://render/latest")).contents[0].blob

        # ``save_as_mainfile(copy=True)`` writes the copy but keeps the current
        # file, so open it to actually be in a different one.
        second_file = str(tmp_path / "second.blend")
        bpy.ops.wm.save_as_mainfile(filepath=second_file, copy=True)
        bpy.ops.wm.open_mainfile(filepath=second_file)
        assert bpy.data.filepath == second_file
        with pytest.raises(Exception) as excinfo:  # noqa: PT011 - SDK error type varies
            await session.read_resource("blender://render/latest")
    assert "no longer open" in str(excinfo.value)


async def test_a_build_a_table_chain_from_the_readme() -> None:
    """Example 4 of the README, run for real."""
    legs = {"LegFL": (-0.9, -0.4), "LegFR": (0.9, -0.4), "LegBL": (-0.9, 0.4), "LegBR": (0.9, 0.4)}
    async with stack() as (session, _):
        await session.call_tool("blender.begin_transaction", {})
        await session.call_tool(
            "blender.create_object",
            {"type": "cube", "name": "TableTop", "location": [0, 0, 0.75], "scale": [2, 1, 0.05]},
        )
        for name, (x, y) in legs.items():
            await session.call_tool(
                "blender.create_object",
                {"type": "cube", "name": name, "location": [x, y, 0.35], "scale": [0.1, 0.1, 0.7]},
            )
        await session.call_tool("blender.commit_transaction", {})

        outcome = payload_of(
            await session.call_tool(
                "blender.execute_python",
                {
                    "code": (
                        "import bpy\n"
                        "wood = bpy.data.materials.new('Wood')\n"
                        "wood.diffuse_color = (0.32, 0.19, 0.07, 1.0)\n"
                        "done = []\n"
                        "for name in ('TableTop', 'LegFL', 'LegFR', 'LegBL', 'LegBR'):\n"
                        "    bpy.data.objects[name].data.materials.append(wood)\n"
                        "    done.append(name)\n"
                        "result = {'materialised': len(done)}\n"
                    )
                },
            )
        )
        scene = payload_of(await session.call_tool("blender.get_scene", {}))

    assert outcome["result"]["result"] == {"materialised": 5}
    assert scene["objects_count"] == 5
    assert bpy.data.objects["TableTop"].data.materials[0].name == "Wood"
    assert scene["objects"][0]["name"] in {"TableTop", "LegFL", "LegFR", "LegBL", "LegBR"}


async def test_the_bridge_reports_not_connected_without_the_addon() -> None:
    """The server half on its own: no add-on, no hang, a clear error."""
    port = free_port()
    server = create_server(Settings(blender_host="127.0.0.1", blender_port=port, blender_request_timeout=2.0))
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(
                server._lowlevel_server.run,  # noqa: SLF001
                server_streams[0],
                server_streams[1],
                server._lowlevel_server.create_initialization_options(),  # noqa: SLF001
            )
            async with ClientSession(client_streams[0], client_streams[1]) as session:
                await session.initialize()
                result = await session.call_tool("blender.get_scene", {})
                assert error_of(result)["code"] == "BLENDER_NOT_CONNECTED"
            task_group.cancel_scope.cancel()


async def test_many_requests_stay_correlated() -> None:
    async with stack() as (session, _):
        results: list[tuple[int, str]] = []

        async def create(index: int) -> None:
            outcome = await session.call_tool("blender.create_object", {"type": "cube", "name": f"C{index}"})
            results.append((index, payload_of(outcome)["object"]["name"]))

        async with anyio.create_task_group() as group:
            for index in range(10):
                group.start_soon(create, index)

    assert sorted(results) == [(index, f"C{index}") for index in range(10)]
    assert len(bpy.data.objects) == 10
