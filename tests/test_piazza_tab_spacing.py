"""The hanging-tab spacing is identified ONCE, at rule generation, and stored as an offset link.

The follower used to re-derive it on every resize from "which chassis dim sits 4.25" inside the
hanger?". That question only has a right answer while the model is still aligned, and it is
asked at the one moment it may not be.

Two live failures forced the change:

  PIAZZA (2026-08-31) — the search was gated on the sketch NAME (`SKETCH81`). PIAZZA's chassis
  has no Sketch81; the spacing lives in `D1@Sketch39`. Nothing matched, nothing was logged, and
  the tabs slid out of the hanger slot on every resize.

  BREAM (2026-09-02) — a hanger swap left a stale component id in the plan, so the applier
  dropped both hanger width writes while the chassis tab write landed. The model was saved with
  its tabs set for a 15.5" hanger it never got. The inset was then 0.750", outside the +/-1.0"
  window, so the dim stopped being recognised AT ALL — a ratchet, because nothing remembered
  what it was. Three resizes later the hanger was 78.000" and the tabs were still 11.250" apart.

Widening the search was tried and is worse than doing nothing (see
`test_the_legacy_search_is_not_widened`), so the fix is to stop searching at resize time.

Real values throughout, read from the logs.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import generate_rules
import hanger_select
import interpret
from models import DimensionIn, GenerateRulesRequest, InterpretRequest, SkipEntry
from rules import ModelRules, OffsetRuleEntry

IN = 0.0254

# ── real chassis, real hangers ────────────────────────────────────────────────
# PIAZZA: no Sketch81 anywhere, and a decoy 1.375" from the real dim.
PZ_TAB = "D1@Sketch39 [12459-CHASSIS-1]"        # 10.000" — 4.250" inside a 14.25" hanger
PZ_DECOY = "D3@Sketch29 [12459-CHASSIS-1]"      # 10.813" — 3.437", the old window let it in
PZ_HANGER_W = "D2@Base-Flange1 [1119-HANGER-1]"
PZ_HANGER_H = "D1@Sketch1 [1119-HANGER-1]"
PZ_MIRROR_W = "D1@Sketch1 [PIAZZA-MIRROR-1]"
PZ_MIRROR_H = "D2@Sketch1 [PIAZZA-MIRROR-1]"

PIAZZA = {PZ_MIRROR_W: 20.5 * IN, PZ_MIRROR_H: 32.5 * IN,
          "D1@Sketch1 [12459-CHASSIS-1]": 20.5 * IN,
          PZ_TAB: 10.0 * IN, PZ_DECOY: 10.813 * IN,
          PZ_HANGER_W: 14.25 * IN, PZ_HANGER_H: 15.0 * IN}
PIAZZA_AXIS = {PZ_MIRROR_W: "W", PZ_MIRROR_H: "H",
               "D1@Sketch1 [12459-CHASSIS-1]": "W",
               PZ_HANGER_W: "W", PZ_HANGER_H: "H"}       # the tab dims carry NO label

# 9535: four dims in the old window, three of them at exactly 4.250".
NINE = {"D1@Sketch81 [9535-CHASSIS-2]": 10.0 * IN,
        "D7@Sketch104 [9535-CHASSIS-2]": 10.0 * IN,
        "D3@Sketch104 [9535-CHASSIS-2]": 10.0 * IN,
        "D9@Sketch104 [9535-CHASSIS-2]": 9.0 * IN,
        PZ_HANGER_W: 14.25 * IN}

# AMBER, the product the 4.25" rule came from.
AMBER = {"D1@Sketch81 [12204-CHASSIS-2]": 15.75 * IN,
         "D3@Sketch81 [12204-CHASSIS-2]": 1.75 * IN,
         "D2@Base-Flange1 [1038-HANGER-1]": 20.0 * IN}


def _gen_req(dims, axis):
    return GenerateRulesRequest(
        assembly_context="",
        dimensions=[DimensionIn(name=n, value_meters=v) for n, v in dims.items()],
        dim_axis_labels=axis)


def _int_req(dims, axis=None):
    return InterpretRequest(
        instruction="resize",
        dimensions=[DimensionIn(name=n, value_meters=v) for n, v in dims.items()],
        dim_axis_labels=axis or {})


def _rules_with(target, source, offset_m):
    return ModelRules(model="T", limits=None,
                      offset=[OffsetRuleEntry(target_dim=target, source_dim=source,
                                              offset_meters=offset_m)])


# ── generation-time identification ────────────────────────────────────────────

def test_piazza_resolves_uniquely_now_that_the_window_is_tight():
    """The whole point. The old +/-1.0" window admitted PIAZZA's 10.813" decoy alongside the
    real 10.000" dim; 0.25" excludes it, so the product that started this gets a link."""
    assert not any("SKETCH81" in n.upper() for n in PIAZZA), "precondition: no Sketch81 here"
    cands = hanger_select.find_tab_spacing_candidates(PIAZZA, PIAZZA_AXIS, 14.25 * IN)
    assert [c.dim for c in cands] == [PZ_TAB], [c.dim for c in cands]
    assert cands[0].inset_in == pytest.approx(4.25)
    assert not cands[0].named, "found by relationship, with no name hint to help"


def test_the_old_window_would_still_be_ambiguous_here():
    """Guards the tolerance itself: at +/-1.0" PIAZZA has two candidates and no way to choose."""
    cands = hanger_select.find_tab_spacing_candidates(
        PIAZZA, PIAZZA_AXIS, 14.25 * IN, tol_in=hanger_select.EXPECTED_TAB_INSET_TOL_IN)
    assert {c.dim for c in cands} == {PZ_TAB, PZ_DECOY}


