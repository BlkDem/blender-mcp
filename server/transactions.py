"""Undo grouping for AI operations, on top of Blender's undo stack.

Blender's undo stack is what the *user* steps through, and this module is what an
agent uses to take back a group of its own work. The two are deliberately
separate: a transaction restores recorded state (exact for the tool layer, and
unaffected by a user editing in the UI mid-transaction), while the undo steps
stay available for a human.

    async with transaction(bridge) as tx:      # commits, or rolls back on error
        ...

    await begin(bridge, "layout")
    await checkpoint(bridge, "shell")
    await rollback(bridge, to="shell")         # only what came after "shell"
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from server.blender.connection import BlenderBridge
from server.blender.protocol import Action

logger = logging.getLogger(__name__)

#: Longest a caller may hold a transaction open before the bridge gives up on
#: the response. Without it a lost request would leave the add-on waiting
#: forever, holding recorded state.
DEFAULT_TRANSACTION_TIMEOUT = 900.0


async def begin(bridge: BlenderBridge, label: str | None = None) -> dict[str, Any]:
    logger.info("MCP transaction begin%s", f" ({label})" if label else "")
    params: dict[str, Any] = {}
    if label:
        params["label"] = label
    return await bridge.request(Action.BEGIN_TRANSACTION, params, timeout=DEFAULT_TRANSACTION_TIMEOUT)


async def checkpoint(bridge: BlenderBridge, label: str) -> dict[str, Any]:
    logger.info("MCP transaction checkpoint %s", label)
    return await bridge.request(Action.CHECKPOINT, {"label": label})


async def commit(bridge: BlenderBridge) -> dict[str, Any]:
    logger.info("MCP transaction commit")
    return await bridge.request(Action.COMMIT_TRANSACTION, {})


async def rollback(bridge: BlenderBridge, to: str | None = None) -> dict[str, Any]:
    logger.info("MCP transaction rollback%s", f" to {to!r}" if to else "")
    params: dict[str, Any] = {}
    if to:
        params["to"] = to
    return await bridge.request(Action.ROLLBACK_TRANSACTION, params)


@asynccontextmanager
async def transaction(bridge: BlenderBridge, label: str | None = None) -> AsyncIterator[None]:
    """Group a sequence of operations so a failure can undo all of them.

    ``async with transaction(bridge): ...`` commits on a clean exit and rolls
    back if the body raises, which is what a plan/execute loop needs.
    """
    await begin(bridge, label)
    try:
        yield
    except BaseException:
        try:
            await rollback(bridge)
        except Exception:  # pragma: no cover - rollback is best effort
            logger.exception("Rollback after a failed transaction did not complete")
        raise
    else:
        await commit(bridge)
