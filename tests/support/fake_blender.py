"""A stand-in for the Blender add-on, for end-to-end tests.

**This is not a Blender emulator.** It is a protocol peer: it speaks the same
JSON over a real WebSocket using the add-on's own standard-library client, and
it keeps a tiny in-memory object store so the tools have something to read and
write. It exists to prove that the MCP server, the transport, the framing, the
timeouts and the error envelopes line up end to end. What ``bpy`` actually does
— mesh generation, modifiers, rendering — is exercised by running the real
add-on inside Blender, not here.
"""

from __future__ import annotations

import threading
from typing import Any

from blender_mcp import protocol
from blender_mcp.websocket import WebSocket, WebSocketClosed, WebSocketError

#: A unit cube has a 2 m side, matching Blender's default primitive.
#: Mesh statistics the double reports. Real values come from the add-on; these
#: just have to be stable and distinct per primitive.
_VERTEX_COUNTS = {"cube": 8, "sphere": 482, "cylinder": 66, "cone": 35, "plane": 4, "torus": 576}
_FACE_COUNTS = {"cube": 6, "sphere": 480, "cylinder": 32, "cone": 33, "plane": 1, "torus": 576}

_BASE_SIZES = {
    "cube": (2.0, 2.0, 2.0),
    "sphere": (2.0, 2.0, 2.0),
    "cylinder": (2.0, 2.0, 2.0),
    "cone": (2.0, 2.0, 2.0),
    "plane": (2.0, 2.0, 0.0),
    "torus": (2.0, 2.0, 0.4),
}


class _Object:
    def __init__(self, object_type: str, name: str, params: dict[str, Any]) -> None:
        self.type = object_type
        self.name = name
        self.location = [float(v) for v in params.get("location", [0.0, 0.0, 0.0])]
        # Rotations cross this boundary in degrees, exactly as on the real add-on.
        self.rotation = [float(v) for v in params.get("rotation", [0.0, 0.0, 0.0])]
        self.scale = [float(v) for v in params.get("scale", [1.0, 1.0, 1.0])]
        self.hidden = False
        self.materials: list[str] = []
        self.base = list(_BASE_SIZES.get(object_type, (1.0, 1.0, 1.0)))
        self.vertices = _VERTEX_COUNTS.get(object_type, 8)
        self.polygons = _FACE_COUNTS.get(object_type, 6)

    @property
    def dimensions(self) -> list[float]:
        return [round(self.base[i] * self.scale[i], 6) for i in range(3)]

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": "MESH",
            "location": [round(v, 6) for v in self.location],
            "rotation": [round(v, 6) for v in self.rotation],
            "scale": [round(v, 6) for v in self.scale],
            "dimensions": self.dimensions,
            "visible": not self.hidden,
        }

    def detail(self) -> dict[str, Any]:
        summary = self.summary()
        summary.update(
            {
                "collection": ["Collection"],
                "parent": None,
                "materials": list(self.materials),
                "modifiers": [],
                "visibility": {
                    "hide_viewport": self.hidden,
                    "hide_render": self.hidden,
                    "visible": not self.hidden,
                },
                "data": f"{self.name}Mesh",
                "vertices": self.vertices,
                "edges": self.vertices,
                "polygons": self.polygons,
                "loops": self.polygons * 4,
            }
        )
        return summary


