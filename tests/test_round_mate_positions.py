"""On a ROUND product the hardware keeps its SHARE of the disc, capped by the LED and the rim.

Pinned to the live ECLIPSE failures of 2026-09-14, both of them.

Ø30 → Ø60 fed the diameter to the W and the H axis alike and added half the delta (15") to each.
A mate holds a cartesian component, not a radius, so that pushed `2867-HANGING-BRACKET-2` from
(5.875", 9.062") — r = 10.80" — out to (20.125", 24.750") — r = 31.86" — past a chassis rim of
29.75", and swung its angle on the way. Ø30 → Ø45 did the same thing more subtly: the brackets
landed at r ≈ 21.3–21.5 and the LED extrusion band runs 20.56"–22.50", so they came down ON the
lit ring instead of behind it.

The rule now: radius scales with the diameter (angle preserved exactly), capped so the part can
reach neither the LED channel nor the chassis rim. The channel's own radius moves by a CONSTANT
inset from the rim, not proportionally — read out of the client's shipped chassis flat patterns,
where it sits 1.671" inside the rim at Ø30, Ø36 AND Ø60, identical to the thousandth. So on a
shrink the channel closes in faster than the hardware does, and that is when the cap bites.
"""

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from interpret import _mate_position_updates
from models import DimensionChange, DimensionIn, InterpretRequest, MatePositionIn

IN = 0.0254
GLASS = "D1@Sketch1 [1026-MIRROR-ECLIPSE-1]"

# Every number below is read off the real ECLIPSE-30: the mate values and component offsets from
# the 2026-09-14 log, the keep-out from 12419-RING's 663.40 mm box.
B2_W, B2_H = "D1@Distance1 [12417-CHASSIS-ASSEMBLY-1]", "D1@Distance4 [12417-CHASSIS-ASSEMBLY-1]"
B3_W, B3_H = "D1@Distance5 [12417-CHASSIS-ASSEMBLY-1]", "D1@Distance3 [12417-CHASSIS-ASSEMBLY-1]"
B1_W = "D1@Distance2 [12417-CHASSIS-ASSEMBLY-1]"

B1 = "2867-HANGING-BRACKET-1"
CLIP = "1005-CLIP-2"
CLIP_W = "D1@Distance4"
B2 = "2867-HANGING-BRACKET-2"
B3 = "2867-HANGING-BRACKET-3"
B2_R = math.hypot(5.875, 9.062) * IN      # 10.800"
B3_R = math.hypot(4.000, 10.688) * IN     # 11.412"
RING = 331.70 / 1000                      # 13.059" — 12419-RING's outer wall

DIMS = {GLASS: 30 * IN, B2_W: 5.125 * IN, B2_H: 9.750 * IN,
        B3_W: 4.000 * IN, B3_H: 10.000 * IN, B1_W: 5.125 * IN}


def _m(dim, value, comp, axis, extent, offset, radius, ring=RING):
    return MatePositionIn(dim=dim, value_meters=value * IN, component=comp, axis=axis,
                          extent_meters=extent * IN, direction=1.0,
                          offset_meters=offset * IN, radius_meters=radius,
                          keep_out_meters=ring)


MATES = [
    _m(B2_W, 5.125, B2, "W", 1.500, 5.875, B2_R),
    _m(B2_H, 9.750, B2, "H", 2.125, 9.062, B2_R),
    _m(B3_W, 4.000, B3, "W", 1.500, 4.000, B3_R),
    _m(B3_H, 10.000, B3, "H", 2.125, 10.688, B3_R),
    _m(B1_W, 5.125, B1, "W", 1.500, 5.875, B2_R),
]

# Where each bracket's box centre sits at the drawn size, and how far its own body reaches past
# that. `overhang` is the widest half of the part, shared by both its mates so the move stays
# radial.
AT_30 = {B2: (5.875, 9.062, 1.0625), B3: (4.000, 10.688, 1.0625)}


def _req(mates=None, round_=True):
    return InterpretRequest(
        instruction="x",
        dimensions=[DimensionIn(name=n, value_meters=v) for n, v in DIMS.items()],
        master_width_dim=GLASS,
        master_height_dim=None,
        mate_positions=MATES if mates is None else mates,
        shape="round" if round_ else "rect",
        master_radial=1 if round_ else 0,
    )


def _to(diameter_in):
    return [DimensionChange(name=GLASS, value_meters=diameter_in * IN)]


