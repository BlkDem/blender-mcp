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
from collections import deque
from dataclasses import dataclass
from dataclasses import field as dataclass_field
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


class ObjectState:
    """The part of an object that the tool layer can change, and how to put it back.

    Everything here is a property this add-on sets: transform, name, visibility,
    material slots, collection membership. Mesh *data* is referenced by name
    rather than copied, so a deleted object's mesh survives in ``bpy.data`` long
    enough to be relinked — as long as the file has not been saved and purged
    in between, which :meth:`restore` reports rather than hides.
    """

    __slots__ = (
        "existed",
        "name",
        "data_name",
        "data_type",
        "location",
        "rotation",
        "scale",
        "materials",
        "hide_viewport",
        "hide_render",
        "collections",
        "parent",
    )

    def __init__(self, **fields: Any) -> None:
        for slot in self.__slots__:
            setattr(self, slot, fields.get(slot))

    @classmethod
    def capture(cls, name: str) -> ObjectState:
        obj = bpy.data.objects.get(name)
        if obj is None:
            # Remember the absence, so a rollback removes an object that did not
            # exist when the transaction touched it.
            return cls(existed=False, name=name)
        data = getattr(obj, "data", None)
        return cls(
            existed=True,
            name=obj.name,
            data_name=getattr(data, "name", None),
            data_type=type(data).__name__ if data is not None else None,
            location=tuple(obj.location),
            rotation=tuple(obj.rotation_euler),
            scale=tuple(obj.scale),
            materials=[slot.name if slot else None for slot in getattr(data, "materials", [])],
            hide_viewport=bool(obj.hide_viewport),
            hide_render=bool(obj.hide_render),
            collections=[collection.name for collection in obj.users_collection],
            parent=obj.parent.name if obj.parent else None,
        )

    def restore(self) -> bool:
        """Put the object back. ``False`` means it could not be fully restored."""
        if not self.existed:
            obj = bpy.data.objects.get(self.name)
            if obj is None:
                return True  # already gone, which is the state we wanted
            bpy.data.objects.remove(obj, do_unlink=True)
            return True

        obj = bpy.data.objects.get(self.name)
        if obj is None:
            obj = self._recreate()
            if obj is None:
                return False

        obj.location = self.location
        obj.rotation_euler = self.rotation
        obj.scale = self.scale
        obj.hide_viewport = self.hide_viewport
        obj.hide_render = self.hide_render

        data = getattr(obj, "data", None)
        if data is not None and hasattr(data, "materials"):
            data.materials.clear()
            for material_name in self.materials or []:
                material = bpy.data.materials.get(material_name) if material_name else None
                data.materials.append(material)

        for collection_name in self.collections or []:
            collection = bpy.data.collections.get(collection_name)
            if collection is not None and collection.objects.get(self.name) is None:
                collection.objects.link(obj)

        parent = bpy.data.objects.get(self.parent) if self.parent else None
        if obj.parent is not parent:
            obj.parent = parent
        return True

    def _recreate(self) -> Any:
        """Rebuild a deleted object from its recorded data-block reference."""
        data = None
        if self.data_name and self.data_type:
            collection = getattr(bpy.data, _DATA_COLLECTIONS.get(self.data_type, ""), None)
            if collection is not None:
                data = collection.get(self.data_name)
        if data is None:
            # The mesh was purged along with the object; a new empty one keeps
            # the scene structurally valid, and the caller is told it happened.
            logger.warning(
                "Could not relink %r: its %s data is gone; recreated empty",
                self.name,
                self.data_type,
            )
            data = bpy.data.meshes.new(f"{self.name}Mesh")
        obj = bpy.data.objects.new(self.name, data)
        target = bpy.context.scene.collection
        for collection_name in self.collections or []:
            found = bpy.data.collections.get(collection_name)
            if found is not None:
                target = found
                break
        target.objects.link(obj)
        return bpy.data.objects.get(self.name)


#: ``Object.type`` -> the ``bpy.data`` collection its data-block lives in.
_DATA_COLLECTIONS = {
    "Mesh": "meshes",
    "Curve": "curves",
    "Surface": "curves",
    "Meta": "metaballs",
    "Font": "fonts",
    "Lattice": "lattices",
    "Camera": "cameras",
    "Light": "lights",
    "Armature": "armatures",
    "Empty": "objects",
}


@dataclass(slots=True)
class Checkpoint:
    """A named point a transaction can be rewound to."""

    label: str
    states: dict[str, ObjectState] = dataclass_field(default_factory=dict)


