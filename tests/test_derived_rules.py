"""Label-derived axis membership: `rules.derive_rules`.

`derive_rules` computes the same membership `generate_rules` does — labels are authoritative,
narrowed by resize_policy — without storing anything. It carries no limits, component labels,
offsets or position rules, and it cannot express SINGLE ("only the chassis") or CONNECTED
("and everything mated to it") scope.

It used to prop up the LLM-classification fallback, supplying OVERALL membership for models
with no stored rule set. That fallback is gone — a resize now REQUIRES a reviewed rule set —
so `derive_rules` is no longer wired into `interpret`, and the tests below assert the refusal
where they once asserted a resize. The function is kept because it is the honest expression of
"membership from labels" and is covered here directly.
"""

import json

import pytest
from unittest.mock import patch, AsyncMock

import rules_store as store
from models import DimensionIn, InterpretRequest
from interpret import interpret
from rules import derive_rules

MIRROR_W = "WIDTH@Sketch1 [1021-MIRROR-1]"
MIRROR_H = "HEIGHT@Sketch1 [1021-MIRROR-1]"
CHASSIS_W = "D1@Sketch2 [9535-CHASSIS-1]"
CHASSIS_H = "D2@Sketch2 [9535-CHASSIS-1]"
PSU_W = "D1@WIDTH [LPM-24096A-2]"
CLIP_W = "D1@Sketch3 [1005-CLIP-1]"
LED_LEN = "D1@Sketch4 [ZORTECH-LEDS-1]"
LED_PROFILE = "D2@Sketch4 [ZORTECH-LEDS-1]"

DIMS = [
    DimensionIn(name=MIRROR_W, value_meters=0.6096),
    DimensionIn(name=MIRROR_H, value_meters=1.2192),
    DimensionIn(name=CHASSIS_W, value_meters=0.5588),
    DimensionIn(name=CHASSIS_H, value_meters=1.1684),
    DimensionIn(name=PSU_W, value_meters=0.0599),
    DimensionIn(name=CLIP_W, value_meters=0.0508),
    DimensionIn(name=LED_LEN, value_meters=1.1176),
    DimensionIn(name=LED_PROFILE, value_meters=0.0127),
]
LABELS = {MIRROR_W: "W", MIRROR_H: "H", CHASSIS_W: "W", CHASSIS_H: "H",
          PSU_W: "W", CLIP_W: "W", LED_LEN: "H", LED_PROFILE: "W"}


def _derived():
    return derive_rules(DIMS, LABELS, MIRROR_W, MIRROR_H)


def test_derives_one_rule_per_axis_mastered_on_the_glass():
    r = _derived()
    assert [p.if_changes for p in r.width] == [MIRROR_W]
    assert [p.if_changes for p in r.height] == [MIRROR_H]


def test_fixed_size_hardware_is_excluded():
    deps = _derived().width[0].also_change
    assert PSU_W not in deps
    assert CLIP_W not in deps
    assert CHASSIS_W in deps


def test_led_profile_excluded_from_width_but_length_kept_on_height():
    r = _derived()
    assert LED_PROFILE not in r.width[0].also_change
    assert LED_LEN in r.height[0].also_change


def test_carries_no_authored_content():
    """Limits / labels / offsets are authored — they can only come from a stored set."""
    r = _derived()
    assert r.limits is None
    assert r.component_labels == {}
    assert r.offset == [] and r.position == []


def test_returns_none_without_labels():
    assert derive_rules(DIMS, {}, None, None) is None


@pytest.mark.asyncio
async def test_labels_alone_no_longer_authorise_a_resize():
    """Derivable membership is NOT permission to resize.

    This model has full [W]/[H] labels and a master on each axis, so the old fallback happily
    resized it from labels alone. A rule set nobody authored is a rule set nobody reviewed, so
    the request is now refused and the user is sent to Generate Rules.
    """
    llm = json.dumps({"target_width_meters": 0.762, "width_dims": [MIRROR_W],
                      "target_height_meters": None, "height_dims": [],
                      "explanation": "Overall resize"})
    with patch("interpret.call_llm", new=AsyncMock(return_value=llm)):
        result = await interpret(InterpretRequest(
            instruction="change width to 30 inches",
            dimensions=DIMS,
            dim_axis_labels=LABELS,
            master_width_dim=MIRROR_W,
            master_height_dim=MIRROR_H,
            model_path="C:\\models\\NEWPRODUCT-24.00X48.00-LED.SLDASM",
        ))
    assert result.changes == []
    assert result.needs_rules is True
    assert _derived() is not None      # …even though membership WAS derivable


@pytest.mark.asyncio
async def test_naming_a_component_does_not_bypass_the_gate():
    """A component-scoped request ("make the chassis wider") is refused the same way.

    There is no path left that resizes without a stored set — not OVERALL, not CONNECTED,
    not SINGLE.
    """
    llm = json.dumps({"target_width_meters": 0.762, "master_width_dim": CHASSIS_W,
                      "width_dims": [CHASSIS_W], "target_height_meters": None,
                      "height_dims": [], "explanation": "Chassis and mated parts"})
    with patch("interpret.call_llm", new=AsyncMock(return_value=llm)):
        result = await interpret(InterpretRequest(
            instruction="make the chassis wider",
            dimensions=DIMS,
            dim_axis_labels=LABELS,
            master_width_dim=MIRROR_W,
            master_height_dim=MIRROR_H,
            model_path="C:\\models\\NEWPRODUCT-24.00X48.00-LED.SLDASM",
        ))
    assert result.changes == []
    assert result.needs_rules is True


@pytest.mark.asyncio
async def test_a_stored_family_set_is_used_for_any_size_of_the_family():
    store.write_key("NEWPRODUCT-LED", {
        "model": "NEWPRODUCT", "family": "NEWPRODUCT-LED",
        "width": [{"if_changes": MIRROR_W, "also_change": [CHASSIS_W]}],
        "height": [],
        "limits": {"max_width_mm": 700},        # authored content only a stored set has
    })
    llm = ('{"rule": {"if_changes": "' + MIRROR_W + '", "also_change": ["' + CHASSIS_W + '"]},'
           ' "scope": "overall", "value_meters": 0.762}')
    with patch("interpret.call_llm", new=AsyncMock(return_value=llm)):
        result = await interpret(InterpretRequest(
            instruction="change width to 30 inches",
            dimensions=DIMS,
            dim_axis_labels=LABELS,
            master_width_dim=MIRROR_W,
            master_height_dim=MIRROR_H,
            model_path="C:\\models\\NEWPRODUCT-30.00X48.00-LED.SLDASM",
        ))
    # The stored set's limit applied → proof the stored tier, not derivation, was used.
    assert result.error is not None and "exceeds maximum" in result.error
