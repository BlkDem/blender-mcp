"""MCP tool registration.

One module per concern; :func:`register_tools` is the single place that knows
the full tool surface and which of it ``ENABLED_TOOLS`` allows.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from mcp.server.mcpserver import MCPServer

from server.mcp.tools import (
    instances,
    objects,
    preview,
    python,
    render,
    scene,
    transactions,
    watch,
)

logger = logging.getLogger(__name__)

__all__ = ["register_tools"]


def register_tools(server: MCPServer, enabled: Callable[[str], bool] | None = None) -> list[str]:
    """Register the tools ``enabled`` allows; return the names it refused.

    The predicate defaults to allowing everything, so a caller that does not care
    about the allowlist does not have to know it exists.
    """
    allow = enabled or (lambda _name: True)
    skipped: list[str] = []

    for register in (
        scene.register_tools,
        objects.register_tools,
        render.register_tools,
        preview.register_tools,
        python.register_tools,
        transactions.register_tools,
        watch.register_tools,
        instances.register_tools,
    ):
        skipped.extend(register(server, allow))

    for name in skipped:
        logger.debug("not registering %s: not in ENABLED_TOOLS", name)
    return skipped
