"""Every ``bpy`` operation the bridge exposes, and nothing else.

This module is the single place in the project that imports ``bpy``. It runs on
Blender's main thread (see :mod:`blender_mcp.connection`) and returns plain
dicts so the payload can be JSON-encoded without thinking about bpy types.

Two conventions worth knowing:

* **Rotation is degrees on this boundary.** ``bpy`` stores radians; models
  reliably think in degrees, so the conversion happens here, once.
* **Payloads are summaries.** No vertex, polygon or face data ever leaves this
  module, whatever the caller asks for.
"""

from __future__ import annotations

import logging
import math
import os
import time
from collections.abc import Callable, Iterable, Sequence
from typing import Any

import bmesh  # type: ignore[import-not-found]
import bpy  # type: ignore[import-not-found]

from blender_mcp import protocol
from blender_mcp.protocol import ActionError

_LOGGER = logging.getLogger(__name__)

SUPPORTED_TYPES: tuple[str, ...] = ("cube", "sphere", "cylinder", "cone", "plane", "torus")

#: Hard cap on one ``get_objects`` page. A scene with thousands of objects must
#: not be able to fill an LLM context, whatever limit it asks for.
MAX_LIST_LIMIT = 500

#: Default and ceiling for how many objects ``get_scene`` inlines. Past a few
#: hundred the summary stops being a summary and starts being the whole scene in
#: the model's context; ``get_objects`` is the tool for going deeper.
DEFAULT_SCENE_OBJECT_LIMIT = 200
MAX_SCENE_OBJECT_LIMIT = 1000

#: Preview renders exist to be looked at, not admired: small, few samples, and
#: always at the same path so ``blender://render/latest`` has something to serve.
DEFAULT_PREVIEW_RESOLUTION = 512
MAX_PREVIEW_RESOLUTION = 1024
DEFAULT_PREVIEW_SAMPLES = 16
PREVIEW_FILENAME = "mcp_render_preview.png"

#: The most recent render, for ``blender://render/latest``. Module-level because
#: the add-on is a singleton inside one Blender; there is nothing to share it with.
_LAST_RENDER: dict[str, Any] = {"result": None, "at": 0.0}

#: Fallback operators, used only if a bmesh primitive cannot be built.
#: ``bpy.ops`` needs a live window context, which the main-thread timer has in
#: the GUI but not under ``blender --background``; bmesh is the primary path for
#: exactly that reason.
_PRIMITIVE_OPS: dict[str, str] = {
    "cube": "primitive_cube_add",
    "sphere": "primitive_uv_sphere_add",
    "cylinder": "primitive_cylinder_add",
    "cone": "primitive_cone_add",
    "plane": "primitive_plane_add",
    "torus": "primitive_torus_add",
}


# --- unit helpers -----------------------------------------------------------


def to_radians(degrees: Sequence[float]) -> tuple[float, float, float]:
    return (math.radians(degrees[0]), math.radians(degrees[1]), math.radians(degrees[2]))


def to_degrees(radians: Iterable[float]) -> list[float]:
    """Radians to degrees, rounded to erase the float round-trip noise.

    90 degrees would otherwise come back as 90.000003, which reads like a bug and
    makes a model that compares before and after think nothing was applied.
    """
    return [round(math.degrees(float(value)), 4) for value in radians]


def _sync() -> None:
    """Bring the depsgraph up to date so ``dimensions`` is not stale.

    Blender derives ``object.dimensions`` from the evaluated mesh, so reading it
    straight after a ``location``/``scale`` assignment returns the *previous*
    value. Every read of a transform-dependent property goes through here first.
    """
    view_layer = getattr(bpy.context, "view_layer", None)
    if view_layer is not None:
        view_layer.update()


def _vector3(value: Any, field: str) -> tuple[float, float, float]:
    if value is None:
        raise ActionError(protocol.INVALID_PARAMETER, f"'{field}' must be a list of three numbers")
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ActionError(
            protocol.INVALID_PARAMETER, f"'{field}' must be a list of three numbers, got {value!r}"
        )
    try:
        return (float(value[0]), float(value[1]), float(value[2]))
    except (TypeError, ValueError) as exc:
        raise ActionError(
            protocol.INVALID_PARAMETER, f"'{field}' must contain numbers, got {value!r}"
        ) from exc


def _round(value: float, digits: int = 6) -> float:
    return round(float(value), digits)


# --- object lookup ----------------------------------------------------------


