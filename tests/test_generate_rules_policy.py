"""generate_rules end-to-end with the LLM stubbed: the deterministic pass must correct
a bad LLM response into mirror-glass-mastered rules with the hardware skipped.

The stub returns the exact failure modes seen in real output: every dim as its own
master (the old N-pairs shape), the LED strip's LENGTH filed under width_rules, and
rules handed to the clips / brackets / power supplies.
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import generate_rules as gr
from models import GenerateRulesRequest, DimensionIn

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
LED_H = "D1@Boss-Extrude1 [Zortech-Low-Profile-Double Row LEDS-1]"

DIMS = {
    MIRROR_W: (0.9144, "W"), MIRROR_H: (0.9144, "H"),
    CHASSIS_W: (0.8636, "W"), CHASSIS_H: (0.8636, "H"),
    HANGER_W: (0.3619, "W"), HANGER_H: (0.3810, "H"),
    CLIP_W: (0.0762, "W"), CLIP_H: (0.0762, "H"),
    BRACKET_W: (0.0500, "W"), BRACKET_H: (0.0500, "H"),
    PSU_W: (0.0590, "W"), PSU_H: (0.1520, "H"),
    LED_W: (0.0127, "W"), LED_H: (0.8636, "H"),
}

COMPONENT_LABELS = {
    "1011-MIRROR-CAROL-1": "Mirror Glass",
    "12204-CHASSIS-2": "Main Chassis",
    "1038-HANGER-1": "Mirror Hanger",
    "1005-CLIP-1": "Mirror Clip Left",
    "1292-LED-BRACKET-2": "LED Bracket Right",
    "LPM-24060A-2": "LED Power Supply Left",
    "LPM-24060A-1": "LED Power Supply Right",
    "Zortech-Low-Profile-Double Row LEDS-1": "LED Strip Left",
}

BAD_LLM_RESPONSE = json.dumps({
    "width_rules": [{"if_changes": n, "also_change": []}
                    for n, (_v, a) in DIMS.items() if a == "W"]
    + [{"if_changes": LED_H, "also_change": [CLIP_W]}],   # strip LENGTH mis-filed as width
    "height_rules": [{"if_changes": n, "also_change": []}
                     for n, (_v, a) in DIMS.items() if a == "H"],
    "skip": [],
    "component_labels": COMPONENT_LABELS,
    "part_label": "",
})


@pytest.fixture
def result(monkeypatch):
    async def fake_llm(system, user, max_tokens=None):
        return BAD_LLM_RESPONSE

    monkeypatch.setattr(gr, "call_llm", fake_llm)
    req = GenerateRulesRequest(
        assembly_context="(stub)",
        dimensions=[DimensionIn(name=n, value_meters=v) for n, (v, _a) in DIMS.items()],
        dim_axis_labels={n: a for n, (_v, a) in DIMS.items()},
        master_width_dim=MIRROR_W,
        master_height_dim=MIRROR_H,
    )
    return asyncio.run(gr.generate_rules(req))


def test_exactly_one_rule_per_axis_mastered_on_the_mirror_glass(result):
    assert len(result.width_rules) == 1
    assert len(result.height_rules) == 1
    assert result.width_rules[0].if_changes == MIRROR_W
    assert result.height_rules[0].if_changes == MIRROR_H


def test_width_rule_carries_only_the_resizable_frame_parts(result):
    assert set(result.width_rules[0].also_change) == {CHASSIS_W}


def test_height_rule_includes_the_led_strip_length(result):
    assert set(result.height_rules[0].also_change) == {CHASSIS_H, LED_H}


def test_fixed_size_hardware_never_appears_in_any_rule(result):
    in_rules = set()
    for r in result.width_rules + result.height_rules:
        in_rules.add(r.if_changes)
        in_rules.update(r.also_change)
    for dim in (CLIP_W, CLIP_H, BRACKET_W, BRACKET_H, PSU_W, PSU_H, LED_W,
                HANGER_W, HANGER_H):
        assert dim not in in_rules, dim


def test_blocked_dims_are_reported_in_skip_with_a_reason(result):
    skipped = {s.name: s.reason for s in result.skip}
    assert set(skipped) == {CLIP_W, CLIP_H, BRACKET_W, BRACKET_H, PSU_W, PSU_H, LED_W,
                            HANGER_W, HANGER_H}
    assert all(reason for reason in skipped.values())
    assert "cross-section" in skipped[LED_W]
    assert "prefab" in skipped[HANGER_H].lower()
