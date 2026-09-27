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
from server.blender.instances import ACTIVE, IDENTITY_TIMEOUT, REFUSED, InstanceRegistry
from server.blender.protocol import Action, encode_disconnect, parse_response
from server.errors import BlenderMCPError, ErrorCode

logger = logging.getLogger(__name__)

# Scene state can be a few hundred KB of JSON; the default 1 MiB frame cap is
# tight once a scene holds many objects.
MAX_MESSAGE_BYTES = 16 * 1024 * 1024


class BlenderBridge:
    """Accepts one Blender add-on and forwards typed requests to it."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        request_timeout: float = 30.0,
        allow_takeover: bool = False,
    ) -> None:
        self._host = host
        self._port = port
        self._request_timeout = request_timeout
        self._server: Server | None = None
        self._client: BlenderClient | None = None
        self._instances = InstanceRegistry(allow_takeover=allow_takeover)
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
        peer = _address_of(websocket)
        client = BlenderClient(websocket, timeout=self._request_timeout)

        # The newcomer becomes the current client *before* the handshake. A
        # reconnect races the old socket's teardown, and a request that lands in
        # that window would otherwise be queued against a dead socket and fail as
        # "connection lost" for no reason. If the handshake then refuses it, the
        # old client goes back.
        async with self._lock:
            previous = self._client
            self._client = client

        # The read loop has to run before the handshake: the answer to the ping
        # arrives on the same socket, and nothing else would be reading it.
        reader = asyncio.create_task(self._read_loop(websocket, client), name="blender-reader")
        instance, displaced = await self._identify(client, peer)
        if instance is not None and instance.status != ACTIVE:
            async with self._lock:
                if self._client is client:
                    self._client = previous
            await self._refuse(websocket, client, instance, reader)
            return

        if instance is not None:
            self._instances.touch(instance)
        logger.info("Blender add-on connected from %s", peer)
        if previous is not None and previous is not client:
            # Nothing should be left hanging on the old socket. With a different
            # Blender it was a takeover; with the same one it was a reconnect, and
            # saying so keeps the error honest.
            same = instance is not None and displaced is not None and instance.id == displaced.id
            previous.fail_pending(
                BlenderMCPError(
                    "The same Blender reconnected" if same else "Replaced by another Blender connection",
                    code=ErrorCode.CONNECTION_LOST,
                )
            )

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
            if instance is not None:
                self._instances.release(instance)
            client.fail_pending(BlenderMCPError("Blender disconnected", code=ErrorCode.CONNECTION_LOST))
            logger.info("Blender add-on disconnected (%s)", peer)

    async def _identify(self, client: BlenderClient, peer: str) -> tuple[Any, Any]:
        """Ask the newcomer who it is, and decide whether it may have the socket.

        A client that cannot answer is treated as a test double or a stub rather
        than refused: refusing it would break every existing test and any
        scripted client, and an unidentified client cannot displace a real one
        anyway, because it is not the same instance id.
        """
        try:
            identity = await client.request(Action.PING, {}, timeout=IDENTITY_TIMEOUT)
        except BlenderMCPError as exc:
            logger.info(
                "Client at %s did not identify itself (%s); accepting as anonymous", peer, exc.message
            )
            return None, None
        instance, previous = self._instances.adopt(identity, peer)
        if instance.status == REFUSED:
            logger.warning("Refusing %s: %s", instance.id, instance.reason)
        else:
            logger.info(
                "Adopted %s (pid %s, Blender %s, file %s)",
                instance.id,
                instance.pid,
                instance.version,
                instance.blend_file,
            )
        return instance, previous

    async def _refuse(
        self, websocket: ServerConnection, client: BlenderClient, instance: Any, reader: asyncio.Task[None]
    ) -> None:
        """Send the reason, then close, so the refusal is visible in Blender."""
        reader.cancel()
        client.fail_pending(
            BlenderMCPError(f"Connection refused: {instance.reason}", code=ErrorCode.CONNECTION_LOST)
        )
        try:
            await client.send_raw(encode_disconnect(instance.reason or "refused"))
            await websocket.close()
        except Exception:  # pragma: no cover - the peer may already be gone
            logger.debug("Could not deliver the refusal to %s", instance.id, exc_info=True)

    @property
    def instances(self) -> InstanceRegistry:
        return self._instances

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


def _address_of(websocket: ServerConnection) -> str:
    """Best available name for the peer, for logs and instance ids."""
    peer = getattr(websocket, "remote_address", None)
    if not peer:
        return "unknown"
    return ":".join(str(part) for part in peer[:2]) if isinstance(peer, tuple) else str(peer)
