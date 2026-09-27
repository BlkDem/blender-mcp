"""End-to-end test: an MCP client talking to a Blender double over a real socket.

This is the acceptance path from the project brief, minus the ``bpy``: an MCP
session lists the tools, calls them, and reads the resources, while
:class:`~tests.support.fake_blender.FakeBlender` answers over a real WebSocket
using the add-on's own client.

What this proves: tool registration and schemas, request/response framing, error
propagation with intact codes, resource reads, transaction bookkeeping, and that
a disconnected Blender surfaces as ``BLENDER_NOT_CONNECTED`` rather than a hang. What it
does not prove: anything about ``bpy`` itself.
"""

from __future__ import annotations

import json
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import anyio
import pytest
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams
from mcp_types import CallToolResult

from server.config import Settings
from server.mcp.server import create_server
from tests.support.fake_blender import FakeBlender

pytestmark = pytest.mark.anyio


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def build_settings(port: int, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "blender_host": "127.0.0.1",
        "blender_port": port,
        "blender_request_timeout": 5.0,
        "blender_render_timeout": 30.0,
        "allow_python_execution": True,
    }
    values.update(overrides)
    return Settings(**values)


@dataclass
class Env:
    """A running MCP server and, once attached, the Blender double."""

    port: int
    session: ClientSession
    blender: FakeBlender | None = None


@asynccontextmanager
async def mcp_server(**overrides: Any) -> AsyncIterator[Env]:
    """Run the real MCP server in-process, wired to a real WebSocket bridge."""
    port = free_port()
    server = create_server(build_settings(port, **overrides))
    # The SDK has no public in-process runner, so the low-level server is
    # started on the same streams stdio would use.
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
                yield Env(port=port, session=session)
            task_group.cancel_scope.cancel()


async def attach(env: Env, timeout: float = 5.0) -> FakeBlender:
    """Connect the Blender double and wait until the bridge has registered it."""
    blender = FakeBlender()
    blender.connect("127.0.0.1", env.port)
    env.blender = blender
    with anyio.fail_after(timeout):
        while True:
            result = await env.session.call_tool("blender.get_scene", {})
            if not result.is_error:
                return blender
            await anyio.sleep(0.02)
    raise AssertionError("unreachable")  # pragma: no cover


@asynccontextmanager
async def connected(**overrides: Any) -> AsyncIterator[Env]:
    async with mcp_server(**overrides) as env:
        await attach(env)
        yield env
        env.blender.close()  # type: ignore[union-attr]


def payload_of(result: CallToolResult) -> Any:
    assert result.structured_content is not None, result.content
    return result.structured_content


def error_of(result: CallToolResult) -> dict[str, Any]:
    assert result.is_error, result.content
    text = result.content[0].text
    _, _, tail = text.partition(": ")
    return json.loads(tail)["error"]


# --- tool surface ------------------------------------------------------------


async def test_the_tool_list_is_what_the_brief_asks_for() -> None:
    async with mcp_server() as env:
        names = {tool.name for tool in (await env.session.list_tools()).tools}
    assert names == {
        "blender.get_scene",
        "blender.get_objects",
        "blender.get_object",
        "blender.create_object",
        "blender.update_object",
        "blender.delete_object",
        "blender.render",
        "blender.execute_python",
        "blender.begin_transaction",
        "blender.commit_transaction",
        "blender.rollback_transaction",
    }


async def test_every_tool_has_a_description_the_model_can_act_on() -> None:
    async with mcp_server() as env:
        tools = (await env.session.list_tools()).tools
    for tool in tools:
        assert tool.description, tool.name
        assert len(tool.description) > 40, tool.name


async def test_tools_advertise_structured_output() -> None:
    async with mcp_server() as env:
        tools = (await env.session.list_tools()).tools
    for tool in tools:
        assert tool.output_schema is not None, tool.name


async def test_create_object_parameters_match_the_brief() -> None:
    async with mcp_server() as env:
        tools = {tool.name: tool for tool in (await env.session.list_tools()).tools}
    properties = tools["blender.create_object"].input_schema["properties"]
    assert set(properties) == {"type", "name", "location", "rotation", "scale", "collection"}
    assert properties["type"]["enum"] == ["cube", "sphere", "cylinder", "cone", "plane", "torus"]
    assert tools["blender.create_object"].input_schema["required"] == ["type", "name"]


async def test_update_object_parameters_are_all_optional() -> None:
    async with mcp_server() as env:
        tools = {tool.name: tool for tool in (await env.session.list_tools()).tools}
    schema = tools["blender.update_object"].input_schema
    assert schema["required"] == ["name"]
    assert set(schema["properties"]) == {
        "name",
        "location",
        "rotation",
        "scale",
        "dimensions",
        "visibility",
        "new_name",
        "material",
        "material_color",
    }