def test_the_axis_label_is_not_a_gate():
    """PIAZZA's tab dims carry no [W] at all — the labeler only labels dims near a bounding-box
    extent and the spacing is internal. Gating on it is a second way to match nothing."""
    assert PIAZZA_AXIS.get(PZ_TAB) is None
    assert hanger_select.find_tab_spacing_candidates(PIAZZA, {}, 14.25 * IN)[0].dim == PZ_TAB


def test_a_name_hint_breaks_a_real_tie():
    """9535 has THREE dims at exactly 4.250" inside the hanger. Identical on every geometric
    measure, so the sketch name is the only thing left — and it still decides."""
    raw = hanger_select.find_tab_spacing_candidates(NINE, {}, 14.25 * IN, tol_in=1.0)
    assert len([c for c in raw if c.inset_in == pytest.approx(4.25)]) == 1, \
        "name narrowing must have collapsed the three-way tie"
    assert raw[0].dim == "D1@Sketch81 [9535-CHASSIS-2]"


def test_amber_is_unchanged():
    cands = hanger_select.find_tab_spacing_candidates(AMBER, {}, 20.0 * IN)
    assert [c.dim for c in cands] == ["D1@Sketch81 [12204-CHASSIS-2]"]


def test_mate_dims_are_never_candidates():
    """12203's `D1@Distance1` (4.000") lands squarely in the window for a 7.5" hanger. A mate is
    a POSITION — driving it with a size delta moves the component bodily."""
    dims = {"D1@Distance1 [12203-CHASSIS-ASSY-1]": 4.0 * IN}
    assert hanger_select.find_tab_spacing_candidates(dims, {}, 7.5 * IN, tol_in=1.0) == []


def test_no_hanger_width_means_no_opinion():
    assert hanger_select.find_tab_spacing_candidates(PIAZZA, PIAZZA_AXIS, 0.0) == []


def test_a_genuine_tie_stays_ambiguous():
    dims = dict(PIAZZA)
    dims["D9@Sketch44 [12459-CHASSIS-1]"] = 10.0 * IN      # identical evidence, different dim
    assert len(hanger_select.find_tab_spacing_candidates(dims, PIAZZA_AXIS, 14.25 * IN)) == 2


# ── what generate_rules stores ────────────────────────────────────────────────

def test_generation_stores_the_link():
    skip = []
    out = generate_rules._tab_spacing_offset(_gen_req(PIAZZA, PIAZZA_AXIS), skip)
    assert len(out) == 1
    r = out[0]
    assert r.target_dim == PZ_TAB
    assert r.source_dim == PZ_HANGER_W
    assert r.offset_meters == pytest.approx(-4.25 * IN)     # negative: tabs sit INSIDE
    assert r.component == "12459-CHASSIS-1"
    assert skip == []


def test_an_ambiguous_model_stores_nothing_and_says_why():
    dims = dict(PIAZZA)
    dims["D9@Sketch44 [12459-CHASSIS-1]"] = 10.0 * IN
    skip = []
    assert generate_rules._tab_spacing_offset(_gen_req(dims, PIAZZA_AXIS), skip) == []
    assert len(skip) == 1 and "NOT stored" in skip[0].reason


def test_a_bracket_product_stores_nothing_quietly():
    """ISABELL has no chassis tabs — a hanging BRACKET seats in the slots instead, handled by a
    different follower that identifies by component id. No link, and no skip noise."""
    dims = {"D2@Base-Flange1 [1119-HANGER-1]": 14.25 * IN,
            "D1@Sketch1 [1047-HANGING-BRACKET-1]": 13.375 * IN}
    skip = []
    assert generate_rules._tab_spacing_offset(_gen_req(dims, {PZ_HANGER_W: "W"}), skip) == []
    assert skip == []


def test_a_sheet_metal_dim_is_not_the_hanger_width():
    """`@Sheet-Metal` dims are flat-pattern metadata, not an outer size (the AMY bug)."""
    dims = {"D2@Sheet-Metal1 [1119-HANGER-1]": 17.590 * IN, PZ_HANGER_W: 14.25 * IN}
    name, value = generate_rules._hanger_width_dim(
        _gen_req(dims, {n: "W" for n in dims}))
    assert name == PZ_HANGER_W and value == pytest.approx(14.25 * IN)


