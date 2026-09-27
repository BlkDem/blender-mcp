"""Read-only scene inspection tools.

Both tools are thin: the payload is built inside the add-on, where ``bpy`` lives,
so there is nothing to duplicate here. What these functions add is error
translation — a :class:`BlenderMCPError` from the bridge becomes an MCP tool
error that still carries ``error.code``.

Angles are reported in **degrees**. Blender stores radians, which models get
wrong often enough to be worth converting at the boundary.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context

from server.blender.protocol import Action
from server.errors import BlenderMCPError
from server.mcp.support import bridge_of, register, tool_error

logger = logging.getLogger(__name__)

GET_SCENE_DESCRIPTION = """\
Return a compact, LLM-sized summary of the active Blender scene: object names,
types, transforms, dimensions, plus collection / camera / light names.

No mesh data is included. Call blender.get_object for one object in detail.\
"""

GET_OBJECT_DESCRIPTION = """\
Return the full detail of a single object: transform, dimensions, collection,
material names, modifiers, visibility and parent.

Fails with OBJECT_NOT_FOUND when the name does not exist.\
"""


async def get_scene(ctx: Context) -> dict[str, Any]:
    """Summarise the active scene."""
    try:
        return await bridge_of(ctx).request(Action.GET_SCENE)
    except BlenderMCPError as exc:
        logger.info("blender.get_scene failed: %s", exc)
        raise tool_error(exc) from exc


async def get_object(ctx: Context, name: str) -> dict[str, Any]:
    """Describe one object."""
    if not name or not name.strip():
        raise tool_error(BlenderMCPError("Object name must not be empty"))
    try:
        return await bridge_of(ctx).request(Action.GET_OBJECT, {"name": name})
    except BlenderMCPError as exc:
        logger.info("blender.get_object(%s) failed: %s", name, exc)
        raise tool_error(exc) from exc


def register_tools(server: MCPServer) -> None:
    register(server, get_scene, "blender.get_scene", GET_SCENE_DESCRIPTION)
    register(server, get_object, "blender.get_object", GET_OBJECT_DESCRIPTION)
