"""Human characters, through MPFB.

A body is not something a model can sculpt one vertex at a time; it is
something it can *describe*. These tools take the description -- sex, age in
years, height in metres, how muscular and how heavy -- and the add-on turns it
into an MPFB character with skin, eyes and brows. The numbers a model has to
choose are the ones a person would say out loud.

MPFB is a separate Blender extension. When it is missing the add-on answers
HUMAN_GENERATOR_UNAVAILABLE, and when its asset pack is missing,
HUMAN_ASSETS_MISSING; both carry a hint a person can follow.

Building a human is slow by this server's standards -- a skin with subsurface
scattering is several seconds -- so these calls get the render timeout rather
than the query timeout.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context
from pydantic import Field

from server.blender.protocol import Action
from server.errors import BlenderMCPError, ErrorCode
from server.mcp.support import bridge_of, register_all, settings_of, tool_error

logger = logging.getLogger(__name__)

BODYPARTS: tuple[str, ...] = ("eyes", "eyebrows", "eyelashes", "hair")
SKIN_TYPES: tuple[str, ...] = ("ENHANCED_SSS", "MAKESKIN", "GAMEENGINE", "LAYERED")
RACES: tuple[str, ...] = ("african", "asian", "caucasian")
#: Values in 0..1, 0.5 being average.
UNIT_VALUES: tuple[str, ...] = ("gender", "muscle", "weight", "proportions", "cupsize", "firmness")

MIN_AGE, MAX_AGE = 1.0, 90.0
MIN_HEIGHT_M, MAX_HEIGHT_M = 0.5, 2.6
MAX_SUBDIVISION_LEVELS = 3
MAX_LOW_LODS = 4

_BODY_GUIDE = """\
Body values are 0..1 with 0.5 average:
  gender       0 = female, 1 = male (0.5 is androgynous; pick a side)
  muscle       0 = no definition, 1 = bodybuilder
  weight       0 = very thin, 1 = obese
  proportions  0 = unremarkable, 1 = idealised
  cupsize, firmness   breast size and shape
age_years: 1..90. height_m: exact height in metres; omit for average.
Rough starting points: athletic = muscle 0.75, weight 0.5; skinny = muscle 0.35,
weight 0.15; heavy = muscle 0.4, weight 0.85; bodybuilder = muscle 1.0, weight 0.65.
race: optional weights for african, asian, caucasian, normalised for you. Set it
when the description implies an appearance: without it the body is an even blend
and skin "auto" falls back to the caucasian skins.
targets: fine shape as {category: value}. Categories ending in -decr-incr,
-down-up, -in-out and similar take -1..1 (negative = the first word); the others
take 0..1. Find names with blender.get_human_options(section=...), e.g.
{"nose-volume-decr-incr": 0.4, "chin-prominent-decr-incr": -0.3}.\
"""

GET_HUMAN_OPTIONS_DESCRIPTION = """\
Check whether humans can be created here, and list what they can be made of.

Returns "available" (MPFB is enabled), "system_assets" (the skin and part pack
is installed), the installed skins, eyes, eyebrows, eyelashes and hair, and the
target sections with how many categories each has. Pass section (e.g. "nose",
"torso", "head") to get that section's target categories and the range each takes.

Call this before blender.create_human the first time; when "available" is false
or "system_assets" is false, tell the user the "hint" rather than retrying.\
"""

CREATE_HUMAN_DESCRIPTION = f"""\
Create a realistic human body (an MPFB character) from a description.

name: unique; an existing name fails with OBJECT_ALREADY_EXISTS.
location: [x, y, z] in metres; the feet stand on z.
{_BODY_GUIDE}
skin: "auto" (default) picks one by sex, age and race; or a name from
      blender.get_human_options; or "none".
skin_type: ENHANCED_SSS (default, realistic), MAKESKIN, GAMEENGINE, LAYERED.
eyes / eyebrows / eyelashes / hair: an asset name, or "none". Defaults: eyes
      "high-poly", eyebrows "eyebrow001", eyelashes "eyelashes01", no hair.

Returns the body: real height, age, the values set, the skin, the part objects
and the face count. The parts are children of the body object.
Example: {{"name": "Runner", "gender": 1, "age_years": 30, "height_m": 1.82,
"muscle": 0.75, "weight": 0.45, "hair": "short02"}}
Fails with HUMAN_GENERATOR_UNAVAILABLE when MPFB is not enabled, and with
HUMAN_ASSETS_MISSING when a skin or part is asked for without the asset pack.\
"""

UPDATE_HUMAN_DESCRIPTION = f"""\
Change an existing human. Only what you pass changes; the eyes, brows and hair
are refitted to the new shape automatically.

name: the body object (not one of its parts).
{_BODY_GUIDE}
skin / skin_type / eyes / eyebrows / eyelashes / hair: as in
blender.create_human; "none" removes a part. A skin that was chosen with "auto"
is chosen again when age, gender or race changes.

Note: a transaction rollback restores the object's transform but not its body
shape; to undo a shape change, set the old values again.\
"""

ADD_HUMAN_LOD_DESCRIPTION = """\
Add levels of detail to a human.

