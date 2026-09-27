"""Tests for the WebSocket transport, using a stand-in for the Blender add-on.

The add-on cannot be imported outside Blender, but its WebSocket client is plain
standard library, so the real client is driven against the real server here.
That covers the handshake, framing, request correlation, timeouts, disconnects
and malformed frames — everything except ``bpy`` itself.
"""

from __future__ import annotations

import asyncio
import json
import random
import socket
import threading
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest
from blender_mcp.protocol import encode_error, encode_response
from blender_mcp.websocket import WebSocket, WebSocketClosed, WebSocketError
from websockets.asyncio.server import Request, Response, ServerConnection, serve

from server.blender.connection import BlenderBridge
from server.blender.protocol import Action
from server.errors import BlenderMCPError, ErrorCode

pytestmark = pytest.mark.anyio

# Linux hands out ephemeral ports from 32768-60999; tests stay below that range
# so a port freed by one test is never immediately reused by another.
_PORT_BASE = 20000 + random.randint(0, 4000)
_next_port = iter(range(_PORT_BASE, _PORT_BASE + 4000))


def free_port() -> int:
    return next(_next_port)


async def wait_for(predicate: Callable[[], Any], timeout: float = 5.0) -> bool:
    """Wait for something a background socket thread has to do."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


#: What a real add-on answers to the identity ping the bridge sends on connect.
IDENTITY: dict[str, Any] = {
    "success": True,
    "pong": True,
    "pid": 4242,
    "blender_version": "4.2.0",
    "scene": "Scene",
    "blend_file": None,
}


class Peer:
    """A scripted protocol peer with no Blender behind it.

    Runs the add-on's real WebSocket client on a background thread and hands
    every inbound request to ``respond``, which returns the frame to send back
    or ``None`` to stay silent. Silence is how the timeout and disconnect cases
    are provoked.

    The bridge identifies a peer before handing it the socket, so the ping is
    answered here by default and kept out of ``requests``: a test asking what the
    bridge sent means the request it made, not the handshake. Pass
    ``answer_ping=False`` to see the handshake too, or ``identity`` to pretend to
    be a particular Blender.
    """

    def __init__(
        self,
        ws: WebSocket,
        respond: Callable[[dict[str, Any]], str | None],
        *,
        identity: dict[str, Any] | None = None,
        answer_ping: bool = True,
    ) -> None:
        self.ws = ws
        self._respond = respond
        self.identity = dict(IDENTITY if identity is None else identity)
        self.answer_ping = answer_ping
        self.requests: list[dict[str, Any]] = []
        self.control: list[dict[str, Any]] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> Peer:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self.ws.close()

    def _loop(self) -> None:
        self.ws.settimeout(0.2)
        while not self._stop.is_set():
            try:
                raw = self.ws.recv()
            except TimeoutError:
                continue
            except (WebSocketClosed, WebSocketError, OSError):
                return
            if raw is None:
                return
            message = json.loads(raw)
            if message.get("type") == "disconnect":
                # A server-side refusal, carrying the reason Blender would show.
                self.control.append(message)
                return
            if self.answer_ping and message.get("action") == "ping":
                self._send(encode_response(message["id"], self.identity))
                continue
            self.requests.append(message)
            answer = self._respond(message)
            if answer is not None:
                self._send(answer)

    def _send(self, frame: str) -> None:
        """Answer, unless the bridge has already hung up on us.

        Racy on purpose: a refusal closes the socket while the identity ping may
        still be in flight, and a thread that raises on shutdown would turn a
        passing test into a warning.
        """
        try:
            self.ws.send(frame)
        except (WebSocketClosed, WebSocketError, OSError):
            return


def constant(result: dict[str, Any]) -> Callable[[dict[str, Any]], str]:
    def respond(message: dict[str, Any]) -> str:
        return encode_response(message["id"], result)

    return respond


def silent(_: dict[str, Any]) -> None:
    return None


class AddonStub(Peer):
    """A :class:`Peer` that always answers with the same payload."""

    def __init__(
        self,
        ws: WebSocket,
        result: dict[str, Any] | None = None,
        *,
        respond: bool = True,
        identity: dict[str, Any] | None = None,
        answer_ping: bool = True,
    ) -> None:
        payload = {"success": True, "stub": True} if result is None else result
        super().__init__(
            ws, constant(payload) if respond else silent, identity=identity, answer_ping=answer_ping
        )


async def open_addon(port: int, **kwargs: Any) -> AddonStub:
    """Attach the add-on's real client from a background thread."""
    ws = await asyncio.to_thread(WebSocket.connect, "127.0.0.1", port, "/", 5.0)
    return AddonStub(ws, **kwargs).start()


