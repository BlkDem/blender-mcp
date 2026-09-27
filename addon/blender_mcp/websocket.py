"""Minimal RFC 6455 WebSocket client, standard library only.

Blender's bundled Python has no ``websockets`` package and this add-on must not
depend on one: an add-on that needs a pip install into Blender's own interpreter
is a support burden nobody wants. ``socket``, ``base64``, ``hashlib`` and
``struct`` are all that is required to speak ``ws://`` to our own server, and
that is exactly what this module implements.

Scope, deliberately: plaintext ``ws://`` only (no TLS), text frames, ping/pong,
close, and continuation frames. No extensions are negotiated, so the server's
``permessage-deflate`` is never activated for this connection.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import socket
import struct
from dataclasses import dataclass
from typing import Final

logger = logging.getLogger(__name__)

_GUID: Final = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONTINUATION: Final = 0x0
OP_TEXT: Final = 0x1
OP_BINARY: Final = 0x2
OP_CLOSE: Final = 0x8
OP_PING: Final = 0x9
OP_PONG: Final = 0xA

_MAX_FRAME_BYTES: Final = 32 * 1024 * 1024


class WebSocketError(Exception):
    """Handshake or framing failure. Always fatal for the connection."""


class WebSocketClosed(WebSocketError):
    """The peer closed the connection."""


@dataclass(slots=True)
class WebSocket:
    """A connected, text-framed WebSocket.

    Not thread-safe: one reader thread owns ``recv`` and one writer thread owns
    ``send``. :mod:`blender_mcp.connection` is the only user and it enforces
    that.
    """

    sock: socket.socket
    _read_buffer: bytearray

    # --- lifecycle ---------------------------------------------------------

    @classmethod
    def connect(cls, host: str, port: int, path: str = "/", timeout: float = 5.0) -> WebSocket:
        """Open a socket and perform the opening handshake."""
        try:
            sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as exc:
            raise WebSocketError(f"Could not connect to {host}:{port}: {exc}") from exc
        sock.settimeout(timeout)
        try:
            leftover = cls._handshake(sock, host, port, path)
        except Exception:
            sock.close()
            raise
        return cls(sock=sock, _read_buffer=bytearray(leftover))

    @staticmethod
    def _handshake(sock: socket.socket, host: str, port: int, path: str) -> bytes:
        """Perform the handshake and return any bytes already read past the header."""
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        sock.sendall(request.encode("ascii"))

        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                raise WebSocketError("Server closed the connection during the handshake")
            response += chunk
            if len(response) > 64 * 1024:
                raise WebSocketError("Handshake response header is implausibly large")

        header_blob, _, remainder = response.partition(b"\r\n\r\n")
        headers = WebSocket._parse_headers(header_blob)
        status = header_blob.split(b"\r\n", 1)[0].decode("latin-1")
        if "101" not in status:
            raise WebSocketError(f"Handshake rejected: {status}")
        expected = base64.b64encode(hashlib.sha1((key + _GUID).encode("ascii")).digest()).decode("ascii")
        if headers.get("sec-websocket-accept") != expected:
            raise WebSocketError("Server returned a bad Sec-WebSocket-Accept")
        return remainder

    @staticmethod
    def _parse_headers(blob: bytes) -> dict[str, str]:
        headers: dict[str, str] = {}
        for line in blob.decode("latin-1").split("\r\n")[1:]:
            name, _, value = line.partition(":")
            if name:
                headers[name.strip().lower()] = value.strip()
        return headers

    def close(self) -> None:
        """Send a close frame (best effort) and drop the socket."""
        try:
            self.send_frame(OP_CLOSE, b"")
        except Exception:
            logger.debug("Close frame could not be sent", exc_info=True)
        try:
            self.sock.close()
        except Exception:  # pragma: no cover - already closed
            logger.debug("Socket close failed", exc_info=True)

    def settimeout(self, timeout: float | None) -> None:
        self.sock.settimeout(timeout)

    # --- reading -----------------------------------------------------------

    def _read_exactly(self, count: int) -> bytes:
        while len(self._read_buffer) < count:
            try:
                chunk = self.sock.recv(65536)
            except TimeoutError as exc:
                raise TimeoutError("socket read timed out") from exc
            except OSError as exc:
                raise WebSocketClosed(str(exc)) from exc
            if not chunk:
                raise WebSocketClosed("peer closed the connection")
            self._read_buffer.extend(chunk)
        data = bytes(self._read_buffer[:count])
        del self._read_buffer[:count]
        return data

    def _read_frame(self) -> tuple[int, bool, bytes]:
        """Read one frame, returning ``(opcode, fin, payload)``."""
        header = self._read_exactly(2)
        fin = bool(header[0] & 0x80)
        opcode = header[0] & 0x0F
        masked = bool(header[1] & 0x80)
        length = header[1] & 0x7F

        if length == 126:
            (length,) = struct.unpack("!H", self._read_exactly(2))
        elif length == 127:
            (length,) = struct.unpack("!Q", self._read_exactly(8))
        if length > _MAX_FRAME_BYTES:
            raise WebSocketError(f"Frame of {length} bytes exceeds the {_MAX_FRAME_BYTES} byte limit")
        if masked:  # servers must not mask
            raise WebSocketError("Server sent a masked frame, which RFC 6455 forbids")

        return opcode, fin, self._read_exactly(length)

    def recv(self) -> str | None:
        """Return the next text message, or ``None`` after a close frame.

        Control frames are handled transparently: a ping is answered with a pong
        and a close is answered and reported as ``None``.
        """
        fragments: list[bytes] = []
        message_opcode: int | None = None
        while True:
            opcode, fin, payload = self._read_frame()

            if opcode == OP_CLOSE:
                self._reply_close(payload)
                return None
            if opcode == OP_PING:
                self.send_frame(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode in (OP_TEXT, OP_BINARY):
                if message_opcode is not None:
                    raise WebSocketError("Interleaved data frames are not allowed")
                message_opcode = opcode
            elif opcode == OP_CONTINUATION:
                if message_opcode is None:
                    raise WebSocketError("Continuation frame without a start frame")
            else:
                raise WebSocketError(f"Unknown opcode 0x{opcode:x}")

            fragments.append(payload)
            if fin:
                # The bridge protocol is JSON text; a binary frame from the
                # server would be a protocol error upstream, so decode leniently
                # here and let the protocol layer reject the content.
                return b"".join(fragments).decode("utf-8", errors="replace")

    def _reply_close(self, payload: bytes) -> None:
        try:
            self.send_frame(OP_CLOSE, payload[:2] or struct.pack("!H", 1000))
        except Exception:  # pragma: no cover - peer already gone
            logger.debug("Could not echo the close frame", exc_info=True)

    # --- writing -----------------------------------------------------------

    def send(self, message: str) -> None:
        """Send one text message (a single, unmasked-as-required client frame)."""
        self.send_frame(OP_TEXT, message.encode("utf-8"))

    def send_frame(self, opcode: int, payload: bytes) -> None:
        """Send one frame, masked as the client side of RFC 6455 must."""
        if len(payload) > _MAX_FRAME_BYTES:
            raise WebSocketError(f"Payload of {len(payload)} bytes is too large to send")
        header = bytearray()
        header.append(0x80 | opcode)  # always FIN: messages are sent whole
        mask_bit = 0x80
        length = len(payload)
        if length < 126:
            header.append(mask_bit | length)
        elif length < 65536:
            header.append(mask_bit | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(mask_bit | 127)
            header.extend(struct.pack("!Q", length))

        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        try:
            self.sock.sendall(bytes(header) + masked)
        except OSError as exc:
            raise WebSocketClosed(str(exc)) from exc
