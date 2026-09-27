"""Create one cube, the smallest useful thing an AI can be asked to do.

Run it through the add-on's own protocol, without an MCP client in the loop —
useful for checking that Blender and the bridge are talking to each other::

    blender --python examples/create_cube.py

The script connects to the MCP server, sends one request, prints the response and
exits. It is a client of the bridge, not of Blender: nothing here touches
``bpy``, exactly like the MCP server.
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "addon"))

from blender_mcp.websocket import WebSocket  # noqa: E402

HOST = os.environ.get("BLENDER_HOST", "127.0.0.1")
PORT = int(os.environ.get("BLENDER_PORT", "8765"))


def main() -> int:
    """Send one create_object request and print the answer."""
    try:
        socket = WebSocket.connect(HOST, PORT, timeout=5.0)
    except Exception as exc:
        print(f"Could not reach the MCP server at ws://{HOST}:{PORT}: {exc}")
        print("Start it with: python -m server.main")
        return 1

    request = {
        "id": "example-1",
        "action": "create_object",
        "params": {"type": "cube", "name": "ExampleCube", "location": [0.0, 0.0, 1.0]},
    }
    socket.send(json.dumps(request))
    socket.settimeout(10.0)

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            raw = socket.recv()
        except TimeoutError:
            break
        if raw is None:
            break
        response = json.loads(raw)
        if response.get("id") != request["id"]:
            continue  # not ours; keep waiting
        print(json.dumps(response, indent=2))
        socket.close()
        return 0 if response.get("success") else 1

    socket.close()
    print(f"No answer for {request['id']} within the timeout.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