async def open_named_addon(port: int, pid: int, **kwargs: Any) -> AddonStub:
    """Attach an add-on that claims to be a particular Blender process."""
    identity = {**IDENTITY, "pid": pid}
    return await open_addon(port, identity=identity, **kwargs)


async def open_peer(port: int, respond: Callable[[dict[str, Any]], str | None], **kwargs: Any) -> Peer:
    """Attach a scripted peer built on the add-on's real client."""
    ws = await asyncio.to_thread(WebSocket.connect, "127.0.0.1", port, "/", 5.0)
    return Peer(ws, respond, **kwargs).start()


@asynccontextmanager
async def open_client(port: int) -> AsyncIterator[WebSocket]:
    """A bare client socket, closed on the way out even if the test fails."""
    ws = await asyncio.to_thread(WebSocket.connect, "127.0.0.1", port, "/", 5.0)
    try:
        yield ws
    finally:
        await asyncio.to_thread(ws.close)


async def wait_until_connected(bridge: BlenderBridge, attempts: int = 150) -> None:
    for _ in range(attempts):
        if bridge.connected:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("the add-on never registered with the bridge")


def make_bridge(port: int, timeout: float = 5.0, *, allow_takeover: bool = False) -> BlenderBridge:
    return BlenderBridge("127.0.0.1", port, request_timeout=timeout, allow_takeover=allow_takeover)


async def echo_handler(websocket: ServerConnection) -> None:
    """Answers every request with the payload under ``stub_result``."""
    async for raw in websocket:
        request = json.loads(raw)
        await websocket.send(encode_response(request["id"], {"echoed": request["action"]}))


# --- the add-on's own WebSocket implementation -------------------------------


async def test_addon_client_completes_the_handshake_and_round_trips() -> None:
    port = free_port()
    seen: list[str] = []

    async def handler(websocket: ServerConnection) -> None:
        async for message in websocket:
            seen.append(message)
            await websocket.send(json.dumps({"echo": json.loads(message)}))

    async with serve(handler, "127.0.0.1", port):
        async with open_client(port) as ws:
            assert isinstance(ws, WebSocket)
            await asyncio.to_thread(ws.send, '"hello"')
            reply = await asyncio.to_thread(ws.recv)
            assert json.loads(reply) == {"echo": "hello"}
            assert seen == ['"hello"']


async def test_addon_client_reports_a_rejected_handshake() -> None:
    port = free_port()

    async def handler(websocket: ServerConnection) -> None:  # pragma: no cover
        await websocket.close()

    def refuse(connection: ServerConnection, request: Request) -> Response:
        return connection.respond(403, "Forbidden")

    async with serve(handler, "127.0.0.1", port, process_request=refuse):
        with pytest.raises(WebSocketError, match="Handshake rejected"):
            await asyncio.to_thread(WebSocket.connect, "127.0.0.1", port, "/", 5.0)


async def test_addon_client_reports_a_refused_port() -> None:
    with pytest.raises(WebSocketError, match="Could not connect"):
        await asyncio.to_thread(WebSocket.connect, "127.0.0.1", free_port(), "/", 1.0)


async def test_addon_client_sends_every_length_class() -> None:
    """RFC 6455 requires client frames to be masked, and the server enforces it."""
    port = free_port()

    async def handler(websocket: ServerConnection) -> None:
        async for message in websocket:
            await websocket.send(str(len(message)))

    async with serve(handler, "127.0.0.1", port):
        async with open_client(port) as ws:
            for size in (10, 300, 70000):  # 7-bit, 16-bit and 64-bit length paths
                await asyncio.to_thread(ws.send, "x" * size)
                assert await asyncio.to_thread(ws.recv) == str(size)