def test_a_measured_inset_is_kept_when_it_really_differs():
    dims = {"D1@Sketch81 [12204-CHASSIS-2]": 15.55 * IN,     # 4.45" inset, a real difference
            "D2@Base-Flange1 [1038-HANGER-1]": 20.0 * IN}
    axis = {"D2@Base-Flange1 [1038-HANGER-1]": "W"}
    out = generate_rules._tab_spacing_offset(_gen_req(dims, axis), [])
    assert out[0].offset_meters == pytest.approx(-4.45 * IN)


# ── what interpret does with it ───────────────────────────────────────────────

def test_the_stored_link_drives_the_resize():
    req = _int_req({PZ_TAB: 10.0 * IN})
    rules = _rules_with(PZ_TAB, PZ_HANGER_W, -4.25 * IN)
    out = interpret._hanger_follower_updates(req, 14.25 * IN, (20.0 - 14.25) * IN, rules)
    assert len(out) == 1
    name, new_val, _cur, applied = out[0]
    assert name == PZ_TAB
    assert new_val / IN == pytest.approx(20.0 - 4.25)
    assert applied == pytest.approx(4.25)


def test_the_bream_ratchet_is_broken():
    """The live failure. Tabs frozen at 11.250" against a 40" hanger — an inset of 28.750",
    far outside any identification window — and the hanger now going to 78". The geometric
    search cannot even see this dim any more; the stored link repairs it."""
    req = _int_req({"D1@Sketch81 [12226-CHASSIS-2]": 11.25 * IN})
    rules = _rules_with("D1@Sketch81 [12226-CHASSIS-2]", PZ_HANGER_W, -4.25 * IN)

    assert interpret._hanger_follower_updates(req, 40.0 * IN, 38.0 * IN, None) == [], \
        "precondition: the legacy search is blind to this dim"

    out = interpret._hanger_follower_updates(req, 40.0 * IN, 38.0 * IN, rules)
    assert out[0][1] / IN == pytest.approx(78.0 - 4.25)     # 73.750", not 11.250"


def test_the_link_survives_a_component_rename():
    """The app renumbers parts on resize (`[RENAME] ... Number='1003'`), and the rules file
    keeps the id it was generated with. Matching only exactly would inherit the very staleness
    bug that broke BREAM."""
    req = _int_req({"D1@Sketch81 [6666-CXP-CUSTOM-CHASSIS-2]": 15.75 * IN})
    rules = _rules_with("D1@Sketch81 [12204-CHASSIS-2]", PZ_HANGER_W, -4.25 * IN)
    out = interpret._hanger_follower_updates(req, 20.0 * IN, 0.0, rules)
    assert out[0][0] == "D1@Sketch81 [6666-CXP-CUSTOM-CHASSIS-2]"


def test_an_unresolvable_link_falls_back_rather_than_guessing():
    req = _int_req({"D1@Sketch81 [A-CHASSIS-1]": 15.75 * IN,
                    "D1@Sketch81 [B-CHASSIS-1]": 15.75 * IN})
    rules = _rules_with("D1@Sketch81 [12204-CHASSIS-2]", PZ_HANGER_W, -4.25 * IN)
    assert interpret._stored_tab_link(req, rules) is None


def test_an_unrelated_offset_link_is_not_mistaken_for_the_tab():
    """A user-authored link between two non-hanger dims belongs to `expand_offsets`."""
    req = _int_req({"D1@Sketch9 [12204-CHASSIS-2]": 1.0 * IN})
    rules = _rules_with("D1@Sketch9 [12204-CHASSIS-2]",
                        "D1@Sketch1 [1011-MIRROR-1]", -4.25 * IN)
    assert interpret._stored_tab_link(req, rules) is None


def test_no_rules_means_the_legacy_search_still_runs():
    """Rule sets written before the link exists must behave exactly as they used to."""
    req = _int_req({"D1@Sketch81 [12204-CHASSIS-2]": 15.75 * IN})
    out = interpret._hanger_follower_updates(req, 20.0 * IN, (24.0 - 20.0) * IN, None)
    assert out[0][0] == "D1@Sketch81 [12204-CHASSIS-2]"
    assert out[0][1] / IN == pytest.approx(24.0 - 4.25)


def test_the_legacy_search_is_not_widened():
    """Why path 2 was left alone. Drop its name gate and a chassis whose real tab dim has
    drifted OUT of the window leaves a decoy as the sole candidate — and the follower writes to
    a dim it has never touched. Measured on 12204 at a 24" hanger: the real `D1@Sketch81`
    (15.750") falls outside, `D5@Sketch105` (20.000") is left alone inside. Doing nothing is
    the correct failure; the fix is the stored link, not a wider net."""
    req = _int_req({"D1@Sketch81 [12204-CHASSIS-2]": 15.75 * IN,
                    "D5@Sketch105 [12204-CHASSIS-2]": 20.0 * IN})
    assert interpret._hanger_follower_updates(req, 24.0 * IN, 0.0, None) == []
