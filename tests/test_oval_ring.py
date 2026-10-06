"""Hardware inside MICHELLE's oval ring must stay inside it, and stay together (ISSUE-094).

Pinned to the live MICHELLE 30x42 -> 50x62 of 2026-10-06. The rectangle rule moved the bottom
hanging bracket `1411-HANGING-BRACKET-3` by half the master change on BOTH axes (+10" / +10"),
from (4.000", -15.188") to (14.000", -25.187"): measured off the model afterwards, 2.3" outside
the ring. The first fix moved the bracket by the ring but left its clip `1005-CLIP-2` (mated onto
the bracket, both 4.000" off centre as built) on the edge rule, and the two split apart.

The ring's inner half-axes (12.2623 x 18.2623", grown by half the glass change to
22.2623 x 28.2623") and every mate below come from that run's log.
"""

import pytest

from engine.resize.interpret import _mate_position_updates, OVAL_RING_MARGIN_M
from engine.core.models import DimensionChange, DimensionIn, InterpretRequest, MatePositionIn

IN = 0.0254
GLASS_W = "D2@Sketch1 [1032-MIRROR-MICHELLE-1]"
GLASS_H = "D1@Sketch1 [1032-MIRROR-MICHELLE-1]"
RING_W, RING_H = 12.2623, 18.2623          # inner surface, inches
BRACKET = "1411-HANGING-BRACKET-3"
B_W, B_H = "D1@Distance5 [12454-CHASSIS-ASSEMBLY-1]", "D1@Distance3 [12454-CHASSIS-ASSEMBLY-1]"
CLIP, LPM = "D1@Distance9", "D1@Distance7"

MATES = [
    MatePositionIn(dim=B_W, value_meters=4.0 * IN, component=BRACKET, axis="W",
                   extent_meters=1.5 * IN, offset_meters=4.0 * IN, radius_meters=15.705 * IN),
    MatePositionIn(dim=B_H, value_meters=14.5 * IN, component=BRACKET, axis="H",
                   extent_meters=2.125 * IN, offset_meters=-15.188 * IN, radius_meters=15.705 * IN),
    MatePositionIn(dim=CLIP, value_meters=4.0 * IN, component="1005-CLIP-2", axis="W",
                   extent_meters=3.0 * IN, offset_meters=-4.0 * IN, radius_meters=13.458 * IN),
    MatePositionIn(dim=LPM, value_meters=3.0 * IN, component="LPM-24060A-1", axis="W",
                   extent_meters=2.323 * IN, offset_meters=-3.0 * IN, radius_meters=3.0 * IN),
]


def _resize(w, h):
    return [DimensionChange(name=GLASS_W, value_meters=w * IN),
            DimensionChange(name=GLASS_H, value_meters=h * IN)]


GROW = _resize(50, 62)


def _req(mates=None, ring=(RING_W, RING_H), shape="ellipse", w=30.0, h=42.0):
    mates = MATES if mates is None else mates
    dims = {GLASS_W: w * IN, GLASS_H: h * IN, **{m.dim: m.value_meters for m in mates}}
    return InterpretRequest(
        instruction="x", shape=shape,
        dimensions=[DimensionIn(name=n, value_meters=v) for n, v in dims.items()],
        master_width_dim=GLASS_W, master_height_dim=GLASS_H, mate_positions=mates,
        ring_half_w_meters=ring[0] * IN, ring_half_h_meters=ring[1] * IN)


def _written(out):
    return {dim: new / IN for dim, new, _old, _comp in out}


def _corner_norm(x, y, hw, hh, a, b):
    """<= 1 when the part's outer corner is inside an a x b ellipse."""
    return ((abs(x) + hw) / a) ** 2 + ((abs(y) + hh) / b) ** 2


def test_the_bracket_moves_in_proportion_to_the_ring():
    w = _written(_mate_position_updates(_req(), GROW))
    assert w[B_W] == pytest.approx(4.0 * 22.2623 / 12.2623, abs=1e-3)       # 7.262"
    assert w[B_H] == pytest.approx(14.5 + 15.188 * (28.2623 / 18.2623 - 1), abs=1e-3)