async def test_addon_client_answers_pings_and_keeps_reading() -> None:
    port = free_port()
    pongs: list[float] = []

    async def handler(websocket: ServerConnection) -> None:
        # The pong waiter resolves only once the peer's pong frame arrives, so
        # this asserts the client really answered the control frame.
        latency_waiter = await websocket.ping(b"ping-payload")
        pongs.append(await asyncio.wait_for(latency_waiter, timeout=5.0))
        await websocket.send("after-ping")

    async with serve(handler, "127.0.0.1", port, ping_interval=None):
        async with open_client(port) as ws:
            assert await asyncio.to_thread(ws.recv) == "after-ping"
            assert len(pongs) == 1
            assert pongs[0] >= 0.0


async def test_addon_client_sees_a_close_frame_as_end_of_stream() -> None:
    port = free_port()

    async def handler(websocket: ServerConnection) -> None:
        await websocket.close(1000, "done")

    async with serve(handler, "127.0.0.1", port):
        async with open_client(port) as ws:
            assert await asyncio.to_thread(ws.recv) is None


async def test_addon_client_surfaces_a_reset_socket() -> None:
    port = free_port()
    connected = asyncio.Event()

    async def handler(websocket: ServerConnection) -> None:
        connected.set()
        async for _ in websocket:
            pass

    async with serve(handler, "127.0.0.1", port):
        ws = await asyncio.to_thread(WebSocket.connect, "127.0.0.1", port, "/", 5.0)
        try:
            await asyncio.wait_for(connected.wait(), timeout=5.0)
            await asyncio.to_thread(ws.sock.shutdown, socket.SHUT_RDWR)
            with pytest.raises((WebSocketClosed, OSError)):
                await asyncio.to_thread(ws.recv)
        finally:
            await asyncio.to_thread(ws.close)


# --- the bridge --------------------------------------------------------------


async def test_request_reaches_the_addon_and_the_result_comes_back() -> None:
    port = free_port()
    bridge = make_bridge(port)
    await bridge.start()
    try:
        stub = await open_addon(port, result={"success": True, "objects_count": 2})
        await wait_until_connected(bridge)

        assert await bridge.request(Action.GET_SCENE) == {"success": True, "objects_count": 2}
        assert stub.requests[0]["action"] == "get_scene"
        assert stub.requests[0]["id"]
        assert bridge.status()["connected"] is True
        stub.stop()
    finally:
        await bridge.stop()


async def test_request_params_travel_intact() -> None:
    port = free_port()
    bridge = make_bridge(port)
    await bridge.start()
    try:
        stub = await open_addon(port)
        await wait_until_connected(bridge)
        params = {"name": "Table", "location": [0.0, 0.0, 1.0], "rotation": [0.0, 0.0, 90.0]}
        await bridge.request(Action.UPDATE_OBJECT, params)
        assert stub.requests[0]["params"] == params
        stub.stop()
    finally:
        await bridge.stop()


async def test_request_without_a_connection_fails_with_not_connected() -> None:
    port = free_port()
    bridge = make_bridge(port)
    await bridge.start()
    try:
        with pytest.raises(BlenderMCPError) as excinfo:
            await bridge.request(Action.GET_SCENE)
        assert excinfo.value.code is ErrorCode.BLENDER_NOT_CONNECTED
    finally:
        await bridge.stop()


async def test_error_responses_are_turned_back_into_exceptions() -> None:
    port = free_port()
    bridge = make_bridge(port)
    await bridge.start()
    try:

        def refuse(message: dict[str, Any]) -> str:
            return encode_error(message["id"], "OBJECT_NOT_FOUND", "no such object")

        peer = await open_peer(port, refuse)
        await wait_until_connected(bridge)
        with pytest.raises(BlenderMCPError) as excinfo:
            await bridge.request(Action.GET_OBJECT, {"name": "Chair"})
        assert excinfo.value.code is ErrorCode.OBJECT_NOT_FOUND
        assert excinfo.value.message == "no such object"
        peer.stop()
    finally:
        await bridge.stop()


