"""Glue between MCP tool signatures and the application context.

The server's lifespan publishes an :class:`AppContext` holding the running
:class:`~server.blender.connection.BlenderBridge` and the
:class:`~server.config.Settings` the process was started with. Tools read both
from the SDK's ``Context``, which means:

* no module-level mutable state, and
* a test can hand a tool a context with a stub bridge and explicit settings.

(The SDK does not inject a ``Context`` into *static* resources, so the resource
handlers close over the bridge instead — see :mod:`server.mcp.resources`.)
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError

from server.blender.connection import BlenderBridge
from server.config import Settings
from server.errors import BlenderMCPError

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


@dataclass(frozen=True, slots=True)
class AppContext:
    """What the lifespan hands to every tool and resource."""

    settings: Settings
    bridge: BlenderBridge


def _app_context(ctx: Context) -> AppContext:
    context = ctx.request_context.lifespan_context
    if not isinstance(context, AppContext):  # pragma: no cover - wiring guard
        raise BlenderMCPError("MCP server lifespan did not provide an application context")
    return context


def bridge_of(ctx: Context) -> BlenderBridge:
    """The bridge to the connected Blender."""
    return _app_context(ctx).bridge


def settings_of(ctx: Context) -> Settings:
    """The settings this server was started with."""
    return _app_context(ctx).settings


def tool_error(exc: BlenderMCPError) -> ToolError:
    """Wrap a structured error so the model sees ``is_error`` *and* the code.

    The SDK prefixes the text with ``Error executing tool <name>:`` and marks the
    call failed; the JSON body after it keeps ``error.code`` machine-readable.
    """
    return ToolError(exc.to_tool_message())


def register(server: MCPServer, fn: F, name: str, description: str) -> None:
    """Register a tool under an explicit, dotted name."""
    server.add_tool(fn, name=name, description=description, structured_output=True)
