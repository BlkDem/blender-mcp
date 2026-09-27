"""MCP tool registration.

One module per concern; :func:`register_tools` is the single place that knows
the full tool surface.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from server.mcp.tools import objects, python, render, scene, transactions

__all__ = ["register_tools"]


def register_tools(server: MCPServer) -> None:
    """Register every blender.* tool on ``server``."""
    scene.register_tools(server)
    objects.register_tools(server)
    render.register_tools(server)
    python.register_tools(server)
    transactions.register_tools(server)
