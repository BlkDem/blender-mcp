"""Assembly of the MCP server: tools, resources and the bridge lifespan.

Layering, outermost first::

    AI client  --MCP-->  tools / resources  --JSON over WebSocket-->  add-on  -->  bpy

This module only knows about the first two hops. It never imports ``bpy`` and
never speaks to Blender directly; that is the whole point of the split.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from mcp.server.mcpserver import MCPServer

from server import __version__
from server.blender.connection import BlenderBridge
from server.config import Settings, get_settings
from server.mcp.resources import register_resources
from server.mcp.support import AppContext
from server.mcp.tools import register_tools

logger = logging.getLogger(__name__)

INSTRUCTIONS = """\
Control a running Blender through these tools.

Order of work: blender.get_scene to see what exists, then blender.create_object /
blender.update_object / blender.delete_object, then blender.render to check the
result. Wrap a multi-object build in blender.begin_transaction so a mistake can
be undone with a single blender.rollback_transaction.

Units are metres and rotations are DEGREES on every tool. Tools return small
JSON summaries; use blender.execute_python when you need something the tools do
not cover.
"""

_SDK_LOG_LEVELS: dict[str, Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]] = {
    "DEBUG": "DEBUG",
    "INFO": "INFO",
    "WARNING": "WARNING",
    "ERROR": "ERROR",
    "CRITICAL": "CRITICAL",
}


@asynccontextmanager
async def _lifespan(app: AppContext) -> AsyncIterator[AppContext]:
    """Start the WebSocket bridge for as long as the MCP server is running."""
    await app.bridge.start()
    try:
        yield app
    finally:
        await app.bridge.stop()


def create_server(settings: Settings | None = None) -> MCPServer:
    """Build the MCP server with every tool and resource registered."""
    settings = settings or get_settings()
    app = AppContext(
        settings=settings,
        bridge=BlenderBridge(
            host=settings.blender_host,
            port=settings.blender_port,
            request_timeout=settings.blender_request_timeout,
        ),
    )
    server = MCPServer(
        name="blender-mcp",
        version=__version__,
        instructions=INSTRUCTIONS,
        log_level=_SDK_LOG_LEVELS.get(settings.log_level, "INFO"),
        lifespan=lambda _server: _lifespan(app),
    )
    register_tools(server)
    register_resources(server, app.bridge)
    logger.debug(
        "MCP server built; bridge will listen on ws://%s:%s",
        settings.blender_host,
        settings.blender_port,
    )
    return server
