"""The arithmetic of a human character, with no Blender in sight.

MPFB describes a body with eight macro values in 0..1 and several hundred
"targets" (shape keys) named after files. Neither is what a model -- or a
person -- thinks in: nobody says "age 0.6875", they say "fifty". This module is
the translation, kept free of ``bpy`` so it can be tested on its own:

* ages in years to MPFB's age macro, and back;
* a target *category* with one signed value (``nose-volume-decr-incr: -0.4``)
  to the one or two target files that implement it;
* a description of a body to the skin that fits it.

:mod:`blender_mcp.humans` does the Blender half.
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable, Mapping
from typing import Any

from blender_mcp import protocol
from blender_mcp.protocol import ActionError

#: The macro values MPFB keeps on a human, each in 0..1, 0.5 being average.
#: ``age`` is not exposed as-is: it goes through :func:`age_to_macro`.
MACROS: tuple[str, ...] = (
    "gender",
    "age",
    "muscle",
    "weight",
    "height",
    "proportions",
    "cupsize",
    "firmness",
)

#: Ethnicity weights. MPFB blends three reference bodies; the weights are
#: normalised to sum to one before they are applied.
RACES: tuple[str, ...] = ("african", "asian", "caucasian")

#: Parts that come from the system asset pack, by the asset folder they live in.
BODYPARTS: tuple[str, ...] = ("eyes", "eyebrows", "eyelashes", "hair")

#: What a new human gets unless told otherwise. No hair by default: a hairstyle
#: is a choice, and a wrong one is more noticeable than none.
DEFAULT_PARTS: dict[str, str | None] = {
    "eyes": "high-poly",
    "eyebrows": "eyebrow001",
    "eyelashes": "eyelashes01",
    "hair": None,
}

SKIN_TYPES: tuple[str, ...] = ("ENHANCED_SSS", "MAKESKIN", "GAMEENGINE", "LAYERED")

#: How close two race weights can be and still count as a tie for the skin.
RACE_TIE_MARGIN = 0.05

#: MakeHuman's age scale: the macro is piecewise linear through these points.
#: 0 is a one-year-old, 0.1875 eleven, 0.5 twenty-five and 1.0 ninety.
_AGE_POINTS: tuple[tuple[float, float], ...] = ((1.0, 0.0), (11.0, 0.1875), (25.0, 0.5), (90.0, 1.0))

MIN_AGE_YEARS = _AGE_POINTS[0][0]
MAX_AGE_YEARS = _AGE_POINTS[-1][0]


def age_to_macro(years: float) -> float:
    """MPFB's age macro for an age in years, clamped to 1..90."""
    years = min(max(float(years), MIN_AGE_YEARS), MAX_AGE_YEARS)
    for (low_years, low_value), (high_years, high_value) in zip(_AGE_POINTS, _AGE_POINTS[1:], strict=False):
        if years <= high_years:
            share = (years - low_years) / (high_years - low_years)
            return round(low_value + share * (high_value - low_value), 4)
    return 1.0  # pragma: no cover - the clamp above makes this unreachable


def macro_to_age(value: float) -> float:
    """The age in years that an age macro stands for."""
    value = min(max(float(value), 0.0), 1.0)
    for (low_years, low_value), (high_years, high_value) in zip(_AGE_POINTS, _AGE_POINTS[1:], strict=False):
        if value <= high_value:
            share = (value - low_value) / (high_value - low_value)
            return round(low_years + share * (high_years - low_years), 1)
    return MAX_AGE_YEARS  # pragma: no cover


def normalise_race(race: Mapping[str, Any]) -> dict[str, float]:
    """Race weights that sum to one. Missing entries count as zero."""
    unknown = sorted(set(race) - set(RACES))
    if unknown:
        raise ActionError(
            protocol.INVALID_PARAMETER,
            f"Unknown race key(s): {', '.join(unknown)}. Use {', '.join(RACES)}.",
        )
    weights = {key: max(0.0, float(race.get(key, 0.0) or 0.0)) for key in RACES}
    total = sum(weights.values())
    if total <= 0:
        raise ActionError(protocol.INVALID_PARAMETER, "race weights must not all be zero")
    return {key: round(value / total, 4) for key, value in weights.items()}


def age_group(years: float) -> str:
    """The skin pack's three age bands."""
    if years < 35:
        return "young"
    if years < 60:
        return "middleage"
    return "old"


