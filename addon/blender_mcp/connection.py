"""Add-on side of the bridge: connect, dispatch onto the main thread, answer.

Threading is the whole difficulty here. ``bpy`` may only be touched from
Blender's main thread, but a socket read blocks, so the connection lives on a
background thread. The arrangement is:

    reader thread  --reads frame-->  _inbox queue
                                            |
    bpy.app.timers  --drains inbox--> main thread runs the action
                                            |
    _outbox queue  <--enqueue response--
                                            |
    reader thread  --writes frame-->  socket

The timer callback is what guarantees the ``bpy`` calls happen on the main
thread; everything the reader thread does is socket I/O and queue juggling. The
timer is registered from :meth:`BlenderConnection.connect`, which the UI calls
on the main thread, and unregisters itself once there is nothing left to do.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any

import bpy  # type: ignore[import-not-found]

from blender_mcp import operators, protocol
from blender_mcp.protocol import ActionError
from blender_mcp.websocket import WebSocket, WebSocketClosed, WebSocketError

logger = logging.getLogger(__name__)

#: Socket read timeout, in seconds. Short enough to notice a stop request
#: promptly, long enough not to spin.
_POLL_INTERVAL = 0.2

#: Timer period, in seconds. Blender runs timers once per frame; this keeps the
#: add-on responsive without a busy loop.
_TIMER_INTERVAL = 0.01

#: Reconnect backoff, in seconds.
RECONNECT_BACKOFF = (1.0, 2.0, 5.0, 10.0, 30.0)


class TransactionState:
    """Undo bookkeeping for one open transaction.

    Blender's undo stack is the rollback mechanism, so rollback means "undo
    exactly as many steps as we pushed". That count is only correct if nothing
    else pushed undo steps in between, which is why transactions are meant to
    be short-lived.
    """

    __slots__ = ("active", "steps")

    def __init__(self) -> None:
        self.active: bool = False
        self.steps: int = 0

    def begin(self) -> None:
        if self.active:
            raise ActionError(
                protocol.TRANSACTION_ACTIVE,
                "A transaction is already open; commit or roll it back first",
            )
        self.active = True
        self.steps = 0

    def commit(self) -> int:
        if not self.active:
            raise ActionError(protocol.TRANSACTION_NOT_ACTIVE, "No transaction is open")
        steps = self.steps
        self.active = False
        self.steps = 0
        return steps

    def rollback(self) -> int:
        """Undo every recorded step. Returns how many were undone."""
        if not self.active:
            raise ActionError(protocol.TRANSACTION_NOT_ACTIVE, "No transaction is open")
        recorded = self.steps
        self.active = False
        self.steps = 0
        undone = 0
        for _ in range(recorded):
            try:
                bpy.ops.ed.undo()
            except RuntimeError as exc:  # pragma: no cover - undo stack exhausted
                raise ActionError(
                    protocol.BLENDER_OPERATION_FAILED,
                    f"Undo stopped after {undone} of {recorded} step(s): {exc}",
                ) from exc
            undone += 1
        return undone


def _push_undo(message: str) -> None:
    """Add one entry to Blender's undo stack.

    Undo cannot be pushed in every context (a global-undo-disabled file, a timer
    callback in some builds), so a failure is logged and swallowed: losing
    rollback is better than failing the operation the user asked for.
    """
    try:
        bpy.ops.ed.undo_push(message=message)
    except Exception:  # pragma: no cover - build and file dependent
        logger.warning("Could not push an undo step for %r", message, exc_info=True)


class BlenderConnection:
    """Owns the socket, the worker thread and the main-thread dispatch loop."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8765, *, auto_reconnect: bool = True) -> None:
        self.host = host
        self.port = port
        self.auto_reconnect = auto_reconnect

        self._ws: WebSocket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._inbox: queue.Queue[tuple[str, str, dict[str, Any]]] = queue.Queue()
        self._outbox: queue.Queue[str] = queue.Queue()
        self._timer_registered = False
        self._lock = threading.Lock()
        self._transactions = TransactionState()

        self.status: str = "Disconnected"
        self.last_error: str | None = None
        self.connected_since: float | None = None
        self._attempt = 0

    # --- state -------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and self._thread is not None and self._thread.is_alive()

    @property
    def url(self) -> str:
        return f"ws://{self.host}:{self.port}"

    @property
    def transaction_open(self) -> bool:
        return self._transactions.active

    def info(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "connected": self.is_connected,
            "server": self.url,
            "auto_reconnect": self.auto_reconnect,
            "last_error": self.last_error,
            "transaction_open": self.transaction_open,
            "connected_since": self.connected_since,
        }

    # --- lifecycle ---------------------------------------------------------

    def connect(self) -> bool:
        """Start the connection. Returns immediately; the worker thread dials."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self.auto_reconnect = True
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="blender-mcp-connection", daemon=True)
            self._thread.start()
        self.start_timer()
        return True

    def disconnect(self) -> None:
        """Close the connection and stop reconnecting."""
        self._stop.set()
        self.auto_reconnect = False
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._thread = None
        self._close_socket()
        self._set_status("Disconnected")
        # Ask the pump to retire itself on its next pass rather than unregistering
        # from here: disconnect() may be called from either thread.
        logger.info("Disconnected from MCP server %s", self.url)

    def set_server(self, host: str, port: int) -> None:
        """Point at a different server, reconnecting if currently connected."""
        if (host, port) == (self.host, self.port):
            return
        was_connected = self.is_connected
        if was_connected:
            self.disconnect()
        self.host = host
        self.port = port
        if was_connected:
            self.connect()

    # --- worker thread -----------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._ws = WebSocket.connect(self.host, self.port, timeout=2.0)
            except (WebSocketError, OSError) as exc:
                if not self._retry(f"Cannot reach {self.url}: {exc}"):
                    return
                continue

            self._attempt = 0
            self.connected_since = time.time()
            self._set_status("Connected")
            logger.info("Connected to MCP server %s", self.url)
            try:
                self._serve()
            except (WebSocketClosed, WebSocketError, OSError) as exc:
                self._set_status(f"Connection lost: {exc}")
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception("Unexpected error in the connection thread")
                self._set_status(f"Error: {exc}")
            finally:
                self.connected_since = None
                self._close_socket()
                self._set_status("Disconnected")
            if not self._retry("Reconnecting"):
                return

    def _retry(self, reason: str) -> bool:
        """Log why, wait, and report whether another attempt should be made."""
        logger.info("%s", reason)
        if not self.auto_reconnect or self._stop.is_set():
            return False
        delay = RECONNECT_BACKOFF[min(self._attempt, len(RECONNECT_BACKOFF) - 1)]
        self._attempt += 1
        self._set_status(f"{reason} (retrying in {delay:g}s)")
        return not self._stop.wait(delay)

    def _serve(self) -> None:
        """Read frames until the socket closes, flushing answers as they appear."""
        assert self._ws is not None
        self._ws.settimeout(_POLL_INTERVAL)
        while not self._stop.is_set():
            self._flush_outbox()
            try:
                raw = self._ws.recv()
            except TimeoutError:
                continue
            if raw is None:
                raise WebSocketClosed("server closed the connection")
            self._handle_frame(raw)

    def _flush_outbox(self) -> None:
        if self._ws is None:
            return
        while True:
            try:
                message = self._outbox.get_nowait()
            except queue.Empty:
                return
            self._ws.send(message)

    def _handle_frame(self, raw: str) -> None:
        try:
            request_id, action, params = protocol.parse_request(raw)
        except protocol.ProtocolError as exc:
            logger.warning("Ignoring unparsable frame: %s", exc)
            return
        self._inbox.put((request_id, action, params))

    def _close_socket(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            ws.close()
        # Answers for requests that died with the socket are worthless: their
        # ids will never be matched again.
        while True:
            try:
                self._outbox.get_nowait()
            except queue.Empty:
                return

    def _set_status(self, status: str) -> None:
        self.status = status
        self.last_error = None if status == "Connected" else status

    # --- main thread -------------------------------------------------------

    def drain(self) -> float | None:
        """Run every queued action on the calling thread.

        Public because it is the seam a driver needs when Blender has no event
        loop to run timers: under ``--background`` the timer never fires, so a
        script calls this directly instead. Returns the delay before the next
        pass, or ``None`` when there is nothing left to do.
        """
        while True:
            try:
                request_id, action, params = self._inbox.get_nowait()
            except queue.Empty:
                break
            self._outbox.put(self._execute(request_id, action, params))

        if (self._stop.is_set() or not self.auto_reconnect) and self._inbox.empty():
            self._timer_registered = False
            return None
        return _TIMER_INTERVAL

    def start_timer(self) -> None:
        """Register the main-thread pump. Must be called from the main thread."""
        if self._timer_registered:
            return
        try:
            bpy.app.timers.register(self._tick, first_interval=_TIMER_INTERVAL)
            self._timer_registered = True
        except Exception:  # pragma: no cover - interpreter shutdown
            logger.warning("Could not register the dispatch timer", exc_info=True)

    def stop_timer(self) -> None:
        """Unregister the pump. Must be called from the main thread."""
        if not self._timer_registered:
            return
        try:
            bpy.app.timers.unregister(self._tick)
        except Exception:  # pragma: no cover - already unregistered
            logger.debug("Dispatch timer was not registered", exc_info=True)
        self._timer_registered = False

    def _tick(self) -> float | None:
        return self.drain()

    def _execute(self, request_id: str, action: str, params: dict[str, Any]) -> str:
        """Execute one action on the main thread and return the response frame."""
        try:
            result = self.run_action(action, params)
        except ActionError as exc:
            logger.info("Action %s rejected: %s", action, exc)
            return protocol.encode_error(request_id, exc.code, exc.message, exc.details)
        except Exception as exc:  # noqa: BLE001 - any bpy failure must reach the MCP server
            logger.exception("Action %s failed", action)
            return protocol.encode_exception(request_id, exc)
        logger.debug("Action %s completed", action)
        return protocol.encode_response(request_id, result)

    def run_action(self, action: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run one action, with the transaction and undo bookkeeping around it.

        Public because it is the seam a Blender script should use to drive the
        add-on without a socket: the timer callback below calls exactly this.
        Must run on the main thread.
        """
        params = params or {}
        if action == protocol.BEGIN_TRANSACTION:
            self._transactions.begin()
            _push_undo("MCP transaction begin")
            return {"success": True, "transaction": "open"}

        if action == protocol.COMMIT_TRANSACTION:
            steps = self._transactions.commit()
            _push_undo("MCP transaction commit")
            return {"success": True, "transaction": "committed", "undo_steps": steps}

        if action == protocol.ROLLBACK_TRANSACTION:
            undone = self._transactions.rollback()
            bpy.context.view_layer.update()
            return {"success": True, "transaction": "rolled_back", "undo_steps": undone}

        mutating = action in protocol.MUTATING_ACTIONS
        result = operators.dispatch(action, params)
        if mutating:
            if self._transactions.active:
                self._transactions.steps += 1
            _push_undo(f"MCP {action}")
        return result
