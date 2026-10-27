"""Tests for the human tools.

Two halves, as everywhere in this project: the MCP tools (which action, which
params, which refusals before anything is sent) and the add-on's catalog logic
(age, targets, skins), which is plain Python. MPFB itself needs a real Blender;
``examples/human_check.py`` is the check for that half.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from blender_mcp import humans_catalog as catalog
from blender_mcp.protocol import ActionError
from mcp.server.mcpserver.exceptions import ToolError

from server.blender.protocol import Action
from server.config import Settings
from server.mcp.tools import humans
from tests.support.stubs import FakeBridge, FakeContext

#: A slice of MPFB's target.json, in its real shape.
CATALOG: dict[str, Any] = {
    "nose": {
        "categories": [
            {
                "name": "nose-volume-decr-incr",
                "has_left_and_right": False,
                "opposites": {
                    "negative-unsided": "nose-volume-decr",
                    "positive-unsided": "nose-volume-incr",
                    "negative-left": "",
                    "negative-right": "",
                    "positive-left": "",
                    "positive-right": "",
                },
                "targets": ["nose-volume-decr", "nose-volume-incr"],
            }
        ]
    },
    "arms": {
        "categories": [
            {
                "name": "lowerarm-scale-depth-decr-incr",
                "has_left_and_right": True,
                "opposites": {
                    "negative-left": "l-lowerarm-scale-depth-decr",
                    "negative-right": "r-lowerarm-scale-depth-decr",
                    "positive-left": "l-lowerarm-scale-depth-incr",
                    "positive-right": "r-lowerarm-scale-depth-incr",
                    "negative-unsided": "",
                    "positive-unsided": "",
                },
                "targets": [
                    "l-lowerarm-scale-depth-decr",
                    "r-lowerarm-scale-depth-decr",
                    "l-lowerarm-scale-depth-incr",
                    "r-lowerarm-scale-depth-incr",
                ],
            }
        ]
    },
    "chin": {
        "categories": [{"name": "chin-triangle", "has_left_and_right": False, "targets": ["chin-triangle"]}]
    },
}

SKINS = [
    "young_caucasian_male",
    "young_caucasian_female",
    "middleage_caucasian_male",
    "old_caucasian_female",
    "young_african_male",
    "old_african_male",
    "young_asian_female",
]

pytestmark = pytest.mark.anyio


def error_code(excinfo: pytest.ExceptionInfo[ToolError]) -> str:
    return str(json.loads(excinfo.value.args[0])["error"]["code"])


# --- catalog: age ----------------------------------------------------------------


@pytest.mark.parametrize("years,value", [(1, 0.0), (11, 0.1875), (25, 0.5), (90, 1.0), (57.5, 0.75)])
def test_age_follows_makehumans_scale(years: float, value: float) -> None:
    assert catalog.age_to_macro(years) == pytest.approx(value, abs=1e-4)
    assert catalog.macro_to_age(value) == pytest.approx(years, abs=0.1)


def test_age_is_clamped_to_what_mpfb_can_show() -> None:
    assert catalog.age_to_macro(0) == 0.0
    assert catalog.age_to_macro(120) == 1.0


# --- catalog: targets ------------------------------------------------------------


def test_a_negative_value_drives_the_first_word_and_zeroes_the_other() -> None:
    assert catalog.resolve_targets(CATALOG, {"nose-volume-decr-incr": -0.4}) == [
        ("nose-volume-decr", 0.4),
        ("nose-volume-incr", 0.0),
    ]


def test_a_positive_value_drives_the_second_word() -> None:
    assert ("nose-volume-incr", 0.6) in catalog.resolve_targets(CATALOG, {"nose-volume-decr-incr": 0.6})


def test_a_sided_category_sets_both_sides() -> None:
    resolved = dict(catalog.resolve_targets(CATALOG, {"lowerarm-scale-depth-decr-incr": 0.5}))
    assert resolved["l-lowerarm-scale-depth-incr"] == resolved["r-lowerarm-scale-depth-incr"] == 0.5
    assert resolved["l-lowerarm-scale-depth-decr"] == resolved["r-lowerarm-scale-depth-decr"] == 0.0


def test_a_single_file_category_and_a_file_name_both_take_0_to_1() -> None:
    assert catalog.resolve_targets(CATALOG, {"chin-triangle": 0.3}) == [("chin-triangle", 0.3)]
    assert catalog.resolve_targets(CATALOG, {"l-lowerarm-scale-depth-incr": 0.2}) == [
        ("l-lowerarm-scale-depth-incr", 0.2)
    ]
    with pytest.raises(ActionError):
        catalog.resolve_targets(CATALOG, {"chin-triangle": -0.3})


def test_an_unknown_target_is_refused_with_the_closest_names() -> None:
    with pytest.raises(ActionError) as excinfo:
        catalog.resolve_targets(CATALOG, {"nose-volume": 0.5})
    assert excinfo.value.code == "INVALID_PARAMETER"
    assert "nose-volume-decr-incr" in excinfo.value.details["suggestions"]


def test_a_signed_value_out_of_range_is_refused() -> None:
    with pytest.raises(ActionError):
        catalog.resolve_targets(CATALOG, {"nose-volume-decr-incr": 1.5})


# --- catalog: skins and race -----------------------------------------------------


def test_the_skin_matches_sex_age_and_race() -> None:
    race = {"african": 1.0, "asian": 0.0, "caucasian": 0.0}
    assert catalog.skin_for(1.0, 70, race, SKINS) == "old_african_male"
    assert catalog.skin_for(0.0, 30, {"asian": 1.0}, SKINS) == "young_asian_female"


def test_an_even_blend_does_not_pick_a_race_alphabetically() -> None:
    """MPFB's default race is an even mix; every unspecified body came out african."""
    even = {"african": 0.3333, "asian": 0.3333, "caucasian": 0.3333}
    assert catalog.skin_for(1.0, 30, even, SKINS) == "young_caucasian_male"