def _get_object(name: str) -> bpy.types.Object:
    obj = bpy.data.objects.get(name)
    if obj is None:
        raise ActionError(protocol.OBJECT_NOT_FOUND, f"Object '{name}' does not exist")
    return obj


def _collection_names(obj: bpy.types.Object) -> list[str]:
    return [collection.name for collection in obj.users_collection]


def _object_summary(obj: bpy.types.Object) -> dict[str, Any]:
    """The compact per-object view used by scene and object listings.

    Deliberately flat and short: this is what an LLM reads to decide what to do
    next, so it carries the transform and nothing it can look up elsewhere.
    """
    return {
        "name": obj.name,
        "type": obj.type,
        "location": [_round(value) for value in obj.location],
        "rotation": to_degrees(obj.rotation_euler),
        "scale": [_round(value) for value in obj.scale],
        "dimensions": [_round(value) for value in obj.dimensions],
        "visible": _is_visible(obj),
    }


def _is_visible(obj: bpy.types.Object) -> bool:
    """Viewport visibility, or ``True`` when the view layer cannot answer.

    ``visible_get`` raises for objects outside the current view layer, and one
    odd object should not turn a whole scene listing into an error.
    """
    try:
        return bool(obj.visible_get())
    except (RuntimeError, AttributeError):
        return not obj.hide_viewport


def _mesh_statistics(obj: bpy.types.Object) -> dict[str, int] | None:
    """Vertex and face counts, for deciding whether detail is worth reading.

    Counts only. A model that wants geometry asks for it explicitly through
    ``execute_python`` rather than having every listing carry it.
    """
    mesh = getattr(obj, "data", None)
    if obj.type != "MESH" or mesh is None or not hasattr(mesh, "polygons"):
        return None
    return {
        "vertices": len(mesh.vertices),
        "edges": len(mesh.edges),
        "polygons": len(mesh.polygons),
        "loops": len(mesh.loops),
    }


def _object_detail(obj: bpy.types.Object) -> dict[str, Any]:
    detail = _object_summary(obj)
    detail.update(
        {
            "collection": _collection_names(obj),
            "parent": obj.parent.name if obj.parent else None,
            "materials": _material_names(obj),
            "modifiers": [{"name": mod.name, "type": mod.type} for mod in obj.modifiers],
            "visibility": _visibility(obj),
            "data": obj.data.name if getattr(obj, "data", None) is not None else None,
        }
    )
    statistics = _mesh_statistics(obj)
    if statistics is not None:
        # Flat, because that is how a model reads them: "vertices": 1240 next to
        # "dimensions" rather than buried under a "mesh" key it has to know about.
        detail.update(statistics)
    return detail


def _material_names(obj: bpy.types.Object) -> list[str]:
    data = getattr(obj, "data", None)
    if not hasattr(data, "materials"):
        return []  # cameras and lights have no material slots
    return [material.name if material else None for material in data.materials]  # type: ignore[misc]


def _visibility(obj: bpy.types.Object) -> dict[str, bool | None]:
    """View-layer visibility, or ``None`` when the view layer cannot say.

    ``visible_get`` raises for objects outside the current view layer, and one
    odd object should not turn a whole scene listing into an error.
    """
    visible: bool | None = None
    try:
        visible = bool(obj.visible_get())
    except (RuntimeError, AttributeError):
        visible = None
    return {
        "hide_viewport": bool(obj.hide_viewport),
        "hide_render": bool(obj.hide_render),
        "visible": visible,
    }


# --- scene ------------------------------------------------------------------


