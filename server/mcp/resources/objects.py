"""``blender://objects`` — the object inventory of the active scene."""

from __future__ import annotations

import logging

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.resources import FunctionResource

from server.blender.connection import BlenderBridge
from server.blender.protocol import Action
from server.mcp.resources.common import JSON_MIME, read_json

logger = logging.getLogger(__name__)

URI = "blender://objects"
NAME = "Blender object inventory"
DESCRIPTION = (
    "JSON list of every object in the active Blender scene with type, location, "
    "rotation (degrees), scale and dimensions. Materials and modifiers are omitted; "
    "use blender.get_object for a single object in full."
)


def register(server: MCPServer, bridge: BlenderBridge) -> None:
    """Register the objects resource, bound to ``bridge``."""

    async def read() -> str:
        return await read_json(bridge, Action.GET_SCENE, {"include_objects": True})

    server.add_resource(
        FunctionResource.from_function(
            fn=read, uri=URI, name=NAME, description=DESCRIPTION, mime_type=JSON_MIME
        )
    )
