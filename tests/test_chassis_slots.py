"""Chassis mounting-slot scaling.

Slot length tracks the chassis width by ratio in BOTH directions; the geometric fit formula is a
ceiling on top of that, not the rule itself. `slot_length_for(w, spec)` with no `scale` therefore
means "hold the length, apply the ceiling" — which is what the pure-ceiling tests below exercise.

The load-bearing tests are pinned to values MEASURED on the real AMBER 60x36 (2026-08-05, driven
over COM): at a 35" chassis 6.50" rebuilds clean and 6.75" fails, and the stock 7.5" slot survives
a 39.0" chassis but not 38.5". If a change to the formula breaks those, it has stopped describing
the actual part.

The spec itself is no longer hard-coded per part — it is measured off the model and arrives as
`slot_rows`. `AMBER_60` below is that measurement written out by hand.
"""

import os
import sys
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import chassis_slots
from chassis_slots import (SlotSpec, max_length_in, slot_length_for,
                           spec_from_measurement)
from interpret import _slot_follower_updates
from models import DimensionChange, DimensionIn, InterpretRequest, SlotRowIn

IN = 0.0254

# What the app measures on AMBER's 60" chassis. Part bbox 59.118", four 7.78"-overall slots
# between 4.000" insets; the driving dim holds the 7.5" centre-to-centre length.
AMBER_60 = SlotSpec(part="12211-CHASSIS-2", length_dim="D2@Sketch113 [12211-CHASSIS-2]",
                    stock_length_in=7.5, count=4, slot_width_in=0.28, inset_in=4.000,
                    gauge_in=0.0, min_gap_in=0.25)
ZERO_GAP = replace(AMBER_60, min_gap_in=0.0)

# The formula puts each boundary 0.0005" BELOW the measured-clean point — agreement to half a
# thou, a hair conservative. Smaller than any slot could be cut to, and absorbed many times over
# by flooring to 1/16" with a 0.25" gap. Tightening to `>=` would mean fudging inset or slot width
# to fit two data points.
AGREE = 1e-3
BBOX = 59.118          # the part width the app reports for the 59.000" sketch dim


def _width(chassis_sketch_in: float) -> float:
    """Sketch dim -> the bounding-box width the app measures (2 x 0.059" gauge)."""
    return chassis_sketch_in + 0.118


# ── the measured boundaries ──────────────────────────────────────────────────

def test_reproduces_the_measured_limit_at_a_35_inch_chassis():
    limit = max_length_in(_width(35.0), ZERO_GAP)
    assert limit == pytest.approx(6.4995, abs=1e-4)
    assert limit + AGREE >= 6.50, "must not contradict the 6.50 that rebuilt clean"
    assert limit < 6.75, "must reject the 6.75 that failed"


def test_reproduces_the_measured_stock_threshold():
    assert max_length_in(_width(39.0), ZERO_GAP) + AGREE >= AMBER_60.stock_length_in
    assert max_length_in(_width(38.5), ZERO_GAP) < AMBER_60.stock_length_in


def test_the_60_inch_original_is_far_inside_the_limit():
    assert max_length_in(BBOX, ZERO_GAP) > AMBER_60.stock_length_in


# ── the write decision ───────────────────────────────────────────────────────

def test_an_unchanged_width_writes_nothing():
    """scale == 1 and the length fits: no width change means no slot change."""
    assert slot_length_for(BBOX, AMBER_60, 1.0) is None
    assert slot_length_for(_width(49.0), AMBER_60, 1.0) is None
    assert slot_length_for(_width(45.0), AMBER_60, 1.0) is None


def test_shortens_at_35_inches_and_stays_under_the_measured_limit():
    """The ceiling alone, with the ratio held at 1 — 7.5" cannot fit, so it is cut to what does."""
    target = slot_length_for(_width(35.0), AMBER_60, 1.0)
    assert target is not None
    assert target <= 6.50, "must not exceed the length measured to rebuild clean"
    assert target == pytest.approx(6.25)


# ── scaling, both directions ─────────────────────────────────────────────────

def test_grows_with_the_chassis():
    """The point of the 2026-08-11 change: a wider chassis gets longer slots.

    Previously this returned None at every width above the drawn length.
    """
    target = slot_length_for(_width(70.0), AMBER_60, 70.118 / BBOX)
    assert target is not None
    assert target > AMBER_60.stock_length_in, "grew the chassis and the slots stayed put"
    assert target == pytest.approx(8.875)


