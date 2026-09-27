"""MCP resources: read-only views of Blender state.

Resources are for state the AI wants *without* spending a tool call.

The SDK does not inject a ``Context`` into static resources, so these handlers
close over the :class:`~server.blender.connection.BlenderBridge` instead of
looking it up in the request context the way the tools do. The bridge object is
created once in :func:`server.mcp.server.create_server` and started in the
lifespan, so the captured reference stays valid for the process lifetime.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from server.blender.connection import BlenderBridge
from server.mcp.resources import objects, scene

__all__ = ["register_resources"]


def register_resources(server: MCPServer, bridge: BlenderBridge) -> None:
    """Register every ``blender://`` resource on ``server``."""
    scene.register(server, bridge)
    objects.register(server, bridge)
