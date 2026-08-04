"""Resize-policy tests: mirror-glass-only masters, fixed-size hardware, LED height-only.

Dim names are taken verbatim from a real generated rules file
(rules/AMBER-36.00X36.00-LED.rules.json) so the patterns are tested against the actual
component-id shapes the app emits, not invented ones.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import resize_policy as policy
from interpret import _dependent_value, _enforce_policy
from models import DimensionChange

MIRROR_W = "WIDTH@Sketch1 [1011-MIRROR-CAROL-1]"
MIRROR_H = "HEIGHT@Sketch1 [1011-MIRROR-CAROL-1]"
CHASSIS_W = "D1@Sketch1 [12204-CHASSIS-2]"
CHASSIS_H = "D2@Sketch1 [12204-CHASSIS-2]"
HANGER_W = "D2@Base-Flange1 [1038-HANGER-1]"
HANGER_H = "D1@Sketch1 [1038-HANGER-1]"
CLIP_W = "D1@Sketch1 [1005-CLIP-1]"
CLIP_H = "D2@Sketch1 [1005-CLIP-1]"
BRACKET_W = "D1@Sketch1 [1292-LED-BRACKET-2]"
BRACKET_H = "D2@Base-Flange1 [1292-LED-BRACKET-2]"
PSU_W = "D1@WIDTH [LPM-24060A-2]"
PSU_H = "D2@LENGTH/HEIGHT [LPM-24060A-1]"
LED_W = "D2@Sketch1 [Zortech-Low-Profile-Double Row LEDS-1]"
LED_H = "D1@Boss-Extrude1 [Zortech-Low-Profile-Double Row LEDS-2]"
PART_DIM = "D1@Sketch1"          # single-part model: no [component] suffix

LABELS = {
    "1011-MIRROR-CAROL-1": "Mirror Glass",
    "12204-CHASSIS-2": "Main Chassis",
    "1038-HANGER-1": "Mirror Hanger",
    "1005-CLIP-1": "Mirror Clip Left",
    "1292-LED-BRACKET-2": "LED Bracket Right",
    "LPM-24060A-2": "LED Power Supply Left",
    "LPM-24060A-1": "LED Power Supply Right",
    "Zortech-Low-Profile-Double Row LEDS-1": "LED Strip Left",
    "Zortech-Low-Profile-Double Row LEDS-2": "LED Strip Right",
}


# ── component extraction ──────────────────────────────────────────────────────

def test_component_of():
    assert policy.component_of(CLIP_W) == "1005-CLIP-1"
    assert policy.component_of(LED_W) == "Zortech-Low-Profile-Double Row LEDS-1"
    assert policy.component_of(PART_DIM) == ""


# ── fixed-size hardware: never resized on either axis ─────────────────────────

def test_fixed_size_components_blocked_on_both_axes():
    for dim in (CLIP_W, CLIP_H, BRACKET_W, BRACKET_H, PSU_W, PSU_H, HANGER_W, HANGER_H):
        for axis in ("width", "height"):
            assert policy.block_reason(dim, axis, LABELS), f"{dim} must be blocked on {axis}"


def test_hanger_is_blocked_as_a_prefab_selection():
    # Regression: ratio scaling drove D1@Sketch1 [1038-HANGER-1] 15.000" -> 20.000" on a
    # 36->48 height change. The hanger is picked from the prefab legend, never scaled.
    reason = policy.block_reason(HANGER_H, "height", LABELS)
    assert reason is not None
    assert "prefab" in reason.lower()


def test_fixed_size_detected_without_labels():
    # The OVERALL/CONNECTED path has no component_labels — id patterns must suffice.
    assert policy.fixed_size_reason(CLIP_W) is not None
    assert policy.fixed_size_reason(BRACKET_W) is not None
    assert policy.fixed_size_reason(PSU_H) is not None


# ── blocking must never be driven by the LLM's friendly label ─────────────────

MOUNT_PLATE = "D1@Sketch1 [12186-MOUNTING-PLATE-1]"


def test_a_structural_plate_is_not_frozen_by_its_label():
    """Live regression (2026-08-03): the LLM labeled 12186-MOUNTING-PLATE "Power Supply
    Mounting Plate" on ALPHA-WHITE, the "POWER SUPPLY" pattern matched the LABEL, and the
    plate was frozen. The vertical extrusions are located off it by a Width mate, so they
    came away from the top of the frame. Part numbers must win over LLM prose."""
    labels = {"12186-MOUNTING-PLATE-1": "Power Supply Mounting Plate"}
    assert policy.fixed_size_reason(MOUNT_PLATE, labels) is None
    assert policy.block_reason(MOUNT_PLATE, "width", labels, 21 * 0.0254) is None


def test_the_same_part_behaves_identically_under_either_label():
    # ALPHA-BLACK called it "Mounting Plate", ALPHA-WHITE "Power Supply Mounting Plate".
    # The policy outcome must not depend on which wording the LLM produced.
    a = policy.block_reason(MOUNT_PLATE, "width", {"12186-MOUNTING-PLATE-1": "Mounting Plate"}, 0.53)
    b = policy.block_reason(MOUNT_PLATE, "width",
                            {"12186-MOUNTING-PLATE-1": "Power Supply Mounting Plate"}, 0.53)
    assert a == b is None


def test_a_misleading_label_cannot_freeze_any_part():
    labels = {"4967-CHASSIS-2": "Chassis with LED power supply cutout"}
    assert policy.fixed_size_reason("D1@Sketch1 [4967-CHASSIS-2]", labels) is None


def test_a_real_psu_is_still_blocked_by_its_part_number():
    # The part number, not the label, is what freezes it.
    assert policy.fixed_size_reason(PSU_W, {"LPM-24060A-2": "Left driver box"}) is not None


def test_label_only_match_is_reported_as_an_advisory_not_a_block():
    labels = {"12186-MOUNTING-PLATE-1": "Power Supply Mounting Plate"}
    hint = policy.label_suggests_fixed_size(MOUNT_PLATE, labels)
    assert hint is not None and "POWER SUPPLY" in hint
    # …and it must NOT block.
    assert policy.fixed_size_reason(MOUNT_PLATE, labels) is None


def test_no_advisory_when_the_part_number_already_matches():
    assert policy.label_suggests_fixed_size(PSU_W, {"LPM-24060A-2": "LED Power Supply"}) is None


def test_resizable_components_not_blocked():
    for dim in (MIRROR_W, MIRROR_H, CHASSIS_W, CHASSIS_H):
        for axis in ("width", "height"):
            assert policy.block_reason(dim, axis, LABELS) is None, f"{dim} must resize on {axis}"


def test_single_part_dim_never_blocked():
    # No component suffix → no policy applies; a standalone part must still resize.
    assert policy.block_reason(PART_DIM, "width", None) is None
    assert policy.block_reason(PART_DIM, "height", None) is None


# ── LED strips: the PROFILE is fixed, the LENGTH scales on whatever axis it runs ──

LED_CROSS_M = 0.0127      # 12.70 mm extrusion profile
LED_LEN_M = 0.8128        # 812.80 mm length


def test_led_cross_section_is_blocked_on_every_axis():
    for axis in ("width", "height", ""):
        assert policy.block_reason(LED_W, axis, LABELS, LED_CROSS_M) is not None


def test_vertical_led_length_scales_on_height():
    assert policy.block_reason(LED_H, "height", LABELS, LED_LEN_M) is None


def test_horizontal_led_length_scales_on_WIDTH():
    """Regression for the 2026-07-30 report: in a model with the strips mounted
    horizontally the labeler tags the LENGTH as [W], and it must still be allowed to grow.
    The old axis-based rule refused this."""
    horizontal_len = "D1@Boss-Extrude1 [Zortech-Low-Profile-Double Row LEDS-1]"
    assert policy.block_reason(horizontal_len, "width", LABELS, LED_LEN_M) is None


def test_led_classification_is_by_value_not_axis():
    # Same dim name, same axis — only the value decides profile vs length.
    assert policy.block_reason(LED_W, "height", LABELS, LED_CROSS_M) is not None
    assert policy.block_reason(LED_W, "height", LABELS, LED_LEN_M) is None


def test_led_dim_is_blocked_when_its_value_is_unknown():
    # Conservative: refusing to grow a strip is recoverable, stretching a 12.7 mm profile
    # to mirror width is not.
    assert policy.block_reason(LED_W, "width", LABELS, None) is not None


def test_led_bracket_is_not_treated_as_a_strip():
    assert policy.is_led_strip(BRACKET_W, LABELS) is False
    assert policy.is_led_strip(LED_W, LABELS) is True


def test_unknown_axis_still_applies_the_fixed_size_ban():
    assert policy.block_reason(CLIP_W, "", LABELS, 0.0762) is not None


# ── mirror glass identification ───────────────────────────────────────────────

def test_is_mirror_glass():
    assert policy.is_mirror_glass(MIRROR_W, LABELS) is True
    # "Mirror Clip Left" / "Mirror Hanger" contain MIRROR but are not the glass.
    assert policy.is_mirror_glass(CLIP_W, LABELS) is False
    assert policy.is_mirror_glass(HANGER_W, LABELS) is False
    assert policy.is_mirror_glass(CHASSIS_W, LABELS) is False


# ── master selection ──────────────────────────────────────────────────────────

def test_pick_master_uses_app_master_when_it_is_the_glass():
    dims = [MIRROR_W, CHASSIS_W, HANGER_W]
    master, _note = policy.pick_master(dims, MIRROR_W, LABELS)
    assert master == MIRROR_W


def test_pick_master_swaps_off_a_non_glass_app_master():
    # Guards the observed failure where a 59 mm "D1@WIDTH [LPM-…]" was treated as master.
    dims = [MIRROR_W, CHASSIS_W, PSU_W]
    master, note = policy.pick_master(dims, PSU_W, LABELS)
    assert master == MIRROR_W
    assert "mirror-glass" in note


def test_pick_master_falls_back_to_app_master_without_a_glass_dim():
    dims = [CHASSIS_W, HANGER_W]
    master, _note = policy.pick_master(dims, CHASSIS_W, LABELS)
    assert master == CHASSIS_W


def test_pick_master_returns_none_when_nothing_matches():
    master, _note = policy.pick_master([CHASSIS_W, HANGER_W], "D9@Nope [X-1]", LABELS)
    assert master is None


# ── axis filtering (what generate_rules feeds into the rules) ─────────────────

VALUES = {MIRROR_W: 0.9144, MIRROR_H: 0.9144, CHASSIS_W: 0.8636, CHASSIS_H: 0.8636,
          HANGER_W: 0.5080, HANGER_H: 0.3810, CLIP_W: 0.0762, CLIP_H: 0.0762,
          BRACKET_W: 0.05, BRACKET_H: 0.05, PSU_W: 0.059, PSU_H: 0.152,
          LED_W: 0.0127, LED_H: 0.8128}


def test_filter_axis_dims_width_drops_hardware_and_led_cross_section():
    allowed, blocked = policy.filter_axis_dims(
        [MIRROR_W, CHASSIS_W, HANGER_W, CLIP_W, BRACKET_W, PSU_W, LED_W], "width",
        LABELS, VALUES)
    assert allowed == [MIRROR_W, CHASSIS_W]
    assert {n for n, _ in blocked} == {HANGER_W, CLIP_W, BRACKET_W, PSU_W, LED_W}


def test_filter_axis_dims_height_keeps_the_led_length():
    allowed, blocked = policy.filter_axis_dims(
        [MIRROR_H, CHASSIS_H, HANGER_H, CLIP_H, BRACKET_H, PSU_H, LED_H], "height",
        LABELS, VALUES)
    assert allowed == [MIRROR_H, CHASSIS_H, LED_H]
    assert {n for n, _ in blocked} == {HANGER_H, CLIP_H, BRACKET_H, PSU_H}


def test_filter_axis_dims_keeps_a_horizontal_led_length_on_the_WIDTH_axis():
    # The horizontal-strip model: the length is labeled [W] and must survive the filter.
    values = dict(VALUES); values["LEN@Boss-Extrude1 [Zortech-LEDS-1]"] = 0.9
    allowed, _blocked = policy.filter_axis_dims(
        [MIRROR_W, "LEN@Boss-Extrude1 [Zortech-LEDS-1]"], "width", LABELS, values)
    assert allowed == [MIRROR_W, "LEN@Boss-Extrude1 [Zortech-LEDS-1]"]


# ── runtime guard in interpret ─────────────────────────────────────────────────

def _chg(name, mm):
    return DimensionChange(name=name, value_meters=mm / 1000.0)


def test_enforce_policy_drops_blocked_changes():
    axis_labels = {MIRROR_W: "W", CHASSIS_W: "W", CLIP_W: "W", PSU_W: "W", LED_W: "W"}
    changes = [_chg(MIRROR_W, 1016), _chg(CHASSIS_W, 990),
               _chg(CLIP_W, 90), _chg(PSU_W, 70), _chg(LED_W, 20)]
    kept, dropped = _enforce_policy(changes, axis_labels, LABELS, "width", VALUES)
    assert [c.name for c in kept] == [MIRROR_W, CHASSIS_W]
    assert len(dropped) == 3


def test_enforce_policy_keeps_led_length_on_height():
    axis_labels = {MIRROR_H: "H", LED_H: "H"}
    kept, dropped = _enforce_policy([_chg(MIRROR_H, 1016), _chg(LED_H, 990)],
                                    axis_labels, LABELS, "height", VALUES)
    assert [c.name for c in kept] == [MIRROR_H, LED_H]
    assert dropped == []


def test_enforce_policy_classifies_on_CURRENT_not_proposed_value():
    """A cross-section wrongly scaled to 990 mm must still be recognised as a
    cross-section — so classification uses the current value, not the new one."""
    kept, dropped = _enforce_policy([_chg(LED_W, 990)], {LED_W: "W"}, LABELS, "width",
                                    VALUES)
    assert kept == []
    assert len(dropped) == 1


def test_enforce_policy_blocks_an_led_dim_with_no_known_value():
    # No values supplied → cannot classify → conservative block.
    kept, dropped = _enforce_policy([_chg(LED_W, 20)], {}, LABELS, "width")
    assert kept == []
    assert len(dropped) == 1


# ── constant-offset dependent scaling (the AMBER 36→48 regression) ─────────────

IN = 0.0254


def test_dependent_keeps_a_constant_offset_from_the_master():
    # Real AMBER numbers: chassis 34.000" and LED strip 32.000" under a 36.000" mirror
    # driven to 48.000". Ratio scaling gave 45.333"/42.667"; the borders must hold instead.
    assert _dependent_value(34 * IN, 36 * IN, 48 * IN) == pytest.approx(46 * IN)
    assert _dependent_value(32 * IN, 36 * IN, 48 * IN) == pytest.approx(44 * IN)


def test_dependent_offset_is_not_proportional():
    ratio = (34 * IN / (36 * IN)) * (48 * IN)
    assert _dependent_value(34 * IN, 36 * IN, 48 * IN) != pytest.approx(ratio)


def test_dependent_offset_holds_when_shrinking():
    assert _dependent_value(34 * IN, 36 * IN, 30 * IN) == pytest.approx(28 * IN)


def test_dependent_falls_back_to_the_target_without_a_master_value():
    assert _dependent_value(34 * IN, 0.0, 48 * IN) == pytest.approx(48 * IN)


# ── fixed-profile guard (the ALPHA 24→30 regression) ──────────────────────────

def test_alpha_extrusion_profile_is_left_alone():
    """Live regression: ALPHA width 24→30 (+6.000") added the delta to a 1.000" extrusion
    profile and produced 7.000" — a 7x blow-up. It must be left untouched instead."""
    assert _dependent_value(1.0 * IN, 24 * IN, 30 * IN) is None


def test_alpha_frame_spanning_dims_still_scale():
    # The same resize, for the dependents that genuinely span the frame.
    assert _dependent_value(21.188 * IN, 24 * IN, 30 * IN) == pytest.approx(27.188 * IN)
    assert _dependent_value(21.5 * IN, 24 * IN, 30 * IN) == pytest.approx(27.5 * IN)
    assert _dependent_value(21.0 * IN, 24 * IN, 30 * IN) == pytest.approx(27.0 * IN)


def test_led_cross_section_would_also_be_caught_by_the_ratio_guard():
    # Belt and braces: the LED profile is blocked by the LED rule AND by this guard.
    assert _dependent_value(0.5 * IN, 36 * IN, 48 * IN) is None


def test_the_threshold_sits_in_the_empty_gap_between_the_two_classes():
    # Observed frame dims are >= 0.875 of the master, profiles <= 0.042. Nothing real sits
    # near 0.5, so the guard has wide margin on both sides.
    assert policy.is_frame_spanning(0.875 * 24 * IN, 24 * IN) is True
    assert policy.is_frame_spanning(0.042 * 24 * IN, 24 * IN) is False


def test_a_dependent_larger_than_the_master_still_scales():
    # e.g. a flat-pattern dim (65" on a 36" product) — ratio > 1, clearly frame-spanning.
    assert _dependent_value(65 * IN, 36 * IN, 48 * IN) == pytest.approx(77 * IN)


def test_is_frame_spanning_is_permissive_without_a_master():
    assert policy.is_frame_spanning(1.0 * IN, 0.0) is True
