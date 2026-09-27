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
from server.config import KNOWN_TOOLS, Settings, get_settings
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

#: Appended when the tool surface has been reduced, so the model is not left
#: guessing why a capability is missing.
_REDUCED_SURFACE = """
This server is running with a reduced tool set (ENABLED_TOOLS). Some operations
are unavailable by configuration, not by accident; do not retry them with
blender.execute_python unless you are sure the task needs it.
"""

_PYTHON_DISABLED = """
blender.execute_python is disabled on this server, so Blender operations must go
through the tools above.
"""


def _instructions_for(settings: Settings) -> str:
    parts = [INSTRUCTIONS]
    if settings.enabled_tools and len(settings.enabled_tools) < len(KNOWN_TOOLS):
        parts.append(_REDUCED_SURFACE)
    if not settings.allow_python_execution:
        parts.append(_PYTHON_DISABLED)
    return "".join(parts)


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
    """Build the MCP server with every enabled tool and resource registered.

    ``ENABLED_TOOLS`` reduces the surface. A name that matches no tool is a
    startup error rather than a silent no-op, because the usual cause is a typo
    and the symptom would otherwise be a model that mysteriously cannot create
    anything.
    """
    settings = settings or get_settings()
    unknown = settings.unknown_tools()
    if unknown:
        raise ValueError(
            f"ENABLED_TOOLS names tools that do not exist: {', '.join(unknown)}. "
            f"Known tools: {', '.join(KNOWN_TOOLS)}"
        )

    app = AppContext(
        settings=settings,
        bridge=BlenderBridge(
            host=settings.blender_host,
            port=settings.blender_port,
            request_timeout=settings.blender_request_timeout,
            allow_takeover=settings.blender_allow_takeover,
        ),
    )
    server = MCPServer(
        name="blender-mcp",
        version=__version__,
        instructions=_instructions_for(settings),
        log_level=_SDK_LOG_LEVELS.get(settings.log_level, "INFO"),
        lifespan=lambda _server: _lifespan(app),
    )
    skipped = register_tools(server, settings.tool_is_enabled)
    register_resources(server, app.bridge)
    if skipped:
        logger.info("ENABLED_TOOLS left these tools unregistered: %s", ", ".join(skipped))
    logger.debug(
        "MCP server built; bridge will listen on ws://%s:%s",
        settings.blender_host,
        settings.blender_port,
    )
    return server
