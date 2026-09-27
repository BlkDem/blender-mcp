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
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context

from server.blender.protocol import Action
from server.errors import BlenderMCPError
from server.mcp.support import bridge_of, register_all, tool_error

logger = logging.getLogger(__name__)

GET_SCENE_DESCRIPTION = """\
Return a compact, LLM-sized summary of the active scene: object names, types,
transforms and visibility, plus collections, cameras, lights, the active object,
the render engine and the current frame.

Use this first to see what exists. At most `object_limit` objects are inlined
(200 by default); when `objects_truncated` is true, use blender.get_objects to
page through the rest. No mesh data is included.\
"""

GET_OBJECTS_DESCRIPTION = """\
List objects with filters and a page window, for scenes too large for
blender.get_scene to be useful.

type: object type to keep, e.g. "MESH", "CAMERA", "LIGHT".
collection: only objects linked to this collection.
name_contains: case-insensitive substring of the name.
limit: page size, 1-500, default 50.
offset: how many matches to skip, default 0.

Returns the page, plus "total" (matches before paging) and "truncated" (true when
more objects remain) so you know whether to ask for the next page.\
"""

GET_OBJECT_DESCRIPTION = """\
Return the full detail of a single object: transform, dimensions, visibility,
collection, material names, modifiers, and vertex/edge/polygon counts for meshes.

Fails with OBJECT_NOT_FOUND when the name does not exist.\
"""


async def get_scene(ctx: Context, object_limit: int = 200) -> dict[str, Any]:
    """Summarise the active scene."""
    try:
        return await bridge_of(ctx).request(
            Action.GET_SCENE, {"object_limit": _bounded(object_limit, 1, 1000, 200)}
        )
    except BlenderMCPError as exc:
        logger.info("blender.get_scene failed: %s", exc)
        raise tool_error(exc) from exc


async def get_objects(
    ctx: Context,
    type: str | None = None,
    collection: str | None = None,
    name_contains: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """List objects, filtered and paged."""
    params: dict[str, Any] = {"limit": _bounded(limit, 1, 500, 50), "offset": max(0, int(offset))}
    if type:
        params["type"] = str(type).upper()
    if collection:
        params["collection"] = collection
    if name_contains:
        params["name_contains"] = name_contains

    try:
        return await bridge_of(ctx).request(Action.GET_OBJECTS, params)
    except BlenderMCPError as exc:
        logger.info("blender.get_objects failed: %s", exc)
        raise tool_error(exc) from exc


def _bounded(value: int, low: int, high: int, default: int) -> int:
    """Clamp a paging parameter, so a model cannot ask for the whole scene."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(number, low), high)


async def get_object(ctx: Context, name: str) -> dict[str, Any]:
    """Describe one object."""
    if not name or not name.strip():
        raise tool_error(BlenderMCPError("Object name must not be empty"))
    try:
        return await bridge_of(ctx).request(Action.GET_OBJECT, {"name": name})
    except BlenderMCPError as exc:
        logger.info("blender.get_object(%s) failed: %s", name, exc)
        raise tool_error(exc) from exc


def register_tools(server: MCPServer, enabled: Callable[[str], bool] | None = None) -> list[str]:
    return register_all(
        server,
        (
            (get_scene, "blender.get_scene", GET_SCENE_DESCRIPTION),
            (get_objects, "blender.get_objects", GET_OBJECTS_DESCRIPTION),
            (get_object, "blender.get_object", GET_OBJECT_DESCRIPTION),
        ),
        enabled,
    )
