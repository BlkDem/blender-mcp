"""Tests for the MCP tools.

These exercise the server half of the contract: which action is sent, with
which params, and how an error coming back is turned into something the model
can act on. Blender's own behaviour is covered by the add-on, not here.
"""

from __future__ import annotations

import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from server.blender.protocol import Action
from server.config import Settings
from server.errors import BlenderMCPError, ErrorCode
from server.mcp.tools import objects, python, render, scene, transactions
from tests.support.stubs import FakeBridge, FakeContext

pytestmark = pytest.mark.anyio


def error_payload(message: str) -> str:
    return json.loads(ToolError(message).args[0])


# --- get_scene / get_object -------------------------------------------------


async def test_get_scene(ctx: FakeContext, bridge: FakeBridge) -> None:
    bridge.results[Action.GET_SCENE] = {"scene": "Scene", "objects_total": 2}
    result = await scene.get_scene(ctx)

    assert bridge.calls == [(Action.GET_SCENE, {})]
    assert result == {"scene": "Scene", "objects_total": 2}


async def test_get_scene_reports_not_connected(ctx: FakeContext, bridge: FakeBridge) -> None:
    bridge.raise_for[Action.GET_SCENE] = BlenderMCPError(
        "No Blender instance is connected", code=ErrorCode.NOT_CONNECTED
    )
    with pytest.raises(ToolError) as excinfo:
        await scene.get_scene(ctx)

    payload = error_payload(excinfo.value.args[0])
    assert payload["success"] is False
    assert payload["error"]["code"] == "NOT_CONNECTED"


async def test_get_object(ctx: FakeContext, bridge: FakeBridge) -> None:
    bridge.results[Action.GET_OBJECT] = {"name": "Cube", "type": "MESH"}
    result = await scene.get_object(ctx, name="Cube")

    assert bridge.calls == [(Action.GET_OBJECT, {"name": "Cube"})]
    assert result["name"] == "Cube"


async def test_get_object_not_found_keeps_the_code(ctx: FakeContext, bridge: FakeBridge) -> None:
    bridge.raise_for[Action.GET_OBJECT] = BlenderMCPError(
        "Object 'Chair' does not exist", code=ErrorCode.OBJECT_NOT_FOUND
    )
    with pytest.raises(ToolError) as excinfo:
        await scene.get_object(ctx, name="Chair")

    assert error_payload(excinfo.value.args[0])["error"]["code"] == "OBJECT_NOT_FOUND"


async def test_get_object_rejects_a_blank_name(ctx: FakeContext, bridge: FakeBridge) -> None:
    with pytest.raises(ToolError):
        await scene.get_object(ctx, name="   ")
    assert bridge.calls == []


# --- create_object ----------------------------------------------------------


async def test_create_object(ctx: FakeContext, bridge: FakeBridge) -> None:
    bridge.results[Action.CREATE_OBJECT] = {
        "success": True,
        "object": {"name": "TableTop", "type": "MESH", "location": [0, 0, 1]},
    }
    result = await objects.create_object(
        ctx, type="cube", name="TableTop", location=[0, 0, 1], scale=[2, 1, 0.1]
    )

    assert bridge.calls == [
        (
            Action.CREATE_OBJECT,
            {"type": "cube", "name": "TableTop", "location": [0.0, 0.0, 1.0], "scale": [2.0, 1.0, 0.1]},
        )
    ]
    assert result["object"]["name"] == "TableTop"


async def test_create_object_omits_unset_fields(ctx: FakeContext, bridge: FakeBridge) -> None:
    await objects.create_object(ctx, type="sphere", name="Ball")
    assert bridge.calls == [(Action.CREATE_OBJECT, {"type": "sphere", "name": "Ball"})]


async def test_create_object_passes_a_collection(ctx: FakeContext, bridge: FakeBridge) -> None:
    await objects.create_object(ctx, type="torus", name="Ring", collection="Props")
    assert bridge.calls[0][1]["collection"] == "Props"


async def test_create_object_rejects_an_unsupported_type(ctx: FakeContext, bridge: FakeBridge) -> None:
    with pytest.raises(ToolError) as excinfo:
        await objects.create_object(ctx, type="dodecahedron", name="D12")  # type: ignore[arg-type]
    payload = error_payload(excinfo.value.args[0])
    assert payload["error"]["code"] == "INVALID_OBJECT_TYPE"
    assert "cube" in payload["error"]["details"]["supported"]
    assert bridge.calls == []


@pytest.mark.parametrize(
    "field",
    ["location", "rotation", "scale"],
)
async def test_create_object_validates_vector_length(ctx: FakeContext, field: str) -> None:
    with pytest.raises(ToolError) as excinfo:
        await objects.create_object(ctx, type="cube", name="X", **{field: [1, 2]})  # type: ignore[arg-type]
    assert error_payload(excinfo.value.args[0])["error"]["code"] == "INVALID_PARAMETER"


async def test_create_object_validates_vector_contents(ctx: FakeContext) -> None:
    with pytest.raises(ToolError):
        await objects.create_object(ctx, type="cube", name="X", location=["a", "b", "c"])  # type: ignore[arg-type]


