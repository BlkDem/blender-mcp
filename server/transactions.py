"""Undo grouping for AI operations.

Blender's undo stack is the only rollback mechanism that is both cheap and
correct for data-block changes, so transactions are built on it:

* ``begin_transaction``    pushes one undo step and starts counting.
* mutating actions         each push their own undo step while a transaction runs.
* ``commit_transaction``   closes the group, leaving the steps on the undo stack
  so the user can still step back manually.
* ``rollback_transaction`` replays the exact number of undo steps recorded since
  ``begin``, returning the scene to the state it had before.

The counting scheme assumes the only undo steps in between come from this
add-on. Manual edits in the Blender UI during an open transaction are not
counted; the transaction is meant to be short-lived.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from server.blender.connection import BlenderBridge
from server.blender.protocol import Action

logger = logging.getLogger(__name__)


async def begin(bridge: BlenderBridge) -> dict[str, object]:
    logger.info("MCP transaction begin")
    return await bridge.request(Action.BEGIN_TRANSACTION)


async def commit(bridge: BlenderBridge) -> dict[str, object]:
    logger.info("MCP transaction commit")
    return await bridge.request(Action.COMMIT_TRANSACTION)


async def rollback(bridge: BlenderBridge) -> dict[str, object]:
    logger.info("MCP transaction rollback")
    return await bridge.request(Action.ROLLBACK_TRANSACTION)


@asynccontextmanager
async def transaction(bridge: BlenderBridge) -> AsyncIterator[None]:
    """Group a sequence of operations so a failure can undo all of them.

    ``async with transaction(bridge): ...`` commits on a clean exit and rolls
    back if the body raises, which is what a future plan/execute loop needs.
    """
    await begin(bridge)
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
