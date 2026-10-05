"""HALF-FRAME parts: a part that runs from the glass's centre line out to ONE edge.

Measured off the real SUZI-24.00X36.00 assembly (live 2026-09-29). Its chassis corner frame is
one part, `12473-CHASSIS CORNERA`, placed twice with the second turned 180 degrees:

    SUZI-MIRROR            D2@Sketch1  914.40 mm  (36.000")  the glass height, master
    12476-CHASSIS          D2@Sketch1  908.05 mm  (35.750")  full-frame, glass - 0.25"
    12473-CHASSIS CORNERA  D2@Sketch1  393.70 mm  (15.500")  y 0 .. +15.5 (and 0 .. -15.5)

15.500 / 36.000 = 43%, under the 50% frame-spanning bar, so the corner height was dropped as a
"fixed profile" and never grew; the LED strip is drawn in context off the corner's edges and
stayed the same size with it. The app now names such dims in `half_frame_dims`: their far edge
follows the glass edge, which moves HALF the master delta.
"""

import pytest

from engine.resize import interpret
from engine.core.models import InterpretRequest
from engine.rules.rules import ModelRules, RulePairEntry

IN = 0.0254
GLASS_W, GLASS_H = 24 * IN, 36 * IN
CHASSIS_H = 35.75 * IN
CORNER_H = 15.5 * IN

M_W = "D1@Sketch1 [SUZI-MIRROR-1]"
M_H = "D2@Sketch1 [SUZI-MIRROR-1]"
CH_H = "D2@Sketch1 [12476-CHASSIS-5]"
CORNER1_H = "D2@Sketch1 [12473-CHASSIS CORNERA-1]"
CORNER5_H = "D2@Sketch1 [12473-CHASSIS CORNERA-5]"

CURRENT = {M_W: GLASS_W, M_H: GLASS_H, CH_H: CHASSIS_H, CORNER1_H: CORNER_H, CORNER5_H: CORNER_H}
DEPS = [CH_H, CORNER1_H, CORNER5_H]


def _by_name(changes):
    return {c.name: c.value_meters / IN for c in changes}


def test_half_frame_corner_takes_half_the_height_change():
    """36 -> 40: the glass edge moves 2", so each half grows 2" and the chassis the full 4"."""
    out = _by_name(interpret._expand_master(M_H, 40 * IN, DEPS, CURRENT, {},
                                            {CORNER1_H, CORNER5_H}))
    assert out[M_H] == pytest.approx(40.0)
    assert out[CH_H] == pytest.approx(39.75)
    assert out[CORNER1_H] == pytest.approx(17.5)
    assert out[CORNER5_H] == pytest.approx(17.5)


def test_half_frame_corner_shrinks_by_half_too():
    out = _by_name(interpret._expand_master(M_H, 30 * IN, DEPS, CURRENT, {},
                                            {CORNER1_H, CORNER5_H}))
    assert out[CORNER1_H] == pytest.approx(12.5)


def test_without_the_verdict_the_corner_is_left_alone_as_before():
    """An older app sends no half_frame_dims: behaviour is exactly what it was."""
    out = _by_name(interpret._expand_master(M_H, 40 * IN, DEPS, CURRENT, {}))
    assert CORNER1_H not in out and CORNER5_H not in out
    assert out[CH_H] == pytest.approx(39.75)


def test_a_small_part_is_still_a_fixed_profile_even_when_half_frame():
    """Judged at double its value: a 3" strip beside the centre line is 17% of the glass, and
    must still keep its size rather than take any delta."""
    small = "D1@Sketch1 [TAB-1]"
    out = _by_name(interpret._expand_master(M_H, 40 * IN, [small], {**CURRENT, small: 3 * IN},
                                            {}, {small}))
    assert small not in out


def test_second_axis_of_a_two_number_request_uses_the_verdict():
    """"28 x 40" reaches the height through the second-axis path, which must honour it too."""
    rules = ModelRules(model="SUZI-24.00X36.00", limits=None,
                       width=[RulePairEntry(M_W, [])],
                       height=[RulePairEntry(M_H, DEPS)])
    req = InterpretRequest(instruction="Resize to 28 x 40", master_width_dim=M_W,
                           master_height_dim=M_H, half_frame_dims=[CORNER1_H, CORNER5_H])
    changes, _note = interpret._second_axis_changes(
        {"other_axis_meters": 40 * IN}, "overall", M_W, rules, req, CURRENT)
    out = _by_name(changes)
    assert out[CORNER1_H] == pytest.approx(17.5)
    assert out[CH_H] == pytest.approx(39.75)