def test_a_missing_age_band_falls_back_to_another_band_of_the_same_race_and_sex() -> None:
    assert catalog.skin_for(1.0, 45, {"african": 1.0}, SKINS) == "young_african_male"


def test_race_is_normalised_and_checked() -> None:
    assert catalog.normalise_race({"african": 2, "caucasian": 2}) == {
        "african": 0.5,
        "asian": 0.0,
        "caucasian": 0.5,
    }
    with pytest.raises(ActionError):
        catalog.normalise_race({"martian": 1})
    with pytest.raises(ActionError):
        catalog.normalise_race({"asian": 0})


# --- MCP tools -------------------------------------------------------------------


async def test_create_human_sends_the_description(
    ctx: FakeContext, bridge: FakeBridge, settings: Settings
) -> None:
    await humans.create_human(
        ctx,
        name="Runner",
        gender=1,
        age_years=30,
        height_m=1.82,
        muscle=0.75,
        race={"african": 1},
        targets={"nose-volume-decr-incr": -0.2},
        hair="short02",
        skin_type="enhanced_sss",
    )
    action, params = bridge.calls[0]
    assert action is Action.CREATE_HUMAN
    assert params == {
        "name": "Runner",
        "gender": 1.0,
        "muscle": 0.75,
        "age_years": 30.0,
        "height_m": 1.82,
        "race": {"african": 1.0},
        "targets": {"nose-volume-decr-incr": -0.2},
        "skin_type": "ENHANCED_SSS",
        "hair": "short02",
    }
    # A skin with subsurface scattering takes seconds: the render timeout, not the query one.
    assert bridge.timeouts == [settings.blender_render_timeout]


async def test_omitted_values_are_left_to_the_add_on(ctx: FakeContext, bridge: FakeBridge) -> None:
    await humans.create_human(ctx, name="Anyone")
    assert bridge.calls[0][1] == {"name": "Anyone"}


@pytest.mark.parametrize(
    "bad",
    [
        {"gender": 1.5},
        {"age_years": 0},
        {"age_years": 120},
        {"height_m": 3.5},
        {"race": {"martian": 1}},
        {"race": {}},
        {"targets": {"nose-volume-decr-incr": 2}},
        {"skin_type": "PLASTIC"},
        {"hair": ""},
        {"location": [1, 2]},
    ],
)
async def test_create_human_refuses_bad_values_before_sending(
    ctx: FakeContext, bridge: FakeBridge, bad: dict[str, Any]
) -> None:
    with pytest.raises(ToolError) as excinfo:
        await humans.create_human(ctx, name="Bad", **bad)
    assert error_code(excinfo) == "INVALID_PARAMETER"
    assert bridge.calls == []


async def test_update_human_needs_something_to_change(ctx: FakeContext, bridge: FakeBridge) -> None:
    with pytest.raises(ToolError) as excinfo:
        await humans.update_human(ctx, name="Runner")
    assert error_code(excinfo) == "INVALID_PARAMETER"
    assert bridge.calls == []


async def test_update_human_sends_only_what_changed(ctx: FakeContext, bridge: FakeBridge) -> None:
    await humans.update_human(ctx, name="Runner", age_years=45, eyebrows="none")
    assert bridge.calls == [(Action.UPDATE_HUMAN, {"name": "Runner", "age_years": 45.0, "eyebrows": "none"})]


async def test_get_human_options_is_a_quick_read(ctx: FakeContext, bridge: FakeBridge) -> None:
    await humans.get_human_options(ctx, section="nose")
    assert bridge.calls == [(Action.GET_HUMAN_OPTIONS, {"section": "nose"})]
    assert bridge.timeouts == [None]


async def test_add_human_lod_sends_levels_and_ratios(ctx: FakeContext, bridge: FakeBridge) -> None:
    await humans.add_human_lod(ctx, name="Runner", subdivision_levels=2, low_ratios=[0.5, 0.25])
    assert bridge.calls == [
        (Action.ADD_HUMAN_LOD, {"name": "Runner", "subdivision_levels": 2, "low_ratios": [0.5, 0.25]})
    ]


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"subdivision_levels": 4},
        {"subdivision_levels": 1.5},
        {"low_ratios": [1.0]},
        {"low_ratios": [0.5] * 5},
    ],
)
async def test_add_human_lod_refuses_bad_values(
    ctx: FakeContext, bridge: FakeBridge, bad: dict[str, Any]
) -> None:
    with pytest.raises(ToolError):
        await humans.add_human_lod(ctx, name="Runner", **bad)
    assert bridge.calls == []


async def test_the_add_ons_refusal_reaches_the_model_with_its_code(
    ctx: FakeContext, bridge: FakeBridge
) -> None:
    from server.errors import BlenderMCPError, ErrorCode

    bridge.raise_for[Action.CREATE_HUMAN] = BlenderMCPError(
        "MPFB is not enabled", code=ErrorCode.HUMAN_GENERATOR_UNAVAILABLE, details={"hint": "enable it"}
    )
    with pytest.raises(ToolError) as excinfo:
        await humans.create_human(ctx, name="Runner")
    assert error_code(excinfo) == "HUMAN_GENERATOR_UNAVAILABLE"
