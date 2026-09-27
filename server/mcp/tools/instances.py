"""``blender.get_instances``: see which Blender this server is attached to.

An agent that has been refused, or that is working against a file it did not
expect, can ask this before touching anything. It is a read of the bridge's own
bookkeeping, so it answers even when the scene is busy and Blender is slow.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context

from server.errors import BlenderMCPError
from server.mcp.support import bridge_of, register, tool_error

logger = logging.getLogger(__name__)

INSTANCES_DESCRIPTION = """\
List the Blender instances that have connected to this server, newest first.

The active instance is the one every other tool acts on. The others are kept as
history so a scene that changed unexpectedly can be explained: an entry with
status "refused" or "replaced" means a second Blender tried to take the socket.

Answers even when no Blender is connected, which makes it a useful first call
after a connection error.\
"""


async def get_instances(ctx: Context) -> dict[str, Any]:
    """Report the active instance and the recent history."""
    bridge = bridge_of(ctx)
    try:
        return bridge.instances.describe()
    except BlenderMCPError as exc:
        raise tool_error(exc) from exc


def register_tools(server: MCPServer, enabled: Callable[[str], bool] | None = None) -> list[str]:
    name = "blender.get_instances"
    if not (enabled or (lambda _n: True))(name):
        return [name]
    register(server, get_instances, name, INSTANCES_DESCRIPTION)
    return []