def test_growth_is_proportional():
    """Double the part width, double the slot — within one 1/16" cut increment."""
    target = slot_length_for(BBOX * 2, AMBER_60, 2.0)
    assert target == pytest.approx(2 * AMBER_60.stock_length_in, abs=chassis_slots.ROUND_TO_IN)


def test_the_fit_ceiling_still_wins_over_the_ratio():
    """Shrinking asks for 4.456"; that fits, so the ratio is used. Growth is what gets clamped —
    and on the narrow side the ceiling must override any ratio that overshoots it."""
    # A deliberately wrong scale that asks for more than the width can hold.
    target = slot_length_for(_width(35.0), AMBER_60, 3.0)
    assert target is not None
    assert target == pytest.approx(6.25), "the ratio was allowed past the fit limit"
    assert 4 * (target + AMBER_60.slot_width_in) + 3 * 0.25 <= 35.118 + 1e-9


def test_shrink_then_grow_does_not_ratchet():
    """The reported bug: a saved shrink became the new ceiling and the length never came back.

    Shrink 59" -> 35", save, then grow back. The recovered length must land near the original
    rather than sticking at the shortened value.
    """
    shrunk = slot_length_for(_width(35.0), AMBER_60, 35.118 / BBOX)
    assert shrunk is not None and shrunk < AMBER_60.stock_length_in

    # Re-measured: the model now reports the shortened slot as its current length.
    after_save = replace(AMBER_60, stock_length_in=shrunk)
    back = slot_length_for(BBOX, after_save, BBOX / 35.118)
    assert back is not None, "growing back returned None — the ratchet is still there"
    assert back == pytest.approx(AMBER_60.stock_length_in, abs=2 * chassis_slots.ROUND_TO_IN)


def test_target_is_a_clean_sixteenth():
    for w in (35.0, 36.5, 37.25, 38.0, 38.75):
        target = slot_length_for(_width(w), AMBER_60, _width(w) / BBOX)
        if target is not None:
            assert abs(target / chassis_slots.ROUND_TO_IN
                       - round(target / chassis_slots.ROUND_TO_IN)) < 1e-9


def test_never_rounds_up_past_what_fits():
    """Swept at the real scale for each width, so the ceiling is checked against ratios that
    actually occur — not just against a held length."""
    for w in [30.0 + 0.25 * i for i in range(40)]:
        target = slot_length_for(_width(w), AMBER_60, _width(w) / BBOX)
        if target is not None:
            assert target <= max_length_in(_width(w), ZERO_GAP)


def test_absurdly_narrow_chassis_gives_up_rather_than_writing_nonsense():
    assert slot_length_for(9.0, AMBER_60, 9.0 / BBOX) is None
    assert slot_length_for(1.0, AMBER_60, 1.0 / BBOX) is None


def test_a_nonsense_scale_is_refused():
    assert slot_length_for(BBOX, AMBER_60, 0.0) is None
    assert slot_length_for(BBOX, AMBER_60, -1.0) is None


# ── building the spec from a measurement ─────────────────────────────────────

def _row(**kw):
    base = dict(dim="D2@Sketch113 [12211-CHASSIS-2]", length_meters=7.5 * IN, count=4,
                slot_width_meters=0.28 * IN, inset_meters=4.0 * IN,
                part_width_meters=BBOX * IN, component="12211-CHASSIS-2")
    base.update(kw)
    return SlotRowIn(**base)


def test_a_measured_row_reproduces_the_hand_written_spec():
    spec = spec_from_measurement(_row())
    assert spec.count == 4
    assert spec.stock_length_in == pytest.approx(7.5)
    assert spec.slot_width_in == pytest.approx(0.28)
    assert spec.inset_in == pytest.approx(4.0)
    assert max_length_in(BBOX, spec) == pytest.approx(max_length_in(BBOX, AMBER_60))


def test_a_row_that_is_not_really_a_row_is_rejected():
    assert spec_from_measurement(_row(count=1)) is None
    assert spec_from_measurement(_row(length_meters=0)) is None
    assert spec_from_measurement(None) is None


def test_no_part_numbers_are_hard_coded_anywhere():
    """The bug this whole redesign exists to prevent: a table keyed to 12211 did nothing at all
    for 12204 on the next product."""
    assert chassis_slots.CHASSIS_SLOTS == ()


# ── the follower ─────────────────────────────────────────────────────────────

WIDTH = "D1@Sketch1 [12211-CHASSIS-2]"
SLOT = "D2@Sketch113 [12211-CHASSIS-2]"
MIRROR = "WIDTH@Sketch1 [1011-MIRROR-CAROL-1]"
REAL = {MIRROR: 60 * IN, WIDTH: 59 * IN, SLOT: 7.5 * IN}