def _run(diameter_in, mates=None):
    """{dim: new value in inches}."""
    return {d: v / IN for d, v, _o, _c in _mate_position_updates(_req(mates), _to(diameter_in))}


def _where(out, w_dim, h_dim, w_off, h_off):
    """Radius and angle of a component after the write, in inches and degrees.

    A mate write moves its component by exactly the change in the mate value (direction +1 here),
    so the new offset is the old one plus that change.
    """
    off_w = w_off + (out[w_dim] - DIMS[w_dim] / IN)
    off_h = h_off + (out[h_dim] - DIMS[h_dim] / IN)
    return math.hypot(off_w, off_h), math.degrees(math.atan2(off_h, off_w))


# ── the rule ──────────────────────────────────────────────────────────────────────────────

def test_the_radius_scales_with_the_diameter():
    """Ø30 → Ø45 is x1.5, so 10.800" becomes 16.200"."""
    r, _ang = _where(_run(45), B2_W, B2_H, 5.875, 9.062)
    assert round(r, 4) == round(B2_R / IN * 1.5, 4)


def test_the_angle_does_not_change():
    """Both offsets scale by the same factor, so the bracket stays lined up with the hanger."""
    want = round(math.degrees(math.atan2(9.062, 5.875)), 6)
    for dia in (36, 45, 60, 26):
        _r, ang = _where(_run(dia), B2_W, B2_H, 5.875, 9.062)
        assert round(ang, 6) == want, dia


def test_every_bracket_moves_and_none_of_them_reaches_the_led():
    out = _run(45)
    assert set(out) == {B2_W, B2_H, B3_W, B3_H, B1_W}
    led_inner = RING / IN + (22.5 - 15.0)     # the channel holds a constant inset from the rim
    for comp, (w_off, h_off, overhang) in AT_30.items():
        w, h = (B2_W, B2_H) if comp == B2 else (B3_W, B3_H)
        r, _ang = _where(out, w, h, w_off, h_off)
        assert r + overhang <= led_inner - 0.25, comp


def test_nothing_ends_up_outside_the_chassis():
    for dia in (36, 45, 60, 90):
        out = _run(dia)
        for comp, (w_off, h_off, overhang) in AT_30.items():
            w, h = (B2_W, B2_H) if comp == B2 else (B3_W, B3_H)
            r, _ang = _where(out, w, h, w_off, h_off)
            assert r + overhang <= dia / 2 - 0.25, (dia, comp)


# ── the two ceilings ──────────────────────────────────────────────────────────────────────

def test_the_led_stops_a_shrink_before_the_rim_does():
    """Ø30 → Ø24. The channel comes in by the full 3", the hardware by only its share, so the
    bracket furthest out hits the LED first — and takes the rest of the group with it, because
    these parts are bolted to each other and clamping them separately is what pulls a stack
    apart."""
    out = _run(24)
    r3, _ang = _where(out, B3_W, B3_H, 4.000, 10.688)
    assert round(r3, 4) == round(RING / IN - 3 - 1.0625 - 0.25, 4)     # the one the LED caught
    r2, _ang = _where(out, B2_W, B2_H, 5.875, 9.062)
    assert r2 < B2_R / IN * 0.8                                        # held back with it
    # …and the group kept its shape: same ratio between them as before.
    assert round(r2 / r3, 4) == round(B2_R / B3_R, 4)


def test_a_big_shrink_caps_everything():
    """The tightest member lands exactly on its ceiling; everyone else is pulled in step and ends
    up inside theirs. Nothing reaches the LED and nothing comes apart."""
    out = _run(20)
    ceiling = RING / IN - 5 - 1.0625 - 0.25
    radii = {}
    for comp, (w_off, h_off, _overhang) in AT_30.items():
        w, h = (B2_W, B2_H) if comp == B2 else (B3_W, B3_H)
        r, _ang = _where(out, w, h, w_off, h_off)
        assert r <= ceiling + 1e-6, comp
        radii[comp] = r
    assert round(max(radii.values()), 4) == round(ceiling, 4)
    assert round(radii[B2] / radii[B3], 4) == round(B2_R / B3_R, 4)


def test_without_a_ring_the_rim_is_the_only_ceiling():
    blind = [m.model_copy(update={"keep_out_meters": 0.0}) for m in MATES]
    out = _run(24, mates=blind)
    r3, _ang = _where(out, B3_W, B3_H, 4.000, 10.688)
    assert round(r3, 4) == round(B3_R / IN * 0.8, 4)      # proportional, nothing in the way


