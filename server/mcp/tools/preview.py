"""Render tools: a full render to a file, and a small one returned inline.

The split is deliberate. ``blender.render`` is for the user: it honours the
scene's settings and hands back a path. ``blender.render_preview`` is for the
*model*: it caps the size and the sample count, and returns the image itself, so
a vision-capable client can close the loop
*plan → build → render → look → correct* without touching the filesystem.

The image is read from the path the add-on reported rather than shipped over
the WebSocket, which keeps megabytes of base64 off the bridge. That assumes the
MCP server shares a filesystem with Blender, which is the same loopback
assumption the bridge already makes.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.utilities.types import Image
from mcp_types import ContentBlock, ImageContent, TextContent

from server.blender.protocol import Action
from server.errors import BlenderMCPError
from server.mcp.support import bridge_of, register, tool_error

logger = logging.getLogger(__name__)

#: Ceiling on a preview's longest edge. A 1024 px PNG is roughly a megabyte of
#: base64 in the reply, already at the edge of what belongs in a conversation; a
#: model looking for gross errors does not need more.
MAX_PREVIEW_EDGE = 2048

PREVIEW_DESCRIPTION = """\
Render a small preview and return the image, so you can look at what you built.

max_edge: longest edge in pixels, 64-2048, default 512. Bigger costs a lot of
          context, and a vision model rarely needs more to spot a mistake.
engine: optional engine id; omit to keep the scene's current one.
samples: optional sample count; omit for a cheap default.
include_image: set false to get only the file path and no image.

Prefer this over blender.render while iterating: it is capped in size and samples
and it comes back as an image. Use blender.render for a final image the user will
keep.\
"""


async def render_preview(
    ctx: Context,
    max_edge: int = 512,
    engine: str | None = None,
    samples: int | None = None,
    include_image: bool = True,
) -> list[ContentBlock]:
    """Render small and return the image, for a model that can look at it."""
    edge = max(64, min(int(max_edge), MAX_PREVIEW_EDGE))
    # The add-on owns the preview path, so nothing has to agree on a directory
    # here; the server reads back whatever path comes with the result.
    params: dict[str, Any] = {"resolution_x": edge, "resolution_y": edge}
    if engine:
        params["engine"] = engine
    if samples:
        params["samples"] = int(samples)

    try:
        result = await bridge_of(ctx).request(Action.RENDER_PREVIEW, params)
    except BlenderMCPError as exc:
        logger.info("blender.render_preview failed: %s", exc)
        raise tool_error(exc) from exc

    summary = {
        "success": True,
        "output_path": result.get("output_path"),
        "render_time": result.get("render_time"),
        "used": result.get("used"),
    }
    blocks: list[ContentBlock] = [TextContent(type="text", text=json.dumps(summary, indent=2))]
    if include_image:
        image = read_image(result.get("output_path", ""))
        if image is not None:
            blocks.append(image)
        else:
            # Say so rather than returning a text-only reply that looks successful.
            blocks.append(
                TextContent(
                    type="text",
                    text="The preview image could not be read; the path above is all there is.",
                )
            )
    return blocks


def read_image(path: str) -> ImageContent | None:
    """Wrap a rendered file as MCP image content, or ``None`` if it is not there."""
    if not path or not os.path.isfile(path):
        logger.info("preview image is not readable at %r", path)
        return None
    mime = mimetypes.guess_type(path)[0] or "image/png"
    if not mime.startswith("image/"):
        logger.info("preview at %r is %s, not an image", path, mime)
        return None
    try:
        with open(path, "rb") as handle:
            return Image(data=handle.read(), format=mime.split("/", 1)[1]).to_image_content()
    except OSError as exc:
        logger.warning("could not read the preview at %r: %s", path, exc)
        return None


def register_tools(server: MCPServer, enabled: Callable[[str], bool] | None = None) -> list[str]:
    """Register the preview tool, which returns content blocks rather than JSON.

    ``structured_output=False`` because a list of content blocks has no single
    object to describe; the JSON summary travels as its first block.
    """
    name = "blender.render_preview"
    if not (enabled or (lambda _n: True))(name):
        return [name]
    register(server, render_preview, name, PREVIEW_DESCRIPTION, structured_output=False)
    return []


__all__ = ["PREVIEW_DESCRIPTION", "read_image", "register_tools", "render_preview"]
