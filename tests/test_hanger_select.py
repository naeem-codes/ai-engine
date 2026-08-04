"""Prefab hanger selection by glass surface area (client rule, 2026-07-30).

Rule: the LARGEST prefab that fits and does not exceed 25% of the glass area. The three
"reproduces" tests below are the ones that matter — they pin the rule to real data:
two hangers actually built into models, plus the user's explicit call on 36x48.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from hanger_select import (MAX_AREA_FRACTION, PREFAB_HANGERS, REVIEW_BELOW_FRACTION,
                           TARGET_AREA_FRACTION, select_hanger, select_hanger_meters)


def test_legend_matches_the_client_sheet():
    assert dict((p, (w, h)) for p, w, h in PREFAB_HANGERS) == {
        "1417": (12.00, 7.5),
        "1004": (14.25, 10.0),
        "1169": (12.00, 13.0),
        "1119": (14.25, 15.0),
        "1038": (20.00, 15.0),
        "1333": (14.25, 24.0),
        "1215": (12.00, 40.0),
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
    assert [c.part for c in choice.rejected_oversize] == ["1215"]
    assert choice.rejected_oversize[0].fraction == pytest.approx(480 / 1728)  # 27.78%


# ── the ceiling is the binding constraint ─────────────────────────────────────

def test_never_overshoots_the_ceiling_when_something_fits_under_it():
    for w, h in [(36, 36), (24, 48), (36, 48), (30, 30), (24, 36), (48, 60)]:
        choice = select_hanger(w, h)
        assert choice.fraction <= MAX_AREA_FRACTION + 1e-12, (w, h, choice.part)
        assert choice.over_ceiling is False


def test_picks_the_largest_eligible_not_the_smallest():
    choice = select_hanger(36, 36)
    eligible = [c.part for c in choice.candidates if c.eligible]
    assert choice.part == "1038"
    # #1119/#1169/#1004/#1417 are all eligible but smaller — the largest must win.
    assert set(eligible) == {"1417", "1004", "1169", "1119", "1038"}


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


def test_1215_is_rejected_on_the_height_cap_for_jen():
    # Its AREA was in band (23.13%) and it "fit" (40 <= 41.5) — only the height cap stops it.
    c = next(c for c in select_hanger(50, 41.5).candidates if c.part == "1215")
    assert c.fits is True
    assert c.fraction == pytest.approx(480 / 2075)      # 23.13%, inside the band
    assert c.too_tall is True
    assert c.eligible is False


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
    assert choice.fraction == pytest.approx(0.225)


def test_the_resized_hanger_keeps_its_aspect_ratio():
    # This is what stops a wide/short hanger becoming tall/narrow, as JEN's did.
    choice = select_hanger(12, 12, fitted_w_in=6, fitted_h_in=3)
    assert choice.target_width_in / choice.target_height_in == pytest.approx(2.0)


def test_the_resized_hanger_lands_inside_the_band():
    choice = select_hanger(12, 12, fitted_w_in=6, fitted_h_in=3)
    frac = (choice.target_width_in * choice.target_height_in) / (12 * 12)
    assert TARGET_AREA_FRACTION <= frac <= MAX_AREA_FRACTION


def test_a_qualifying_prefab_still_wins_over_resizing():
    # Prefab substitution remains the default when one genuinely suits.
    choice = select_hanger(36, 48, fitted_w_in=20, fitted_h_in=15)
    assert choice.part == "1333"
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
    # #1215 is 40" tall on a 48" glass = 83%, so the height cap rejects it before area does.
    assert any("too tall" in ln and "#1215" in ln for ln in lines)