#: Mutating actions that leave the scene as it was, so they are not changes a
#: client would be waiting for.
_SCENE_NEUTRAL_ACTIONS = frozenset(
    {protocol.BEGIN_TRANSACTION, protocol.CHECKPOINT, protocol.COMMIT_TRANSACTION}
)

#: Which object names a mutating action can change. Used to decide what a
#: transaction has to record before the change happens.
_TOUCHED_BY_ACTION: dict[str, tuple[str, ...]] = {
    protocol.CREATE_OBJECT: ("name",),
    protocol.UPDATE_OBJECT: ("name", "new_name"),
    protocol.DELETE_OBJECT: ("name",),
}


def _touched_objects(action: str, params: dict[str, Any]) -> list[str]:
    """Object names a mutating action may change, including the result of a rename.

    A rename touches two names: the object as it is now, and the name it is
    leaving, which is what a rollback has to find.
    """
    names = []
    for field in _TOUCHED_BY_ACTION.get(action, ()):
        value = params.get(field)
        if isinstance(value, str) and value:
            names.append(value)
    return names


class TransactionState:
    """Undo bookkeeping for one open transaction.

    Rollback does **not** count undo steps. Blender's undo history is not
    readable from Python — there is no ``undo_history`` attribute to walk — so a
    count of steps is only right when nothing else touched the undo stack, which
    a user editing in the UI certainly does.

    Instead this records the state of every object the transaction touches,
    *before* the change, and puts it back on rollback. That is exact for the tool
    layer no matter what happens in between. It is not a full undo: mesh edits
    made through ``execute_python`` are outside what is recorded, and
    :func:`rollback` says so in its result rather than pretending otherwise.

    Blender's own undo steps are still pushed, so a user can keep using Ctrl-Z.
    """

    __slots__ = ("active", "steps", "checkpoints")

    def __init__(self) -> None:
        self.active: bool = False
        self.steps: int = 0
        self.checkpoints: list[Checkpoint] = []

    def begin(self, label: str = "begin") -> Checkpoint:
        if self.active:
            raise ActionError(
                protocol.TRANSACTION_ACTIVE,
                "A transaction is already open; commit or roll it back first",
            )
        self.active = True
        self.steps = 0
        self.checkpoints = [Checkpoint(label=label, states={})]
        return self.checkpoints[0]

    def commit(self) -> int:
        if not self.active:
            raise ActionError(protocol.TRANSACTION_NOT_ACTIVE, "No transaction is open")
        steps = self.steps
        self.active = False
        self.steps = 0
        self.checkpoints = []
        return steps

    def checkpoint(self, label: str) -> Checkpoint:
        """Record a named point that a later rollback can return to.

        The new point starts empty on purpose. Each checkpoint owns the state
        captured *since it was taken*, so rewinding to one discards exactly the
        stage that followed it and keeps everything before. Inheriting the
        parent's state here would make a partial rollback undo earlier stages too.
        """
        if not self.active:
            raise ActionError(protocol.TRANSACTION_NOT_ACTIVE, "No transaction is open")
        if any(point.label == label for point in self.checkpoints):
            raise ActionError(
                protocol.INVALID_PARAMETER,
                f"A checkpoint named '{label}' already exists in this transaction",
            )
        point = Checkpoint(label=label)
        self.checkpoints.append(point)
        return point

    def current(self) -> Checkpoint:
        if not self.active or not self.checkpoints:
            raise ActionError(protocol.TRANSACTION_NOT_ACTIVE, "No transaction is open")
        return self.checkpoints[-1]

    def record(self, names: list[str]) -> None:
        """Capture the current state of ``names`` if not already captured.

        Called before every mutating action, so a rollback restores the state as
        it was before the first change rather than the state before the last one.
        """
        checkpoint = self.current()
        for name in names:
            if name not in checkpoint.states:
                checkpoint.states[name] = ObjectState.capture(name)

    def labels(self) -> list[str]:
        return [point.label for point in self.checkpoints]

    def rollback(self, to: str | None = None) -> dict[str, Any]:
        """Restore the scene to a checkpoint. Returns what was undone.

        ``to=None`` rewinds to the start of the transaction. Naming a label
        rewinds to that checkpoint, which is what makes a plan with stages
        recoverable one stage at a time.
        """
        if not self.active:
            raise ActionError(protocol.TRANSACTION_NOT_ACTIVE, "No transaction is open")
        target = self.checkpoints[0] if to is None else self._find(to)
        # The target counts: it holds the state captured since it was taken,
        # which is exactly the stage a rewind to it has to discard.
        discarded = self.checkpoints[self.checkpoints.index(target) :]
        restored = 0
        unrecoverable: list[str] = []
        # Unwind in reverse: the most recent state is applied first, so an object
        # created and then deleted is removed before its own restore is attempted.
        for point in reversed(discarded):
            for name, state in reversed(list(point.states.items())):
                outcome = state.restore()
                if outcome is True:
                    restored += 1
                else:
                    unrecoverable.append(name)

        partial = to is not None
        if not partial:
            # Rewinding to the start is the end of the transaction; rewinding to a
            # checkpoint in the middle is not, because work continues from there.
            self.finish()
        else:
            self.checkpoints = self.checkpoints[: self.checkpoints.index(target) + 1]
            self.steps = 0
        return {
            "restored": restored,
            "checkpoint": target.label,
            "transaction": "open" if partial else "closed",
            "checkpoints": self.labels(),
            "unrecoverable": sorted(set(unrecoverable)),
            "note": (
                "Only state changed through the tools is restored; edits made by "
                "execute_python are not tracked."
            ),
        }

    def _find(self, label: str) -> Checkpoint:
        for point in self.checkpoints:
            if point.label == label:
                return point
        known = ", ".join(self.labels())
        raise ActionError(
            protocol.INVALID_PARAMETER,
            f"No checkpoint named '{label}' in this transaction. Known: {known}",
        )

    def finish(self) -> None:
        """Drop the recording without restoring anything (used after a commit)."""
        self.active = False
        self.steps = 0
        self.checkpoints = []


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