async def test_a_silent_addon_times_out() -> None:
    port = free_port()
    bridge = make_bridge(port, timeout=0.2)
    await bridge.start()
    try:
        stub = await open_addon(port, respond=False)
        await wait_until_connected(bridge)
        with pytest.raises(BlenderMCPError) as excinfo:
            await bridge.request(Action.GET_SCENE)
        assert excinfo.value.code is ErrorCode.TIMEOUT
        stub.stop()
    finally:
        await bridge.stop()


async def test_a_dropped_addon_fails_pending_requests() -> None:
    port = free_port()
    bridge = make_bridge(port, timeout=10.0)
    await bridge.start()
    try:
        stub = await open_addon(port, respond=False)
        await wait_until_connected(bridge)
        pending = asyncio.create_task(bridge.request(Action.GET_SCENE))
        await asyncio.sleep(0.2)
        stub.stop()
        with pytest.raises(BlenderMCPError) as excinfo:
            await pending
        assert excinfo.value.code is ErrorCode.CONNECTION_LOST
    finally:
        await bridge.stop()


async def test_a_malformed_frame_does_not_break_the_connection() -> None:
    """Garbage from the add-on is logged and dropped; the next request still works."""
    port = free_port()
    bridge = make_bridge(port, timeout=2.0)
    await bridge.start()
    try:
        seen: list[dict[str, Any]] = []

        def sloppy(message: dict[str, Any]) -> str:
            if message.get("action") == "ping":
                return encode_response(message["id"], IDENTITY)
            seen.append(message)
            if len(seen) == 1:
                return "{not json"
            return encode_response(message["id"], {"recovered": True})

        # The script answers the identity handshake itself, so every frame the
        # bridge gets after that is one this test provoked.
        peer = await open_peer(port, sloppy, answer_ping=False)
        await wait_until_connected(bridge)
        # The first request times out because the answer is unparsable...
        with pytest.raises(BlenderMCPError) as first:
            await bridge.request(Action.GET_SCENE)
        assert first.value.code is ErrorCode.TIMEOUT
        # ...and the connection is still usable afterwards.
        assert await bridge.request(Action.GET_SCENE) == {"recovered": True}
        peer.stop()
    finally:
        await bridge.stop()


async def test_a_second_blender_is_refused_with_a_reason() -> None:
    """A takeover is not silent: the newcomer is told why, and told to stop."""
    port = free_port()
    bridge = make_bridge(port, timeout=5.0)
    await bridge.start()
    try:
        first = await open_named_addon(port, 1001, result={"which": "first"})
        await wait_until_connected(bridge)
        second = await open_named_addon(port, 1002, result={"which": "second"})

        assert await wait_for(lambda: second.control), "the second Blender was never told why"
        reason = second.control[0]["reason"]
        assert "already connected" in reason
        assert "BLENDER_ALLOW_TAKEOVER" in reason

        # The first Blender keeps the socket: its calls still land in its scene.
        assert await bridge.request(Action.GET_SCENE) == {"which": "first"}
        assert bridge.instances.active is not None
        assert bridge.instances.active.pid == 1001

        first.stop()
        second.stop()
    finally:
        await bridge.stop()


async def test_a_refused_blender_is_remembered_as_refused() -> None:
    """The refusal is on the record, which is how a mystery scene gets explained."""
    port = free_port()
    bridge = make_bridge(port)
    await bridge.start()
    try:
        first = await open_named_addon(port, 2001)
        await wait_until_connected(bridge)
        second = await open_named_addon(port, 2002)
        assert await wait_for(lambda: second.control)

        described = bridge.instances.describe()
        assert described["active"] is not None
        assert described["active"]["pid"] == 2001
        assert described["takeover_allowed"] is False
        statuses = {entry["id"]: entry["status"] for entry in described["instances"]}
        assert statuses == {"blender-2001": "active", "blender-2002": "refused"}

        first.stop()
        second.stop()
    finally:
        await bridge.stop()


