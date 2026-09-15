"""ROUND mirrors (the ECLIPSE family).

Every number here is measured off the real ECLIPSE-30.00-LED assembly (live 2026-09-11), so a
failure means the code no longer reproduces a product the client ships:

    1026-MIRROR-ECLIPSE   D1@Sketch1  762.00 mm  (30.000")  the glass DIAMETER, ratio 1.00
    12418-CHASSIS-CIRCLINE D1@Sketch1 749.30 mm  (29.500")  chassis diameter, glass - 0.5"
    LED FLEX EXTRUSION    D1@Sketch1  762.00 mm  (30.000")  LED ring, same as the glass
    LED FLEX EXTRUSION    D2@Sketch1   49.30 mm  ( 1.941")  radial band width, ratio 2.00
    12419-RING            D1@Sketch1   50.80 mm  ( 2.000")  feature radius, ratio 2.00

The shape verdict itself is measured by the APP (one dim growing TWO bounding extents equally),
not by this side -- what is tested here is that the engine does the right thing when told.
"""

import math

import pytest

import interpret
import resize_policy as policy
import rules_store
from hanger_select import select_hanger
from models import InterpretRequest
from prompts import rules_dependent_prompt
from rules import ModelRules, RulePairEntry

IN = 0.0254
GLASS = 0.762          # 30.000" diameter
CHASSIS = 0.74930      # 29.500"
LED_RING = 0.762
LED_BAND = 0.04930     # 1.941", radial
RING_R = 0.05080       # 2.000", radial

M_GLASS = "D1@Sketch1 [1026-MIRROR-ECLIPSE-1]"
M_CHASSIS = "D1@Sketch1 [12418-CHASSIS-CIRCLINE-2]"
M_LED = "D1@Sketch1 [LED FLEX EXTRUSION-ECLIPSE  30-1]"
M_BAND = "D2@Sketch1 [LED FLEX EXTRUSION-ECLIPSE  30-1]"
M_RING = "D1@Sketch1 [12419-RING-1]"


# ── family keying ─────────────────────────────────────────────────────────────

def test_lone_diameter_token_is_stripped_from_the_family():
    """A round product names itself with ONE size, so the WxH pattern never matched it and
    every diameter became its own family -- rules regenerated per size, lost on a variant save."""
    assert rules_store.family_from_stem("ECLIPSE-30.00-LED") == "ECLIPSE-LED"
    assert rules_store.family_from_stem("ECLIPSE-36.00-LED") == "ECLIPSE-LED"
    assert rules_store.family_from_stem("ECLIPSE-30.00") == "ECLIPSE"


def test_rectangular_families_are_untouched():
    assert rules_store.family_from_stem("KELLY-24.00X48.00-LED") == "KELLY-LED"
    assert rules_store.family_from_stem("ISABELL-44.00X56.00-LED-HO") == "ISABELL-LED-HO"
    assert rules_store.family_from_stem("CLARA-36.00X36.00") == "CLARA"


def test_a_bare_integer_is_not_treated_as_a_size():
    """The lone-token rule demands a decimal point on purpose: an integer is far too easy to
    hit inside a product name, and every round stem the client writes carries two decimals."""
    assert rules_store.family_from_stem("ECLIPSE-2-LED") == "ECLIPSE-2-LED"


# ── radial arithmetic ─────────────────────────────────────────────────────────

def test_diameter_dim_takes_the_whole_delta_and_a_radius_takes_half():
    assert policy.radial_delta(0.254, policy.RADIAL_DIAMETER) == pytest.approx(0.254)
    assert policy.radial_delta(0.254, policy.RADIAL_RADIUS) == pytest.approx(0.127)
    assert policy.radial_delta(0.254, 0) == pytest.approx(0.254)


def test_chassis_holds_its_half_inch_inset_at_the_new_diameter():
    """30" -> 40" glass. Constant offset, exactly as on a rectangular product."""
    new = interpret._dependent_value(CHASSIS, GLASS, 40 * IN, policy.RADIAL_DIAMETER)
    assert new / IN == pytest.approx(39.5)