def test_the_clip_stays_on_its_bracket():
    """The live complaint: bracket 7.262", clip 14.000" — now the same offset, as built."""
    w = _written(_mate_position_updates(_req(), GROW))
    assert w[CLIP] == pytest.approx(w[B_W], abs=1e-9)


def test_the_clip_floor_does_not_pull_the_clip_off_its_bracket():
    """master/6 = 8.333" at 50" — above the bracket's 7.262", so it must not apply here."""
    w = _written(_mate_position_updates(_req(), GROW))
    assert w[CLIP] < 50 / 6


def test_the_bracket_ends_up_inside_the_ring_with_the_margin():
    w = _written(_mate_position_updates(_req(), GROW))
    m = OVAL_RING_MARGIN_M / IN
    assert _corner_norm(w[B_W], 15.188 + (w[B_H] - 14.5), 0.75, 1.0625,
                        22.2623 - m, 28.2623 - m) <= 1.0


def test_the_old_rectangle_answer_really_was_outside():
    """Guards the test itself: the live (14, -25.187) position fails the same check."""
    assert _corner_norm(14.0, 25.187, 0.75, 1.0625, 22.2623, 28.2623) > 1.0


def test_the_power_supply_keeps_the_edge_rule_and_is_not_checked():
    w = _written(_mate_position_updates(_req(), GROW))
    assert w[LPM] == pytest.approx(13.0)                # 3 + 20/2, exactly as before


def test_a_shrink_pulls_the_bracket_and_its_clip_in_together():
    """Shrinking makes a fixed-size bracket relatively bigger: 30x42 -> 24x36 needs a pull."""
    out = _written(_mate_position_updates(_req(), _resize(24, 36)))
    ring_w, ring_h = RING_W - 3.0, RING_H - 3.0          # half of the 6" shrink per axis
    m = OVAL_RING_MARGIN_M / IN
    y = 15.188 + (out[B_H] - 14.5)
    assert out[B_W] < 4.0 * ring_w / RING_W              # pulled in past plain proportion
    assert _corner_norm(out[B_W], y, 0.75, 1.0625, ring_w - m, ring_h - m) == pytest.approx(1.0, abs=1e-6)
    assert out[CLIP] == pytest.approx(out[B_W], abs=1e-9)   # still on its bracket


def test_shrinking_back_returns_the_stack_to_where_it_was_built():
    grown = _written(_mate_position_updates(_req(), GROW))
    mates = [
        MATES[0].model_copy(update={"value_meters": grown[B_W] * IN,
                                    "offset_meters": grown[B_W] * IN}),
        MATES[1].model_copy(update={"value_meters": grown[B_H] * IN,
                                    "offset_meters": -(15.188 + grown[B_H] - 14.5) * IN}),
        MATES[2].model_copy(update={"value_meters": grown[CLIP] * IN,
                                    "offset_meters": -grown[CLIP] * IN}),
    ]
    back = _written(_mate_position_updates(_req(mates, ring=(22.2623, 28.2623), w=50, h=62),
                                           _resize(30, 42)))
    assert back[B_W] == pytest.approx(4.0, abs=1e-6)
    assert back[B_H] == pytest.approx(14.5, abs=1e-6)
    assert back[CLIP] == pytest.approx(4.0, abs=1e-6)


def test_no_ring_keeps_the_old_rectangle_behaviour():
    w = _written(_mate_position_updates(_req(ring=(0, 0)), GROW))
    assert w[B_W] == pytest.approx(14.0)
    assert w[B_H] == pytest.approx(24.5)
    assert w[CLIP] == pytest.approx(14.0)


def test_a_round_product_never_uses_the_oval_ring():
    w = _written(_mate_position_updates(_req(shape="round"), GROW))
    assert B_W not in w or w[B_W] != pytest.approx(4.0 * 22.2623 / 12.2623, abs=1e-3)


def test_a_seated_bracket_is_never_moved_by_the_ring():
    seated = [m.model_copy(update={"on_hanger": True}) for m in MATES[:2]]
    w = _written(_mate_position_updates(_req(seated), GROW))
    assert B_W not in w and B_H not in w               # no hanger here, so it holds
