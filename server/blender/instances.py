"""Which Blender is this server talking to, and what happened to the others.

One MCP server, one bridge, one socket: only one Blender can hold it. Without a
registry, a second Blender connecting is silent and total — the first user's tool
calls start landing in someone else's scene, and the symptom ("why did my cube
move in a file I don't have open?") is very hard to trace back.

So the bridge identifies every add-on that dials in, remembers the ones it
refused or replaced, and refuses a takeover by default. The refusal is not
silent either: the newcomer gets a ``disconnect`` frame carrying the reason, so
its operator panel says "another Blender is already connected" instead of
appearing connected and quietly being ignored.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from server.errors import BlenderMCPError, ErrorCode

#: How many past instances to remember. Enough to explain what happened over a
#: session without growing without bound on a long-lived server.
HISTORY_LIMIT = 20

#: Seconds to wait for the newcomer's identity before deciding. A Blender whose
#: main thread is busy loading a 2 GB file may take a while to answer; too short
#: and a legitimate instance gets refused for being slow.
IDENTITY_TIMEOUT = 10.0

#: Instance states. ``active`` is the only one holding the socket.
ACTIVE = "active"
GONE = "disconnected"
REPLACED = "replaced"
REFUSED = "refused"


@dataclass(slots=True)
class Instance:
    """One add-on that has tried to connect."""

    id: str
    address: str
    status: str
    pid: int | None = None
    version: str | None = None
    scene: str | None = None
    blend_file: str | None = None
    connected_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    reason: str | None = None

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "address": self.address,
            "pid": self.pid,
            "blender_version": self.version,
            "scene": self.scene,
            "blend_file": self.blend_file,
            "connected_at": self.connected_at,
            "last_seen": self.last_seen,
            "reason": self.reason,
        }


class InstanceRegistry:
    """Tracks the active instance and a short history of the rest."""

    def __init__(self, *, allow_takeover: bool = False) -> None:
        self._allow_takeover = allow_takeover
        self._instances: dict[str, Instance] = {}
        self._order: list[str] = []
        # The object, not the id: a reconnect re-adopts the same id, and the old
        # connection's release must not clear the slot the new one just took.
        self._active: Instance | None = None

    @property
    def active(self) -> Instance | None:
        return self._active

    @property
    def allow_takeover(self) -> bool:
        return self._allow_takeover

    def adopt(self, identity: dict[str, Any], address: str) -> tuple[Instance, Instance | None]:
        """Register a connection attempt.

        Returns the new instance and the instance it displaced, if any. A refusal
        is reported by the new instance's status rather than an exception, because
        the caller still has to send the ``disconnect`` frame explaining it.
        """
        instance = Instance(
            id=_instance_id(identity, address),
            address=address,
            status=ACTIVE,
            pid=_as_int(identity.get("pid")),
            version=_as_str(identity.get("blender_version")),
            scene=_as_str(identity.get("scene")),
            blend_file=_as_str(identity.get("blend_file")) or None,
        )
        previous = self.active
        if previous is not None and previous.id != instance.id:
            if self._allow_takeover:
                previous.status = REPLACED
                previous.reason = f"replaced by {instance.id}"
            else:
                instance.status = REFUSED
                instance.reason = (
                    f"{previous.id} (pid {previous.pid}) is already connected. "
                    "Close it, or start the server with BLENDER_ALLOW_TAKEOVER=1 "
                    "if you meant to replace it."
                )

        self._remember(instance)
        if instance.status == ACTIVE:
            self._active = instance
        return instance, previous

    def release(self, instance: Instance) -> None:
        """Record that a connection let go of the socket.

        A same-instance reconnect produces two ``Instance`` objects with one id,
        and the older one goes last. Only the object currently holding the socket
        may clear it.
        """
        if self._active is not instance:
            return
        instance.status = GONE
        instance.last_seen = time.time()
        self._active = None

    def touch(self, instance: Instance) -> None:
        instance.last_seen = time.time()

    def _remember(self, instance: Instance) -> None:
        if instance.id not in self._instances:
            self._order.append(instance.id)
        self._instances[instance.id] = instance
        while len(self._order) > HISTORY_LIMIT:
            dropped = self._order.pop(0)
            if self._active is None or self._active.id != dropped:
                del self._instances[dropped]

    def describe(self) -> dict[str, Any]:
        """The payload behind ``blender.get_instances``."""
        active = self.active
        recent = [self._instances[key].describe() for key in reversed(self._order)]
        return {
            "active": active.describe() if active else None,
            "takeover_allowed": self._allow_takeover,
            "instance_count": len(self._instances),
            "instances": recent,
            "note": (
                "Only the active instance receives tool calls. Refused and replaced "
                "entries are kept to explain a scene that changed unexpectedly."
            ),
        }


def _instance_id(identity: dict[str, Any], address: str) -> str:
    """Identify an add-on by process, so a reconnect is not a new instance.

    The pid is the honest identifier: it survives a socket drop and a reconnect,
    which an address does not. Without one (a non-Blender client, a test double)
    the address is the best available handle.
    """
    pid = _as_int(identity.get("pid"))
    if pid is not None:
        return f"blender-{pid}"
    return f"client@{address}"


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def refusal_error(reason: str) -> Exception:
    """The error a client sees if it asks while a refusal is in force."""
    return BlenderMCPError(reason, code=ErrorCode.VALIDATION_ERROR)
