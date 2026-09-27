"""``blender://render/latest`` — the most recent render, as an image.

The resource form rather than part of the render tool, because a GUI wants to
fetch the picture on its own schedule and a model wants it inline; neither should
have to pay for the other's access pattern.

Bytes, not a path, because that is what a resource is for. The trade is context:
this resource is not something to read into a conversation, it is something for a
client to fetch.
"""

from __future__ import annotations

import logging
import mimetypes
import os
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError
from mcp.server.mcpserver.resources import FunctionResource

from server.blender.connection import BlenderBridge
from server.blender.protocol import Action
from server.errors import BlenderMCPError
from server.mcp.resources.common import JSON_MIME

logger = logging.getLogger(__name__)

URI = "blender://render/latest"
NAME = "Latest render"
DESCRIPTION = (
    "The most recent image rendered by this Blender, as image bytes. Empty until "
    "blender.render or blender.render_preview has been called."
)
#: Guards a client from pulling an unbounded file into its own memory.
MAX_IMAGE_BYTES = 32 * 1024 * 1024


def register(server: MCPServer, bridge: BlenderBridge) -> None:
    """Register the resource, bound to ``bridge``."""

    async def read() -> bytes:
        try:
            info = await bridge.request(Action.LAST_RENDER)
        except BlenderMCPError as exc:
            raise ResourceError(exc.to_tool_message()) from exc
        return _read_bytes(info)

    server.add_resource(
        FunctionResource.from_function(
            fn=read,
            uri=URI,
            name=NAME,
            description=DESCRIPTION,
            # The mime type is guessed per read, so declare the common case and
            # let a PNG/JPEG override it in the contents.
            mime_type="image/png",
        )
    )


def _read_bytes(info: dict[str, Any]) -> bytes:
    path = str(info.get("output_path", ""))
    if not info.get("exists") or not path or not os.path.isfile(path):
        raise ResourceError(
            '{"success": false, "error": {"code": "OBJECT_NOT_FOUND", '
            '"message": "no rendered image is available"}}'
        )
    size = os.path.getsize(path)
    if size > MAX_IMAGE_BYTES:
        raise ResourceError(
            '{"success": false, "error": {"code": "INVALID_PARAMETER", '
            f'"message": "the last render is {size} bytes, over the {MAX_IMAGE_BYTES} limit"}}}}'
        )
    with open(path, "rb") as handle:
        return handle.read()


def guess_mime(path: str) -> str:
    """The mime type of a rendered file, for clients that need it."""
    return mimetypes.guess_type(path)[0] or "image/png"


__all__ = ["JSON_MIME", "MAX_IMAGE_BYTES", "URI", "guess_mime", "register"]