async def test_render_parameters_match_the_brief() -> None:
    async with mcp_server() as env:
        tools = {tool.name: tool for tool in (await env.session.list_tools()).tools}
    properties = tools["blender.render"].input_schema["properties"]
    assert set(properties) == {"engine", "resolution_x", "resolution_y", "samples", "output_path"}
    assert tools["blender.render"].input_schema.get("required", []) == []


async def test_resource_list_matches_the_brief() -> None:
    async with mcp_server() as env:
        uris = {str(r.uri) for r in (await env.session.list_resources()).resources}
    assert uris == {"blender://scene", "blender://objects"}


async def test_server_advertises_instructions() -> None:
    async with mcp_server() as env:
        result = await env.session.initialize()
    assert result.instructions
    assert "blender.get_scene" in result.instructions


# --- without Blender ---------------------------------------------------------


async def test_calls_fail_cleanly_when_blender_is_not_connected() -> None:
    async with mcp_server() as env:
        result = await env.session.call_tool("blender.get_scene", {})
        assert result.is_error
        assert error_of(result)["code"] == "BLENDER_NOT_CONNECTED"


async def test_resources_report_not_connected() -> None:
    async with mcp_server() as env:
        with pytest.raises(BaseException, match="BLENDER_NOT_CONNECTED"):
            await env.session.read_resource("blender://scene")


# --- the full flow -----------------------------------------------------------


async def test_the_acceptance_walkthrough() -> None:
    """Create, read, update, delete — the sequence from the project brief."""
    async with connected() as env:
        session = env.session

        scene = payload_of(await session.call_tool("blender.get_scene", {}))
        assert scene["scene"] == "Scene"
        assert scene["objects_count"] == 0
        assert scene["collections"] == ["Collection"]
        assert scene["cameras"] == ["Camera"]

        created = payload_of(
            await session.call_tool(
                "blender.create_object",
                {"type": "cube", "name": "Box", "location": [0, 0, 1], "scale": [2, 1, 0.1]},
            )
        )
        assert created["success"] is True
        assert created["object"]["name"] == "Box"
        assert created["object"]["location"] == [0.0, 0.0, 1.0]
        assert created["object"]["dimensions"] == [4.0, 2.0, 0.2]

        fetched = payload_of(await session.call_tool("blender.get_object", {"name": "Box"}))
        assert fetched["name"] == "Box"
        assert fetched["collection"] == ["Collection"]
        assert fetched["modifiers"] == []
        assert fetched["visibility"] == {"hide_viewport": False, "hide_render": False, "visible": True}

        updated = payload_of(
            await session.call_tool("blender.update_object", {"name": "Box", "rotation": [0, 0, 90]})
        )
        assert updated["object"]["rotation"] == [0.0, 0.0, 90.0]
        assert updated["object"]["location"] == [0.0, 0.0, 1.0]  # untouched

        assert error_of(await session.call_tool("blender.get_object", {"name": "Ghost"})) == {
            "code": "OBJECT_NOT_FOUND",
            "message": "Object 'Ghost' does not exist",
        }

        duplicate = await session.call_tool("blender.create_object", {"type": "cube", "name": "Box"})
        assert error_of(duplicate) == {
            "code": "OBJECT_ALREADY_EXISTS",
            "message": "Object 'Box' already exists",
        }
        # No silent "Box.001" was created.
        assert payload_of(await session.call_tool("blender.get_scene", {}))["objects_count"] == 1

        assert payload_of(await session.call_tool("blender.delete_object", {"name": "Box"})) == {
            "success": True,
            "deleted": "Box",
            "type": "MESH",
        }
        assert error_of(await session.call_tool("blender.delete_object", {"name": "Box"}))["code"] == (
            "OBJECT_NOT_FOUND"
        )


async def test_execute_python_runs_and_reports_its_result() -> None:
    code = "import bpy\nresult = {'name': bpy.context.scene.name}"
    async with connected() as env:
        result = payload_of(await env.session.call_tool("blender.execute_python", {"code": code}))
    assert result["success"] is True
    assert result["result"]["result"] == {"fake": True}
    assert result["validation"]["screened"] is True
    assert result["validation"]["summary"].startswith("2 line(s)")


async def test_execute_python_is_refused_when_disabled() -> None:
    async with mcp_server(allow_python_execution=False) as env:
        await attach(env)
        result = await env.session.call_tool("blender.execute_python", {"code": "import bpy"})
        assert error_of(result)["code"] == "PYTHON_EXECUTION_DISABLED"
        env.blender.close()  # type: ignore[union-attr]


