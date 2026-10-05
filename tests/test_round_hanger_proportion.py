"""A ROUND product keeps the hanger's own proportion of the diameter, and comes back.

Reported live 2026-09-14: after Ø30 → Ø45 → Ø30 the hanging brackets collided with `12419-RING`.

The brackets sit in the hanger's slots, so they go wherever the hanger's ends go — and the hanger
did not come back. `TARGET_WIDTH_FRACTION` re-derives its width as 65% of the glass every time,
so 14.250" became 29.250" at Ø45 (65% of 45) and then **19.500"** back at Ø30 (65% of 30), not the
14.250" it started at. The brackets rode out with it:

    as drawn      hanger 14.25 x 10.00   bracket 10.80" from centre   ring 13.059"   clear 1.20"
    round trip    hanger 19.50 x  8.25   bracket 11.80" from centre   ring 13.059"   clear 0.19"

That 65% target and the 55% floor under it were read off the client's landscape RECTANGLES.
ECLIPSE's own drawn hanger is 14.25" on a 30" disc — **47.5%** — so the rectangle rule inflates it
by more than a third the moment anything is resized.
"""

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.hangers.hanger_select import select_hanger

# ECLIPSE-30 as drawn, all measured off the model.
FIT_W, FIT_H = 14.25, 10.00
OFF_W, OFF_H = 5.875, 9.062        # bracket 1/2 box centre, from the mirror's centre
HALF = 1.0625                      # its widest half
RING_AT_30 = 13.059                # 12419-RING's outer wall


def _pick(dia, fitted_w, fitted_h, on_dia):
    return select_hanger(dia, dia, fitted_w_in=fitted_w, fitted_h_in=fitted_h,
                         chassis_w_in=dia - 0.5, round_glass=True, fitted_glass_w_in=on_dia)


def _bracket_radius(hanger_w, hanger_h):
    """Where bracket 1/2 ends up: it holds a constant inset from the hanger's ends, so it moves
    by exactly half of whatever the hanger's overall size does."""
    return math.hypot(OFF_W + (hanger_w - FIT_W) / 2, OFF_H + (hanger_h - FIT_H) / 2)


def test_the_hanger_comes_back_to_exactly_what_it_was():
    up = _pick(45, FIT_W, FIT_H, 30)
    assert up.resize_fitted
    down = _pick(30, up.target_width_in, up.target_height_in, 45)
    assert round(down.target_width_in, 4) == FIT_W
    assert round(down.target_height_in, 4) == FIT_H


def test_the_brackets_come_back_off_the_ring():
    """0.19" of clearance after a round trip was the reported collision; it must be the drawn
    1.20" again."""
    drawn = RING_AT_30 - _bracket_radius(FIT_W, FIT_H) - HALF
    up = _pick(45, FIT_W, FIT_H, 30)
    down = _pick(30, up.target_width_in, up.target_height_in, 45)
    after = RING_AT_30 - _bracket_radius(down.target_width_in, down.target_height_in) - HALF
    assert round(after, 4) == round(drawn, 4)
    assert round(after, 2) == 1.20


def test_the_old_rule_really_did_walk_the_hanger_out():
    """What it used to do, kept as a number so the regression is recognisable."""
    assert round(0.65 * 45, 3) == 29.250          # Ø45
    assert round(0.65 * 30, 3) == 19.500          # back at Ø30 — not 14.250
    walked = RING_AT_30 - _bracket_radius(19.500, 8.250) - HALF
    assert round(walked, 2) == 0.19               # the reported collision


def test_it_holds_the_drawn_fraction_at_every_size():
    """Proportional, then snapped to the 0.25" manufacturing increment — a resized hanger has to
    be cut, so 21.500" beats 21.375". That snap is the only thing between the result and the exact
    fraction, and it is half a step at worst."""
    from engine.hangers.hanger_select import RESIZE_ROUND_TO_IN
    for dia in (24, 36, 45, 60):
        c = _pick(dia, FIT_W, FIT_H, 30)
        if not c.resize_fitted:
            continue
        exact = FIT_W * dia / 30
        assert abs(c.target_width_in - exact) <= RESIZE_ROUND_TO_IN / 2 + 1e-9, dia


def test_the_snap_settles_instead_of_walking():
    """Ø30 → Ø24 → Ø30 lands on 14.500" rather than 14.250": 11.400" snaps up to 11.500", and
    14.375" back is an exact tie that rounds up too. One step, and it does NOT keep going — the
    next round trip returns 14.500" again. Worth pinning: the bug this file exists for was a
    5.250" walk, and a quarter inch that settles is a different animal from one that does not."""
    w, h = FIT_W, FIT_H
    seen = []
    for _ in range(4):
        up = _pick(24, w, h, 30)
        back = _pick(30, up.target_width_in, up.target_height_in, 24)
        w, h = back.target_width_in, back.target_height_in
        seen.append(round(w, 4))
    assert seen[0] == 14.5
    assert len(set(seen)) == 1          # settled on the first trip, never moves again


def test_growing_only_ever_opens_the_gap_to_the_ring():
    """The ring holds a CONSTANT inset from the rim, so it travels the full radius change while
    the hardware travels only its share. Clearance can only improve on a grow."""
    drawn = RING_AT_30 - _bracket_radius(FIT_W, FIT_H) - HALF
    for dia in (36, 45, 60):
        c = _pick(dia, FIT_W, FIT_H, 30)
        ring = RING_AT_30 + (dia - 30) / 2
        gap = ring - _bracket_radius(c.target_width_in, c.target_height_in) - HALF
        assert gap > drawn, dia


def test_a_rectangular_product_is_untouched():
    """No `round_glass`, so the span target still governs — this is every other model."""
    c = select_hanger(90, 36, fitted_w_in=20, fitted_h_in=15, fitted_glass_w_in=60)
    if c.resize_fitted:
        assert round(c.target_width_in / 90, 2) == 0.65


def test_no_fitted_glass_size_falls_back_to_the_old_rule():
    """An older app sends nothing, and must behave exactly as it did before."""
    c = _pick(45, FIT_W, FIT_H, 0)
    if c.resize_fitted:
        assert round(c.target_width_in / 45, 2) == 0.65
