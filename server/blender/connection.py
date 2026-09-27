"""Server side of the Blender bridge: the WebSocket endpoint the add-on dials.

The bridge is a process-wide singleton created once in the MCP server lifespan.
It owns the listening socket and the currently connected add-on; tools reach it
through the lifespan context rather than through module globals.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from websockets.asyncio.server import Server, ServerConnection, serve

from server.blender.client import BlenderClient
from server.blender.protocol import Action, parse_response
from server.errors import BlenderMCPError, ErrorCode

logger = logging.getLogger(__name__)

# Scene state can be a few hundred KB of JSON; the default 1 MiB frame cap is
# tight once a scene holds many objects.
MAX_MESSAGE_BYTES = 16 * 1024 * 1024


class BlenderBridge:
    """Accepts one Blender add-on and forwards typed requests to it."""

    def __init__(self, host: str, port: int, *, request_timeout: float = 30.0) -> None:
        self._host = host
        self._port = port
        self._request_timeout = request_timeout
        self._server: Server | None = None
        self._client: BlenderClient | None = None
        self._lock = asyncio.Lock()

    # --- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        self._server = await serve(
            self._handle_connection,
            self._host,
            self._port,
            max_size=MAX_MESSAGE_BYTES,
            ping_interval=20,
            ping_timeout=20,
        )
        logger.info("Blender bridge listening on ws://%s:%s", self._host, self._port)

    async def stop(self) -> None:
        async with self._lock:
            if self._client is not None:
                self._client.fail_pending(
                    BlenderMCPError("MCP server is shutting down", code=ErrorCode.CONNECTION_LOST)
                )
            self._client = None
        if self._server is not None:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=5.0)
            except TimeoutError:  # pragma: no cover - only on a wedged socket
                logger.warning("Blender bridge did not close cleanly")
            self._server = None
            logger.info("Blender bridge stopped")

    # --- state -------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._client is not None

    def status(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "url": f"ws://{self._host}:{self._port}",
            "pending_requests": self._client.pending_count if self._client else 0,
        }

    # --- requests ----------------------------------------------------------

    async def request(
        self,
        action: Action,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Forward a request to Blender. Raises :class:`BlenderMCPError` on any failure."""
        async with self._lock:
            client = self._client
        if client is None:
            raise BlenderMCPError(
                f"No Blender instance is connected. Start Blender, enable the "
                f"'Blender MCP' add-on and press Connect. Bridge: ws://{self._host}:{self._port}",
                code=ErrorCode.BLENDER_NOT_CONNECTED,
            )
        return await client.request(action, params, timeout=timeout)

    # --- connection handling ----------------------------------------------

    async def _handle_connection(self, websocket: ServerConnection) -> None:
        peer = getattr(websocket, "remote_address", None)
        client = BlenderClient(websocket, timeout=self._request_timeout)
        async with self._lock:
            previous = self._client
            self._client = client
        logger.info("Blender add-on connected from %s", peer)
        if previous is not None:
            # A second Blender took over the bridge: fail the old one's
            # in-flight requests so nothing hangs, then let it reconnect.
            previous.fail_pending(
                BlenderMCPError("Replaced by another Blender connection", code=ErrorCode.CONNECTION_LOST)
            )

        reader = asyncio.create_task(self._read_loop(websocket, client), name="blender-reader")
        try:
            await reader
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            raise
        except Exception as exc:
            logger.warning("Blender connection error: %s", exc)
        finally:
            reader.cancel()
            async with self._lock:
                if self._client is client:
                    self._client = None
            client.fail_pending(BlenderMCPError("Blender disconnected", code=ErrorCode.CONNECTION_LOST))
            logger.info("Blender add-on disconnected (%s)", peer)

    async def _read_loop(self, websocket: ServerConnection, client: BlenderClient) -> None:
        async for raw in websocket:
            if isinstance(raw, bytes):  # pragma: no cover - add-on only sends text
                raw = raw.decode("utf-8", errors="replace")
            await self._dispatch(raw, client)

    async def _dispatch(self, raw: str, client: BlenderClient) -> None:
        try:
            response = parse_response(raw)
        except BlenderMCPError as exc:
            # Nothing to correlate the error with, so it can only be logged:
            # every in-flight request will hit its own timeout.
            logger.warning("Dropping unparsable frame from Blender: %s", exc.message)
            return
        logger.debug("Blender response id=%s success=%s", response.id, response.success)
        client.deliver(response)
