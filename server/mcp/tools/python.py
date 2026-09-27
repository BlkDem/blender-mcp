"""``blender.execute_python``: run model-written Python inside Blender.

Two gates, in this order, both in :mod:`server.validation` and
:mod:`server.config` rather than here:

1. ``ALLOW_PYTHON_EXECUTION`` must be true, otherwise the call is refused with
   PERMISSION_DENIED before the socket is touched.
2. the code must pass the AST screen in :func:`server.validation.python`.

The code itself is never executed here. It is validated, forwarded, and run by
the add-on's executor on Blender's main thread, because ``bpy`` only exists
there.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context

from server.blender.protocol import Action
from server.config import Settings
from server.errors import BlenderMCPError, ErrorCode
from server.mcp.support import bridge_of, register, settings_of, tool_error
from server.validation.python import validate_python

logger = logging.getLogger(__name__)

EXECUTE_PYTHON_DESCRIPTION = """\
Execute Python inside the running Blender and return whatever the snippet left
in a variable named `result`.

code: statements to run. `import bpy`, `import mathutils` and the standard
      library are available. Anything that touches the network, spawns
      processes, reads or writes files, or reaches for eval/exec/__import__ is
      rejected with VALIDATION_ERROR before Blender is contacted.
      Assign a JSON-serialisable value to `result` to get structured data back;
      otherwise only a summary is returned.

      Otherwise, if the server was started with ALLOW_PYTHON_EXECUTION=false,
      the call is refused with PERMISSION_DENIED without contacting Blender.

Example:
    import bpy
    bpy.ops.mesh.primitive_uv_sphere_add(location=(0, 0, 1))
    obj = bpy.context.object
    obj.name = "Ball"
    result = {"created": obj.name, "verts": len(obj.data.vertices)}

This tool is meant for a trusted, local AI client. It is a policy screen, not a
sandbox: Blender's own API is powerful enough to do damage, and enabling this
tool should be a deliberate choice.\
"""


def _require_permission(settings: Settings) -> None:
    if not settings.allow_python_execution:
        raise tool_error(
            BlenderMCPError(
                "Python execution is disabled. Set ALLOW_PYTHON_EXECUTION=true in your "
                "environment (or .env) and restart the MCP server to enable "
                "blender.execute_python.",
                code=ErrorCode.PERMISSION_DENIED,
            )
        )


def _screen(code: str) -> dict[str, Any]:
    report = validate_python(code)
    if not report.allowed:
        logger.info("blender.execute_python rejected by policy: %s", report.violations)
        raise tool_error(
            BlenderMCPError(
                "Code rejected by the Python execution policy",
                code=ErrorCode.VALIDATION_ERROR,
                details={"violations": report.violations},
            )
        )
    return {"screened": True, "summary": report.summary()}


async def execute_python(ctx: Context, code: str) -> dict[str, Any]:
    """Run Python code inside Blender."""
    _require_permission(settings_of(ctx))
    screening = _screen(code)
    try:
        result = await bridge_of(ctx).request(Action.EXECUTE_PYTHON, {"code": code})
    except BlenderMCPError as exc:
        logger.info("blender.execute_python failed: %s", exc)
        raise tool_error(exc) from exc
    return {"success": True, "validation": screening, "result": result}


def register_tools(server: MCPServer) -> None:
    register(server, execute_python, "blender.execute_python", EXECUTE_PYTHON_DESCRIPTION)
