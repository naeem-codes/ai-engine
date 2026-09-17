"""A CURVED mirror is narrower at its ends, and every fit check has to ask where it is looking.

Pinned to the shape verdicts the app read off the models on 2026-09-17:

    CAPSULE glass    40.000 x 20.000   area 714.2 sq in   fill 0.893  -> OBROUND
    MICHELLE glass   42.000 x 30.000   area 981.5 sq in   fill 0.779  -> ELLIPSE
    MICHELLE chassis 41.500 x 29.500   area 956.2 sq in   fill 0.781  -> ELLIPSE

A textbook obround at 20 x 40 is pi*10^2 + 20*20 = 714.16 sq in, so CAPSULE's outline is a
stadium to within 0.006%. The client draws BOTH curves; one "oval" rule would be wrong for one
of them.

⚠️ **The ceiling in here does NOT explain the CAPSULE failures, and these tests say so.** The
slot row was assumed to sit about 15" up; measured, it sits at 10.000", where a 28.118" obround
is still 26.234" across. The ceiling duly computed a 25.516" hanger cap, the 20" #1038 sailed
through, `D3@Sketch6` was written to 15.750" and `Cut-Extrude2` failed exactly as before (run
16:34:23). Whatever stops that dim at this size, it is not the outline running out of width.

So the real fix lives in the APP, not here: `SolidWorksService.SalvageCulpritByBisect` asks the
model how far the dim can actually go, and `ClearMoveInterferences` asks it which parts really
overlap. What remains below is a genuine constraint (a slot cannot run off the part it is cut
into) that happens not to be the binding one on CAPSULE — kept, tightly gated, and tested for
what it actually does rather than what it was hoped to do.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import outline
from interpret import _mate_position_updates, _row_headroom, _tab_dim_in_row, _tab_row
from models import DimensionChange, DimensionIn, InterpretRequest, MatePositionIn, SlotRowIn

IN = 0.0254

GLASS_W = "D1@Sketch1 [MIRROR-CAPSULE-20.00X40.00--1]"
GLASS_H = "D2@Sketch1 [MIRROR-CAPSULE-20.00X40.00--1]"
SLOT = "D2@Sketch6 [12375-CHASSIS-1]"
TABS = "D3@Sketch6 [12375-CHASSIS-1]"
CLIP_W = "D1@Distance3 [12374-CHASSIS ASSY-1]"
CLIP_H = "D1@Distance2 [12374-CHASSIS ASSY-1]"
CLIP = "2004-HANGING-BRACKET-CLIP-1"

# Every one of these is measured, off the 20x40 as the app reported it on 2026-09-17:
#   [slot row] D2@Sketch6 ... 2 x 4.000 in on a 18.118 x 37.998 in part,
#              row sits 10.000 in off centre, outer end 6.140 in
SHELL_W, SHELL_H = 18.118, 37.998
ROW_Y = 10.000
OUTERMOST = 6.140
HANGER_W = 14.25              # 1119-HANGER as fitted


# ── the outline itself ───────────────────────────────────────────────────────────────────────

def test_rectangle_is_full_width_everywhere():
    """The property the whole change rests on: 60-odd rectangular products must not move."""
    for y in (0.0, 0.3, 0.49, 0.6):
        assert outline.half_width_at(y, 0.5, 0.5, "rect") == 0.5


def test_unknown_shape_falls_back_to_the_rectangle():
    """An older app sends no shape, and a future one may send a word this build never heard."""
    for shape in (None, "", "squircle", "ROUND-ISH"):
        assert outline.half_width_at(0.4, 0.5, 0.5, shape) == 0.5


def test_obround_is_flat_over_its_straight_section_then_curves():
    half_w, half_h = 10.0, 20.0              # CAPSULE 20 x 40
    assert outline.half_width_at(0.0, half_w, half_h, "obround") == pytest.approx(10.0)
    assert outline.half_width_at(9.9, half_w, half_h, "obround") == pytest.approx(10.0)
    # Cap centre sits at 20 - 10 = 10; 5" above it the chord is sqrt(100 - 25).
    assert outline.half_width_at(15.0, half_w, half_h, "obround") == pytest.approx(8.6603, abs=1e-4)
    assert outline.half_width_at(20.0, half_w, half_h, "obround") == 0.0


def test_landscape_obround_is_capped_on_its_SIDES_not_its_ends():
    """Which ends are round follows from which side is shorter, and is never assumed."""
    half_w, half_h = 20.0, 10.0
    assert outline.half_width_at(0.0, half_w, half_h, "obround") == pytest.approx(20.0)
    # The very top of a landscape obround is the FLAT edge between its two side caps, so it is
    # `half_w - r` wide, not zero. The boundary is the one place this is easy to get wrong.
    assert outline.half_width_at(10.0, half_w, half_h, "obround") == pytest.approx(10.0)


def test_ellipse_and_circle_share_one_branch():
    assert outline.half_width_at(0.0, 15.0, 21.0, "ellipse") == pytest.approx(15.0)
    assert outline.half_width_at(21.0, 15.0, 21.0, "ellipse") == 0.0
    # A circle IS an ellipse with equal axes, and the app sends the diameter on both extents.
    assert (outline.half_width_at(6.0, 10.0, 10.0, "round")
            == pytest.approx(outline.half_width_at(6.0, 10.0, 10.0, "ellipse")))


def test_the_measured_areas_identify_the_two_products():
    """The discriminator the app actually uses, run against what it measured."""
    import math
    w, h = 20.0, 40.0
    obround = math.pi * (w / 2) ** 2 + w * (h - w)
    assert obround == pytest.approx(714.16, abs=0.01)          # app read 714.2 on CAPSULE
    assert obround / (w * h) == pytest.approx(0.893, abs=0.001)
    ellipse = math.pi * 15.0 * 21.0
    assert ellipse / (30.0 * 42.0) == pytest.approx(0.785, abs=0.001)   # app read 0.779


# ── the row ceiling: what it does, and what it does not ──────────────────────────────────────

def _row(**kw):
    base = dict(dim=SLOT, length_meters=4.000 * IN, count=2, slot_width_meters=0.280 * IN,
                inset_meters=2.919 * IN, part_width_meters=SHELL_W * IN,
                component="12375-CHASSIS-1", part_height_meters=SHELL_H * IN,
                row_y_meters=ROW_Y * IN, outermost_meters=OUTERMOST * IN)
    base.update(kw)
    return SlotRowIn(**base)


def _req(shape="obround", **kw):
    base = dict(
        instruction="", shape=shape,
        outline_w_meters=SHELL_W * IN, outline_h_meters=SHELL_H * IN,
        master_width_dim=GLASS_W, master_height_dim=GLASS_H,
        dimensions=[DimensionIn(name=GLASS_W, value_meters=20 * IN),
                    DimensionIn(name=GLASS_H, value_meters=40 * IN),
                    DimensionIn(name=SLOT, value_meters=4.000 * IN),
                    DimensionIn(name=TABS, value_meters=10.000 * IN)],
        slot_rows=[_row()],
        dim_axis_labels={GLASS_W: "W", GLASS_H: "H", SLOT: "W", TABS: "W"},
    )
    base.update(kw)
    return InterpretRequest(**base)


def _changes(width_to=30.0, height_to=40.0, slot_to=6.188):
    return [DimensionChange(name=GLASS_W, value_meters=width_to * IN),
            DimensionChange(name=GLASS_H, value_meters=height_to * IN),
            DimensionChange(name=SLOT, value_meters=slot_to * IN)]


def test_the_ceiling_does_NOT_explain_the_capsule_failure():
    """The honest record of what the live run showed, so nobody re-derives the wrong theory.

    At the row's MEASURED height the obround still has room, the ceiling allows a 25.5" hanger,
    the 20" #1038 passes, and `D3@Sketch6` is written to the 15.750" that kills the rebuild. The
    app's bisect salvage is what handles this, not the outline.
    """
    growth, _why = _row_headroom(_req(), _changes())
    assert 10.000 + growth / IN > 15.750                 # the value that broke it is NOT blocked
    assert (HANGER_W + growth / IN) == pytest.approx(25.516, abs=0.05)


def test_the_ceiling_still_bites_on_a_row_high_in_the_curve():
    """It is a real constraint even though it is not the binding one on CAPSULE.

    Put the same row where the curve has actually closed in and the answer changes, which is the
    behaviour worth keeping: a slot cannot be driven off the part it is cut into.
    """
    high = _req(slot_rows=[_row(row_y_meters=17.0 * IN)])
    growth, why = _row_headroom(high, _changes())
    assert 10.000 + growth / IN < 15.750
    assert "obround" in why


def test_the_ceiling_only_ever_reports_room_that_exists():
    """Sanity: more curve above the row means less room, never more."""
    low = _row_headroom(_req(slot_rows=[_row(row_y_meters=2.0 * IN)]), _changes())[0]
    high = _row_headroom(_req(slot_rows=[_row(row_y_meters=17.0 * IN)]), _changes())[0]
    assert high < low


def test_a_rectangle_gets_no_ceiling_at_all():
    assert _row_headroom(_req(shape="rect"), _changes()) is None


def test_a_round_product_keeps_its_own_radial_path():
    assert _row_headroom(_req(shape="round"), _changes()) is None


def test_no_ceiling_without_a_measured_shell():
    assert _row_headroom(_req(outline_w_meters=0, outline_h_meters=0), _changes()) is None


def test_a_row_whose_sketch_is_not_centred_is_refused():
    """A row that claims to sit outside its own part has an origin somewhere else."""
    req = _req(slot_rows=[_row(row_y_meters=30.0 * IN)])
    assert _tab_row(req) is None
    assert _row_headroom(req, _changes()) is None


def test_the_tab_dim_is_only_accepted_from_the_row_it_drives():
    req = _req()
    assert _tab_dim_in_row(req, _tab_row(req), HANGER_W * IN, None) == TABS
    # Same value, different sketch: it cannot be moving these contours.
    other = _req(dimensions=list(_req().dimensions)
                 + [DimensionIn(name="D5@Sketch99 [12375-CHASSIS-1]", value_meters=10.0 * IN)],
                 slot_rows=[_row(dim="D2@Sketch99 [12375-CHASSIS-1]")])
    assert _tab_dim_in_row(other, _tab_row(other), HANGER_W * IN, None) != TABS


# ── the radial hold-back, which is switched off ──────────────────────────────────────────────

def _clip_req(shape="obround"):
    return InterpretRequest(
        instruction="", shape=shape,
        outline_w_meters=SHELL_W * IN, outline_h_meters=SHELL_H * IN,
        master_width_dim=GLASS_W, master_height_dim=GLASS_H,
        dimensions=[DimensionIn(name=GLASS_W, value_meters=20 * IN),
                    DimensionIn(name=GLASS_H, value_meters=40 * IN),
                    DimensionIn(name=CLIP_W, value_meters=4.000 * IN),
                    DimensionIn(name=CLIP_H, value_meters=15.500 * IN)],
        mate_positions=[
            MatePositionIn(dim=CLIP_W, value_meters=4.000 * IN, component=CLIP, axis="W",
                           extent_meters=3.000 * IN, direction=1.0, offset_meters=4.000 * IN),
            MatePositionIn(dim=CLIP_H, value_meters=15.500 * IN, component=CLIP, axis="H",
                           extent_meters=3.000 * IN, direction=1.0, offset_meters=15.500 * IN),
        ],
        dim_axis_labels={GLASS_W: "W", GLASS_H: "H"},
    )


def _grown():
    return [DimensionChange(name=GLASS_W, value_meters=40 * IN),
            DimensionChange(name=GLASS_H, value_meters=50 * IN)]


def _by_dim(out):
    return {dim: val / IN for dim, val, _base, _comp in out}


def test_the_hold_back_is_switched_off_and_changes_nothing():
    """It computed 9.256in; live, its 10.246in answer STILL fouled the cap. Not the real limit.

    Disabled rather than tuned: a part moved 0.6" without the collision being fixed is a change
    with no benefit and one more thing to unpick next time. The app now asks SolidWorks for real
    interference volumes instead (`ClearMoveInterferences`), which needs no theory about where
    the outline runs.

    This test exists so nobody switches it back on without first checking its answer against the
    model's — if it is ever re-enabled, this is the test that will say so.
    """
    curved = _by_dim(_mate_position_updates(_clip_req(), _grown(), None))
    flat = _by_dim(_mate_position_updates(_clip_req(shape="rect"), _grown(), None))
    assert curved == flat


def test_the_clip_takes_the_ordinary_rectangular_answer():
    """Half the +20in master change on W, half the +10in on H — unchanged from before all this."""
    out = _by_dim(_mate_position_updates(_clip_req(), _grown(), None))
    assert out[CLIP_W] == pytest.approx(14.000, abs=1e-6)
    assert out[CLIP_H] == pytest.approx(20.500, abs=1e-6)


def test_a_rectangle_is_untouched_by_any_of_this():
    """The regression that matters most: every rectangular product keeps today's answer."""
    out = _by_dim(_mate_position_updates(_clip_req(shape="rect"), _grown(), None))
    assert out[CLIP_W] == pytest.approx(14.000, abs=1e-6)
    assert out[CLIP_H] == pytest.approx(20.500, abs=1e-6)
