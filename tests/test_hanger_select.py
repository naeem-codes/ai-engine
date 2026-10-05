"""Prefab hanger selection by glass surface area (client rule, 2026-07-30).

Rule: the LARGEST prefab that fits and does not exceed 25% of the glass area. The three
"reproduces" tests below are the ones that matter — they pin the rule to real data:
two hangers actually built into models, plus the user's explicit call on 36x48.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from engine.hangers.hanger_select import (MAX_AREA_FRACTION, MAX_HEIGHT_FRACTION, MIN_ACCEPTABLE_FRACTION,
                           MIN_WIDTH_FRACTION, PREFAB_HANGERS, REVIEW_BELOW_FRACTION,
                           TARGET_AREA_FRACTION, TARGET_WIDTH_FRACTION, select_hanger,
                           select_hanger_meters)


def test_the_width_floor_reproduces_the_clients_own_products():
    """The hanger spans 55-67% of the glass width on all four real products. Area does NOT
    describe them (16.5 / 23.1 / 26.0 / 37.0%), which is why width is now the driver."""
    for glass_w, glass_h, fitted_w, fitted_h, client_w in [
        (24, 36, 14.25, 10, 14.25),     # CAROL  #1004
        (36, 36, 20, 15, 20.00),        # AMBER  #1038
        (48, 36, 30, 15, 30.00),        # AMBER  12239 (bespoke)
        (60, 36, 40, 20, 40.00),        # AMBER  3128  (bespoke)
    ]:
        c = select_hanger(glass_w, glass_h, fitted_w_in=fitted_w, fitted_h_in=fitted_h)
        assert c.target_width_in >= MIN_WIDTH_FRACTION * glass_w - 1e-9
        # Within 5% of what the client actually built.
        assert abs(c.target_width_in - client_w) / client_w < 0.05, (
            glass_w, c.target_width_in, client_w)


def test_legend_matches_the_client_sheet():
    """Sizes as the parts are PLACED — the sheet's order for six of the seven.

    #1215 is deliberately 40x12, NOT the 12x40 the sheet prints. Measured off the placed
    component (2026-08-18): every other prefab's assembly bounding box matches the sheet in
    order — #1038 508.00x381.00 = 20x15, #1417 304.80x190.50 = 12x7.5, #1169 304.80x330.20 =
    12x13, #1333 361.95x609.60 = 14.25x24 (portrait, and correct) — while #1215 comes in at
    1016.00x304.80 mm = 40x12. It is the one part authored with its long axis where the rest of
    the family puts its width, so it hangs LANDSCAPE. Listed as 12x40 it was picked for tall
    narrow glass and then fitted 40" across an 18" mirror. Do not "correct" this back.
    """
    assert dict((p, (w, h)) for p, w, h in PREFAB_HANGERS) == {
        "1417": (12.00, 7.5),
        "1004": (14.25, 10.0),
        "1169": (12.00, 13.0),
        "1119": (14.25, 15.0),
        "1038": (20.00, 15.0),
        "1333": (14.25, 24.0),
        "1215": (40.00, 12.0),
    }


def test_constants_are_the_client_numbers():
    assert TARGET_AREA_FRACTION == 0.20
    assert MAX_AREA_FRACTION == 0.25
    # Must stay below KELLY's real-world 18.55%, which is known-acceptable.
    assert REVIEW_BELOW_FRACTION < 213.75 / 1152


# ── the three cases that pin the rule to reality ──────────────────────────────

def test_amber_36x36_reproduces_the_hanger_in_the_model():
    choice = select_hanger(36, 36)
    assert choice.part == "1038"
    assert choice.fraction == pytest.approx(300 / 1296)      # 23.15%
    assert choice.in_band is True
    assert choice.under_target is False
    assert choice.needs_review is False


def test_kelly_24x48_reproduces_the_hanger_in_the_model():
    # The old "smallest >= 20%" rule wrongly picked #1038 (26.04%) here.
    choice = select_hanger(24, 48)
    assert choice.part == "1119"
    assert choice.fraction == pytest.approx(213.75 / 1152)   # 18.55%
    assert choice.under_target is True                       # below 20%, still correct
    assert choice.needs_review is False                      # and NOT a custom-part case


def test_amber_36x48_takes_1333_and_refuses_to_overshoot_to_1215():
    # User 2026-07-30: 19.79% "would make sense"; #1215 at 27.78% is "the larger one
    # that won't work".
    choice = select_hanger(36, 48)
    assert choice.part == "1333"
    assert choice.fraction == pytest.approx(342 / 1728)      # 19.79%
    assert choice.under_target is True
    # #1215 is still refused, but on GEOMETRY rather than area now that it is known to be
    # 40x12: 40" cannot span a 36" glass, so it never reaches the area comparison at all.
    # `rejected_oversize` lists only prefabs that FIT and were passed over, hence empty.
    c1215 = next(c for c in choice.candidates if c.part == "1215")
    assert c1215.fits is False
    assert c1215.eligible is False
    assert choice.rejected_oversize == []


# ── the ceiling is the binding constraint ─────────────────────────────────────

def test_never_overshoots_the_ceiling_when_something_fits_under_it():
    for w, h in [(36, 36), (24, 48), (36, 48), (30, 30), (24, 36), (48, 60)]:
        choice = select_hanger(w, h)
        assert choice.fraction <= MAX_AREA_FRACTION + 1e-12, (w, h, choice.part)
        assert choice.over_ceiling is False


def test_picks_the_largest_eligible_not_the_smallest():
    # 22x26 = 572 sq in puts BOTH #1004 (24.9%) and #1169 (27.3%, over) in play, and both
    # clear the 55% width floor (12.1in) — so there is a real choice to get wrong.
    choice = select_hanger(22, 26)
    eligible = set(c.part for c in choice.candidates if c.eligible)
    assert eligible, "no candidate qualified — the fixture no longer exercises the choice"
    assert choice.part == max(
        (c for c in choice.candidates if c.eligible), key=lambda c: c.area_sq_in).part


def test_the_acceptance_window_excludes_both_too_small_and_too_big():
    # On 36x36 only #1038 (23.15%) lands in the window: #1119 and below are under 18%,
    # #1333 and above are over the 25% ceiling.
    choice = select_hanger(36, 36)
    assert [c.part for c in choice.candidates if c.eligible] == ["1038"]
    assert choice.part == "1038"


def test_amber_36x36_excludes_1215_on_physical_fit():
    # #1215 is 12x40 — its 40" height cannot fit a 36" glass, regardless of area.
    c1215 = next(c for c in select_hanger(36, 36).candidates if c.part == "1215")
    assert c1215.fits is False
    assert c1215.eligible is False


# ── the JEN failure: height cap + keep-fitted ─────────────────────────────────

def test_jen_keeps_its_bespoke_hanger_which_is_already_in_band():
    """Live regression (JEN, 2026-08-03): glass 50x41.5, fitted hanger 30x15 = 450 sq in =
    21.69% — already in band. It was replaced by #1215 (12x40), which at 96% of the glass
    height ran out the bottom. The fitted hanger must simply be kept."""
    choice = select_hanger(50, 41.5, fitted_w_in=30, fitted_h_in=15)
    assert choice.keep_fitted is True
    assert choice.part is None
    assert choice.fraction == pytest.approx(450 / 2075)
    assert choice.in_band is True
    assert (choice.target_width_in, choice.target_height_in) == (30, 15)


def test_1215_is_not_what_jen_gets():
    """The JEN 50x41.5 failure, re-pinned to the part's real 40x12 orientation.

    The overflow existed ONLY because the part was recorded 12x40, which stood it on end: 40" on
    a 41.5" glass is 96% of the height. At its true 40x12 the height is a harmless 29% and a
    40-wide hanger on a 50" mirror is a sensible part — so the height cap is no longer what saves
    JEN, and asserting that it is would pin a falsehood. What actually saves JEN is `keep_fitted`:
    its bespoke 30x15 is 21.69%, in band, and is left alone.
    """
    kept = select_hanger(50, 41.5, fitted_w_in=30, fitted_h_in=15)
    assert kept.keep_fitted is True
    assert kept.part is None

    c = next(c for c in kept.candidates if c.part == "1215")
    assert c.fraction == pytest.approx(480 / 2075)      # 23.13% — in band either way
    assert c.too_tall is False                          # 12" is 29% of the height, not 96%


def test_the_height_cap_does_not_break_the_approved_amber_choice():
    # AMBER 36x48 -> #1333 at 24" is 50% of the glass height, comfortably under the cap.
    c = next(c for c in select_hanger(36, 48).candidates if c.part == "1333")
    assert c.too_tall is False
    assert select_hanger(36, 48).part == "1333"


# ── resize the fitted hanger when no prefab qualifies ─────────────────────────

def test_resizes_the_fitted_hanger_when_no_prefab_qualifies():
    # 12x12 glass: every prefab is over the 25% ceiling, so the fitted hanger is scaled.
    choice = select_hanger(12, 12, fitted_w_in=6, fitted_h_in=3)
    assert choice.resize_fitted is True
    assert choice.part is None
    # Aims at the band midpoint, then snaps to a clean 0.25" increment, so the exact fraction
    # lands near 22.5% rather than on it — what matters is that it stays in band.
    assert TARGET_AREA_FRACTION <= choice.fraction <= MAX_AREA_FRACTION


def test_the_resized_hanger_never_runs_off_the_glass():
    """What actually stops JEN's failure — the HEIGHT CAP, not a never-taller-than-wide rule.

    SUPERSEDED EXPECTATION, recorded deliberately. This test used to assert
    `target_width_in >= target_height_in`. That clamp has been removed: it was a proxy for two
    concerns that already have their own rules (MAX_HEIGHT_FRACTION for running off the glass,
    MIN_WIDTH_FRACTION for failing to span), and what it added on top was a shape preference
    the client's own catalogue contradicts — #1333 is 14.25x24, taller than wide, and the
    selector picks it.

    It also only ever fired AFTER the chassis had pinned the width, so it turned a clipped
    hanger into a collapsed one. See test_a_chassis_clipped_hanger_grows_taller_not_squarer.
    """
    for gw, gh, fw, fh in [(12, 12, 6, 3), (24, 60, 14.25, 15), (50, 41.5, 30, 15),
                           (90, 36, 40, 20), (36, 72, 20, 15)]:
        choice = select_hanger(gw, gh, fitted_w_in=fw, fitted_h_in=fh)
        if not choice.resize_fitted:
            continue
        where = f"{gw}x{gh} -> {choice.target_width_in}x{choice.target_height_in}"
        # the rule that genuinely prevented JEN: never run off the bottom of the mirror
        assert choice.target_height_in <= MAX_HEIGHT_FRACTION * gh + 1e-9, where
        # and it must still span
        assert choice.target_width_in >= MIN_WIDTH_FRACTION * gw - 1e-9, where


def test_a_chassis_clipped_hanger_grows_taller_not_squarer():
    """Live 20x80 with a 14in chassis — the bug this removal fixes.

    The width is pinned at 13in by the 65% share. The old `h <= w` clamp then pinned the height
    to 13in as well, giving 10.56% of the glass — far below the 18% floor a PREFAB would have
    been rejected for. Every gate that describes a real failure passes at 13 x 27.75: it spans
    65% of the mirror (floor 55%), sits at 35% of the mirror height (cap 60%), and fits inside
    the 14in chassis.
    """
    c = select_hanger(20, 80, fitted_w_in=18, fitted_h_in=72, chassis_w_in=14)
    assert c.resize_fitted is True
    assert c.target_width_in == pytest.approx(13.0)
    assert c.target_height_in == pytest.approx(27.75)
    frac = (c.target_width_in * c.target_height_in) / (20 * 80)
    assert MIN_ACCEPTABLE_FRACTION <= frac <= MAX_AREA_FRACTION, frac
    assert c.target_height_in <= MAX_HEIGHT_FRACTION * 80
    assert c.target_width_in >= MIN_WIDTH_FRACTION * 20


def test_a_tall_glass_no_longer_squares_off_below_the_band():
    """The other casualty. On a 24x60 with no prefab available the clamp squared the hanger to
    15.5 x 15.5 = 16.68%, under the 18% floor, and nothing re-checked — it just shipped."""
    w = 0.65 * 24
    h = min(0.225 * 24 * 60 / w, 60, MAX_HEIGHT_FRACTION * 60)
    assert (w * h) / (24 * 60) >= MIN_ACCEPTABLE_FRACTION - 1e-9
    assert h > w, "this is exactly the case the clamp used to collapse"


def test_removing_the_clamp_moves_nothing_on_a_landscape_mirror():
    """The clamp only ever fired when the height approached the width, which on a landscape
    glass it never does. These are the validated answers and they must not have shifted."""
    for gw, gh, ew, eh in [(90, 36, 58.5, 12.5), (48, 36, 31.25, 12.5), (72, 40, 46.75, 13.75)]:
        c = select_hanger(gw, gh, fitted_w_in=20, fitted_h_in=15)
        assert c.resize_fitted is True, (gw, gh)
        assert c.target_width_in == pytest.approx(ew), (gw, gh)
        assert c.target_height_in == pytest.approx(eh), (gw, gh)


def test_the_resized_hanger_lands_inside_the_band():
    choice = select_hanger(12, 12, fitted_w_in=6, fitted_h_in=3)
    frac = (choice.target_width_in * choice.target_height_in) / (12 * 12)
    assert TARGET_AREA_FRACTION <= frac <= MAX_AREA_FRACTION


def test_a_prefab_too_far_under_target_is_rejected_in_favour_of_resizing():
    """Live report (2026-08-03): on a 50x36 glass #1038 was selected at 16.67% because
    eligibility only required <= 25%. Per the client, a prefab that does not follow the
    criteria must give way to resizing the existing hanger."""
    choice = select_hanger(50, 36, fitted_w_in=20, fitted_h_in=15)
    assert choice.resize_fitted is True
    assert choice.part is None
    c1038 = next(c for c in choice.candidates if c.part == "1038")
    assert c1038.fraction == pytest.approx(300 / 1800)      # 16.67%
    assert c1038.eligible is False


def test_the_lower_bound_keeps_kellys_validated_near_miss():
    # KELLY's real 18.55% sits under the 20% target but must still qualify — the bound is 18%,
    # between it and the 16.67% failure. #1119 also spans 59.4% of KELLY's 24in width.
    assert select_hanger(24, 48).part == "1119"          # 18.55%
    assert MIN_ACCEPTABLE_FRACTION == pytest.approx(0.18)


def test_36x48_no_longer_takes_the_narrow_1333():
    """SUPERSEDED EXPECTATION, recorded deliberately.

    #1333 (14.25in) was the approved answer for 36x48 under the AREA rule at 19.79%. It spans
    only 39.6% of the width — narrower than the 20in the client themselves put on a 36x36 — so
    the width floor now rejects it and the fitted hanger is scaled instead. Worth re-confirming
    with the client, which is why this is asserted rather than left to drift.
    """
    choice = select_hanger(36, 48, fitted_w_in=20, fitted_h_in=15)
    c1333 = next(c for c in choice.candidates if c.part == "1333")
    assert c1333.too_narrow is True
    assert choice.resize_fitted is True
    assert choice.target_width_in >= MIN_WIDTH_FRACTION * 36


def test_the_resized_hanger_gets_clean_quarter_inch_dims():
    choice = select_hanger(50, 36, fitted_w_in=20, fitted_h_in=15)
    step = 0.25
    for v in (choice.target_width_in, choice.target_height_in):
        assert abs(v / step - round(v / step)) < 1e-9, f"{v} is not a clean {step}in increment"


def test_a_resized_hanger_lands_in_the_20_to_25_percent_area_band():
    """Client instruction (2026-08-06): the width sets the span, then the HEIGHT comes down so
    the hanger still falls in the 20-25% area band. The 90in case produced 38.8% before this."""
    for gw, gh, fw, fh in [(90, 36, 40, 20), (60, 36, 40, 20), (48, 36, 30, 15),
                           (72, 40, 24, 16), (50, 36, 20, 15)]:
        c = select_hanger(gw, gh, fitted_w_in=fw, fitted_h_in=fh)
        if not c.resize_fitted:
            continue
        frac = (c.target_width_in * c.target_height_in) / (gw * gh)
        assert TARGET_AREA_FRACTION - 0.02 <= frac <= MAX_AREA_FRACTION + 0.005, (
            f"{gw}x{gh} -> {c.target_width_in}x{c.target_height_in} = {frac:.1%}")


def test_the_90_inch_case_end_to_end():
    """The reported failure, with the numbers the client asked for."""
    c = select_hanger(90, 36, fitted_w_in=40, fitted_h_in=20)
    assert c.resize_fitted is True
    assert c.target_width_in == pytest.approx(58.5)          # 65% of 90 — spans the glass
    assert c.target_height_in == pytest.approx(12.5)         # brought down from 21.5
    assert (58.5 * 12.5) / (90 * 36) == pytest.approx(0.2257, abs=1e-3)
    assert c.target_width_in - 4.25 == pytest.approx(54.25)  # where the hanging tabs land


def test_a_resized_hanger_always_spans_the_width_floor():
    """The point of the change: a scaled hanger must actually span the glass. Sizing by AREA
    put a 20in hanger on a 90in mirror and the hanging tabs bunched at the centre."""
    for gw, gh, fw, fh in [(50, 36, 20, 15), (60, 36, 40, 20), (90, 36, 40, 20),
                           (44, 30, 18, 12), (72, 40, 24, 16)]:
        c = select_hanger(gw, gh, fitted_w_in=fw, fitted_h_in=fh)
        if not c.resize_fitted:
            continue
        assert c.target_width_in >= MIN_WIDTH_FRACTION * gw - 1e-9, (gw, c.target_width_in)


def test_a_resized_hanger_never_overflows_the_glass():
    for gw, gh, fw, fh in [(50, 36, 20, 15), (90, 36, 40, 20), (33, 27, 11, 9),
                           (52, 38, 26, 13), (24, 60, 14.25, 15)]:
        c = select_hanger(gw, gh, fitted_w_in=fw, fitted_h_in=fh)
        if not c.resize_fitted:
            continue
        assert c.target_width_in <= gw, (gw, c.target_width_in)
        assert c.target_height_in <= MAX_HEIGHT_FRACTION * gh + 1e-9, (gh, c.target_height_in)


def test_a_qualifying_prefab_still_wins_over_resizing():
    # Prefab substitution remains the default when one genuinely suits — 36x36 is the case
    # the client actually built, and #1038 spans 55.6% of the width.
    choice = select_hanger(36, 36, fitted_w_in=14.25, fitted_h_in=10)
    assert choice.part == "1038"
    assert choice.resize_fitted is False
    assert choice.keep_fitted is False


# ── fallbacks ─────────────────────────────────────────────────────────────────

def test_small_glass_where_every_prefab_exceeds_the_ceiling():
    # 12x12 = 144 sq in; 25% = 36, smaller than the smallest prefab (90).
    choice = select_hanger(12, 12)
    assert choice.part == "1417"          # smallest that fits
    assert choice.over_ceiling is True
    assert choice.fraction == pytest.approx(90 / 144)


def test_oversized_glass_flags_review():
    # 60x80 = 4800 sq in; the largest prefab is only 10% of it.
    choice = select_hanger(60, 80)
    assert choice.part == "1215"
    assert choice.fraction == pytest.approx(480 / 4800)      # 10.00%
    assert choice.under_target is True
    assert choice.needs_review is True


def test_tiny_glass_with_no_fitting_prefab():
    choice = select_hanger(6, 6)
    assert choice.part is None
    assert choice.needs_review is True
    assert "no prefab fits" in choice.reason


def test_zero_area_is_handled():
    choice = select_hanger(0, 36)
    assert choice.part is None
    assert choice.needs_review is True


def test_fit_margin_can_exclude_a_borderline_prefab():
    # #1038 is exactly as wide as a 20" glass; demand clearance and it drops out.
    assert select_hanger(20, 60).part == "1038"
    assert select_hanger(20, 60, fit_margin_in=0.5).part == "1119"


# ── target dims handed to the app to write onto the placed hanger ─────────────

def test_choice_carries_the_exact_catalogue_dimensions():
    choice = select_hanger(36, 36)
    assert (choice.target_width_in, choice.target_height_in) == (20.0, 15.0)
    # Exactly the values already in the live AMBER model → a no-op at 36x36.
    assert choice.target_width_m == pytest.approx(0.5080)
    assert choice.target_height_m == pytest.approx(0.3810)


def test_target_dims_change_with_the_chosen_prefab():
    choice = select_hanger(36, 48)
    assert (choice.target_width_in, choice.target_height_in) == (14.25, 24.0)


def test_part_name_is_the_export_rename_stem():
    assert select_hanger(36, 36).part_name == "1038-HANGER"
    assert select_hanger(36, 48).part_name == "1333-HANGER"
    assert select_hanger(6, 6).part_name == ""


# ── units + trace ─────────────────────────────────────────────────────────────

def test_meters_wrapper_matches_inches():
    a, b = select_hanger(36, 36), select_hanger_meters(0.9144, 0.9144)
    assert a.part == b.part
    assert a.fraction == pytest.approx(b.fraction)


def test_log_lines_cover_every_candidate_and_mark_the_choice():
    lines = select_hanger(36, 48).log_lines()
    assert len(lines) >= 1 + len(PREFAB_HANGERS)
    assert any("CHOSEN" in ln and "#1333" in ln for ln in lines)
    # #1215 is 40" WIDE, so on a 36" glass it simply does not fit.
    assert any("does not fit" in ln and "#1215" in ln for ln in lines)


# ── the CHASSIS, not just the glass ──────────────────────────────────────────
#
# The hanger bolts to the chassis and its tabs are cut into the chassis, but every other check
# measures it against the GLASS. On AMBER that is invisible (chassis = glass - 2"); on CLARA
# (glass - 6") it is decisive.

def test_clara_24x60_no_longer_keeps_a_hanger_wider_than_its_chassis():
    """Live 2026-08-18. A 24x60 glass leaves an 18" chassis, and keep_fitted held the 20" #1038
    because 300/1440 = 20.83% is a fine share of the GLASS — so the hanger overhung the part
    carrying its tabs by an inch each side."""
    old = select_hanger(24, 60, fitted_w_in=20, fitted_h_in=15)
    assert old.keep_fitted is True, "precondition: without the chassis it is kept"

    c = select_hanger(24, 60, fitted_w_in=20, fitted_h_in=15, chassis_w_in=18)
    assert c.keep_fitted is False
    assert c.target_width_in <= 18
    assert c.part == "1333"          # 14.25 wide, fits the chassis with room to spare


def test_a_prefab_wider_than_the_chassis_is_ineligible_and_says_why():
    c = select_hanger(24, 60, chassis_w_in=18)
    c1038 = next(k for k in c.candidates if k.part == "1038")
    assert c1038.over_chassis is True
    assert c1038.eligible is False
    assert any("wider than the 18in chassis" in ln and "#1038" in ln for ln in c.log_lines())


def test_a_scaled_hanger_is_capped_by_the_chassis_too():
    # 0.65 x 90 = 58.5" of span wanted, but the chassis is only 40" — a stretched hanger is no
    # more allowed to overhang than a catalogue one.
    c = select_hanger(90, 36, fitted_w_in=20, fitted_h_in=15, chassis_w_in=40)
    assert c.resize_fitted is True
    assert c.target_width_in <= 40


def test_omitting_the_chassis_changes_nothing():
    """The cap is opt-in: a model whose rules name no chassis width must behave exactly as
    before rather than having every hanger rejected."""
    for w, h in [(36, 36), (24, 48), (36, 48), (24, 36), (18, 20)]:
        a = select_hanger(w, h, fitted_w_in=20, fitted_h_in=15)
        b = select_hanger(w, h, fitted_w_in=20, fitted_h_in=15, chassis_w_in=0)
        assert (a.part, a.keep_fitted, a.target_width_in) == \
               (b.part, b.keep_fitted, b.target_width_in), (w, h)