def get_scene(params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Summarise the active scene."""
    params = params or {}
    scene = bpy.context.scene
    _sync()
    objects = list(bpy.data.objects)
    include_details = bool(params.get("include_details"))
    include_objects = bool(params.get("include_objects", True))

    active = bpy.context.view_layer.objects.active if bpy.context.view_layer else None
    payload: dict[str, Any] = {
        "scene": scene.name,
        "objects_count": len(objects),
        "frame": scene.frame_current,
        "active_object": active.name if active else None,
        "active_camera": scene.camera.name if scene.camera else None,
        "render_engine": scene.render.engine,
    }
    if include_objects:
        limit = min(
            max(1, int(params.get("object_limit", DEFAULT_SCENE_OBJECT_LIMIT))),
            MAX_SCENE_OBJECT_LIMIT,
        )
        shown = objects[:limit]
        payload["objects"] = [
            _object_detail(obj) if include_details else _object_summary(obj) for obj in shown
        ]
        payload["objects_shown"] = len(shown)
        # Explicit, so a model that sees fewer objects than objects_count knows
        # to reach for blender.get_objects rather than concluding the rest are
        # missing.
        payload["objects_truncated"] = len(shown) < len(objects)
    payload["collections"] = [collection.name for collection in bpy.data.collections]
    payload["cameras"] = [obj.name for obj in objects if obj.type == "CAMERA"]
    payload["lights"] = [
        {"name": obj.name, "type": obj.data.type, "energy": _round(obj.data.energy)}
        for obj in objects
        if obj.type == "LIGHT"
    ]
    # The engine list and the render size are not part of the required shape, but
    # a model that is about to render needs them and get_scene is where it looks.
    payload["render"] = {
        "engines": available_engines(scene),
        "resolution": [scene.render.resolution_x, scene.render.resolution_y],
    }
    return payload


def get_objects(params: dict[str, Any] | None = None) -> dict[str, Any]:
    """List objects with filters and a page window.

    ``get_scene`` returns the whole scene, which stops being useful somewhere
    around a few hundred objects. This is the paged view: filter first, then read
    a slice, and the response always says how much was left behind.
    """
    params = params or {}
    _sync()
    objects = sorted(bpy.data.objects, key=lambda obj: obj.name)

    wanted_type = params.get("type")
    if wanted_type:
        wanted_type = str(wanted_type).upper()
        objects = [obj for obj in objects if obj.type == wanted_type]

    collection = params.get("collection")
    if collection:
        collection = str(collection)
        objects = [obj for obj in objects if collection in _collection_names(obj)]

    needle = params.get("name_contains")
    if needle:
        needle = str(needle).lower()
        objects = [obj for obj in objects if needle in obj.name.lower()]

    total = len(objects)
    offset = max(0, int(params.get("offset", 0)))
    limit = min(max(1, int(params.get("limit", 50))), MAX_LIST_LIMIT)
    page = objects[offset : offset + limit]

    return {
        "objects": [_object_summary(obj) for obj in page],
        "count": len(page),
        "total": total,
        "offset": offset,
        "limit": limit,
        "truncated": offset + len(page) < total,
    }


#: Engine ids worth probing. Blender renamed EEVEE between 4.x and 5.x, and a
#: wrong id is an error the model cannot recover from without being told the
#: right one, so the scene payload carries the ids this Blender accepts. EEVEE
#: and Workbench are C++ and appear in neither the RNA enum nor
#: ``RenderEngine.__subclasses__``, which leaves assignment as the only test.
KNOWN_ENGINES: tuple[str, ...] = (
    "BLENDER_EEVEE",
    "BLENDER_EEVEE_NEXT",
    "BLENDER_WORKBENCH",
    "CYCLES",
)


def available_engines(scene: Any) -> list[str]:
    """Ids from :data:`KNOWN_ENGINES` this Blender accepts, current one first.

    Headless returns just the current engine on purpose: assigning a GL engine
    with no window aborts the process inside Blender's own GPU loader, and a
    headless render has to be Cycles regardless.
    """
    settings = scene.render
    original = settings.engine
    if bpy.app.background:
        return [original]

    found: list[str] = [original]
    for engine in KNOWN_ENGINES:
        if engine == original:
            continue
        try:
            settings.engine = engine
        except TypeError:
            continue
        found.append(engine)
    settings.engine = original
    return found


def get_object(params: dict[str, Any]) -> dict[str, Any]:
    """Describe one object."""
    name = params.get("name")
    if not isinstance(name, str) or not name:
        raise ActionError(protocol.INVALID_PARAMETER, "'name' is required")
    _sync()
    return _object_detail(_get_object(name))


def ping(params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Cheap liveness probe: is Blender responsive on the main thread."""
    return {
        "pong": True,
        "blender_version": bpy.app.version_string,
        "scene": bpy.context.scene.name,
        # Identity, so the server can tell one Blender from another and refuse
        # a silent takeover. os.getpid is Blender's own process, because the
        # add-on's Python is Blender's Python.
        "pid": os.getpid(),
        "blend_file": bpy.data.filepath or None,
        "time": time.time(),
    }


# --- mutations --------------------------------------------------------------


def create_object(params: dict[str, Any]) -> dict[str, Any]:
    """Create a primitive mesh object."""
    # Case-tolerant, because a model writing "CUBE" should not be told off by a
    # primitive list. The MCP tool normalises too; doing it here as well keeps the
    # two entry points to the bridge behaving identically.
    object_type = str(params.get("type", "")).lower()
    name = params.get("name")
    if object_type not in _PRIMITIVE_OPS:
        raise ActionError(
            protocol.INVALID_OBJECT_TYPE,
            f"Unsupported object type '{object_type}'. Supported: {', '.join(SUPPORTED_TYPES)}",
        )
    if not isinstance(name, str) or not name.strip():
        raise ActionError(protocol.INVALID_PARAMETER, "'name' is required")
    if name in bpy.data.objects:
        # Deliberately not "Table.001": a silent rename hides the collision from
        # the model that asked for the object by name.
        raise ActionError(protocol.OBJECT_ALREADY_EXISTS, f"Object '{name}' already exists")

    collection = _target_collection(params.get("collection"))
    location = _vector3(params.get("location", [0.0, 0.0, 0.0]), "location")
    obj = _new_primitive(object_type, name, collection)
    obj.location = location

    if params.get("rotation") is not None:
        obj.rotation_euler = to_radians(_vector3(params["rotation"], "rotation"))
    if params.get("scale") is not None:
        obj.scale = _vector3(params["scale"], "scale")

    _make_active(obj)
    _sync()
    return {"success": True, "object": _object_detail(obj)}


def _make_active(obj: Any) -> None:
    """Select the new object and make it the active one.

    Blender's own primitive operators do this, and ``get_scene`` reports
    ``active_object``; without it a freshly created object would be invisible to
    that field and to anything a user does next in the UI.
    """
    view_layer = getattr(bpy.context, "view_layer", None)
    if view_layer is None:  # pragma: no cover - no view layer means no window
        return
    try:
        obj.select_set(True)
        view_layer.objects.active = obj
    except RuntimeError:  # pragma: no cover - object not in this view layer
        _LOGGER.debug("Could not make %s active in this view layer", obj.name)


def _target_collection(name: Any) -> Any:
    """Resolve the collection to link into, defaulting to the active one."""
    if not name:
        return bpy.context.scene.collection
    collection = bpy.data.collections.get(str(name))
    if collection is None:
        raise ActionError(protocol.OBJECT_NOT_FOUND, f"Collection '{name}' does not exist")
    return collection


# --- primitive construction --------------------------------------------------
#
# Built with bmesh rather than ``bpy.ops.mesh.primitive_*_add`` on purpose:
# operators need a live window context, which the main-thread timer has in the
# GUI but not under ``blender --background``. bmesh needs nothing but the
# datablocks. The operator is kept as a fallback for the case where a bmesh
# signature this module expects is gone in a future Blender.


def _build_bmesh(object_type: str) -> bmesh.types.BMesh:
    bm = bmesh.new()
    if object_type == "cube":
        bmesh.ops.create_cube(bm, size=2.0)
    elif object_type == "plane":
        # create_grid's `size` is a half-extent, so 1.0 gives Blender's default
        # 2 m plane.
        bmesh.ops.create_grid(bm, x_segments=1, y_segments=1, size=1.0)
    elif object_type == "sphere":
        _uv_sphere(bm)
    elif object_type in ("cylinder", "cone"):
        _cone(bm, cap_radius=0.0 if object_type == "cone" else 1.0)
    elif object_type == "torus":
        _torus(bm)
    else:  # pragma: no cover - guarded by the caller
        bm.free()
        raise ActionError(protocol.INVALID_OBJECT_TYPE, f"Unsupported object type '{object_type}'")
    bm.normal_update()
    return bm


def _uv_sphere(bm: bmesh.types.BMesh) -> None:
    """A 2 m sphere. Blender 4.0 renamed ``diameter`` to ``radius``."""
    common = {"u_segments": 32, "v_segments": 16, "calc_uvs": True}
    try:
        bmesh.ops.create_uvsphere(bm, radius=1.0, **common)
    except TypeError:
        bmesh.ops.create_uvsphere(bm, diameter=2.0, **common)


def _cone(bm: bmesh.types.BMesh, cap_radius: float) -> None:
    """A 2 m tall cylinder, or a cone when ``cap_radius`` is zero.

    Blender 4.0 renamed ``diameter1``/``diameter2`` to ``radius1``/``radius2``.
    """
    common = {
        "cap_ends": True,
        "cap_tris": False,
        "segments": 32,
        "depth": 2.0,
        "calc_uvs": True,
    }
    try:
        bmesh.ops.create_cone(bm, radius1=1.0, radius2=cap_radius, **common)
    except TypeError:
        bmesh.ops.create_cone(bm, diameter1=2.0, diameter2=cap_radius * 2.0, **common)


def _torus(bm: bmesh.types.BMesh, major_radius: float = 1.0, minor_radius: float = 0.25) -> None:
    """A parametric torus, matching Blender's default primitive.

    ``bmesh.ops`` has no torus, and building one by hand is a handful of
    predictable lines that work the same on every Blender version.
    """
    major_segments, minor_segments = 48, 12
    grid: list[list[Any]] = []
    for i in range(major_segments):
        u = 2.0 * math.pi * i / major_segments
        ring = []
        for j in range(minor_segments):
            v = 2.0 * math.pi * j / minor_segments
            distance = major_radius + minor_radius * math.cos(v)
            ring.append(
                bm.verts.new(
                    (
                        distance * math.cos(u),
                        distance * math.sin(u),
                        minor_radius * math.sin(v),
                    )
                )
            )
        grid.append(ring)
    for i in range(major_segments):
        for j in range(minor_segments):
            bm.faces.new(
                (
                    grid[i][j],
                    grid[(i + 1) % major_segments][j],
                    grid[(i + 1) % major_segments][(j + 1) % minor_segments],
                    grid[i][(j + 1) % minor_segments],
                )
            )


def _new_primitive(object_type: str, name: str, collection: Any) -> Any:
    """Create, name and link a primitive mesh object."""
    try:
        bm = _build_bmesh(object_type)
    except ActionError:
        raise
    except Exception as exc:  # pragma: no cover - only on an unexpected bmesh API
        _LOGGER.warning("bmesh could not build a %s, falling back to the operator: %s", object_type, exc)
        return _new_primitive_via_operator(object_type, name, collection)

    mesh = bpy.data.meshes.new(f"{name}Mesh")
    bm.to_mesh(mesh)
    bm.free()
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    return obj


def _new_primitive_via_operator(object_type: str, name: str, collection: Any) -> Any:
    """Fallback path. Needs a valid context, so GUI-only in practice."""
    operator = getattr(bpy.ops.mesh, _PRIMITIVE_OPS[object_type])
    operator()
    obj = bpy.context.view_layer.objects.active
    if obj is None:  # pragma: no cover - the operator always creates something
        raise ActionError(protocol.BLENDER_OPERATION_FAILED, f"The {object_type} operator produced no object")
    obj.name = name
    for current in list(obj.users_collection):
        current.objects.unlink(obj)
    collection.objects.link(obj)
    return obj


def update_object(params: dict[str, Any]) -> dict[str, Any]:
    """Change only the properties that were supplied."""
    obj = _get_object(str(params.get("name", "")))
    # The dimensions maths below reads obj.dimensions, which is derived from the
    # evaluated mesh and is stale until the depsgraph is refreshed.
    _sync()

    if params.get("location") is not None:
        obj.location = _vector3(params["location"], "location")
    if params.get("rotation") is not None:
        obj.rotation_euler = to_radians(_vector3(params["rotation"], "rotation"))
    if params.get("scale") is not None:
        obj.scale = _vector3(params["scale"], "scale")
    if params.get("dimensions") is not None:
        dimensions = _vector3(params["dimensions"], "dimensions")
        if any(value <= 0 for value in dimensions):
            raise ActionError(protocol.INVALID_PARAMETER, "'dimensions' must be positive")
        # dimensions = scale * local bounding box, so scale is derived rather
        # than set, which keeps non-uniform scale correct.
        local = [
            abs(obj.dimensions[index] / obj.scale[index]) if obj.scale[index] else 0.0 for index in range(3)
        ]
        if any(size <= 1e-9 for size in local):
            raise ActionError(
                protocol.INVALID_PARAMETER,
                f"Cannot derive a scale for '{obj.name}': it has no measurable size",
            )
        obj.scale = tuple(dimensions[index] / local[index] for index in range(3))
    if params.get("visibility") is not None:
        hidden = not bool(params["visibility"])
        obj.hide_viewport = hidden
        obj.hide_render = hidden

    renamed_to = _rename_object(obj, params.get("new_name"))
    material_result = _assign_material(obj, params)

    _sync()
    payload: dict[str, Any] = {"success": True, "object": _object_detail(obj)}
    if renamed_to is not None:
        payload["renamed_to"] = renamed_to
    if material_result is not None:
        payload.update(material_result)
    return payload


def _rename_object(obj: Any, new_name: Any) -> str | None:
    """Rename in place, refusing to collide.

    Blender's alternative is a silent ``Cube.001``, which breaks every later
    reference the model makes by name, so the collision is an error instead.
    """
    if new_name is None or not str(new_name).strip():
        return None
    new_name = str(new_name)
    if new_name == obj.name:
        return None  # idempotent: renaming to the same name is a no-op, not an error
    existing = bpy.data.objects.get(new_name)
    if existing is not None and existing is not obj:
        raise ActionError(
            protocol.OBJECT_ALREADY_EXISTS, f"Cannot rename '{obj.name}' to '{new_name}': that name is taken"
        )
    previous, obj.name = obj.name, new_name
    return f"{previous} -> {new_name}"


def _assign_material(obj: Any, params: dict[str, Any]) -> dict[str, Any] | None:
    """Assign a material by name, creating it when it does not exist.

    Creating it here rather than failing is what lets a model's loop converge in
    one call: "make this red" should not need a separate material-creation step
    before it can be verified. The response says whether it was created, so the
    model is never guessing.
    """
    name = params.get("material")
    if not name or not str(name).strip():
        return None
    if obj.type != "MESH":
        raise ActionError(
            protocol.INVALID_PARAMETER,
            f"Cannot assign a material to a {obj.type} object; only MESH objects have materials",
        )
    name = str(name)
    material = bpy.data.materials.get(name)
    created = False
    if material is None:
        material = bpy.data.materials.new(name)
        created = True
        color = params.get("material_color")
        if isinstance(color, (list, tuple)) and len(color) in (3, 4):
            _apply_material_colour(material, [float(component) for component in color[:3]])

    obj.data.materials.clear()
    obj.data.materials.append(material)
    return {"material": material.name, "material_created": created}


def _apply_material_colour(material: Any, rgb: list[float]) -> None:
    """Set the viewport colour *and* the shader.

    ``diffuse_color`` alone is the classic mistake: it colours the solid
    viewport, while the Principled BSDF keeps its default grey, so a render comes
    out white and the model cannot tell which of the two it is looking at.
    """
    rgba = (*rgb[:3], 1.0)
    material.diffuse_color = rgba
    if not material.use_nodes:
        material.use_nodes = True
    node_tree = material.node_tree
    bsdf = node_tree.nodes.get("Principled BSDF") if node_tree else None
    if bsdf is not None:
        bsdf.inputs["Base Color"].default_value = rgba
    else:  # pragma: no cover - a node tree without a Principled BSDF
        _LOGGER.warning("Material %s has no Principled BSDF; set its colour by hand", material.name)


def delete_object(params: dict[str, Any]) -> dict[str, Any]:
    """Delete an object after checking that it exists."""
    name = str(params.get("name", ""))
    obj = _get_object(name)
    object_type = obj.type
    bpy.data.objects.remove(obj, do_unlink=True)
    # Orphaned mesh data is left behind on purpose: other objects may still
    # reference it, and removing shared data is not this tool's decision.
    return {"success": True, "deleted": name, "type": object_type}


# --- render -----------------------------------------------------------------


def render(params: dict[str, Any]) -> dict[str, Any]:
    """Render the active scene.

    Any override is temporary: the scene's own engine, resolution, sample count
    and output path are put back afterwards. Two reasons. An AI that renders
    twice should get the same result both times without re-sending parameters,
    and a user who has set up Cycles in the UI should not find the scene
    switched to EEVEE because a model rendered a preview.
    """
    result = _render_now(params)
    _remember_render(result)
    return result


def render_preview(params: dict[str, Any]) -> dict[str, Any]:
    """Render small, for a model that wants to look at the result.

    Same machinery as :func:`render` with the size and sample count pinned to
    something cheap, and the output going to a fixed path so
    ``blender://render/latest`` always has something to serve.
    """
    request = dict(params)
    request.setdefault("resolution_x", DEFAULT_PREVIEW_RESOLUTION)
    request.setdefault("resolution_y", DEFAULT_PREVIEW_RESOLUTION)
    request["resolution_x"] = min(int(request["resolution_x"]), MAX_PREVIEW_RESOLUTION)
    request["resolution_y"] = min(int(request["resolution_y"]), MAX_PREVIEW_RESOLUTION)
    if not request.get("samples"):
        # Without a sample cap a "preview" can cost more than the final render.
        request["samples"] = DEFAULT_PREVIEW_SAMPLES
    request["output_path"] = _preview_path()
    result = _render_now(request)
    result["preview"] = True
    _remember_render(result)
    return result


def last_render(_params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Describe the most recent render this add-on performed.

    A render belongs to the file it was made in. Once another file is open the
    remembered image is not "the latest render" of anything the caller can see,
    so it is reported as absent rather than served under a caption it does not
    match.
    """
    remembered = _LAST_RENDER["result"]
    if remembered is not None and remembered.get("blend_file") != (bpy.data.filepath or None):
        raise ActionError(
            protocol.OBJECT_NOT_FOUND,
            f"The last render belongs to {remembered.get('blend_file') or 'an unsaved file'}, "
            "which is no longer open. Render again to have one here.",
        )
    if remembered is None:
        raise ActionError(
            protocol.OBJECT_NOT_FOUND,
            "Nothing has been rendered yet. Call blender.render or blender.render_preview first.",
        )
    result = dict(remembered)
    result["age_seconds"] = _round(time.time() - _LAST_RENDER["at"], 1)
    result["exists"] = os.path.exists(result.get("output_path", ""))
    return result


def _render_now(params: dict[str, Any]) -> dict[str, Any]:
    scene = bpy.context.scene
    overrides = _render_overrides(scene, params)
    applied = _apply_render_overrides(scene, overrides)
    try:
        started = time.perf_counter()
        error: str | None = None
        try:
            result = bpy.ops.render.render(write_still=True)
            if "FINISHED" not in result:
                error = f"render returned {result}"
        except RuntimeError as exc:  # Blender raises RuntimeError for render failures
            error = str(exc)
        render_time = time.perf_counter() - started
        # Read while the overrides are still applied, so it describes this render.
        output_path = _still_output_path(scene)
    finally:
        _restore_render_overrides(scene, overrides)

    if error is not None:
        raise ActionError(protocol.BLENDER_OPERATION_FAILED, f"Render failed: {error}")


    return {
        "success": True,
        "output_path": output_path,
        "render_time": _round(render_time, 3),
        "used": applied,
        "settings_restored": True,
    }


#: ``image_settings.file_format`` -> the extension Blender actually writes.
#: ``frame_path()`` is no help here: it appends a frame number, which is right
#: for an animation and wrong for the single still this renders.
_FILE_FORMAT_EXTENSIONS = {
    "PNG": ".png",
    "JPEG": ".jpg",
    "OPEN_EXR": ".exr",
    "TIFF": ".tif",
    "WEBP": ".webp",
    "BMP": ".bmp",
    "TARGA": ".targa",
    "IRIS": ".iris",
    "HDR": ".hdr",
    "AVI_JPEG": ".avi",
    "FFMPEG": ".mp4",
}


def _still_output_path(scene: Any) -> str:
    """The file a still render at these settings writes to.

    Blender's rule for ``write_still``: the path is used as given when it already
    ends in the format's extension, and otherwise the extension is appended. So
    ``/out/shot`` becomes ``/out/shot.png`` while ``/out/shot.png`` is left alone,
    and a directory such as ``/out/`` becomes ``/out/.png`` — which is ugly but
    is what Blender does, and reporting anything else would name a file that is
    not there.
    """
    path = bpy.path.abspath(scene.render.filepath)
    if not scene.render.use_file_extension:
        return path
    suffix = _FILE_FORMAT_EXTENSIONS.get(scene.render.image_settings.file_format, ".png")
    return path if path.lower().endswith(suffix) else path + suffix


def _preview_path() -> str:
    """Where previews go: a stable name next to Blender's temp directory."""
    return os.path.join(bpy.app.tempdir, PREVIEW_FILENAME)


def forget_render() -> None:
    """Drop the remembered render.

    Called when Blender loads another file: the picture belongs to the scene that
    was open when it was made, and serving it as "the latest render" of a
    different scene is worse than reporting nothing.
    """
    _LAST_RENDER["result"] = None
    _LAST_RENDER["at"] = 0.0


def _remember_render(result: dict[str, Any]) -> None:
    result = dict(result)
    # Which file this belongs to, so serving it later cannot pass it off as a
    # render of a different scene.
    result["blend_file"] = bpy.data.filepath or None
    _LAST_RENDER["result"] = result
    _LAST_RENDER["at"] = time.time()


def _render_overrides(scene: Any, params: dict[str, Any]) -> dict[str, Any]:
    """What the caller asked for, plus the values needed to undo it."""
    settings = scene.render
    return {
        "engine": str(params["engine"]) if params.get("engine") else None,
        "resolution_x": _optional_int(params.get("resolution_x")),
        "resolution_y": _optional_int(params.get("resolution_y")),
        "resolution_percentage": _optional_int(params.get("resolution_percentage")),
        "samples": int(params["samples"]) if params.get("samples") else None,
        "filepath": str(params["output_path"]) if params.get("output_path") else None,
        "previous": {
            "engine": settings.engine,
            "resolution_x": settings.resolution_x,
            "resolution_y": settings.resolution_y,
            "resolution_percentage": settings.resolution_percentage,
            "filepath": settings.filepath,
            "samples": getattr(getattr(scene, "cycles", None), "samples", None),
        },
    }


def _apply_render_overrides(scene: Any, overrides: dict[str, Any]) -> dict[str, Any]:
    """Apply the overrides and report the settings actually used."""
    settings = scene.render
    if overrides["engine"]:
        try:
            settings.engine = overrides["engine"]
        except TypeError as exc:
            # Blender raises TypeError for an engine id it does not know, and the
            # id changed name between 4.x and 5.x, so hand back the working set.
            raise ActionError(
                protocol.INVALID_PARAMETER,
                f"Unknown render engine '{overrides['engine']}'",
                details={
                    "available": available_engines(scene),
                    "known_ids": list(KNOWN_ENGINES),
                },
            ) from exc
    for field in ("resolution_x", "resolution_y", "resolution_percentage"):
        if overrides[field] is not None:
            setattr(settings, field, overrides[field])
    if overrides["samples"] is not None and hasattr(scene, "cycles"):
        scene.cycles.samples = overrides["samples"]
    if overrides["filepath"] is not None:
        settings.filepath = overrides["filepath"]
    elif not settings.filepath:
        settings.filepath = "//mcp_render.png"

    return {
        "engine": settings.engine,
        "resolution": [settings.resolution_x, settings.resolution_y],
        "samples": getattr(getattr(scene, "cycles", None), "samples", None),
    }


def _restore_render_overrides(scene: Any, overrides: dict[str, Any]) -> None:
    """Put back exactly the values that were there before."""
    previous = overrides["previous"]
    settings = scene.render
    if overrides["engine"]:
        settings.engine = previous["engine"]
    for field in ("resolution_x", "resolution_y", "resolution_percentage"):
        if overrides[field] is not None:
            setattr(settings, field, previous[field])
    if overrides["samples"] is not None and previous["samples"] is not None:
        scene.cycles.samples = previous["samples"]
    if overrides["filepath"] is not None:
        settings.filepath = previous["filepath"]


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


# --- python execution -------------------------------------------------------


def execute_python(params: dict[str, Any]) -> dict[str, Any]:
    """Execute code with a restricted namespace and report what it produced."""
    from blender_mcp.executor import execute_user_code

    code = params.get("code")
    if not isinstance(code, str) or not code.strip():
        raise ActionError(protocol.INVALID_PARAMETER, "'code' must be a non-empty string")
    return execute_user_code(code)


# --- dispatch ---------------------------------------------------------------

HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    protocol.GET_SCENE: get_scene,
    protocol.GET_OBJECTS: get_objects,
    protocol.GET_OBJECT: get_object,
    protocol.PING: ping,
    protocol.CREATE_OBJECT: create_object,
    protocol.UPDATE_OBJECT: update_object,
    protocol.DELETE_OBJECT: delete_object,
    protocol.RENDER: render,
    protocol.RENDER_PREVIEW: render_preview,
    protocol.LAST_RENDER: last_render,
    protocol.EXECUTE_PYTHON: execute_python,
}

READ_ONLY_ACTIONS = frozenset({protocol.GET_SCENE, protocol.GET_OBJECTS, protocol.GET_OBJECT, protocol.PING})


def dispatch(action: str, params: dict[str, Any]) -> dict[str, Any]:
    """Route one action to its handler."""
    handler = HANDLERS.get(action)
    if handler is None:
        raise ActionError(
            protocol.UNKNOWN_ACTION,
            f"Unknown action '{action}'. Supported: {', '.join(sorted(HANDLERS))}",
        )
    return handler(params)
