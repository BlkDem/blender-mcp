"""Client side of the Blender bridge: correlate one request with one response.

A :class:`BlenderClient` owns exactly one WebSocket connection. It keeps the
pending futures keyed by request id, so a reader task can resolve them in any
order and a dropped connection can fail all of them at once.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Protocol

from server.blender.protocol import Action, Request, Response
from server.errors import BlenderMCPError, ErrorCode

logger = logging.getLogger(__name__)


class SupportsSend(Protocol):
    """The only part of a WebSocket connection this layer depends on."""

    async def send(self, message: str) -> None: ...


class BlenderClient:
    """Round-trips requests over a single connection with a hard timeout."""

    def __init__(self, websocket: SupportsSend, timeout: float = 30.0) -> None:
        self._ws = websocket
        self._timeout = timeout
        self._pending: dict[str, asyncio.Future[Response]] = {}

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    async def send_raw(self, message: str) -> None:
        """Send an already-encoded frame, e.g. a protocol-level error."""
        await self._ws.send(message)

    async def request(
        self,
        action: Action,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Send a request, wait for its response, return the result payload."""
        request = Request(id=uuid.uuid4().hex, action=action.value, params=params or {})
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Response] = loop.create_future()
        self._pending[request.id] = future

        try:
            await self._ws.send(request.model_dump_json())
        except Exception as exc:
            self._pending.pop(request.id, None)
            raise BlenderMCPError(
                f"Could not send '{request.action}' to Blender: {exc}",
                code=ErrorCode.CONNECTION_LOST,
            ) from exc

        wait_for = self._timeout if timeout is None else timeout
        try:
            response = await asyncio.wait_for(future, timeout=wait_for)
        except TimeoutError as exc:
            raise BlenderMCPError(
                f"Blender did not answer '{request.action}' within {wait_for:g}s",
                code=ErrorCode.TIMEOUT,
                details={"action": request.action, "timeout": wait_for},
            ) from exc
        finally:
            self._pending.pop(request.id, None)

        return response.raise_for_status()

    def deliver(self, response: Response) -> None:
        """Hand a response to whoever is waiting for it. Never raises."""
        future = self._pending.pop(response.id, None)
        if future is None or future.done():
            # A late reply after a timeout, or a duplicate: log and drop it
            # rather than letting it resolve an unrelated future.
            logger.warning("Discarding response for unknown request id %s", response.id)
            return
        future.set_result(response)

    def fail_pending(self, error: BlenderMCPError) -> None:
        """Fail every in-flight request, e.g. because the socket went away."""
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(error)
