"""A round product must say so when one of its own concentric circles is left out of the resize.

ECLIPSE, 2026-09-14. `12419-RING` carries no size dimension at all — it is driven IN CONTEXT off an
edge of the glass — and the chassis's `Sketch46` CONVERTS ~25 of that ring's edges while holding
`D15@Sketch46` (12.980") and `D17@Sketch46` (13.079") as dimensions of the same geometry. At the
drawn Ø30 the two agree. Grow the glass and the converted edges travel with it while the
dimensions stay put, so `Sketch45` and `Sketch46` go over-defined — which SolidWorks reports as
`swSketchErrorExtRefFail` and shows in the UI as "the sketch is overdefined".

The app now labels those two dims, but a rule set SAVED BEFORE that still lists three dependants
and none of them is the channel, so the fix applies and nothing changes. That is exactly what
happened on the run that was meant to prove it: no rules were regenerated at all, and the resize
looked identical. This is the line that says so.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.resize.interpret import _round_circles_left_behind
from engine.core.models import DimensionIn, InterpretRequest

IN = 0.0254
GLASS = "D1@Sketch1 [1026-MIRROR-ECLIPSE-1]"
CHASSIS = "D1@Sketch1 [12418-CHASSIS-CIRCLINE-2]"
D17 = "D17@Sketch46 [12418-CHASSIS-CIRCLINE-2]"      # LED channel, outer wall
D15 = "D15@Sketch46 [12418-CHASSIS-CIRCLINE-2]"      # LED channel, inner wall

DIMS = {GLASS: 30 * IN, CHASSIS: 29.5 * IN, D17: 13.079 * IN, D15: 12.980 * IN}


def _req(round_=True, radial=None):
    return InterpretRequest(
        instruction="Resize to 45.00",
        dimensions=[DimensionIn(name=n, value_meters=v) for n, v in DIMS.items()],
        master_width_dim=GLASS, master_height_dim=None,
        shape="round" if round_ else "rect", master_radial=1 if round_ else 0,
        radial_dims={GLASS: 1, CHASSIS: 1, D17: 2, D15: 2} if radial is None else radial)


def test_a_stale_rule_set_is_called_out():
    """The rules as saved: three dependants, and the channel is not one of them."""
    note = _round_circles_left_behind(_req(), GLASS, [CHASSIS], DIMS)
    assert "2 dimension(s)" in note
    assert D17 in note and D15 in note
    assert "Generate Rules" in note


def test_a_complete_rule_set_says_nothing():
    assert _round_circles_left_behind(_req(), GLASS, [CHASSIS, D17, D15], DIMS) == ""


def test_the_master_itself_never_counts_as_left_behind():
    assert _round_circles_left_behind(_req(), GLASS, [CHASSIS, D17, D15], DIMS) == ""
    only_master = _round_circles_left_behind(
        _req(radial={GLASS: 1}), GLASS, [], DIMS)
    assert only_master == ""


def test_a_rectangular_product_is_never_warned():
    """A rectangle has no concentric circles to leave behind; the check must not fire on one."""
    assert _round_circles_left_behind(_req(round_=False), GLASS, [CHASSIS], DIMS) == ""


def test_a_dim_the_app_did_not_send_is_not_invented():
    """A radial dim that is not in this model's dimension list cannot be reported missing."""
    ghost = dict(DIMS)
    ghost.pop(D15)
    note = _round_circles_left_behind(_req(), GLASS, [CHASSIS], ghost)
    assert D17 in note and D15 not in note
    assert "1 dimension(s)" in note
