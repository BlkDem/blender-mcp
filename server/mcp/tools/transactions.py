"""Transaction tools: group several AI operations into one undoable step.

An AI that builds a scene touches many objects in a row. Wrapping that in a
transaction means one mistake can be undone in a single call, and the undo
history stays readable instead of collecting a dozen "MCP" steps.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context

from server import transactions
from server.errors import BlenderMCPError
from server.mcp.support import bridge_of, register, tool_error

logger = logging.getLogger(__name__)

BEGIN_DESCRIPTION = """\
Open a transaction. Every mutating tool called until commit or rollback is
grouped, and blender.rollback_transaction undoes all of them at once.

Fails with TRANSACTION_ACTIVE if one is already open.\
"""

COMMIT_DESCRIPTION = """\
Close the open transaction and keep the changes. The undo steps stay on Blender's
undo stack, so the user can still step back by hand.

Fails with TRANSACTION_NOT_ACTIVE if no transaction is open.\
"""

ROLLBACK_DESCRIPTION = """\
Undo every operation performed since blender.begin_transaction, restoring the
scene to the state it was in when the transaction opened.

Fails with TRANSACTION_NOT_ACTIVE if no transaction is open.\
"""


async def begin_transaction(ctx: Context) -> dict[str, Any]:
    """Start an undo group."""
    try:
        result = await transactions.begin(bridge_of(ctx))
    except BlenderMCPError as exc:
        raise tool_error(exc) from exc
    return {"success": True, **result}


async def commit_transaction(ctx: Context) -> dict[str, Any]:
    """Close an undo group, keeping the changes."""
    try:
        result = await transactions.commit(bridge_of(ctx))
    except BlenderMCPError as exc:
        raise tool_error(exc) from exc
    return {"success": True, **result}


async def rollback_transaction(ctx: Context) -> dict[str, Any]:
    """Undo everything done since begin_transaction."""
    try:
        result = await transactions.rollback(bridge_of(ctx))
    except BlenderMCPError as exc:
        raise tool_error(exc) from exc
    return {"success": True, **result}


def register_tools(server: MCPServer) -> None:
    register(server, begin_transaction, "blender.begin_transaction", BEGIN_DESCRIPTION)
    register(server, commit_transaction, "blender.commit_transaction", COMMIT_DESCRIPTION)
    register(server, rollback_transaction, "blender.rollback_transaction", ROLLBACK_DESCRIPTION)
