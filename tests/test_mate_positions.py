"""Absolute-position mates must follow the edge on a resize.

Pinned to the real AMBER 60x36: the mirror clip is held by `D1@Distance8` = 18.500" from the
assembly centre plane. On a 60" glass that is 11.5" inside the edge; after a shrink to 36" the
untouched mate leaves the clip 0.5" OUTSIDE the glass, hanging off the mirror (seen live
2026-08-06). Shifting by half the master delta lands it at 6.500", within half an inch of the
6.000" the client's own AMBER 36x36 carries.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from interpret import _mate_position_updates
from models import DimensionChange, DimensionIn, InterpretRequest, MatePositionIn

IN = 0.0254
MIRROR_W = "WIDTH@Sketch1 [1011-MIRROR-CAROL-1]"
MIRROR_H = "HEIGHT@Sketch1 [1011-MIRROR-CAROL-1]"
CLIP_MATE = "D1@Distance8"

REAL_DIMS = {MIRROR_W: 60 * IN, MIRROR_H: 36 * IN, CLIP_MATE: 18.5 * IN}
CLIP = MatePositionIn(dim=CLIP_MATE, value_meters=18.5 * IN,
                      component="1005-CLIP-1", axis="W", extent_meters=3 * IN)


def _req(dims=None, mates=None):
    return InterpretRequest(
        instruction="x",
        dimensions=[DimensionIn(name=n, value_meters=v)
                    for n, v in (dims or REAL_DIMS).items()],
        master_width_dim=MIRROR_W,
        master_height_dim=MIRROR_H,
        mate_positions=mates if mates is not None else [CLIP],
    )


def test_clip_follows_the_edge_on_the_real_60_to_36():
    out = _mate_position_updates(_req(), [DimensionChange(name=MIRROR_W, value_meters=36 * IN)])
    assert len(out) == 1
    dim, new_val, old_val, comp = out[0]
    assert dim == CLIP_MATE
    assert comp == "1005-CLIP-1"
    assert old_val / IN == pytest.approx(18.5)
    assert new_val / IN == pytest.approx(6.5)


def test_the_clip_ends_up_inside_the_glass():
    """The whole point: before, the clip centre sat outside the new half-width."""
    out = _mate_position_updates(_req(), [DimensionChange(name=MIRROR_W, value_meters=36 * IN)])
    new_half_width = 18.0
    assert 18.5 > new_half_width, "the untouched mate really was outside"
    assert out[0][1] / IN < new_half_width


def test_distance_from_the_edge_is_preserved():
    out = _mate_position_updates(_req(), [DimensionChange(name=MIRROR_W, value_meters=36 * IN)])
    before = 30.0 - 18.5          # 11.5" in from the edge of a 60" glass
    after = 18.0 - out[0][1] / IN
    assert after == pytest.approx(before)


def test_growing_pushes_the_clip_outward():
    out = _mate_position_updates(_req(), [DimensionChange(name=MIRROR_W, value_meters=72 * IN)])
    assert out[0][1] / IN == pytest.approx(24.5)      # 18.5 + 6


def test_height_change_does_not_move_a_width_mate():
    out = _mate_position_updates(_req(), [DimensionChange(name=MIRROR_H, value_meters=48 * IN)])
    assert out == []


def test_height_mate_follows_a_height_change():
    mate = MatePositionIn(dim="D1@Distance9", value_meters=10 * IN,
                          component="1005-CLIP-1", axis="H")
    dims = dict(REAL_DIMS)
    dims["D1@Distance9"] = 10 * IN
    out = _mate_position_updates(_req(dims, [mate]),
                                 [DimensionChange(name=MIRROR_H, value_meters=48 * IN)])
    assert out[0][1] / IN == pytest.approx(16.0)      # 10 + (48-36)/2


def test_no_mates_reported_is_a_no_op():
    out = _mate_position_updates(_req(mates=[]),
                                 [DimensionChange(name=MIRROR_W, value_meters=36 * IN)])
    assert out == []


def test_no_master_change_is_a_no_op():
    # Some unrelated dim moved; the glass did not.
    out = _mate_position_updates(_req(), [DimensionChange(name="D1@Sketch1 [X-1]",
                                                          value_meters=5 * IN)])
    assert out == []


def test_a_mate_already_being_written_is_never_shifted_twice():
    changes = [DimensionChange(name=MIRROR_W, value_meters=36 * IN),
               DimensionChange(name=CLIP_MATE, value_meters=7 * IN)]
    assert _mate_position_updates(_req(), changes) == []


def test_duplicate_mate_entries_yield_one_change():
    out = _mate_position_updates(_req(mates=[CLIP, CLIP]),
                                 [DimensionChange(name=MIRROR_W, value_meters=36 * IN)])
    assert len(out) == 1


def test_a_shrink_that_would_drive_the_mate_negative_is_skipped():
    # 60 -> 4 moves each edge 28"; the 18.5" mate cannot absorb that. The floor keeps it
    # positive, so what matters is that the clips still cannot end up on top of each other.
    out = _mate_position_updates(_req(), [DimensionChange(name=MIRROR_W, value_meters=4 * IN)])
    if out:
        assert out[0][1] >= 1.5 * IN + 0.25 * IN


# ── the narrow-mirror floor (60x36 -> 24x36 put the two clips ON TOP of each other) ──────

def test_24_inch_lands_on_the_clients_own_carol_value():
    """Holding the 11.5" edge offset gives 0.5", where the mirrored clips OVERLAP. The client's
    real CAROL 24x36 puts the clip centre at 4.000" — exactly 24/6."""
    out = _mate_position_updates(_req(), [DimensionChange(name=MIRROR_W, value_meters=24 * IN)])
    assert out[0][1] / IN == pytest.approx(4.0)