async def test_execute_python_never_reaches_blender_when_blocked() -> None:
    async with connected() as env:
        result = await env.session.call_tool("blender.execute_python", {"code": "import os"})
        assert error_of(result)["code"] == "VALIDATION_ERROR"
    assert "execute_python" not in [action for action, _ in env.blender.calls]  # type: ignore[union-attr]


async def test_render_returns_a_path_and_a_duration() -> None:
    async with connected() as env:
        result = payload_of(
            await env.session.call_tool("blender.render", {"resolution_x": 1024, "resolution_y": 1024})
        )
    assert result["success"] is True
    assert result["output_path"].endswith(".png")
    assert isinstance(result["render_time"], float)
    assert result["resolution"] == [1024, 1024]


# --- transactions ------------------------------------------------------------


async def test_a_transaction_groups_several_operations() -> None:
    async with connected() as env:
        session = env.session
        await session.call_tool("blender.begin_transaction", {})
        for index in range(3):
            await session.call_tool("blender.create_object", {"type": "cylinder", "name": f"Leg{index}"})
        committed = payload_of(await session.call_tool("blender.commit_transaction", {}))
    assert committed["transaction"] == "committed"
    assert committed["undo_steps"] == 3


async def test_rollback_reports_the_step_count() -> None:
    async with connected() as env:
        await env.session.call_tool("blender.begin_transaction", {})
        await env.session.call_tool("blender.create_object", {"type": "plane", "name": "Floor"})
        rolled = payload_of(await env.session.call_tool("blender.rollback_transaction", {}))
    assert rolled["transaction"] == "rolled_back"
    assert rolled["undo_steps"] == 1


async def test_committing_without_a_transaction_is_an_error() -> None:
    async with connected() as env:
        result = await env.session.call_tool("blender.commit_transaction", {})
        assert error_of(result)["code"] == "TRANSACTION_NOT_ACTIVE"


# --- resources ---------------------------------------------------------------


async def test_scene_resource_returns_compact_json() -> None:
    async with connected() as env:
        await env.session.call_tool("blender.create_object", {"type": "cube", "name": "Box"})
        read = await env.session.read_resource("blender://scene")
    payload = json.loads(read.contents[0].text)
    assert payload["scene"] == "Scene"
    assert payload["objects_count"] == 1
    assert payload["objects"][0]["name"] == "Box"
    assert "materials" not in payload["objects"][0]


async def test_objects_resource_lists_every_object() -> None:
    async with connected() as env:
        for name in ("A", "B", "C"):
            await env.session.call_tool("blender.create_object", {"type": "sphere", "name": name})
        read = await env.session.read_resource("blender://objects")
    payload = json.loads(read.contents[0].text)
    assert [obj["name"] for obj in payload["objects"]] == ["A", "B", "C"]


# --- transport under load ----------------------------------------------------


async def test_many_requests_in_a_row_stay_correlated() -> None:
    async with connected() as env:
        results: list[tuple[int, str]] = []

        async def create(index: int) -> None:
            result = await env.session.call_tool(
                "blender.create_object", {"type": "cube", "name": f"C{index}"}
            )
            payload = payload_of(result)
            results.append((index, payload["object"]["name"]))

        async with anyio.create_task_group() as task_group:
            for index in range(20):
                task_group.start_soon(create, index)

    assert len(results) == 20
    assert sorted(results) == [(index, f"C{index}") for index in range(20)]
    assert len(env.blender.objects) == 20  # type: ignore[union-attr]


async def test_a_registered_call_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    """The log is wired in by register(), not by each tool: prove it end to end."""
    import logging
    import re

    caplog.set_level(logging.INFO, logger="server.mcp.support")
    async with connected() as env:
        await env.session.call_tool("blender.get_scene", {})
        await env.session.call_tool("blender.get_object", {"name": "Ghost"})

    lines = [record.getMessage() for record in caplog.records if record.getMessage().startswith("mcp ")]
    pattern = re.compile(
        r"tool=(?P<tool>\S+) request=(?P<request>\S+) duration_ms=[\d.]+ "
        r"success=(?P<success>true|false)(?: error=(?P<error>\S+))?"
    )
    parsed = [pattern.search(line) for line in lines]
    # attach() probes with get_scene until the double is up, so only the last
    # get_scene and the get_object are ours.
    mine = [match for match in parsed if match and match["tool"] != "blender.get_scene"] + [
        match for match in parsed if match and match["tool"] == "blender.get_scene"
    ][-1:]
    by_tool = {match["tool"]: match for match in mine if match}
    assert set(by_tool) == {"blender.get_scene", "blender.get_object"}, lines
    assert by_tool["blender.get_scene"]["success"] == "true"
    assert by_tool["blender.get_object"]["success"] == "false"
    assert by_tool["blender.get_object"]["error"] == "OBJECT_NOT_FOUND"
