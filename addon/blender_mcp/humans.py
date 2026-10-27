"""Human characters, built with MPFB inside Blender.

MPFB (MakeHuman Plugin For Blender) is a separate extension. This module never
installs or enables it: it looks for it among the loaded modules and, when it is
not there, says so with a code the model can act on. Enabling an extension
writes the person's preferences, which is their decision, not a tool's.

Everything that does not need ``bpy`` -- age conversion, target names, choosing
a skin -- lives in :mod:`blender_mcp.humans_catalog`, where it can be tested.

Three things in here are easy to get wrong, and each is a bug that happened:

* **dimensions lag.** A body's height is read after ``view_layer.update()``;
  before it, ``dimensions`` still reports the shape before the macros changed.
* **macros are two steps.** Setting a value on the object changes nothing until
  ``reapply_macro_details`` rebuilds the shape keys from it.
* **parts follow the body only when refitted.** Eyes fitted to one face float
  in front of the next one unless ``refit`` runs after a shape change.
* **parts are placed as if the body stood at the origin.** MPFB copies the
  body's location into a new part *and* parents it, so a body at x=2 gets its
  eyes at x=4. Parts are therefore added and refitted with the body moved to
  the origin, and the body is put back afterwards.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import re
import sys
from collections.abc import Callable, Iterator, Mapping
from typing import Any

import bpy  # type: ignore[import-not-found]

from blender_mcp import humans_catalog as catalog
from blender_mcp import protocol
from blender_mcp.protocol import ActionError

#: MPFB's services, by the module suffix they are found under. An extension is
#: imported as ``bl_ext.<repository>.mpfb``, and the repository part depends on
#: where it was installed from, so only the suffix is stable.
_SERVICES: dict[str, str] = {
    "HumanService": "mpfb.services.humanservice",
    "TargetService": "mpfb.services.targetservice",
    "AssetService": "mpfb.services.assetservice",
    "LocationService": "mpfb.services.locationservice",
    "ObjectService": "mpfb.services.objectservice",
    "HumanObjectProperties": "mpfb.entities.objectproperties",
}

_ENABLE_HINT = (
    "Install MPFB from Blender's Get Extensions, then tick it in Edit > Preferences > Add-ons. "
    "The tool does not enable it itself."
)
_ASSETS_HINT = (
    "Download makehuman_system_assets from "
    "https://static.makehumancommunity.org/assets/assetpacks/makehuman_system_assets.html "
    "and install it from MPFB's sidebar tab: Start here > Install pack from zip."
)

#: The part types MPFB writes on an asset object, by the asset folder name.
_PART_TYPES: dict[str, str] = {part: part.capitalize() for part in catalog.BODYPARTS}

#: Height search: how close is close enough, and how many tries it gets.
_HEIGHT_TOLERANCE_M = 0.005
_HEIGHT_STEPS = 9

MAX_SUBDIVISION_LEVELS = 3
MAX_LOW_LODS = 4

#: Custom properties on the body: the skin's asset name, and whether the tool
#: picked it. The material name alone does not say which asset it came from.
_SKIN_PROPERTY = "mcp_skin"
_SKIN_AUTO_PROPERTY = "mcp_skin_auto"


# --- finding MPFB --------------------------------------------------------------


def _find_services() -> dict[str, Any] | None:
    services: dict[str, Any] = {}
    for key, suffix in _SERVICES.items():
        module_name = next((name for name in list(sys.modules) if name.endswith(suffix)), None)
        if module_name is None:
            return None
        services[key] = getattr(importlib.import_module(module_name), key)
    return services


def _mpfb() -> dict[str, Any]:
    services = _find_services()
    if services is None:
        raise ActionError(
            protocol.HUMAN_GENERATOR_UNAVAILABLE,
            "MPFB is not enabled in this Blender, so humans cannot be created.",
            {"hint": _ENABLE_HINT},
        )
    return services


def _assets_installed(services: Mapping[str, Any]) -> bool:
    try:
        installed, _current = services["AssetService"].check_if_modern_makehuman_system_assets_installed()
    except Exception:  # noqa: BLE001 - an MPFB without the check is treated as "unknown, so no"
        return False
    return bool(installed)


def _require_assets(services: Mapping[str, Any], wanted: str) -> None:
    if not _assets_installed(services):
        raise ActionError(
            protocol.HUMAN_ASSETS_MISSING,
            f"{wanted} needs the MakeHuman system asset pack, which is not installed.",
            {"hint": _ASSETS_HINT},
        )


def _mpfb_version() -> str | None:
    module_name = next((name for name in sys.modules if name.endswith(".mpfb") or name == "mpfb"), None)
    module = sys.modules.get(module_name or "")
    manifest = os.path.join(os.path.dirname(getattr(module, "__file__", "") or ""), "blender_manifest.toml")
    try:
        with open(manifest, encoding="utf-8") as handle:
            match = re.search(r'^version\s*=\s*"([^"]+)"', handle.read(), re.MULTILINE)
    except OSError:
        return None
    return match.group(1) if match else None


def _assets(services: Mapping[str, Any], subdir: str, extension: str) -> list[str]:
    roots = services["AssetService"].get_asset_roots(subdir)
    files = services["AssetService"].find_asset_files_matching_pattern(roots, f"*.{extension}")
    return sorted({os.path.basename(str(path))[: -len(extension) - 1] for path in files})


def _target_catalog(services: Mapping[str, Any]) -> dict[str, Any]:
    path = os.path.join(services["LocationService"].get_mpfb_data("targets"), "target.json")
    with open(path, encoding="utf-8") as handle:
        loaded: dict[str, Any] = json.load(handle)
    return loaded


# --- reading a human -------------------------------------------------------------


def _get_human(services: Mapping[str, Any], name: Any) -> Any:
    if not isinstance(name, str) or not name.strip():
        raise ActionError(protocol.INVALID_PARAMETER, "'name' must be a non-empty string")
    obj = bpy.data.objects.get(name)
    if obj is None:
        raise ActionError(protocol.OBJECT_NOT_FOUND, f"No object named '{name}'")
    if services["ObjectService"].get_object_type(obj) != "Basemesh":
        raise ActionError(
            protocol.INVALID_OBJECT_TYPE,
            f"'{name}' is not a human made with MPFB; pass the body object, not one of its parts.",
        )
    return obj


def _parts(services: Mapping[str, Any], body: Any) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for part, kind in _PART_TYPES.items():
        objects = services["ObjectService"].find_all_objects_of_type_amongst_nearest_relatives(
            body, kind, only_children=True
        )
        names = sorted(obj.name for obj in objects)
        if names:
            found[part] = names
    return found


def _faces(obj: Any) -> int:
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    mesh = evaluated.to_mesh()
    try:
        return len(mesh.polygons)
    finally:
        evaluated.to_mesh_clear()


def _height(obj: Any) -> float:
    bpy.context.view_layer.update()
    return round(float(obj.dimensions.z), 4)


def _describe(services: Mapping[str, Any], body: Any, **extra: Any) -> dict[str, Any]:
    props = services["HumanObjectProperties"]
    macros = {key: round(float(props.get_value(key, entity_reference=body)), 4) for key in catalog.MACROS}
    race = {key: round(float(props.get_value(key, entity_reference=body)), 4) for key in catalog.RACES}
    keys = body.data.shape_keys.key_blocks if body.data.shape_keys else []
    targets = {
        key.name: round(float(key.value), 4)
        for key in keys
        if key.name != "Basis" and not key.name.startswith(("$md", "macrodetail")) and key.value
    }
    materials = [slot.material.name for slot in body.material_slots if slot.material]
    skin = body.get(_SKIN_PROPERTY) or (materials[0] if materials else None)
    return {
        "name": body.name,
        "height_m": _height(body),
        "location": [round(value, 4) for value in body.location],
        "age_years": catalog.macro_to_age(macros["age"]),
        "macros": macros,
        "race": race,
        "targets": targets,
        "skin": skin,
        "parts": _parts(services, body),
        "faces": _faces(body),
        "modifiers": [modifier.name for modifier in body.modifiers],
        **extra,
    }


# --- shaping a body --------------------------------------------------------------


def _numbers(params: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, float]:
    values: dict[str, float] = {}
    for key in keys:
        if params.get(key) is None:
            continue
        try:
            value = float(params[key])
        except (TypeError, ValueError) as exc:
            raise ActionError(protocol.INVALID_PARAMETER, f"'{key}' must be a number") from exc
        if not 0.0 <= value <= 1.0:
            raise ActionError(protocol.INVALID_PARAMETER, f"'{key}' must be within 0..1, got {value:g}")
        values[key] = value
    return values


def _requested_macros(params: Mapping[str, Any]) -> dict[str, float]:
    """The macro values a request sets, with age converted from years."""
    macros = _numbers(params, ("gender", "muscle", "weight", "proportions", "cupsize", "firmness"))
    if params.get("age_years") is not None:
        try:
            years = float(params["age_years"])
        except (TypeError, ValueError) as exc:
            raise ActionError(protocol.INVALID_PARAMETER, "'age_years' must be a number") from exc
        if not catalog.MIN_AGE_YEARS <= years <= catalog.MAX_AGE_YEARS:
            raise ActionError(
                protocol.INVALID_PARAMETER,
                f"'age_years' must be within {catalog.MIN_AGE_YEARS:g}..{catalog.MAX_AGE_YEARS:g}",
            )
        macros["age"] = catalog.age_to_macro(years)
    return macros


def _apply_macros(
    services: Mapping[str, Any], body: Any, macros: Mapping[str, float], race: Mapping[str, float]
) -> None:
    props = services["HumanObjectProperties"]
    for key, value in {**macros, **race}.items():
        props.set_value(key, value, entity_reference=body)
    services["TargetService"].reapply_macro_details(body)


def _fit_height(services: Mapping[str, Any], body: Any, height_m: float) -> dict[str, Any]:
    """Search the height macro for ``height_m``; scale the rest of the way.

    The macro changes proportions the way a real height difference does, which
    a uniform scale does not, so it is searched first. A height outside what the
    macro can reach for this body is finished with a uniform scale, and the
    response says so.
    """
    props = services["HumanObjectProperties"]

    def measure(value: float) -> float:
        props.set_value("height", value, entity_reference=body)
        services["TargetService"].reapply_macro_details(body)
        return _height(body)

    low, high = 0.0, 1.0
    shortest, tallest = measure(low), measure(high)
    if height_m <= shortest:
        value, reached = low, measure(low)
    elif height_m >= tallest:
        value, reached = high, tallest
    else:
        value, reached = 0.5, measure(0.5)
        for _ in range(_HEIGHT_STEPS):
            if abs(reached - height_m) <= _HEIGHT_TOLERANCE_M:
                break
            if reached < height_m:
                low = value
            else:
                high = value
            value = (low + high) / 2
            reached = measure(value)
    scaled = 1.0
    if abs(reached - height_m) > _HEIGHT_TOLERANCE_M and reached > 0:
        scaled = height_m / reached
        body.scale = [component * scaled for component in body.scale]
    return {"height_macro": round(value, 4), "uniform_scale": round(scaled, 4)}


def _apply_targets(services: Mapping[str, Any], body: Any, requested: Mapping[str, Any]) -> list[str]:
    if not requested:
        return []
    if not isinstance(requested, Mapping):
        raise ActionError(protocol.INVALID_PARAMETER, "'targets' must be an object of {name: value}")
    target_service = services["TargetService"]
    resolved = catalog.resolve_targets(_target_catalog(services), requested)
    applied: list[str] = []
    for target, weight in resolved:
        path = target_service.target_full_path(target)
        if path is None:
            raise ActionError(protocol.INVALID_PARAMETER, f"MPFB has no file for target '{target}'")
        key_name = target_service.filename_to_shapekey_name(path)
        keys = body.data.shape_keys.key_blocks if body.data.shape_keys else {}
        existing = keys.get(key_name) if keys else None
        if existing is not None:
            existing.value = weight
        elif weight:
            target_service.load_target(body, path, weight=weight)
        else:
            continue
        applied.append(target)
    return applied


# --- dressing a body -------------------------------------------------------------


def _set_skin(services: Mapping[str, Any], body: Any, skin: Any, skin_type: Any) -> str | None:
    if skin is None or skin == "none":
        return None
    skin_type = str(skin_type or "ENHANCED_SSS").upper()
    if skin_type not in catalog.SKIN_TYPES:
        raise ActionError(
            protocol.INVALID_PARAMETER, f"'skin_type' must be one of {', '.join(catalog.SKIN_TYPES)}"
        )
    _require_assets(services, "A skin")
    available = _assets(services, "skins", "mhmat")
    if skin == "auto":
        chosen = _auto_skin(services, body, available)
        if chosen is None:
            raise ActionError(
                protocol.HUMAN_ASSETS_MISSING, "No skins are installed.", {"hint": _ASSETS_HINT}
            )
    elif skin in available:
        chosen = str(skin)
    else:
        raise ActionError(
            protocol.INVALID_PARAMETER,
            f"No skin named '{skin}'.",
            {"available": available},
        )
    path = services["AssetService"].find_asset_absolute_path(f"{chosen}.mhmat", asset_subdir="skins")
    services["HumanService"].set_character_skin(path, body, skin_type=skin_type)
    # Remembered on the body, so a later age or sex change can follow with the
    # skin -- but only a skin the tool chose; one the person named stays.
    body[_SKIN_PROPERTY] = chosen
    body[_SKIN_AUTO_PROPERTY] = skin == "auto"
    return chosen


def _auto_skin(services: Mapping[str, Any], body: Any, available: list[str]) -> str | None:
    props = services["HumanObjectProperties"]
    race = {key: float(props.get_value(key, entity_reference=body)) for key in catalog.RACES}
    return catalog.skin_for(
        float(props.get_value("gender", entity_reference=body)),
        catalog.macro_to_age(float(props.get_value("age", entity_reference=body))),
        race,
        available,
    )


@contextlib.contextmanager
def _at_origin(body: Any) -> Iterator[None]:
    """Hold the body at the origin while MPFB places parts on it; see the module note."""
    location = tuple(body.location)
    body.location = (0.0, 0.0, 0.0)
    bpy.context.view_layer.update()
    try:
        yield
    finally:
        body.location = location
        bpy.context.view_layer.update()


def _set_part(services: Mapping[str, Any], body: Any, part: str, choice: Any) -> str | None:
    """Put one part on, replacing whatever was there. ``"none"`` takes it off."""
    human_service, object_service = services["HumanService"], services["ObjectService"]
    for old in list(
        object_service.find_all_objects_of_type_amongst_nearest_relatives(
            body, _PART_TYPES[part], only_children=True
        )
    ):
        human_service.unload_mhclo_asset(body, old)
    if choice is None or choice == "none":
        return None
    _require_assets(services, f"'{part}'")
    available = _assets(services, part, "mhclo")
    if choice not in available:
        raise ActionError(
            protocol.INVALID_PARAMETER, f"No {part} asset named '{choice}'.", {"available": available}
        )
    path = services["AssetService"].find_asset_absolute_path(f"{choice}.mhclo", asset_subdir=part)
    human_service.add_mhclo_asset(path, body, asset_type=_PART_TYPES[part])
    return str(choice)


def _dress(
    services: Mapping[str, Any], body: Any, params: Mapping[str, Any], *, creating: bool
) -> dict[str, Any]:
    """Skin and parts, as asked. A new human gets the defaults for what is not asked."""
    dressed: dict[str, Any] = {}
    warnings: list[str] = []
    assets = _assets_installed(services)
    skin = params.get("skin", "auto" if creating else None)
    if skin is not None:
        if assets or "skin" in params:
            dressed["skin"] = _set_skin(services, body, skin, params.get("skin_type"))
        else:
            warnings.append("No skin: the MakeHuman system asset pack is not installed.")
    for part in catalog.BODYPARTS:
        if part in params:
            dressed[part] = _set_part(services, body, part, params[part])
        elif creating and catalog.DEFAULT_PARTS[part]:
            if assets:
                dressed[part] = _set_part(services, body, part, catalog.DEFAULT_PARTS[part])
            else:
                warnings.append(f"No {part}: the MakeHuman system asset pack is not installed.")
    if warnings:
        dressed["warnings"] = warnings + [_ASSETS_HINT]
    return dressed


# --- actions ---------------------------------------------------------------------


def get_human_options(params: dict[str, Any] | None = None) -> dict[str, Any]:
    """What can be built here: whether MPFB is on, and what it has to offer."""
    params = params or {}
    services = _find_services()
    if services is None:
        return {"available": False, "reason": "MPFB is not enabled in this Blender.", "hint": _ENABLE_HINT}
    assets = _assets_installed(services)
    categories = catalog.target_categories(_target_catalog(services))
    section = params.get("section")
    result: dict[str, Any] = {
        "available": True,
        "mpfb_version": _mpfb_version(),
        "system_assets": assets,
        "skins": _assets(services, "skins", "mhmat") if assets else [],
        **{part: (_assets(services, part, "mhclo") if assets else []) for part in catalog.BODYPARTS},
        "skin_types": list(catalog.SKIN_TYPES),
        "target_sections": {
            name: sum(1 for category in categories.values() if category["section"] == name)
            for name in sorted({category["section"] for category in categories.values()})
        },
    }
    if not assets:
        result["hint"] = _ASSETS_HINT
    if section:
        chosen = [
            catalog.describe_category(category)
            for category in categories.values()
            if category["section"] == section
        ]
        if not chosen:
            raise ActionError(
                protocol.INVALID_PARAMETER,
                f"No target section named '{section}'.",
                {"sections": sorted(result["target_sections"])},
            )
        result["targets"] = {section: chosen}
    return result


def create_human(params: dict[str, Any]) -> dict[str, Any]:
    """A new human, shaped and dressed, under a name nobody else has."""
    services = _mpfb()
    name = params.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ActionError(protocol.INVALID_PARAMETER, "'name' must be a non-empty string")
    if bpy.data.objects.get(name) is not None:
        raise ActionError(protocol.OBJECT_ALREADY_EXISTS, f"Object '{name}' already exists")
    macros = _requested_macros(params)
    race = catalog.normalise_race(params["race"]) if params.get("race") else {}
    # Validated before anything is built: a typo in a target must not leave a
    # half-made body behind.
    if params.get("targets"):
        catalog.resolve_targets(_target_catalog(services), params["targets"])

    body = services["HumanService"].create_human()
    try:
        body.name = name
        if body.data is not None:
            body.data.name = name
        _apply_macros(services, body, macros, race)
        fitted = _fit_height(services, body, float(params["height_m"])) if params.get("height_m") else {}
        applied = _apply_targets(services, body, params.get("targets") or {})
        with _at_origin(body):
            dressed = _dress(services, body, params, creating=True)
        # Placed last, once every part is parented: see the module note.
        if params.get("location") is not None:
            body.location = [float(value) for value in params["location"]]
    except Exception:
        _remove_with_children(body)
        raise
    return {"success": True, "human": _describe(services, body, targets_applied=applied, **fitted, **dressed)}


def update_human(params: dict[str, Any]) -> dict[str, Any]:
    """Change only what is passed; refit the parts when the body changed."""
    services = _mpfb()
    body = _get_human(services, params.get("name"))
    macros = _requested_macros(params)
    race = catalog.normalise_race(params["race"]) if params.get("race") else {}
    if macros or race:
        _apply_macros(services, body, macros, race)
    fitted = _fit_height(services, body, float(params["height_m"])) if params.get("height_m") else {}
    applied = _apply_targets(services, body, params.get("targets") or {})
    reshaped = bool(macros or race or fitted or applied)
    request = dict(params)
    if (
        (race or "gender" in macros or "age" in macros)
        and "skin" not in params
        and body.get(_SKIN_AUTO_PROPERTY)
    ):
        # The skin was chosen for the old age, sex or race; choose again.
        if _auto_skin(services, body, _assets(services, "skins", "mhmat")) != body.get(_SKIN_PROPERTY):
            request["skin"] = "auto"
    with _at_origin(body):
        if reshaped and _parts(services, body):
            services["HumanService"].refit(body)
        dressed = _dress(services, body, request, creating=False)
    if not (reshaped or dressed):
        raise ActionError(
            protocol.INVALID_PARAMETER,
            "Nothing to update: pass a body value, targets, height_m, skin or a part.",
        )
    return {"success": True, "human": _describe(services, body, targets_applied=applied, **fitted, **dressed)}


def add_human_lod(params: dict[str, Any]) -> dict[str, Any]:
    """More detail on the body itself, and lighter copies of it beside it.

    Detail is a Multires modifier: subdivision now, a place to sculpt or bake a
    displacement map into later. The light copies are the body mesh only --
    eyes, brows and hair stay with the original -- each with a Decimate
    modifier, placed side by side so they can be compared.
    """
    services = _mpfb()
    body = _get_human(services, params.get("name"))
    levels = int(params.get("subdivision_levels") or 0)
    if not 0 <= levels <= MAX_SUBDIVISION_LEVELS:
        raise ActionError(
            protocol.INVALID_PARAMETER, f"'subdivision_levels' must be within 0..{MAX_SUBDIVISION_LEVELS}"
        )
    ratios = [float(ratio) for ratio in params.get("low_ratios") or []]
    if len(ratios) > MAX_LOW_LODS or any(not 0.01 <= ratio < 1.0 for ratio in ratios):
        raise ActionError(
            protocol.INVALID_PARAMETER,
            f"'low_ratios' takes up to {MAX_LOW_LODS} values in 0.01..0.99, e.g. [0.5, 0.25]",
        )
    for name in lod_names(body.name, len(ratios)):
        if bpy.data.objects.get(name) is not None:
            raise ActionError(protocol.OBJECT_ALREADY_EXISTS, f"Object '{name}' already exists")

    lods: list[dict[str, Any]] = [{"name": body.name, "level": "LOD0", "faces": None}]
    if levels:
        _subdivide(body, levels)
    lods[0]["faces"] = _faces(body)

    spacing = float(params.get("spacing_m") or max(0.8, float(body.dimensions.x) * 1.2))
    for index, (ratio, name) in enumerate(
        zip(ratios, lod_names(body.name, len(ratios)), strict=True), start=1
    ):
        copy = body.copy()
        copy.data = body.data.copy()
        copy.name = name
        copy.data.name = name
        for collection in body.users_collection:
            collection.objects.link(copy)
        for modifier in [modifier for modifier in copy.modifiers if modifier.type == "MULTIRES"]:
            copy.modifiers.remove(modifier)
        decimate = copy.modifiers.new("MCP_LOD", "DECIMATE")
        decimate.ratio = ratio
        copy.location = (body.location.x + spacing * index, body.location.y, body.location.z)
        lods.append({"name": name, "level": f"LOD{index}", "ratio": ratio, "faces": _faces(copy)})
    return {"success": True, "lods": lods, "subdivision_levels": levels}


def lod_names(name: str, count: int) -> list[str]:
    """The objects ``add_human_lod`` creates, so a transaction can record them first."""
    return [f"{name}_LOD{index}" for index in range(1, count + 1)]


def _subdivide(body: Any, levels: int) -> None:
    modifier = next((modifier for modifier in body.modifiers if modifier.type == "MULTIRES"), None)
    if modifier is None:
        modifier = body.modifiers.new("MCP_Detail", "MULTIRES")
    while modifier.total_levels < levels:
        with bpy.context.temp_override(object=body, active_object=body):
            bpy.ops.object.multires_subdivide(modifier=modifier.name, mode="CATMULL_CLARK")
    modifier.levels = modifier.sculpt_levels = modifier.render_levels = levels


def _remove_with_children(obj: Any) -> None:
    for child in list(obj.children_recursive):
        bpy.data.objects.remove(child, do_unlink=True)
    bpy.data.objects.remove(obj, do_unlink=True)


HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    protocol.GET_HUMAN_OPTIONS: get_human_options,
    protocol.CREATE_HUMAN: create_human,
    protocol.UPDATE_HUMAN: update_human,
    protocol.ADD_HUMAN_LOD: add_human_lod,
}