# ── the bug this replaces ─────────────────────────────────────────────────────────────────

def test_the_old_half_delta_put_the_bracket_outside_the_chassis():
    blown_up = math.hypot(5.875 + 15, 9.062 + 15)
    assert round(blown_up, 2) == 31.86
    assert blown_up > 59.500 / 2                          # outside the chassis it bolts to
    r, _ang = _where(_run(60), B2_W, B2_H, 5.875, 9.062)
    assert round(r, 3) == 21.600                          # what it does now
    assert r + 1.0625 < 59.500 / 2


def test_the_old_half_delta_also_swung_the_angle():
    was = math.degrees(math.atan2(9.062, 5.875))
    then = math.degrees(math.atan2(9.062 + 15, 5.875 + 15))
    assert abs(was - then) > 6                            # 57.0 -> 50.7 degrees


# ── things that must not change ───────────────────────────────────────────────────────────

def test_an_app_that_never_measured_the_radius_holds_position():
    blind = [m.model_copy(update={"radius_meters": 0.0}) for m in MATES]
    assert _mate_position_updates(_req(mates=blind), _to(60)) == []


def test_a_diameter_that_does_not_move_writes_nothing():
    assert _mate_position_updates(_req(), _to(30)) == []


def test_a_rectangular_product_still_follows_the_edge():
    out = _mate_position_updates(_req(round_=False), _to(60))
    assert {d: round(v / IN, 3) for d, v, _o, _c in out} == {B2_W: 20.125, B1_W: 20.125,
                                                             B3_W: 19.000}


def test_a_radius_master_scales_from_the_radius_not_the_diameter():
    req = _req()
    # The master dim IS the radius here, so 30 -> 45 is x1.5, not the x1.5-from-15 a diameter
    # master would read out of the same numbers.
    req.master_radial = 2
    out = {d: v / IN for d, v, _o, _c in
           _mate_position_updates(req, [DimensionChange(name=GLASS, value_meters=45 * IN)])}
    off_w = 5.875 + (out[B2_W] - 5.125)
    off_h = 9.062 + (out[B2_H] - 9.750)
    assert round(math.hypot(off_w, off_h), 4) == round(B2_R / IN * 1.5, 4)


# ── seated in the hanger's slots ──────────────────────────────────────────────────────────
#
# Brackets 1 and 2 drop their Edge-Flange1 tabs into slots cut in 1004-HANGER. The slot is part
# of the HANGER, so it travels when the hanger is stretched, and the bracket has to go with it.
# Reported live 2026-09-14: the disc rule put them at 8.812" while the stretched hanger's slots
# had gone out to 13.375".

from models import HangerSelection                                       # noqa: E402

HW = "D2@Base-Flange1 [1004-HANGER-1]"
HH = "D1@Sketch1 [1004-HANGER-1]"


def _seated():
    return [m.model_copy(update={"on_hanger": m.component in (B1, B2)}) for m in MATES]


def _hanger(w_in=29.25, h_in=12.25, **kw):
    return HangerSelection(width_dim=HW, height_dim=HH, target_width_meters=w_in * IN,
                           target_height_meters=h_in * IN, resize_fitted=True, **kw)


def _run_seated(diameter_in, hanger=None, stretch=True, mates=None):
    dims = dict(DIMS, **{HW: 14.25 * IN, HH: 10.0 * IN})
    for m in (mates or []):
        dims.setdefault(m.dim, m.value_meters)
    req = InterpretRequest(
        instruction="x",
        dimensions=[DimensionIn(name=n, value_meters=v) for n, v in dims.items()],
        master_width_dim=GLASS, master_height_dim=None,
        mate_positions=mates if mates is not None else _seated(),
        shape="round", master_radial=1)
    hanger = hanger or _hanger()
    changes = [DimensionChange(name=GLASS, value_meters=diameter_in * IN)]
    if stretch:                      # a STRETCH writes the hanger dims in the same batch
        changes += [DimensionChange(name=HW, value_meters=hanger.target_width_meters),
                    DimensionChange(name=HH, value_meters=hanger.target_height_meters)]
    return {d: v / IN for d, v, _o, _c in _mate_position_updates(req, changes, hanger)}


