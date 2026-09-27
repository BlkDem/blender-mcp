"""Render tool.

Rendering is the slowest thing this bridge does, so the tool returns timing and
the output path and nothing else. The response shape deliberately leaves room for
the next step — an inline image plus a ``blender://render/latest`` resource — so
a vision model can close the loop without changing the tool contract.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context

from server.blender.protocol import Action
from server.errors import BlenderMCPError
from server.mcp.support import bridge_of, register, settings_of, tool_error

logger = logging.getLogger(__name__)

RENDER_DESCRIPTION = """\
Render the active scene with the current camera and return the output path and
the wall-clock render time.

engine: Blender engine id, e.g. "CYCLES" or "BLENDER_EEVEE" (4.x called EEVEE
        "BLENDER_EEVEE_NEXT"). blender.get_scene lists the ids this Blender
        accepts under render.engines. Omit to keep the scene's current engine.
resolution_x / resolution_y: pixels. Omit to keep the scene's render size.
samples: Cycles sample count; ignored by engines without sampling.
output_path: absolute or Blender-relative path. Omit to use the scene's
             configured output path.

Render time grows quickly with resolution and samples; prefer 512-1024 px while
iterating.\
"""


async def render(
    ctx: Context,
    engine: str | None = None,
    resolution_x: int | None = None,
    resolution_y: int | None = None,
    samples: int | None = None,
    output_path: str | None = None,
) -> dict[str, Any]:
    """Render the active scene."""
    params: dict[str, Any] = {}
    if engine:
        params["engine"] = engine
    if resolution_x is not None:
        params["resolution_x"] = int(resolution_x)
    if resolution_y is not None:
        params["resolution_y"] = int(resolution_y)
    if samples is not None:
        params["samples"] = int(samples)
    if output_path:
        params["output_path"] = output_path

    logger.info("MCP request blender.render %s", params)
    try:
        return await bridge_of(ctx).request(
            Action.RENDER, params, timeout=settings_of(ctx).blender_render_timeout
        )
    except BlenderMCPError as exc:
        logger.info("blender.render failed: %s", exc)
        raise tool_error(exc) from exc


def register_tools(server: MCPServer) -> None:
    register(server, render, "blender.render", RENDER_DESCRIPTION)