def test_led_ring_follows_the_glass_one_for_one():
    new = interpret._dependent_value(LED_RING, GLASS, 40 * IN, policy.RADIAL_DIAMETER)
    assert new / IN == pytest.approx(40.0)


@pytest.mark.parametrize("value", [LED_BAND, RING_R])
def test_small_radial_features_are_left_alone(value):
    """Doubling a radius for the frame-spanning test must not let the small stuff through:
    1.941" and 2.000" are fixed profiles at any diameter."""
    assert interpret._dependent_value(value, GLASS, 40 * IN, policy.RADIAL_RADIUS) is None


def test_a_frame_spanning_radius_is_not_mistaken_for_a_fixed_profile():
    """A radius dim that really does drive the outline measures ~half the master diameter, i.e.
    exactly MIN_DEPENDENT_FRACTION -- the raw comparison made that a coin flip."""
    new = interpret._dependent_value(GLASS / 2, GLASS, 40 * IN, policy.RADIAL_RADIUS)
    assert new is not None
    assert new / IN == pytest.approx(20.0)          # half of 40", still the radius


def test_expand_master_writes_the_whole_round_assembly():
    current = {M_GLASS: GLASS, M_CHASSIS: CHASSIS, M_LED: LED_RING,
               M_BAND: LED_BAND, M_RING: RING_R}
    radial = {M_GLASS: policy.RADIAL_DIAMETER, M_CHASSIS: policy.RADIAL_DIAMETER,
              M_LED: policy.RADIAL_DIAMETER, M_BAND: policy.RADIAL_RADIUS,
              M_RING: policy.RADIAL_RADIUS}
    out = interpret._expand_master(
        M_GLASS, 40 * IN, [M_CHASSIS, M_LED, M_BAND, M_RING], current, radial)
    got = {c.name: c.value_meters / IN for c in out}
    assert got[M_GLASS] == pytest.approx(40.0)
    assert got[M_CHASSIS] == pytest.approx(39.5)
    assert got[M_LED] == pytest.approx(40.0)
    assert M_BAND not in got and M_RING not in got


# ── one size axis ─────────────────────────────────────────────────────────────

def _round_req(**kw):
    return InterpretRequest(instruction="make it 40 inches", shape="round",
                            master_width_dim=M_GLASS, master_height_dim=None, **kw)


def test_round_request_refuses_a_second_axis():
    """The prompt already says a circle has one size; if the model produces a second number
    anyway, applying it would write a diameter onto whatever sits on [H] -- i.e. hardware."""
    changes, note = interpret._second_axis_changes(
        {"other_axis_meters": 30 * IN}, "overall", M_GLASS, None, _round_req(), {})
    assert changes == []
    assert "round mirror" in note.lower()


def test_rectangular_request_still_applies_the_second_axis():
    """The round guard must not swallow a genuine "40 x 30" on a rectangular product."""
    m_h = "D2@Sketch1 [1046-MIRROR-KAREN-1]"
    model_rules = ModelRules(model="KELLY-LED", limits=None,
                             height=[RulePairEntry(if_changes=m_h, also_change=[])])
    changes, note = interpret._second_axis_changes(
        {"other_axis_meters": 30 * IN}, "overall", M_GLASS, model_rules,
        InterpretRequest(instruction="40 x 30", master_width_dim=M_GLASS,
                         master_height_dim=m_h),
        {M_GLASS: GLASS, m_h: 36 * IN})
    assert [c.value_meters / IN for c in changes] == pytest.approx([30.0])
    assert "round" not in note.lower()


def test_round_prompt_asks_for_a_diameter_and_refuses_a_pair():
    p = rules_dependent_prompt("{}", is_round=True, master_width_dim=M_GLASS)
    assert "ROUND" in p and "DIAMETER" in p
    assert "master diameter" in p
    assert "BOTH AXES IN ONE REQUEST" not in p
    assert "other_axis_meters" in p          # only to forbid it
    assert "NEVER set \"other_axis_meters\"" in p


