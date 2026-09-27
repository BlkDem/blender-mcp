"""``blender://scene`` — compact state of the active scene."""

from __future__ import annotations

import logging

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.resources import FunctionResource

from server.blender.connection import BlenderBridge
from server.blender.protocol import Action
from server.mcp.resources.common import JSON_MIME, read_json

logger = logging.getLogger(__name__)

URI = "blender://scene"
NAME = "Blender scene state"
DESCRIPTION = (
    "Compact JSON summary of the active Blender scene: every object with its transform, "
    "plus collection, camera and light names. Mesh data is not included."
)


def register(server: MCPServer, bridge: BlenderBridge) -> None:
    """Register the scene resource, bound to ``bridge``."""

    async def read() -> str:
        return await read_json(bridge, Action.GET_SCENE)

    server.add_resource(
        FunctionResource.from_function(
            fn=read, uri=URI, name=NAME, description=DESCRIPTION, mime_type=JSON_MIME
        )
    )