def test_a_seated_bracket_moves_with_the_hanger_edge_not_the_disc():
    """Hanger 14.250 -> 29.250 moves each end +7.500, so the tab goes with it."""
    out = _run_seated(45)
    assert round(out[B2_W] - 5.125, 4) == 7.5000        # W edge moved +7.500
    assert round(out[B2_H] - 9.750, 4) == 1.1250        # H edge moved +1.125
    assert round(out[B1_W] - 5.125, 4) == 7.5000


def test_the_tab_keeps_its_inset_from_the_hanger_end():
    """1.250" in from the end at Ø30, still 1.250" in after the stretch."""
    out = _run_seated(45)
    assert round(14.25 / 2 - 5.875, 4) == 1.2500                     # as drawn
    assert round(29.25 / 2 - (5.875 + (out[B2_W] - 5.125)), 4) == 1.2500


def test_a_bracket_not_on_the_hanger_takes_the_hanger_factor_on_X():
    """Brackets 3 and 4 carry the CLIP, and the clip has to stay on their `Edge-Flange1`. They
    cannot be moved by a different rule from brackets 1 and 2 and still line up, so X comes from
    the hanger; only Y stays on the disc."""
    out = _run_seated(45)
    k = 13.375 / 5.875                                   # what the seated brackets did to their X
    assert round(4.000 * k, 4) == round(4.000 + (out[B3_W] - 4.000), 4)
    assert round(out[B3_W], 3) == 9.106
    assert round(out[B3_H] - 10.000, 4) == 5.3440        # Y still x1.5, the disc


def test_the_clip_and_its_bracket_move_by_exactly_the_same_amount():
    """`Jog4` on the clip sits on `Edge-Flange1` on the bracket. Both start 4.000" off centre, so
    any rule that moves them differently pulls the joint apart — which is what the clip floor did
    (45/6 = 7.500" for the clip against 6.000" for the bracket, and 36/6 = 6.000" against 4.800")."""
    clip = _m(CLIP_W, 4.000, CLIP, "W", 3.000, 4.000, B3_R)
    mates = _seated() + [clip]
    for dia in (36, 45, 60):
        out = _run_seated(dia, mates=mates)
        assert round(out[CLIP_W], 4) == round(out[B3_W], 4), dia


def test_the_clip_floor_is_a_rectangle_rule_and_stays_off_a_disc():
    clip = _m(CLIP_W, 4.000, CLIP, "W", 3.000, 4.000, B3_R)
    out = _run_seated(45, mates=_seated() + [clip])
    assert out[CLIP_W] != 45 / 6                         # master/6 must not pin it


def test_x_falls_back_to_the_disc_when_no_bracket_is_seated():
    """No hanger to take a factor from — the previous behaviour, unchanged."""
    out = _run_seated(45, hanger=_hanger(14.25, 10.0), stretch=False)
    assert round(out[B3_W] - 4.000, 4) == 2.0000         # 4.000 x1.5 = 6.000


def test_a_catalogue_swap_moves_them_too_even_with_no_dim_write():
    """`replace` changes the hanger's size by swapping the file, so nothing is written."""
    hanger = HangerSelection(width_dim=HW, height_dim=HH, target_width_meters=20 * IN,
                             target_height_meters=15 * IN, replace=True, part="1038")
    out = _run_seated(45, hanger=hanger, stretch=False)
    assert round(out[B2_W] - 5.125, 4) == round((20 - 14.25) / 2, 4)
    assert round(out[B2_H] - 9.750, 4) == round((15 - 10.0) / 2, 4)


def test_a_hanger_that_does_not_change_size_leaves_them_alone():
    hanger = HangerSelection(width_dim=HW, height_dim=HH, target_width_meters=14.25 * IN,
                             target_height_meters=10.0 * IN, keep_fitted=True)
    out = _run_seated(45, hanger=hanger, stretch=False)
    assert B2_W not in out and B2_H not in out and B1_W not in out
    assert B3_W in out and B3_H in out                  # the disc parts still move


def test_no_hanger_block_at_all_leaves_the_seated_parts_held():
    req = InterpretRequest(
        instruction="x",
        dimensions=[DimensionIn(name=n, value_meters=v) for n, v in DIMS.items()],
        master_width_dim=GLASS, master_height_dim=None,
        mate_positions=_seated(), shape="round", master_radial=1)
    out = {d for d, _v, _o, _c in _mate_position_updates(req, _to(45))}
    assert out == {B3_W, B3_H}
