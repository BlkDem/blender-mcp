"""Object creation, modification and deletion.

Argument validation happens here, before anything crosses the socket: a bad
vector length or an unsupported primitive is a local :class:`BlenderMCPError`,
while facts that only Blender knows (does the name exist? is it in edit mode?)
come back as errors from the add-on.

Naming is idempotent by design. ``create_object(name="Table")`` on an existing
``Table`` fails with OBJECT_ALREADY_EXISTS instead of quietly producing
``Table.001``; the model is in a better position to decide what to do than a
silent rename is.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context

from server.blender.protocol import Action
from server.errors import BlenderMCPError, ErrorCode
from server.mcp.support import bridge_of, register, tool_error

logger = logging.getLogger(__name__)

SUPPORTED_TYPES: tuple[str, ...] = ("cube", "sphere", "cylinder", "cone", "plane", "torus")

CREATE_OBJECT_DESCRIPTION = """\
Create a mesh object in the active scene.

type: one of cube, sphere, cylinder, cone, plane, torus.
name: must be unique; an existing name fails with OBJECT_ALREADY_EXISTS
      rather than being renamed to "Name.001".
location / scale: Blender units (meters), as [x, y, z].
rotation: DEGREES as [x, y, z], applied in XYZ Euler order.

Example: {"type": "cube", "name": "TableTop", "location": [0, 0, 1], "scale": [2, 1, 0.1]}\
"""

UPDATE_OBJECT_DESCRIPTION = """\
Change an existing object. Only the fields you pass are touched; omitted fields
keep their current value.

location / scale: Blender units (meters) as [x, y, z].
rotation: DEGREES as [x, y, z].
dimensions: final bounding-box size in meters as [x, y, z] (replaces scale).
visibility: hide or show the object in the viewport and renders.\
"""

DELETE_OBJECT_DESCRIPTION = """\
Delete an object from the active scene, checking first that it exists.

Fails with OBJECT_NOT_FOUND when the name is unknown.\
"""

Vector3 = list[float]


def _vector(name: str, value: Any, *, allow_negative: bool = True) -> Vector3:
    """Validate a 3-component float vector coming from the model."""
    if value is None:
        raise tool_error(BlenderMCPError(f"{name} must not be null"))
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise tool_error(
            BlenderMCPError(
                f"{name} must be a list of 3 numbers",
                code=ErrorCode.INVALID_PARAMETER,
                details={"received": value},
            )
        )
    try:
        numbers = [float(component) for component in value]
    except (TypeError, ValueError) as exc:
        raise tool_error(
            BlenderMCPError(
                f"{name} must contain only numbers",
                code=ErrorCode.INVALID_PARAMETER,
                details={"received": value},
            )
        ) from exc
    if not allow_negative and any(component < 0 for component in numbers):
        raise tool_error(
            BlenderMCPError(
                f"{name} must not contain negative values",
                code=ErrorCode.INVALID_PARAMETER,
                details={"received": numbers},
            )
        )
    return numbers


def _require_name(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise tool_error(BlenderMCPError("Object name must not be empty"))
    return name


async def create_object(
    ctx: Context,
    type: Literal["cube", "sphere", "cylinder", "cone", "plane", "torus"],
    name: str,
    location: Vector3 | None = None,
    rotation: Vector3 | None = None,
    scale: Vector3 | None = None,
    collection: str | None = None,
) -> dict[str, Any]:
    """Create a mesh object."""
    if type not in SUPPORTED_TYPES:
        raise tool_error(
            BlenderMCPError(
                f"Unsupported object type '{type}'",
                code=ErrorCode.INVALID_OBJECT_TYPE,
                details={"supported": list(SUPPORTED_TYPES)},
            )
        )
    params: dict[str, Any] = {"type": type, "name": _require_name(name)}
    if location is not None:
        params["location"] = _vector("location", location)
    if rotation is not None:
        params["rotation"] = _vector("rotation", rotation)
    if scale is not None:
        params["scale"] = _vector("scale", scale)
    if collection:
        params["collection"] = collection

    try:
        return await bridge_of(ctx).request(Action.CREATE_OBJECT, params)
    except BlenderMCPError as exc:
        logger.info("blender.create_object(%s) failed: %s", name, exc)
        raise tool_error(exc) from exc


async def update_object(
    ctx: Context,
    name: str,
    location: Vector3 | None = None,
    rotation: Vector3 | None = None,
    scale: Vector3 | None = None,
    dimensions: Vector3 | None = None,
    visibility: bool | None = None,
) -> dict[str, Any]:
    """Change only the given properties of an object."""
    _require_name(name)
    params: dict[str, Any] = {"name": name}
    if location is not None:
        params["location"] = _vector("location", location)
    if rotation is not None:
        params["rotation"] = _vector("rotation", rotation)
    if scale is not None:
        params["scale"] = _vector("scale", scale)
    if dimensions is not None:
        params["dimensions"] = _vector("dimensions", dimensions, allow_negative=False)
    if visibility is not None:
        params["visibility"] = bool(visibility)
    if len(params) == 1:
        raise tool_error(
            BlenderMCPError(
                "Nothing to update: pass at least one of location, rotation, scale, dimensions, visibility",
                code=ErrorCode.INVALID_PARAMETER,
            )
        )

    try:
        return await bridge_of(ctx).request(Action.UPDATE_OBJECT, params)
    except BlenderMCPError as exc:
        logger.info("blender.update_object(%s) failed: %s", name, exc)
        raise tool_error(exc) from exc


async def delete_object(ctx: Context, name: str) -> dict[str, Any]:
    """Delete an object after verifying that it exists."""
    _require_name(name)
    try:
        return await bridge_of(ctx).request(Action.DELETE_OBJECT, {"name": name})
    except BlenderMCPError as exc:
        logger.info("blender.delete_object(%s) failed: %s", name, exc)
        raise tool_error(exc) from exc


def register_tools(server: MCPServer) -> None:
    register(server, create_object, "blender.create_object", CREATE_OBJECT_DESCRIPTION)
    register(server, update_object, "blender.update_object", UPDATE_OBJECT_DESCRIPTION)
    register(server, delete_object, "blender.delete_object", DELETE_OBJECT_DESCRIPTION)
