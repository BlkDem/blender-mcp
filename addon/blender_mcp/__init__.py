"""Blender MCP add-on: lets the MCP server drive this Blender.

Install it with ``Edit > Preferences > Add-ons > Install...`` and pick the
zipped ``blender_mcp`` folder, then enable *Blender MCP* and open the
``N`` panel in the 3D Viewport.

Every module in this package except :mod:`blender_mcp.protocol` and
:mod:`blender_mcp.websocket` imports ``bpy``, so only those two can be imported
outside a running Blender — which is exactly what ``tests/test_addon_protocol.py``
relies on.
"""

from __future__ import annotations

import logging
from typing import Any

bl_info: dict[str, Any] = {
    "name": "Blender MCP",
    "author": "Blender MCP contributors",
    "version": (0, 1, 0),
    "blender": (3, 6, 0),
    "location": "View3D > Sidebar (N) > Blender MCP",
    "description": "Connect this Blender to a Blender MCP Server so an AI client can drive it",
    "category": "Development",
    "doc_url": "https://github.com/your-org/blender-mcp",
}

_LOGGER_NAME = "blender_mcp"


def _configure_logging() -> logging.Logger:
    """Send add-on logs to Blender's console, never to stdout."""
    logger = logging.getLogger(_LOGGER_NAME)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def register() -> None:
    """Called by Blender when the add-on is enabled."""
    logger = _configure_logging()
    from blender_mcp import ui
    from blender_mcp.connection import BlenderConnection

    ui.set_connection(BlenderConnection())
    ui.register()
    logger.info("Blender MCP add-on registered")


def unregister() -> None:
    """Called by Blender when the add-on is disabled."""
    logger = logging.getLogger(_LOGGER_NAME)
    from blender_mcp import ui

    ui.unregister()
    logger.info("Blender MCP add-on unregistered")