def test_the_two_clips_can_never_overlap():
    """A 3" clip at ±c intersects its mirror image whenever c < 1.5"."""
    for target in (30, 24, 20, 16, 12):
        out = _mate_position_updates(
            _req(), [DimensionChange(name=MIRROR_W, value_meters=target * IN)])
        assert out, f"no change emitted at {target}in"
        centre = out[0][1]
        assert centre >= 1.5 * IN, f"clips overlap at {target}in (centre {centre / IN:.3f}in)"


def test_the_floor_does_not_disturb_the_widths_that_already_worked():
    # 36" was verified live at 6.500"; the 36/6 = 6.000" floor must not override it.
    out = _mate_position_updates(_req(), [DimensionChange(name=MIRROR_W, value_meters=36 * IN)])
    assert out[0][1] / IN == pytest.approx(6.5)


def test_the_floor_never_binds_when_growing():
    out = _mate_position_updates(_req(), [DimensionChange(name=MIRROR_W, value_meters=72 * IN)])
    assert out[0][1] / IN == pytest.approx(24.5)      # not 72/6 = 12


def test_clip_stays_inside_the_glass_at_every_width():
    for target in (48, 36, 30, 24, 20):
        out = _mate_position_updates(
            _req(), [DimensionChange(name=MIRROR_W, value_meters=target * IN)])
        outer_edge = out[0][1] / IN + 1.5      # centre + half the 3" clip
        assert outer_edge <= target / 2, f"clip hangs off the {target}in glass"


def test_the_master_component_is_never_moved_by_its_own_resize():
    """The real AMBER pins the glass itself to an assembly plane (Distance7). Shifting that would
    slide the master and desynchronise everything measured from it."""
    mate = MatePositionIn(dim="D1@Distance7", value_meters=6 * IN,
                          component="1011-MIRROR-CAROL-1", axis="W")
    dims = dict(REAL_DIMS)
    dims["D1@Distance7"] = 6 * IN
    out = _mate_position_updates(_req(dims, [mate]),
                                 [DimensionChange(name=MIRROR_W, value_meters=36 * IN)])
    assert out == []


def test_a_clip_lines_up_with_the_hanging_tabs_when_they_move():
    """Requested 2026-08-06. The client's own products already do this to within ~1-2":
    tabs vs clips are +/-17.87 / +/-18.50 on the 60", +/-5.00 / +/-4.00 on CAROL."""
    class _Hanger:
        follower_dims = ["D1@Sketch81 [12211-CHASSIS-2]"]

    changes = [DimensionChange(name=MIRROR_W, value_meters=90 * IN),
               DimensionChange(name="D1@Sketch81 [12211-CHASSIS-2]", value_meters=54.25 * IN)]
    out = _mate_position_updates(_req(), changes, _Hanger())
    assert out[0][1] / IN == pytest.approx(54.25 / 2)      # 27.125, half the tab spacing


def test_clip_alignment_is_skipped_when_the_tabs_did_not_move():
    class _Hanger:
        follower_dims = []

    out = _mate_position_updates(_req(), [DimensionChange(name=MIRROR_W, value_meters=36 * IN)],
                                 _Hanger())
    assert out[0][1] / IN == pytest.approx(6.5)            # the plain edge-offset result


def test_depth_axis_mates_are_never_touched():
    mate = MatePositionIn(dim="D1@Distance5", value_meters=2 * IN,
                          component="1005-CLIP-1", axis="D")
    dims = dict(REAL_DIMS)
    dims["D1@Distance5"] = 2 * IN
    out = _mate_position_updates(_req(dims, [mate]),
                                 [DimensionChange(name=MIRROR_W, value_meters=36 * IN)])
    assert out == []