async def test_create_object_already_exists_is_surfaced(ctx: FakeContext, bridge: FakeBridge) -> None:
    bridge.raise_for[Action.CREATE_OBJECT] = BlenderMCPError(
        "Object 'Table' already exists", code=ErrorCode.OBJECT_ALREADY_EXISTS
    )
    with pytest.raises(ToolError) as excinfo:
        await objects.create_object(ctx, type="cube", name="Table")
    assert error_payload(excinfo.value.args[0])["error"]["code"] == "OBJECT_ALREADY_EXISTS"


# --- update_object ----------------------------------------------------------


async def test_update_object_sends_only_the_given_fields(ctx: FakeContext, bridge: FakeBridge) -> None:
    await objects.update_object(ctx, name="Cube", location=[1, 2, 3])
    assert bridge.calls == [(Action.UPDATE_OBJECT, {"name": "Cube", "location": [1.0, 2.0, 3.0]})]


async def test_update_object_accepts_every_field(ctx: FakeContext, bridge: FakeBridge) -> None:
    await objects.update_object(
        ctx,
        name="Cube",
        location=[1, 0, 0],
        rotation=[0, 0, 90],
        scale=[2, 2, 2],
        dimensions=[4, 4, 4],
        visibility=False,
    )
    params = bridge.calls[0][1]
    assert params == {
        "name": "Cube",
        "location": [1.0, 0.0, 0.0],
        "rotation": [0.0, 0.0, 90.0],
        "scale": [2.0, 2.0, 2.0],
        "dimensions": [4.0, 4.0, 4.0],
        "visibility": False,
    }


async def test_update_object_requires_at_least_one_field(ctx: FakeContext, bridge: FakeBridge) -> None:
    with pytest.raises(ToolError) as excinfo:
        await objects.update_object(ctx, name="Cube")
    assert error_payload(excinfo.value.args[0])["error"]["code"] == "INVALID_PARAMETER"
    assert bridge.calls == []


async def test_update_object_rejects_negative_dimensions(ctx: FakeContext) -> None:
    with pytest.raises(ToolError) as excinfo:
        await objects.update_object(ctx, name="Cube", dimensions=[-1, 1, 1])
    assert error_payload(excinfo.value.args[0])["error"]["code"] == "INVALID_PARAMETER"


# --- delete_object ----------------------------------------------------------


async def test_delete_object(ctx: FakeContext, bridge: FakeBridge) -> None:
    bridge.results[Action.DELETE_OBJECT] = {"success": True, "deleted": "Cube"}
    result = await objects.delete_object(ctx, name="Cube")

    assert bridge.calls == [(Action.DELETE_OBJECT, {"name": "Cube"})]
    assert result["deleted"] == "Cube"


async def test_delete_object_missing(ctx: FakeContext, bridge: FakeBridge) -> None:
    bridge.raise_for[Action.DELETE_OBJECT] = BlenderMCPError(
        "Object 'Chair' does not exist", code=ErrorCode.OBJECT_NOT_FOUND
    )
    with pytest.raises(ToolError) as excinfo:
        await objects.delete_object(ctx, name="Chair")
    assert error_payload(excinfo.value.args[0])["error"]["code"] == "OBJECT_NOT_FOUND"


# --- render -----------------------------------------------------------------


async def test_render_sends_only_overrides(ctx: FakeContext, bridge: FakeBridge) -> None:
    await render.render(ctx, resolution_x=512, resolution_y=512)
    assert bridge.calls == [(Action.RENDER, {"resolution_x": 512, "resolution_y": 512})]


async def test_render_uses_the_render_timeout(
    ctx: FakeContext, bridge: FakeBridge, settings: Settings
) -> None:
    await render.render(ctx, engine="CYCLES", samples=64, output_path="/tmp/out.png")
    params = bridge.calls[0][1]
    assert params == {"engine": "CYCLES", "samples": 64, "output_path": "/tmp/out.png"}
    assert bridge.timeouts == [settings.blender_render_timeout]


async def test_render_returns_path_and_time(ctx: FakeContext, bridge: FakeBridge) -> None:
    bridge.results[Action.RENDER] = {
        "success": True,
        "output_path": "/tmp/out.png",
        "render_time": 3.42,
    }
    result = await render.render(ctx)
    assert result == {"success": True, "output_path": "/tmp/out.png", "render_time": 3.42}


# --- execute_python ---------------------------------------------------------


async def test_execute_python_forwards_valid_code(python_ctx: FakeContext, bridge: FakeBridge) -> None:
    bridge.results[Action.EXECUTE_PYTHON] = {"success": True, "result": {"created": "Ball"}}
    code = "import bpy\nbpy.ops.mesh.primitive_uv_sphere_add()\nresult = {'created': 'Ball'}"

    result = await python.execute_python(python_ctx, code=code)

    assert bridge.calls == [(Action.EXECUTE_PYTHON, {"code": code})]
    assert result["result"] == {"success": True, "result": {"created": "Ball"}}
    assert result["validation"]["screened"] is True
    assert "bpy" in result["validation"]["summary"]