class FakeBlender:
    """An add-on-shaped peer: a socket, a thread and a dict of objects."""

    def __init__(self) -> None:
        self.objects: dict[str, _Object] = {}
        self.materials: list[str] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.transaction_open = False
        self.transaction_steps = 0
        self._ws: WebSocket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- lifecycle ---------------------------------------------------------

    def connect(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, args=(host, port), name="fake-blender", daemon=True
        )
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._ws is not None:
            self._ws.close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def __enter__(self) -> FakeBlender:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _loop(self, host: str, port: int) -> None:
        try:
            self._ws = WebSocket.connect(host, port, "/", timeout=2.0)
        except (WebSocketError, OSError):
            return
        self._ws.settimeout(0.2)
        while not self._stop.is_set():
            try:
                raw = self._ws.recv()
            except TimeoutError:
                continue
            except (WebSocketClosed, WebSocketError, OSError):
                return
            if raw is None:
                return
            self._handle(raw)

    # --- protocol ----------------------------------------------------------

    def _handle(self, raw: str) -> None:
        assert self._ws is not None
        try:
            request_id, action, params = protocol.parse_request(raw)
        except protocol.ProtocolError as exc:
            self._ws.send(protocol.encode_error("", protocol.MALFORMED_MESSAGE, str(exc)))
            return
        self.calls.append((action, params))
        try:
            result = self._dispatch(action, params)
        except Exception as exc:  # noqa: BLE001 - mirrors the real add-on
            self._ws.send(protocol.encode_exception(request_id, exc))
            return
        self._ws.send(protocol.encode_response(request_id, result))

    def _require_transaction(self) -> None:
        if not self.transaction_open:
            raise protocol.ActionError(protocol.TRANSACTION_NOT_ACTIVE, "No transaction is open")

    def _lookup(self, name: str) -> _Object:
        obj = self.objects.get(name)
        if obj is None:
            raise protocol.ActionError(protocol.OBJECT_NOT_FOUND, f"Object '{name}' does not exist")
        return obj

    def _dispatch(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        if action == protocol.BEGIN_TRANSACTION:
            if self.transaction_open:
                raise protocol.ActionError(protocol.TRANSACTION_ACTIVE, "A transaction is already open")
            self.transaction_open = True
            self.transaction_steps = 0
            return {"success": True, "transaction": "open"}
        if action == protocol.COMMIT_TRANSACTION:
            self._require_transaction()
            steps, self.transaction_steps = self.transaction_steps, 0
            self.transaction_open = False
            return {"success": True, "transaction": "committed", "undo_steps": steps}
        if action == protocol.ROLLBACK_TRANSACTION:
            self._require_transaction()
            undone, self.transaction_steps = self.transaction_steps, 0
            self.transaction_open = False
            return {"success": True, "transaction": "rolled_back", "undo_steps": undone}

        if action in protocol.MUTATING_ACTIONS and self.transaction_open:
            self.transaction_steps += 1

        if action == protocol.PING:
            return {"pong": True, "blender_version": "fake", "scene": "Scene"}
        if action == protocol.GET_SCENE:
            return self._get_scene(params)
        if action == protocol.GET_OBJECTS:
            return self._get_objects(params)
        if action == protocol.GET_OBJECT:
            return self._lookup(params["name"]).detail()
        if action == protocol.CREATE_OBJECT:
            return self._create(params)
        if action == protocol.UPDATE_OBJECT:
            return self._update(params)
        if action == protocol.DELETE_OBJECT:
            name = params["name"]
            self._lookup(name)
            del self.objects[name]
            return {"success": True, "deleted": name, "type": "MESH"}
        if action == protocol.RENDER:
            return {
                "success": True,
                "output_path": params.get("output_path", "/tmp/mcp_render.png"),
                "render_time": 0.01,
                "engine": params.get("engine", "CYCLES"),
                "resolution": [
                    params.get("resolution_x", 1920),
                    params.get("resolution_y", 1080),
                ],
            }
        if action == protocol.EXECUTE_PYTHON:
            return {"success": True, "executed": True, "result": {"fake": True}}
        raise protocol.ActionError(protocol.UNKNOWN_ACTION, f"Unknown action '{action}'")

    # --- actions -----------------------------------------------------------

    def _get_scene(self, params: dict[str, Any]) -> dict[str, Any]:
        objects = list(self.objects.values())
        detailed = bool(params.get("include_details"))
        return {
            "scene": "Scene",
            "objects_count": len(objects),
            "frame": 1,
            "active_object": objects[0].name if objects else None,
            "active_camera": "Camera",
            "render_engine": "CYCLES",
            "objects": [obj.detail() if detailed else obj.summary() for obj in objects],
            "collections": ["Collection"],
            "cameras": ["Camera"],
            "lights": [],
            "render": {"engines": ["CYCLES"], "resolution": [1920, 1080]},
        }

    def _get_objects(self, params: dict[str, Any]) -> dict[str, Any]:
        matches = sorted(self.objects.values(), key=lambda obj: obj.name)
        wanted_type = params.get("type")
        if wanted_type:
            matches = [obj for obj in matches if obj.type == wanted_type]
        collection = params.get("collection")
        if collection:
            matches = [obj for obj in matches if collection in obj.collection]
        needle = params.get("name_contains")
        if needle:
            matches = [obj for obj in matches if needle.lower() in obj.name.lower()]
        total = len(matches)
        offset = max(0, int(params.get("offset", 0)))
        limit = min(max(1, int(params.get("limit", 50))), 500)
        page = matches[offset : offset + limit]
        return {
            "objects": [obj.summary() for obj in page],
            "count": len(page),
            "total": total,
            "offset": offset,
            "limit": limit,
            "truncated": offset + len(page) < total,
        }

    def _create(self, params: dict[str, Any]) -> dict[str, Any]:
        object_type = params.get("type")
        if object_type not in _BASE_SIZES:
            raise protocol.ActionError(
                protocol.INVALID_OBJECT_TYPE, f"Unsupported object type '{object_type}'"
            )
        name = params.get("name")
        if not isinstance(name, str) or not name.strip():
            raise protocol.ActionError(protocol.INVALID_PARAMETER, "'name' is required")
        if name in self.objects:
            raise protocol.ActionError(protocol.OBJECT_ALREADY_EXISTS, f"Object '{name}' already exists")
        obj = _Object(object_type, name, params)
        self.objects[name] = obj
        return {"success": True, "object": obj.detail()}

    def _update(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params["name"]
        obj = self._lookup(name)
        for field in ("location", "rotation"):
            if params.get(field) is not None:
                setattr(obj, field, [float(v) for v in params[field]])
        if params.get("scale") is not None:
            obj.scale = [float(v) for v in params["scale"]]
        if params.get("dimensions") is not None:
            obj.scale = [float(params["dimensions"][i]) / max(obj.base[i], 1e-6) for i in range(3)]
        if params.get("visibility") is not None:
            obj.hidden = not bool(params["visibility"])
        payload: dict[str, Any] = {"success": True, "object": obj.detail()}
        new_name = params.get("new_name")
        if new_name:
            if new_name in self.objects and new_name != obj.name:
                raise protocol.ActionError(
                    protocol.OBJECT_ALREADY_EXISTS, f"Cannot rename to '{new_name}': taken"
                )
            if new_name != obj.name:
                self.objects[new_name] = self.objects.pop(obj.name)
                obj.name = new_name
                payload = {"success": True, "object": obj.detail(), "renamed_to": f"{name} -> {new_name}"}
        material = params.get("material")
        if material:
            created = material not in self.materials
            if created:
                self.materials.append(material)
            obj.materials = [material]
            payload["material"] = material
            payload["material_created"] = created
            payload["object"] = obj.detail()
        return payload