def skin_for(gender: float, years: float, race: Mapping[str, float], available: Iterable[str]) -> str | None:
    """The skin that fits a body best, from the skins that are installed.

    Skins are named ``<age>_<race>_<sex>``. The exact match comes first; then the
    same sex and race at any age, then the same sex at all; ``None`` when the
    pack offers nothing.

    When no race clearly leads -- MPFB's default is an even blend -- the
    caucasian set is used, which is what MakeHuman itself starts from. Without
    the tie-break the first race alphabetically won every tie, and every body
    built without a race came out with the same skin by accident.
    """
    names = sorted(set(available))
    sex = "male" if gender >= 0.5 else "female"
    top = max(race.get(key, 0.0) for key in RACES)
    leading = [key for key in RACES if race.get(key, 0.0) >= top - RACE_TIE_MARGIN]
    dominant = leading[0] if len(leading) == 1 else "caucasian"
    group = age_group(years)
    preferred = [
        f"{group}_{dominant}_{sex}",
        *(f"{other}_{dominant}_{sex}" for other in ("young", "middleage", "old") if other != group),
        f"young_caucasian_{sex}",
    ]
    for candidate in preferred:
        if candidate in names:
            return candidate
    same_sex = [name for name in names if name.endswith(f"_{sex}")]
    if same_sex:
        return same_sex[0]
    return names[0] if names else None


def target_categories(catalog: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Every category in MPFB's ``target.json``, by name, with its section."""
    found: dict[str, dict[str, Any]] = {}
    for section, body in catalog.items():
        for category in body.get("categories", []):
            found[category["name"]] = {**category, "section": section}
    return found


def describe_category(category: Mapping[str, Any]) -> dict[str, Any]:
    """A category as a model should see it: its name and the range it takes."""
    signed = bool(category.get("opposites"))
    return {
        "name": category["name"],
        "range": "-1..1" if signed else "0..1",
        "sided": bool(category.get("has_left_and_right")),
    }


def resolve_targets(catalog: Mapping[str, Any], requested: Mapping[str, Any]) -> list[tuple[str, float]]:
    """``{category or target: value}`` to ``[(target file stem, weight)]``.

    A two-sided category (``nose-volume-decr-incr``) takes one value in -1..1:
    a negative value drives the ``decr`` file, a positive one the ``incr`` file,
    and the other is set to zero so that changing direction does not leave the
    old one behind. A category with left and right files sets both sides. A
    single file name (``l-eye-bag-in``) is accepted as-is, in 0..1, for the
    rare case where one side should differ.

    Unknown names are refused with the closest matches, because a model that
    guessed ``nose-size`` should be told about ``nose-scale-*``, not silently
    ignored.
    """
    categories = target_categories(catalog)
    files = {name for category in categories.values() for name in category.get("targets", [])}
    resolved: list[tuple[str, float]] = []
    for name, raw in requested.items():
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise ActionError(protocol.INVALID_PARAMETER, f"target '{name}' needs a number") from exc
        category = categories.get(name)
        if category is not None and category.get("opposites"):
            if not -1.0 <= value <= 1.0:
                raise ActionError(protocol.INVALID_PARAMETER, f"target '{name}' takes -1..1, got {value:g}")
            resolved.extend(_signed(category["opposites"], value))
        elif category is not None:
            if not 0.0 <= value <= 1.0:
                raise ActionError(protocol.INVALID_PARAMETER, f"target '{name}' takes 0..1, got {value:g}")
            resolved.extend((target, value) for target in category["targets"])
        elif name in files:
            if not 0.0 <= value <= 1.0:
                raise ActionError(protocol.INVALID_PARAMETER, f"target '{name}' takes 0..1, got {value:g}")
            resolved.append((name, value))
        else:
            close = difflib.get_close_matches(name, list(categories), n=5, cutoff=0.5)
            raise ActionError(
                protocol.INVALID_PARAMETER,
                f"No target category named '{name}'."
                + (f" Close matches: {', '.join(close)}." if close else "")
                + " blender.get_human_options lists them by section.",
                {"suggestions": close},
            )
    return resolved


def _signed(opposites: Mapping[str, str], value: float) -> list[tuple[str, float]]:
    """The files a signed value drives, with the opposite direction zeroed."""
    pairs: list[tuple[str, float]] = []
    for side in ("unsided", "left", "right"):
        negative = opposites.get(f"negative-{side}") or ""
        positive = opposites.get(f"positive-{side}") or ""
        if negative:
            pairs.append((negative, round(-value, 4) if value < 0 else 0.0))
        if positive:
            pairs.append((positive, round(value, 4) if value > 0 else 0.0))
    return pairs
