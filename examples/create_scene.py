"""Build a small scene over several requests: table, four legs, a render.

This is the request/response sequence an AI performs for "make me a table". Run
it with the MCP server and Blender both up::

    blender --python examples/create_scene.py

Each step is a separate request, so a failure is attributable to one call —
which is exactly how a model experiences the same task.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "addon"))

from blender_mcp.websocket import WebSocket, WebSocketError  # noqa: E402

HOST = os.environ.get("BLENDER_HOST", "127.0.0.1")
PORT = int(os.environ.get("BLENDER_PORT", "8765"))

#: Leg placement, so the four legs come from one loop rather than four literals.
_LEGS = {
    "LegFL": (-0.9, -0.4),
    "LegFR": (0.9, -0.4),
    "LegBL": (-0.9, 0.4),
    "LegBR": (0.9, 0.4),
}


def _cube(name: str, location: list[float], scale: list[float] | None = None) -> tuple[str, dict[str, Any]]:
    params: dict[str, Any] = {"type": "cube", "name": name, "location": location}
    if scale is not None:
        params["scale"] = scale
    return "create_object", params


#: (action, params) for the whole scene, in order.
SCRIPT: list[tuple[str, dict[str, Any]]] = [
    ("begin_transaction", {}),
    _cube("TableTop", [0, 0, 0.75], [2, 1, 0.05]),
    *[_cube(name, [x, y, 0.35], [0.1, 0.1, 0.7]) for name, (x, y) in _LEGS.items()],
    ("commit_transaction", {}),
    ("get_scene", {}),
]


def main() -> int:
    """Run the script, stopping at the first failure."""
    try:
        socket = WebSocket.connect(HOST, PORT, timeout=5.0)
    except (WebSocketError, OSError) as exc:
        print(f"Could not reach the MCP server at ws://{HOST}:{PORT}: {exc}")
        print("Start it with: python -m server.main")
        return 1

    socket.settimeout(0.5)
    for action, params in SCRIPT:
        request_id = uuid.uuid4().hex
        socket.send(json.dumps({"id": request_id, "action": action, "params": params}))
        response = _await(socket, request_id)
        if response is None:
            socket.close()
            print(f"No answer for '{action}' within the timeout.")
            return 1
        if not response.get("success"):
            socket.close()
            print(f"{action} failed: {json.dumps(response.get('error'), indent=2)}")
            return 1
        summary = _summarise(response.get("result", {}))
        print(f"{action}: {summary}")

    socket.close()
    return 0


def _await(socket: WebSocket, request_id: str, timeout: float = 15.0) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            raw = socket.recv()
        except TimeoutError:
            continue
        if raw is None:
            return None
        response = json.loads(raw)
        if response.get("id") == request_id:
            return response
    return None


def _summarise(result: dict[str, Any]) -> str:
    """One line per result, so the output stays readable."""
    if "object" in result:
        obj = result["object"]
        return f"{obj['name']} at {obj['location']} ({obj['dimensions']})"
    if "objects_count" in result:
        names = [obj["name"] for obj in result.get("objects", [])]
        shown = result.get("objects_shown", len(names))
        return f"{result['objects_count']} object(s), {shown} shown: {', '.join(names) or 'none'}"
    return json.dumps(result)


if __name__ == "__main__":
    raise SystemExit(main())
