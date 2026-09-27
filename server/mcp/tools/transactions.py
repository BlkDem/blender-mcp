"""Transaction tools: group operations so a mistake can be undone in one call.

An AI that builds a scene touches a dozen objects in a row. Without grouping, a
single wrong placement means a dozen corrective calls — or a user pressing Ctrl-Z
and losing the rest. With it, one ``rollback_transaction`` undoes the group, and
``checkpoint`` lets a plan with stages be rewound one stage at a time.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context

from server import transactions
from server.errors import BlenderMCPError
from server.mcp.support import bridge_of, register_all, tool_error

logger = logging.getLogger(__name__)

BEGIN_DESCRIPTION = """\
Open a transaction. Every mutating call until commit or rollback is recorded, and
blender.rollback_transaction puts all of them back.

label: optional name for the starting point, e.g. "layout". Later rollbacks can
       return to it by name.

Fails with TRANSACTION_ACTIVE if one is already open.\
"""

CHECKPOINT_DESCRIPTION = """\
Record a named point inside the open transaction, so a later rollback can stop
there instead of unwinding everything.

Use it between stages of a build: begin -> build the shell -> checkpoint "shell"
-> add the details -> commit. A rollback to "shell" then discards only the
details.

label: required, and unique within the transaction.\
"""

COMMIT_DESCRIPTION = """\
Close the open transaction and keep the changes. The undo steps stay on Blender's
undo stack, so the user can still step back by hand.

Fails with TRANSACTION_NOT_ACTIVE if no transaction is open.\
"""

ROLLBACK_DESCRIPTION = """\
Put back everything the transaction changed.

to: optional checkpoint label. Omit it to rewind to the start of the transaction;
    name one to rewind only to that checkpoint.

Restores what the tools changed: transforms, names, visibility, materials and
collection membership. Edits made through blender.execute_python are not
tracked, so a transaction containing them rolls back only its tool calls.

Fails with TRANSACTION_NOT_ACTIVE if no transaction is open.\
"""


async def begin_transaction(ctx: Context, label: str | None = None) -> dict[str, Any]:
    """Start an undo group."""
    try:
        result = await transactions.begin(bridge_of(ctx), label)
    except BlenderMCPError as exc:
        raise tool_error(exc) from exc
    return {"success": True, **result}


async def checkpoint(ctx: Context, label: str) -> dict[str, Any]:
    """Record a point a later rollback can return to."""
    try:
        result = await transactions.checkpoint(bridge_of(ctx), label)
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


async def rollback_transaction(ctx: Context, to: str | None = None) -> dict[str, Any]:
    """Undo the transaction, back to its start or to a named checkpoint."""
    try:
        result = await transactions.rollback(bridge_of(ctx), to)
    except BlenderMCPError as exc:
        raise tool_error(exc) from exc
    return {"success": True, **result}


def register_tools(server: MCPServer, enabled: Callable[[str], bool] | None = None) -> list[str]:
    return register_all(
        server,
        (
            (begin_transaction, "blender.begin_transaction", BEGIN_DESCRIPTION),
            (checkpoint, "blender.checkpoint", CHECKPOINT_DESCRIPTION),
            (commit_transaction, "blender.commit_transaction", COMMIT_DESCRIPTION),
            (rollback_transaction, "blender.rollback_transaction", ROLLBACK_DESCRIPTION),
        ),
        enabled,
    )
