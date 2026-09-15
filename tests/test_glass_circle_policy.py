"""A part drawn at the GLASS'S OWN diameter is the frame, whatever its name says.

HALO, reported live 2026-09-14. `12645-LED BRACKET ASSY<1>` and `<2>` stayed at their drawn size
through every resize. Both instances share `12646-LED BRACKET-BOTTOM`, an ARC of the disc whose
`D1@Sketch1` is 762.00 mm — the glass diameter to the hundredth.

The label fix got it into the rule's `also_change` correctly. It was then dropped on the way out,
by NAME:

    [POLICY] DROP D1@Sketch1 [12646-LED BRACKET-BOTTOM-1] (1143.00 mm)
             - LED bracket is fixed-size hardware - repositioned by its mates, never resized

`resize_policy` classifies by name, and "LED BRACKET" reads as hardware. It is not hardware here —
it is the frame, and two measured facts say so: the nudge proved the dim drives a CIRCLE, and its
value IS the master's. A hole or a fillet on the same part passes neither test.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from interpret import _enforce_policy, _is_the_glass_circle
from models import DimensionChange, DimensionIn, InterpretRequest

IN = 0.0254
GLASS = "D1@Sketch1 [HALO-30-MIRROR-1]"
BRACKET = "D1@Sketch1 [12646-LED BRACKET-BOTTOM-1]"      # 762.00 mm — the same circle
BRACKET_LIP = "D2@Sketch1 [12646-LED BRACKET-BOTTOM-1]"  # 31.75 mm — genuinely hardware
RING = "D1@Sketch1 [12435-CHASSIS RING-1]"               # 727.07 mm — 4.6% off, NOT the same
CLIP = "D1@Sketch1 [1005-CLIP-1]"                        # a real fixed-size part

CURRENT = {GLASS: 762.00 / 1000, BRACKET: 762.00 / 1000, BRACKET_LIP: 31.75 / 1000,
           RING: 727.07 / 1000, CLIP: 76.20 / 1000}
LABELS = {GLASS: "W", BRACKET: "W", RING: "W", CLIP: "W"}
COMPONENT_LABELS = {"12646-LED BRACKET-BOTTOM-1": "LED Bracket Bottom",
                    "12435-CHASSIS RING-1": "Chassis Ring",
                    "1005-CLIP-1": "Mirror Clip", "HALO-30-MIRROR-1": "Mirror Glass"}


def _req(round_=True, radial=None):
    return InterpretRequest(
        instruction="Resize to 45",
        dimensions=[DimensionIn(name=n, value_meters=v) for n, v in CURRENT.items()],
        dim_axis_labels=LABELS, master_width_dim=GLASS, master_height_dim=None,
        shape="round" if round_ else "rect", master_radial=1 if round_ else 0,
        radial_dims={GLASS: 1, BRACKET: 1, RING: 2} if radial is None else radial)


def _keep(changes, req):
    kept, _notes = _enforce_policy(
        [DimensionChange(name=n, value_meters=v) for n, v in changes],
        LABELS, COMPONENT_LABELS, "width", CURRENT, None, req)
    return {c.name for c in kept}


# ── the reported bug ──────────────────────────────────────────────────────────────────────

def test_the_led_bracket_survives_the_policy():
    assert BRACKET in _keep([(BRACKET, 1.143)], _req())


def test_it_used_to_be_dropped():
    """Without `req` the exemption cannot be evaluated — this is the old behaviour, and it is
    still what an older caller gets."""
    kept, notes = _enforce_policy([DimensionChange(name=BRACKET, value_meters=1.143)],
                                  LABELS, COMPONENT_LABELS, "width", CURRENT, None)
    assert kept == []
    assert notes and "fixed-size" in notes[0]


# ── what must still be dropped ────────────────────────────────────────────────────────────

def test_a_real_clip_is_still_dropped():
    assert CLIP not in _keep([(CLIP, 0.2)], _req())


def test_another_dim_on_the_same_bracket_is_still_dropped():
    """The exemption is per DIMENSION, not per part: the bracket's 31.75 mm lip is hardware."""
    assert BRACKET_LIP not in _keep([(BRACKET_LIP, 0.05)], _req())


def test_a_circle_that_is_not_the_glass_is_not_exempt():
    """The chassis ring drives a circle too, but at 727.07 mm it is 4.6% off the glass — a
    different circle, so it stands or falls on the policy alone."""
    assert not _is_the_glass_circle(RING, _req(), CURRENT)


def test_a_dim_that_does_not_drive_a_circle_is_not_exempt():
    """Same value, but the nudge never proved it round. Both facts are required."""
    assert not _is_the_glass_circle(BRACKET, _req(radial={GLASS: 1}), CURRENT)


def test_a_rectangular_product_is_untouched():
    assert not _is_the_glass_circle(BRACKET, _req(round_=False), CURRENT)
    assert BRACKET not in _keep([(BRACKET, 1.143)], _req(round_=False))


def test_the_tolerance_window_is_where_it_says_it_is():
    from interpret import SAME_CIRCLE_FRACTION
    edge = 762.00 * (1 + SAME_CIRCLE_FRACTION) / 1000
    near = dict(CURRENT, **{BRACKET: edge - 1e-9})
    far = dict(CURRENT, **{BRACKET: edge * 1.01})
    assert _is_the_glass_circle(BRACKET, _req(), near)
    assert not _is_the_glass_circle(BRACKET, _req(), far)