async def test_execute_python_is_refused_when_disabled(ctx: FakeContext, bridge: FakeBridge) -> None:
    with pytest.raises(ToolError) as excinfo:
        await python.execute_python(ctx, code="import bpy")
    payload = error_payload(excinfo.value.args[0])
    assert payload["error"]["code"] == "PERMISSION_DENIED"
    assert "ALLOW_PYTHON_EXECUTION" in payload["error"]["message"]
    assert bridge.calls == []


async def test_execute_python_rejects_dangerous_code(python_ctx: FakeContext, bridge: FakeBridge) -> None:
    with pytest.raises(ToolError) as excinfo:
        await python.execute_python(python_ctx, code="import os\nos.system('ls')")
    payload = error_payload(excinfo.value.args[0])
    assert payload["error"]["code"] == "VALIDATION_ERROR"
    assert payload["error"]["details"]["violations"]
    assert bridge.calls == []


async def test_the_permission_gate_runs_before_the_screener(ctx: FakeContext, bridge: FakeBridge) -> None:
    """Disabled means disabled: dangerous code reports PERMISSION_DENIED, not VALIDATION_ERROR."""
    with pytest.raises(ToolError) as excinfo:
        await python.execute_python(ctx, code="import os")
    assert error_payload(excinfo.value.args[0])["error"]["code"] == "PERMISSION_DENIED"
    assert bridge.calls == []


async def test_execute_python_surfaces_blender_failures(python_ctx: FakeContext, bridge: FakeBridge) -> None:
    bridge.raise_for[Action.EXECUTE_PYTHON] = BlenderMCPError(
        "NameError: name 'foo' is not defined", code=ErrorCode.EXECUTION_ERROR
    )
    with pytest.raises(ToolError) as excinfo:
        await python.execute_python(python_ctx, code="result = foo")
    assert error_payload(excinfo.value.args[0])["error"]["code"] == "EXECUTION_ERROR"


# --- transactions -----------------------------------------------------------


async def test_begin_commit_rollback_use_the_bridge(ctx: FakeContext, bridge: FakeBridge) -> None:
    await transactions.begin_transaction(ctx)
    await transactions.commit_transaction(ctx)
    await transactions.rollback_transaction(ctx)

    assert [call[0] for call in bridge.calls] == [
        Action.BEGIN_TRANSACTION,
        Action.COMMIT_TRANSACTION,
        Action.ROLLBACK_TRANSACTION,
    ]


async def test_rollback_without_a_transaction_is_an_error(ctx: FakeContext, bridge: FakeBridge) -> None:
    bridge.raise_for[Action.ROLLBACK_TRANSACTION] = BlenderMCPError(
        "No transaction is open", code=ErrorCode.TRANSACTION_NOT_ACTIVE
    )
    with pytest.raises(ToolError) as excinfo:
        await transactions.rollback_transaction(ctx)
    assert error_payload(excinfo.value.args[0])["error"]["code"] == "TRANSACTION_NOT_ACTIVE"


async def test_transaction_context_manager_commits(ctx: FakeContext, bridge: FakeBridge) -> None:
    from server.transactions import transaction

    async with transaction(bridge):
        await scene.get_scene(ctx)

    assert [call[0] for call in bridge.calls] == [
        Action.BEGIN_TRANSACTION,
        Action.GET_SCENE,
        Action.COMMIT_TRANSACTION,
    ]


async def test_transaction_context_manager_rolls_back_on_error(bridge: FakeBridge) -> None:
    from server.transactions import transaction

    with pytest.raises(RuntimeError):
        async with transaction(bridge):
            raise RuntimeError("plan failed halfway")

    assert [call[0] for call in bridge.calls] == [Action.BEGIN_TRANSACTION, Action.ROLLBACK_TRANSACTION]


# --- documentation the model relies on --------------------------------------


def test_every_tool_function_has_a_docstring() -> None:
    for module in (scene, objects, render, python, transactions):
        exported = [
            value
            for name, value in vars(module).items()
            if callable(value)
            and getattr(value, "__module__", "") == module.__name__
            and not name.startswith("_")
            and name not in {"register_tools"}
        ]
        assert exported, f"no tools found in {module.__name__}"
        for tool in exported:
            assert tool.__doc__, f"{tool.__qualname__} is missing a docstring"


def test_descriptions_state_the_units_the_model_must_use() -> None:
    assert "DEGREES" in objects.CREATE_OBJECT_DESCRIPTION
    assert "DEGREES" in objects.UPDATE_OBJECT_DESCRIPTION
    assert "OBJECT_ALREADY_EXISTS" in objects.CREATE_OBJECT_DESCRIPTION
    assert "OBJECT_NOT_FOUND" in objects.DELETE_OBJECT_DESCRIPTION
    assert "ALLOW_PYTHON_EXECUTION" in python.EXECUTE_PYTHON_DESCRIPTION
