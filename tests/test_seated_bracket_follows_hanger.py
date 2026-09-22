"""A bracket seated in the hanger's slots follows the HANGER, whatever shape the mirror is.

Pinned to MICHELLE 30x42 -> 40x52, measured live 2026-09-18. The hanger went 18x18 -> 26x18, so
its W edge moved +4.000" and its H edge did not move at all. The seated brackets were given the
glass's half-delta instead:

    D1@Distance1  1411-HANGING-BRACKET-2 on W:  7.000" -> 12.000"   (+5.000", edge moved +4.000")
    D1@Distance4  1411-HANGING-BRACKET-2 on H:  9.500" -> 14.500"   (+5.000", edge did not move)

`_hanger_seat_shifts` has always known the right answer; it was gated behind `is_round`, and an
ellipse is not round. Both reported faults come out of that one gate:

  * `Edge-Flange1` walks out of `Cut-Extrude1`'s `Sketch3` — the tab travels further than the
    slot it sits in.
  * the brackets march toward the rim and foul the oval ring — they are being driven by the
    glass instead of by the part they are bolted to.

The brackets NOT seated in the hanger keep the edge-offset rule, and rectangles keep every other
behaviour they had. Both are asserted here, because widening who the seat rule applies to is the
kind of change that quietly moves parts on products nobody was looking at.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from interpret import _mate_position_updates
from models import (DimensionChange, DimensionIn, HangerSelection, InterpretRequest,
                    MatePositionIn)

IN = 0.0254

GLASS_W = "D2@Sketch1 [1032-MIRROR-MICHELLE-1]"
GLASS_H = "D1@Sketch1 [1032-MIRROR-MICHELLE-1]"
HANGER_W = "D2@Base-Flange1 [12456-HANGER-1]"
HANGER_H = "D1@Sketch1 [12456-HANGER-1]"

SEATED_W = "D1@Distance1 [12454-CHASSIS-ASSEMBLY-1]"
SEATED_H = "D1@Distance4 [12454-CHASSIS-ASSEMBLY-1]"
FREE_W = "D1@Distance5 [12454-CHASSIS-ASSEMBLY-1]"
FREE_H = "D1@Distance3 [12454-CHASSIS-ASSEMBLY-1]"

SEATED = "1411-HANGING-BRACKET-2"
FREE = "1411-HANGING-BRACKET-3"


def _mate(dim, value, comp, axis, offset, on_hanger):
    return MatePositionIn(dim=dim, value_meters=value * IN, component=comp, axis=axis,
                          extent_meters=1.5 * IN, direction=1.0,
                          offset_meters=offset * IN, on_hanger=on_hanger)


def _req(shape="ellipse"):
    return InterpretRequest(
        instruction="", shape=shape,
        master_width_dim=GLASS_W, master_height_dim=GLASS_H,
        dimensions=[DimensionIn(name=GLASS_W, value_meters=30 * IN),
                    DimensionIn(name=GLASS_H, value_meters=42 * IN),
                    DimensionIn(name=HANGER_W, value_meters=18 * IN),
                    DimensionIn(name=HANGER_H, value_meters=18 * IN),
                    DimensionIn(name=SEATED_W, value_meters=7.000 * IN),
                    DimensionIn(name=SEATED_H, value_meters=9.500 * IN),
                    DimensionIn(name=FREE_W, value_meters=4.000 * IN),
                    DimensionIn(name=FREE_H, value_meters=14.500 * IN)],
        mate_positions=[
            _mate(SEATED_W, 7.000, SEATED, "W", 7.750, True),
            _mate(SEATED_H, 9.500, SEATED, "H", 8.812, True),
            _mate(FREE_W, 4.000, FREE, "W", 4.000, False),
            _mate(FREE_H, 14.500, FREE, "H", 15.188, False),
        ],
        dim_axis_labels={GLASS_W: "W", GLASS_H: "H"},
    )


def _grown():
    """30x42 -> 40x52, and the hanger stretched 18x18 -> 26x18 exactly as the selector chose."""
    return [DimensionChange(name=GLASS_W, value_meters=40 * IN),
            DimensionChange(name=GLASS_H, value_meters=52 * IN),
            DimensionChange(name=HANGER_W, value_meters=26 * IN)]


def _hanger():
    return HangerSelection(resize_fitted=True, width_dim=HANGER_W, height_dim=HANGER_H,
                           target_width_meters=26 * IN, target_height_meters=18 * IN)


def _by_dim(out):
    return {dim: val / IN for dim, val, _base, _comp in out}


def test_seated_bracket_follows_the_hangers_edge_not_the_glass():
    """The tab must travel exactly as far as the slot it sits in: +4.000", not +5.000"."""
    out = _by_dim(_mate_position_updates(_req(), _grown(), _hanger()))
    assert out[SEATED_W] == pytest.approx(11.000, abs=1e-6)


def test_seated_bracket_is_HELD_on_an_axis_the_hanger_does_not_grow():
    """The hanger's height did not change, so the slot did not move, so nor may the tab.

    This is the bigger of the two errors: the bracket was travelling 5" on an axis where the
    thing it is bolted to travelled nothing.
    """
    out = _by_dim(_mate_position_updates(_req(), _grown(), _hanger()))
    assert SEATED_H not in out


def test_an_unseated_bracket_still_keeps_its_distance_from_the_edge():
    """Only parts IN the slots change rule. Everything else is untouched."""
    out = _by_dim(_mate_position_updates(_req(), _grown(), _hanger()))
    assert out[FREE_W] == pytest.approx(9.000, abs=1e-6)     # half the +10" width change
    assert out[FREE_H] == pytest.approx(19.500, abs=1e-6)    # half the +10" height change


def test_the_same_rule_applies_to_a_rectangle():
    """The seat rule is about the JOINT, not the outline — CAPSULE's bracket had it too."""
    out = _by_dim(_mate_position_updates(_req(shape="rect"), _grown(), _hanger()))
    assert out[SEATED_W] == pytest.approx(11.000, abs=1e-6)
    assert SEATED_H not in out


def test_nothing_moves_when_the_hanger_does_not():
    """A resize that leaves the hanger alone must leave everything seated in it alone."""
    kept = HangerSelection(keep_fitted=True, width_dim=HANGER_W, height_dim=HANGER_H,
                           target_width_meters=18 * IN, target_height_meters=18 * IN)
    changes = [DimensionChange(name=GLASS_W, value_meters=40 * IN),
               DimensionChange(name=GLASS_H, value_meters=52 * IN)]
    out = _by_dim(_mate_position_updates(_req(), changes, kept))
    assert SEATED_W not in out and SEATED_H not in out
    # ...while the parts that follow the glass still do.
    assert out[FREE_W] == pytest.approx(9.000, abs=1e-6)


def test_the_bracket_stays_well_inside_the_rim():
    """The collision complaint, as a number.

    On the 40x52 the chassis ellipse is 39.5 x 51.5, so at the bracket's own height the shell is
    much wider than where the bracket now sits. The old +5.000" answer was what walked it out.
    """
    import outline
    out = _by_dim(_mate_position_updates(_req(), _grown(), _hanger()))
    half_w, half_h = 39.5 / 2, 51.5 / 2
    # Held on H, so it stays at 8.812" off centre; W comes back to 11.000".
    room = outline.half_width_at(8.812, half_w, half_h, "ellipse")
    assert out[SEATED_W] + 0.75 < room          # 0.75" is half the bracket's own width
