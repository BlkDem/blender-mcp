"""Stubs for the two seams the tool tests need: the bridge and the MCP context."""

from __future__ import annotations

from typing import Any

from server.blender.protocol import Action
from server.config import Settings
from server.mcp.support import AppContext


class FakeInstances:
    """Stands in for the bridge's instance registry."""

    def __init__(self, described: dict[str, Any] | None = None) -> None:
        self.described = described or {
            "active": {"id": "blender-4242", "status": "active", "pid": 4242},
            "takeover_allowed": False,
            "instance_count": 1,
            "instances": [{"id": "blender-4242", "status": "active", "pid": 4242}],
            "note": "Only the active instance receives tool calls.",
        }

    def describe(self) -> dict[str, Any]:
        return self.described


class FakeBridge:
    """Stands in for :class:`server.blender.connection.BlenderBridge`.

    Records the actions it was asked to perform and replies from a scripted
    table, so a tool test can assert on the request the tool built and the
    response the tool passed through — with no socket and no Blender.
    """

    def __init__(self, results: dict[Action, Any] | None = None) -> None:
        self.results: dict[Action, Any] = results or {}
        self.calls: list[tuple[Action, dict[str, Any]]] = []
        self.raise_for: dict[Action, Exception] = {}
        self.timeouts: list[float | None] = []
        self.instances = FakeInstances()

    async def request(
        self,
        action: Action,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        self.calls.append((action, params or {}))
        self.timeouts.append(timeout)
        if action in self.raise_for:
            raise self.raise_for[action]
        return self.results.get(action, {"success": True, "action": action.value})


class FakeContext:
    """Minimal stand-in for the MCP ``Context`` handed to a tool.

    Only ``request_context.lifespan_context`` is exercised, which is the single
    attribute :func:`server.mcp.support.bridge_of` reads.
    """

    def __init__(self, bridge: Any, settings: Settings | None = None) -> None:
        app = AppContext(settings=settings or Settings(), bridge=bridge)
        self.request_context = type("FakeRequestContext", (), {"lifespan_context": app, "request_id": 1})()