async def test_a_reconnect_of_the_same_blender_is_not_a_takeover() -> None:
    """Reconnecting is normal, so it is allowed; only a different process is not."""
    port = free_port()
    bridge = make_bridge(port, timeout=5.0)
    await bridge.start()
    try:
        first = await open_named_addon(port, 3001, result={"which": "first"})
        await wait_until_connected(bridge)
        first.stop()

        again = await open_named_addon(port, 3001, result={"which": "first-again"})
        for _ in range(100):
            if await bridge.request(Action.GET_SCENE) == {"which": "first-again"}:
                break
            await asyncio.sleep(0.05)
        else:  # pragma: no cover - only on a very slow machine
            pytest.fail("the same Blender could not reconnect")

        assert again.control == []
        assert bridge.instances.active is not None
        assert bridge.instances.active.pid == 3001
        again.stop()
    finally:
        await bridge.stop()


async def test_takeover_is_allowed_when_asked_for() -> None:
    """BLENDER_ALLOW_TAKEOVER=1 is the escape hatch for a deliberate switch."""
    port = free_port()
    bridge = make_bridge(port, timeout=5.0, allow_takeover=True)
    await bridge.start()
    try:
        first = await open_named_addon(port, 4001, result={"which": "first"})
        await wait_until_connected(bridge)
        second = await open_named_addon(port, 4002, result={"which": "second"})

        for _ in range(100):
            if await bridge.request(Action.GET_SCENE) == {"which": "second"}:
                break
            await asyncio.sleep(0.05)
        else:  # pragma: no cover - only on a very slow machine
            pytest.fail("the second add-on never took over")

        described = bridge.instances.describe()
        statuses = {entry["id"]: entry["status"] for entry in described["instances"]}
        assert statuses == {"blender-4001": "replaced", "blender-4002": "active"}

        first.stop()
        second.stop()
    finally:
        await bridge.stop()


async def test_a_peer_that_will_not_identify_is_still_accepted() -> None:
    """A client that cannot answer the ping is anonymous, not rejected.

    Scripted clients and test doubles are legitimate users of the bridge, and an
    anonymous client cannot displace a real Blender anyway: it has no pid, so it
    never matches an identified instance.
    """
    port = free_port()
    bridge = make_bridge(port, timeout=5.0)
    await bridge.start()
    try:
        peer = await open_addon(port, respond=False, answer_ping=False)
        for _ in range(200):
            if bridge.instances.active is None and any(
                frame.get("action") == "ping" for frame in peer.requests
            ):
                break
            await asyncio.sleep(0.05)
        assert bridge.instances.active is None
        peer.stop()
    finally:
        await bridge.stop()


async def test_bridge_status_reports_the_endpoint() -> None:
    port = free_port()
    bridge = make_bridge(port)
    assert bridge.status() == {
        "connected": False,
        "url": f"ws://127.0.0.1:{port}",
        "pending_requests": 0,
    }
    await bridge.start()
    try:
        assert bridge.connected is False
        stub = await open_addon(port)
        await wait_until_connected(bridge)
        assert bridge.status()["connected"] is True
        stub.stop()
    finally:
        await bridge.stop()


async def test_the_bridge_ignores_frames_it_cannot_correlate() -> None:
    """A request-shaped frame from the add-on is dropped, not answered.

    The bridge only ever expects responses, so an unsolicited or unparsable frame
    is logged and discarded: it has no id to answer and must not disturb the
    requests that are in flight.
    """
    port = free_port()
    bridge = make_bridge(port, timeout=2.0)
    await bridge.start()
    try:
        seen: list[dict[str, Any]] = []

        def chatty(message: dict[str, Any]) -> str:
            if message.get("action") == "ping":
                return encode_response(message["id"], IDENTITY)
            seen.append(message)
            if len(seen) == 1:
                return json.dumps({"id": "wrong-id", "action": "get_scene", "params": {}})
            return encode_response(message["id"], {"fine": True})

        peer = await open_peer(port, chatty, answer_ping=False)
        await wait_until_connected(bridge)
        with pytest.raises(BlenderMCPError) as first:
            await bridge.request(Action.GET_SCENE)
        assert first.value.code is ErrorCode.TIMEOUT
        assert await bridge.request(Action.GET_SCENE) == {"fine": True}
        peer.stop()
    finally:
        await bridge.stop()