subdivision_levels: 0..3. Adds a Multires modifier to the body: more detail for
      close-ups and a base to sculpt or bake displacement onto. Each level is
      about four times the faces (13k -> 54k -> 214k -> 850k).
low_ratios: up to 4 values in 0.01..0.99, e.g. [0.5, 0.25]. Each makes a copy
      of the body named "<name>_LOD1", "<name>_LOD2"... with a Decimate modifier
      at that ratio, placed beside the original. Copies are the body mesh only.
spacing_m: distance between the copies; defaults to the body's width plus a margin.

Returns each level with its face count.\
"""


def _unit(name: str, value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise tool_error(
            BlenderMCPError(f"{name} must be a number", code=ErrorCode.INVALID_PARAMETER)
        ) from exc
    if not 0.0 <= number <= 1.0:
        raise tool_error(
            BlenderMCPError(
                f"{name} must be within 0..1", code=ErrorCode.INVALID_PARAMETER, details={"received": value}
            )
        )
    return number


def _within(name: str, value: Any, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise tool_error(
            BlenderMCPError(f"{name} must be a number", code=ErrorCode.INVALID_PARAMETER)
        ) from exc
    if not low <= number <= high:
        raise tool_error(
            BlenderMCPError(
                f"{name} must be within {low:g}..{high:g}",
                code=ErrorCode.INVALID_PARAMETER,
                details={"received": value},
            )
        )
    return number


def _body(
    *,
    gender: Any,
    age_years: Any,
    height_m: Any,
    muscle: Any,
    weight: Any,
    proportions: Any,
    cupsize: Any,
    firmness: Any,
    race: Any,
    targets: Any,
) -> dict[str, Any]:
    """The shape half of a request, validated, with omitted values left out."""
    params: dict[str, Any] = {}
    units = {
        "gender": gender,
        "muscle": muscle,
        "weight": weight,
        "proportions": proportions,
        "cupsize": cupsize,
        "firmness": firmness,
    }
    for key, value in units.items():
        if value is not None:
            params[key] = _unit(key, value)
    if age_years is not None:
        params["age_years"] = _within("age_years", age_years, MIN_AGE, MAX_AGE)
    if height_m is not None:
        params["height_m"] = _within("height_m", height_m, MIN_HEIGHT_M, MAX_HEIGHT_M)
    if race is not None:
        if not isinstance(race, Mapping) or not race or set(race) - set(RACES):
            raise tool_error(
                BlenderMCPError(
                    f"race must be an object with some of {', '.join(RACES)}",
                    code=ErrorCode.INVALID_PARAMETER,
                    details={"received": race},
                )
            )
        params["race"] = {key: _unit(f"race.{key}", value) for key, value in race.items()}
    if targets is not None:
        if not isinstance(targets, Mapping):
            raise tool_error(
                BlenderMCPError(
                    "targets must be an object of {category: value}", code=ErrorCode.INVALID_PARAMETER
                )
            )
        params["targets"] = {
            str(key): _within(f"targets.{key}", value, -1.0, 1.0) for key, value in targets.items()
        }
    return params


def _dress(*, skin: Any, skin_type: Any, parts: Mapping[str, Any]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if skin is not None:
        if not isinstance(skin, str) or not skin.strip():
            raise tool_error(
                BlenderMCPError('skin must be a name, "auto" or "none"', code=ErrorCode.INVALID_PARAMETER)
            )
        params["skin"] = skin.strip()
    if skin_type is not None:
        normalized = str(skin_type).upper()
        if normalized not in SKIN_TYPES:
            raise tool_error(
                BlenderMCPError(
                    f"skin_type must be one of {', '.join(SKIN_TYPES)}",
                    code=ErrorCode.INVALID_PARAMETER,
                    details={"received": skin_type},
                )
            )
        params["skin_type"] = normalized
    for part, value in parts.items():
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            raise tool_error(
                BlenderMCPError(f'{part} must be an asset name or "none"', code=ErrorCode.INVALID_PARAMETER)
            )
        params[part] = value.strip()
    return params


def _name(name: Any) -> str:
    if not isinstance(name, str) or not name.strip():
        raise tool_error(BlenderMCPError("name must not be empty", code=ErrorCode.INVALID_PARAMETER))
    return name


async def _send(ctx: Context, action: Action, params: dict[str, Any], *, slow: bool = True) -> dict[str, Any]:
    timeout = settings_of(ctx).blender_render_timeout if slow else None
    try:
        return await bridge_of(ctx).request(action, params, timeout=timeout)
    except BlenderMCPError as exc:
        logger.info("blender.%s failed: %s", action.value, exc)
        raise tool_error(exc) from exc


async def get_human_options(ctx: Context, section: str | None = None) -> dict[str, Any]:
    """Whether humans can be made here, and from what."""
    params = {"section": section} if section else {}
    return await _send(ctx, Action.GET_HUMAN_OPTIONS, params, slow=False)


async def create_human(
    ctx: Context,
    name: str = Field(description="Unique object name for the body."),
    location: list[float] | None = None,
    gender: float | None = None,
    age_years: float | None = None,
    height_m: float | None = None,
    muscle: float | None = None,
    weight: float | None = None,
    proportions: float | None = None,
    cupsize: float | None = None,
    firmness: float | None = None,
    race: dict[str, float] | None = None,
    targets: dict[str, float] | None = None,
    skin: str | None = None,
    skin_type: str | None = None,
    eyes: str | None = None,
    eyebrows: str | None = None,
    eyelashes: str | None = None,
    hair: str | None = None,
) -> dict[str, Any]:
    """Create a human from a description."""
    params: dict[str, Any] = {"name": _name(name)}
    if location is not None:
        if not isinstance(location, list) or len(location) != 3:
            raise tool_error(BlenderMCPError("location must be [x, y, z]", code=ErrorCode.INVALID_PARAMETER))
        params["location"] = [float(value) for value in location]
    params.update(
        _body(
            gender=gender,
            age_years=age_years,
            height_m=height_m,
            muscle=muscle,
            weight=weight,
            proportions=proportions,
            cupsize=cupsize,
            firmness=firmness,
            race=race,
            targets=targets,
        )
    )
    params.update(
        _dress(
            skin=skin,
            skin_type=skin_type,
            parts={"eyes": eyes, "eyebrows": eyebrows, "eyelashes": eyelashes, "hair": hair},
        )
    )
    return await _send(ctx, Action.CREATE_HUMAN, params)


async def update_human(
    ctx: Context,
    name: str = Field(description="The human's body object."),
    gender: float | None = None,
    age_years: float | None = None,
    height_m: float | None = None,
    muscle: float | None = None,
    weight: float | None = None,
    proportions: float | None = None,
    cupsize: float | None = None,
    firmness: float | None = None,
    race: dict[str, float] | None = None,
    targets: dict[str, float] | None = None,
    skin: str | None = None,
    skin_type: str | None = None,
    eyes: str | None = None,
    eyebrows: str | None = None,
    eyelashes: str | None = None,
    hair: str | None = None,
) -> dict[str, Any]:
    """Change only the given properties of a human."""
    params: dict[str, Any] = {"name": _name(name)}
    params.update(
        _body(
            gender=gender,
            age_years=age_years,
            height_m=height_m,
            muscle=muscle,
            weight=weight,
            proportions=proportions,
            cupsize=cupsize,
            firmness=firmness,
            race=race,
            targets=targets,
        )
    )
    params.update(
        _dress(
            skin=skin,
            skin_type=skin_type,
            parts={"eyes": eyes, "eyebrows": eyebrows, "eyelashes": eyelashes, "hair": hair},
        )
    )
    if len(params) == 1:
        raise tool_error(
            BlenderMCPError(
                "Nothing to update: pass a body value, targets, height_m, skin or a part",
                code=ErrorCode.INVALID_PARAMETER,
            )
        )
    return await _send(ctx, Action.UPDATE_HUMAN, params)


async def add_human_lod(
    ctx: Context,
    name: str = Field(description="The human's body object."),
    subdivision_levels: int = 0,
    low_ratios: list[float] | None = None,
    spacing_m: float | None = None,
) -> dict[str, Any]:
    """Add detail levels to a human."""
    params: dict[str, Any] = {"name": _name(name)}
    levels = int(_within("subdivision_levels", subdivision_levels, 0, MAX_SUBDIVISION_LEVELS))
    if levels != subdivision_levels:
        raise tool_error(
            BlenderMCPError("subdivision_levels must be a whole number", code=ErrorCode.INVALID_PARAMETER)
        )
    ratios = [_within("low_ratios", ratio, 0.01, 0.99) for ratio in low_ratios or []]
    if len(ratios) > MAX_LOW_LODS:
        raise tool_error(
            BlenderMCPError(
                f"low_ratios takes at most {MAX_LOW_LODS} values", code=ErrorCode.INVALID_PARAMETER
            )
        )
    if not levels and not ratios:
        raise tool_error(
            BlenderMCPError(
                "Nothing to add: pass subdivision_levels or low_ratios", code=ErrorCode.INVALID_PARAMETER
            )
        )
    params["subdivision_levels"] = levels
    params["low_ratios"] = ratios
    if spacing_m is not None:
        params["spacing_m"] = _within("spacing_m", spacing_m, 0.0, 100.0)
    return await _send(ctx, Action.ADD_HUMAN_LOD, params)


def register_tools(server: MCPServer, enabled: Callable[[str], bool] | None = None) -> list[str]:
    return register_all(
        server,
        (
            (get_human_options, "blender.get_human_options", GET_HUMAN_OPTIONS_DESCRIPTION),
            (create_human, "blender.create_human", CREATE_HUMAN_DESCRIPTION),
            (update_human, "blender.update_human", UPDATE_HUMAN_DESCRIPTION),
            (add_human_lod, "blender.add_human_lod", ADD_HUMAN_LOD_DESCRIPTION),
        ),
        enabled,
    )