def test_rectangular_prompt_is_unchanged():
    p = rules_dependent_prompt("{}", master_width_dim="w", master_height_dim="h")
    assert "BOTH AXES IN ONE REQUEST" in p
    assert "THIS MIRROR IS ROUND" not in p


# ── hanger geometry ───────────────────────────────────────────────────────────

def test_round_glass_area_is_pi_d_squared_over_four():
    """d^2 overstates a disc by 27%, which understates every prefab's share by the same margin
    and lifts the 25% ceiling high enough to pick a hanger the client would not use."""
    rect = select_hanger(30, 30, chassis_w_in=29.5)
    disc = select_hanger(30, 30, chassis_w_in=29.5, round_glass=True)
    ratio = disc.candidates[0].fraction / rect.candidates[0].fraction
    assert ratio == pytest.approx(4 / math.pi, rel=1e-6)


def test_a_hanger_must_fit_inside_the_disc_by_its_diagonal():
    """#1215 is 40x12: 40" alone would pass a 41" disc on width, but its 41.76" diagonal does
    not fit -- near the top of a circle the chord is far shorter than the diameter."""
    disc = select_hanger(41, 41, chassis_w_in=41, round_glass=True)
    c1215 = next(c for c in disc.candidates if c.part == "1215")
    assert math.hypot(40, 12) > 41
    assert not c1215.fits or c1215.over_chassis

    rect = select_hanger(41, 41, chassis_w_in=41)
    r1215 = next(c for c in rect.candidates if c.part == "1215")
    assert r1215.fits and not r1215.over_chassis


def test_a_hanger_well_inside_the_disc_still_fits():
    disc = select_hanger(30, 30, chassis_w_in=29.5, round_glass=True)
    c1004 = next(c for c in disc.candidates if c.part == "1004")   # 14.25x10 -> 17.4" diagonal
    assert c1004.fits and not c1004.over_chassis


def test_round_glass_gets_a_hanger_at_all():
    """A circle has no separate height, so glass_h reads 0 from the (absent) height master and
    the whole hanger step used to bail out with "no master glass width/height available"."""
    req = _round_req(dimensions=[{"name": M_GLASS, "value_meters": GLASS}])
    changes, sel = interpret._hanger_changes(req, [], None)
    assert sel is not None or changes == []      # no hanger dims in this stub; must not crash


# ── the name collision that blocked ECLIPSE outright ──────────────────────────

def test_eclipse_is_not_a_clip():
    """"ECLIPSE" contains "CLIP" — E-CLIP-SE. A substring match classified every part of the
    round product as fixed-size clip hardware, mirror glass included, and fixed-size beats
    is_mirror_glass — so the glass could not be a rule master and ECLIPSE could not be resized
    by any path, with nothing in the output to say why."""
    assert policy.fixed_size_reason(M_GLASS) is None
    assert policy.is_mirror_glass(M_GLASS, None) is True
    assert policy.block_reason(M_GLASS, "width", None, GLASS) is None


def test_real_hardware_is_still_blocked():
    """The word-boundary fix must not let any genuine fixed-size part through."""
    for name in ("D1@Sketch1 [1005-CLIP-2]",
                 "D2@Sketch1 [2004-HANGING-BRACKET-CLIP-1]",
                 "D1@WIDTH [LPM-24060A-1]",
                 "D2@Base-Flange1 [2867-HANGING-BRACKET-1]",
                 "D1@Sketch1 [12296-LED-BRACKET-2]"):
        assert policy.fixed_size_reason(name) is not None, name


def test_a_pattern_glued_to_digits_still_matches():
    """Only a LETTER neighbour disqualifies a match, so an id written without a separator is
    still classified — it is the pattern buried inside a longer WORD that must be refused."""
    assert policy.fixed_size_reason("D1@Sketch1 [1005-CLIP2]") is not None


def test_the_chassis_and_mirror_still_resize():
    assert policy.fixed_size_reason(M_CHASSIS) is None
    assert policy.fixed_size_reason("D1@Sketch1 [1046-MIRROR-KAREN-1]") is None