class ChangeLog:
    """Bounded log of what changed, for ``blender.wait_for_change``.

    A count and a short list of names, not the scene: a client waiting for an
    event needs to know that *something* happened and roughly what, then read the
    detail itself. Keeping full payloads would grow without bound in a session
    where a user drags an object for ten seconds.

    The counter is monotonic and never reset, so a waiter can read it once and
    ask "what happened since" with a plain integer. Once the oldest entries fall
    out of the window, :meth:`since` says how many were dropped instead of
    quietly returning a short list.
    """

    __slots__ = ("_next", "_base", "_entries", "_last_manual", "_last_manual_at")

    #: Distinct updates kept. Enough for a busy minute of editing.
    CAPACITY = 200

    #: Updates within this many seconds of each other, naming the same objects,
    #: are one change: a drag fires the handler continuously.
    COALESCE_SECONDS = 0.2

    def __init__(self) -> None:
        self._next: int = 0
        self._base: int = 0
        self._entries: deque[dict[str, Any]] = deque(maxlen=self.CAPACITY)
        self._last_manual: tuple[str, ...] = ()
        self._last_manual_at: float = 0.0

    @property
    def count(self) -> int:
        return self._next

    def record(self, action: str, names: list[str] | tuple[str, ...] = ()) -> None:
        """Note one change. Cheap enough to call for every mutation."""
        now = time.monotonic()
        if action == "manual" and self._coalesce(tuple(names), now):
            return
        self._entries.append({"index": self._next, "action": action, "names": [n for n in names if n]})
        self._next += 1
        if len(self._entries) == self._entries.maxlen:
            # The window is full, so this append evicted the oldest entry: move
            # the window's start to the oldest one actually kept.
            self._base = self._next - self._entries.maxlen
        if action == "manual":
            self._last_manual, self._last_manual_at = tuple(names), now

    def _coalesce(self, names: tuple[str, ...], now: float) -> bool:
        if (
            names == self._last_manual
            and self._last_manual
            and now - self._last_manual_at < self.COALESCE_SECONDS
        ):
            return True
        return False

    def since(self, since: int) -> dict[str, Any]:
        """Entries recorded after ``since``, plus whether any were dropped."""
        entries = [entry for entry in self._entries if entry["index"] >= since]
        return {
            "count": self._next,
            "changes": entries,
            "dropped": max(0, self._base - since),
            "window_start": self._base,
        }

    def clear(self) -> None:
        self._next = 0
        self._base = 0
        self._entries.clear()
        self._last_manual = ()
        self._last_manual_at = 0.0


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
        self._handler_registered = False
        self._busy = False
        self._refused: str | None = None
        self._lock = threading.Lock()
        self._transactions = TransactionState()
        self._changes = ChangeLog()

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
        self.start_change_watch()
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
        self.stop_change_watch()
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
            except WebSocketClosed as exc:
                refused, self._refused = self._refused, None
                self._set_status(f"Refused: {refused}" if refused else str(exc))
                if refused:
                    logger.warning("MCP server refused this Blender: %s", refused)
                    self.auto_reconnect = False
                    return
                self._set_status(f"Connection lost: {exc}")
            except (WebSocketError, OSError) as exc:
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
            reason = protocol.parse_disconnect(raw)
            if reason is not None:
                # The server is telling us to stop on purpose. Retrying would
                # just be refused again, and a retry loop is exactly the noise
                # this frame exists to avoid.
                self._refused = reason
                raise WebSocketClosed(f"Refused by the server: {reason}")
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

    def start_change_watch(self) -> None:
        """Record edits the user makes in the UI, not just the ones we make.

        Without this, a wait for a change would hang while someone drags an
        object: nothing on our side changed, so nothing would be reported. Must
        be called from the main thread.
        """
        if self._handler_registered:
            return
        try:
            bpy.app.handlers.depsgraph_update_post.append(self._on_depsgraph_update)
            bpy.app.handlers.load_post.append(self._on_load_post)
            self._handler_registered = True
        except Exception:  # pragma: no cover - interpreter shutdown
            logger.warning("Could not watch for manual edits", exc_info=True)

    def stop_change_watch(self) -> None:
        """Stop recording manual edits. Must be called from the main thread."""
        if not self._handler_registered:
            return
        for handlers, handler in (
            (bpy.app.handlers.depsgraph_update_post, self._on_depsgraph_update),
            (bpy.app.handlers.load_post, self._on_load_post),
        ):
            try:
                handlers.remove(handler)
            except Exception:  # pragma: no cover - already removed
                logger.debug("Handler was not registered", exc_info=True)
        self._handler_registered = False

    def _on_depsgraph_update(self, _scene: object, depsgraph: Any) -> None:
        """Note which objects the user touched, ignoring our own changes."""
        if self._busy:
            return
        names = sorted(
            {
                ident.id.name
                for ident in getattr(depsgraph, "updates", ())
                if isinstance(getattr(ident, "id", None), bpy.types.Object)
            }
        )
        if names:
            self._changes.record("manual", names)

    def _on_load_post(self, *_args: Any) -> None:
        """A different file means a different scene, and a different history."""
        self._changes.clear()
        operators.forget_render()

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
        if action == protocol.CHANGE_COUNT:
            return {"success": True, "count": self._changes.count}

        if action == protocol.CHANGES:
            since = params.get("since", 0)
            if not isinstance(since, int):
                raise ActionError(protocol.INVALID_PARAMETER, f"'since' must be an int, got {since!r}")
            return {"success": True, **self._changes.since(since)}

        if action == protocol.BEGIN_TRANSACTION:
            point = self._transactions.begin(str(params.get("label") or "begin"))
            _push_undo("MCP transaction begin")
            return {"success": True, "transaction": "open", "checkpoint": point.label}

        if action == protocol.CHECKPOINT:
            point = self._transactions.checkpoint(str(params.get("label") or ""))
            _push_undo(f"MCP checkpoint {point.label}")
            return {
                "success": True,
                "checkpoint": point.label,
                "checkpoints": self._transactions.labels(),
            }

        if action == protocol.COMMIT_TRANSACTION:
            steps = self._transactions.commit()
            _push_undo("MCP transaction commit")
            return {
                "success": True,
                "transaction": "committed",
                "undo_steps": steps,
                "checkpoints": [],
            }

        if action == protocol.ROLLBACK_TRANSACTION:
            outcome = self._transactions.rollback(params.get("to"))
            bpy.context.view_layer.update()
            # Undo steps recorded during the transaction are no longer ours to
            # replay: the state has been restored directly.
            _push_undo("MCP transaction rollback")
            return {"success": True, "transaction": "rolled_back", **outcome}

        mutating = action in protocol.MUTATING_ACTIONS
        self._busy = True
        try:
            return self._run_mutating(action, params, mutating)
        finally:
            self._busy = False

    def _run_mutating(self, action: str, params: dict[str, Any], mutating: bool) -> dict[str, Any]:
        if mutating and self._transactions.active:
            # Capture before the change, so a rollback restores the state as it
            # was at the start of the transaction rather than the previous step.
            self._transactions.record(_touched_objects(action, params))
        result = operators.dispatch(action, params)
        if mutating:
            if self._transactions.active:
                self._transactions.steps += 1
            _push_undo(f"MCP {action}")
            if action not in _SCENE_NEUTRAL_ACTIONS:
                # Rollback is in here: it changes the scene back, which is
                # exactly the kind of change a client wants to hear about.
                self._changes.record(action, _touched_objects(action, params))
        return result