def _req(dims=None, rows=None):
    return InterpretRequest(
        instruction="x",
        dimensions=[DimensionIn(name=n, value_meters=v) for n, v in (dims or REAL).items()],
        dim_axis_labels={WIDTH: "W", SLOT: "W", MIRROR: "W"},
        master_width_dim=MIRROR,
        slot_rows=rows if rows is not None else [_row()],
    )


def test_follower_shortens_the_slots_on_the_real_60_to_36():
    """59" -> 35" driver, so the part goes 59.118" -> 35.118": scale 0.5941, 7.5" -> 4.456",
    floored to 4.4375". Well inside the 6.312" that fits, so the ratio governs, not the ceiling."""
    out = _slot_follower_updates(_req(), [DimensionChange(name=WIDTH, value_meters=35 * IN)])
    assert len(out) == 1
    name, new_val, old_val = out[0]
    assert name == SLOT
    assert old_val == pytest.approx(7.5 * IN)
    assert new_val / IN == pytest.approx(4.4375)
    assert 4 * (new_val / IN + 0.28) + 3 * 0.25 <= 35.118 - 8.0 + 1e-9


def test_follower_scales_on_the_50_inch_resize():
    """Used to be a no-op ("stock still fits"). The slots now track the width like anything else."""
    out = _slot_follower_updates(_req(), [DimensionChange(name=WIDTH, value_meters=49 * IN)])
    assert len(out) == 1
    assert out[0][1] / IN == pytest.approx(6.1875)


def test_follower_lengthens_the_slots_when_the_chassis_grows():
    """The behaviour the user asked for: growing the mirror grows the slots.

    59" -> 70" driver: part 70.118", scale 1.1861, 7.5" -> 8.898", floored to 8.875".
    """
    out = _slot_follower_updates(_req(), [DimensionChange(name=WIDTH, value_meters=70 * IN)])
    assert len(out) == 1
    name, new_val, old_val = out[0]
    assert name == SLOT
    assert new_val > old_val, "the chassis grew and the slots did not"
    assert new_val / IN == pytest.approx(8.875)


def test_follower_does_nothing_without_a_measurement():
    """No slot_rows (older app build, or a part with no slot row) must be a clean no-op."""
    assert _slot_follower_updates(
        _req(rows=[]), [DimensionChange(name=WIDTH, value_meters=35 * IN)]) == []


def test_follower_works_on_a_completely_different_chassis():
    """The 12204 case that the hard-coded table silently skipped: AMBER 36x36 -> 24" wide.
    Three 6.28"-overall slots on a 35.118" part, shrinking to 23"."""
    width_dim = "D1@Sketch1 [12204-CHASSIS-2]"
    slot_dim = "D2@Sketch99 [12204-CHASSIS-2]"
    dims = {MIRROR: 36 * IN, width_dim: 35 * IN, slot_dim: 6.0 * IN}
    row = SlotRowIn(dim=slot_dim, length_meters=6.0 * IN, count=3,
                    slot_width_meters=0.28 * IN, inset_meters=4.0 * IN,
                    part_width_meters=35.118 * IN, component="12204-CHASSIS-2")
    req = InterpretRequest(
        instruction="x",
        dimensions=[DimensionIn(name=n, value_meters=v) for n, v in dims.items()],
        dim_axis_labels={width_dim: "W", slot_dim: "W", MIRROR: "W"},
        master_width_dim=MIRROR, slot_rows=[row])

    out = _slot_follower_updates(req, [DimensionChange(name=width_dim, value_meters=23 * IN)])
    assert len(out) == 1, "the 12204 chassis was skipped — the part-number bug is back"
    name, new_val, _old = out[0]
    assert name == slot_dim
    # Part 35.118" -> 23.118": scale 0.6583, 6.0" -> 3.950", floored to 3.9375". The ceiling here
    # is 4.593" (15.118" span, 3 slots + 2 x 0.25" gaps), so the ratio governs — but still fits.
    assert new_val / IN == pytest.approx(3.9375)
    assert 3 * (new_val / IN + 0.28) + 2 * 0.25 <= 23.118 - 8.0 + 1e-9


def test_follower_ignores_a_row_belonging_to_another_part():
    out = _slot_follower_updates(
        _req(rows=[_row(component="SOME-OTHER-PART-1")]),
        [DimensionChange(name=WIDTH, value_meters=35 * IN)])
    assert out == []
