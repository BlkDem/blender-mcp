"""Shared plumbing for the MCP resource handlers."""

from __future__ import annotations

import json
from typing import Any

from mcp.server.mcpserver.exceptions import ResourceError

from server.blender.connection import BlenderBridge
from server.blender.protocol import Action
from server.errors import BlenderMCPError

JSON_MIME = "application/json"


async def read_json(bridge: BlenderBridge, action: Action, params: dict[str, Any] | None = None) -> str:
    """Fetch a payload from Blender and render it as indented JSON.

    A :class:`BlenderMCPError` becomes a :class:`ResourceError` on purpose: the
    SDK forwards an anticipated resource failure's message to the client but
    replaces a crash with a generic one, and ``NOT_CONNECTED`` is exactly the
    kind of thing the client needs to read.
    """
    try:
        payload = await bridge.request(action, params)
    except BlenderMCPError as exc:
        raise ResourceError(exc.to_tool_message()) from exc
    return json.dumps(payload, indent=2, ensure_ascii=False)
