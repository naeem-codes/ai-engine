"""AMY's chassis tabs track the hanger, found by the SLOT they are cut to.

Live 2026-09-21, AMY 36x36 -> 46x46. The hanger was replaced 12217-HANGER (35.000in wide, a
custom part) with the 40.000in prefab #1215, and the chassis tab row was not touched at all:
`follower_dims: []`, no [TAB] line in the log, no stored link in AMY's rules file. The tabs
stayed 25.500in apart while the slots they seat in moved.

The follower identifies the tab row by "it sits 4.25in inside the hanger width". That inset is a
property of the HANGER, and 12217 is not a prefab -- its slots sit 4.750in in from each edge:

    chassis  D2@Sketch53      = 25.500 in
    hanger   D2@Base-Flange1  = 35.000 in       35.000 - 25.500 = 9.500 in

9.500in is nowhere near the 4.25 +/- 0.25 generation window, so the row was never identified and
no link was ever stored. The resize-time search missed it a second way as well: its name gate is
`SKETCH81` and AMY's row is `Sketch53`.

The row is still findable by STRUCTURE. A tab is cut to fit the hanger's slot, the slot is
1.750in long on every hanger in every log here, and the chassis sketch that draws the tab row
carries that same 1.750in with the row's spacing as its largest dim. Verified against every
chassis dim in every log on this machine: exactly one sketch per chassis contains a 1.750in dim,
and in all three known cases its largest dim is the tab spacing. See `HANGER_SLOT_LENGTH_IN`.

The VALUE is the canonical 4.25in, not AMY's own 9.500in, because the inset belongs to whichever
hanger is fitted and after the swap that is a prefab -- the client's confirmed pairs are
14.25 -> 10.000, 20 -> 15.750, 30 -> 25.750, 40 -> 35.740. So 40.000 - 4.250 = 35.750in.
"""

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate_rules
import hanger_select
import interpret
import models

IN = 0.0254

HANGER_W_DIM = "D2@Base-Flange1 [12217-HANGER-1]"
TAB_DIM = "D2@Sketch53 [12216-CHASSIS-1]"

# AMY's chassis, read live. Sketch53 is the tab row; Sketch35 is the tab's own PROFILE, which
# carries no spacing at all -- which is why there was nothing for a value search to find.
AMY = {
    HANGER_W_DIM: 35.000 * IN,
    "D1@Sketch1 [12217-HANGER-1]": 17.000 * IN,
    "D1@Sketch3 [12217-HANGER-1]": 1.750 * IN,      # the slot, on the hanger
    "D2@Sketch3 [12217-HANGER-1]": 4.750 * IN,
    "D1@Sketch1 [12216-CHASSIS-1]": 35.000 * IN,
    "D2@Sketch1 [12216-CHASSIS-1]": 34.000 * IN,
    "D1@Sketch53 [12216-CHASSIS-1]": 1.750 * IN,    # the slot, echoed on the chassis
    TAB_DIM: 25.500 * IN,
    "D3@Sketch53 [12216-CHASSIS-1]": 0.500 * IN,
    "D3@Sketch35 [12216-CHASSIS-1]": 1.625 * IN,    # tab profile, not a spacing
    "D2@Sketch35 [12216-CHASSIS-1]": 0.375 * IN,
    "D1@Sketch35 [12216-CHASSIS-1]": 0.0625 * IN,
    "D3@Sketch36 [12216-CHASSIS-1]": 18.000 * IN,   # a big chassis dim that is NOT the row
}

# The products that already work, whose rows the inset window finds. Read live from the
# release-build log.
AMBER_LIKE = {
    "D2@Base-Flange1 [1038-HANGER-1]": 20.000 * IN,
    "D1@Sketch81 [1111-CHASSIS-2]": 15.750 * IN,
    "D3@Sketch81 [1111-CHASSIS-2]": 1.750 * IN,
    "D2@Sketch81 [1111-CHASSIS-2]": 0.728 * IN,
}


def _req(dims):
    return models.GenerateRulesRequest(
        assembly_context="",
        dimensions=[models.DimensionIn(name=n, value_meters=v) for n, v in dims.items()],
        dim_axis_labels={HANGER_W_DIM: "W", "D2@Base-Flange1 [1038-HANGER-1]": "W"})


def test_the_inset_window_really_does_miss_amy():
    """The fault itself. Without this the rest of the file proves nothing."""
    assert hanger_select.find_tab_spacing_candidates(AMY, None, 35.000 * IN) == []


def test_the_slot_length_finds_amys_row_and_only_that():
    cands = hanger_select.find_tab_spacing_by_slot_row(AMY, 35.000 * IN)
    assert len(cands) == 1, [c.dim for c in cands]
    assert cands[0].dim == TAB_DIM
    assert cands[0].value_meters == pytest.approx(25.500 * IN)
    assert cands[0].by_slot_row


def test_the_tab_PROFILE_sketch_is_not_mistaken_for_the_row():
    """Sketch35 is what the user pointed at, and it is the tab's outline, not its spacing.

    It holds no 1.750in slot dim, so it cannot be picked; and its largest dim (1.625in) is
    smaller than the slot it would have to seat in anyway.
    """
    found = {c.dim for c in hanger_select.find_tab_spacing_by_slot_row(AMY, 35.000 * IN)}
    assert not any("Sketch35" in d for d in found)
    assert not any("Sketch36" in d for d in found)


def test_the_stored_link_records_what_the_model_ACTUALLY_has():
    """The link keeps AMY's own 9.500in, because the inset belongs to the FITTED hanger.

    Storing the canonical 4.250in here was tried and broke a correct model — see
    `test_a_kept_hanger_leaves_the_tabs_exactly_where_they_are`. Whether the canonical inset
    applies is a resize-time question, because only the resize knows if a prefab is coming.
    """
    rules = generate_rules._tab_spacing_offset(_req(AMY), [])
    assert len(rules) == 1
    assert rules[0].target_dim == TAB_DIM
    assert rules[0].source_dim == HANGER_W_DIM
    assert rules[0].offset_meters == pytest.approx(-9.500 * IN)


def _follower(dims, old_w, new_w, inset_in, to_prefab):
    req = models.InterpretRequest(
        instruction="", shape="rect",
        dimensions=[models.DimensionIn(name=n, value_meters=v) for n, v in dims.items()])
    rules = types.SimpleNamespace(offset=[types.SimpleNamespace(
        target_dim=TAB_DIM, source_dim=HANGER_W_DIM, offset_meters=-inset_in * IN)])
    return interpret._hanger_follower_updates(
        req, old_w * IN, (new_w - old_w) * IN, rules, to_prefab)


def test_a_PREFAB_swap_lands_the_tabs_on_the_new_hangers_slots():
    """35.000in custom hanger -> 40.000in prefab #1215, the swap that was actually performed.

    The prefab brings its own slot pattern, cut the canonical 4.25in inside its width, so the
    tabs move onto THAT rather than carrying AMY's 9.500in forward (which would give 30.500in).
    """
    out = _follower(AMY, 35.000, 40.000, 9.500, to_prefab=True)
    assert len(out) == 1
    dim, new_val, was_in, applied_in = out[0]
    assert dim == TAB_DIM
    assert new_val == pytest.approx(35.750 * IN)      # 40.000 - 4.250
    assert was_in == pytest.approx(9.500, abs=1e-6)   # what the old custom hanger had
    assert applied_in == pytest.approx(4.250)


def test_a_kept_hanger_leaves_the_tabs_exactly_where_they_are():
    """The regression this file now guards, live 2026-09-21.

    AMY 24x48 -> 34x58 KEEPS its bespoke 23.000in `12214-HANGER` — the selector said so, the
    glass grew and the hanger did not. Its slots give a 13.500in tab row and they did not move,
    so neither may the tabs. Forcing the canonical inset sent the row to 18.750in and lifted the
    tabs clean out of the slots: worse than the original bug, which merely did nothing.
    """
    kept = {HANGER_W_DIM: 23.000 * IN, TAB_DIM: 13.500 * IN}
    out = _follower(kept, 23.000, 23.000, 9.500, to_prefab=False)
    # The follower reports the row's correct value; `finish` drops a write that matches what
    # the model already has. What matters is that the value is unchanged.
    assert out[0][1] == pytest.approx(13.500 * IN)
    assert out[0][3] == pytest.approx(9.500)


def test_a_STRETCHED_hanger_keeps_its_own_inset():
    """No prefab fits, so the fitted hanger is stretched — it keeps the slots it has."""
    kept = {HANGER_W_DIM: 23.000 * IN, TAB_DIM: 13.500 * IN}
    out = _follower(kept, 23.000, 28.000, 9.500, to_prefab=False)
    assert out[0][1] == pytest.approx(18.500 * IN)    # 28.000 - 9.500, not 28.000 - 4.250
    assert out[0][3] == pytest.approx(9.500)


def test_a_product_already_on_the_canonical_inset_is_unaffected_either_way():
    """AMBER's fitted hanger IS a prefab, so both branches agree — as they must."""
    amber = {HANGER_W_DIM: 20.000 * IN, TAB_DIM: 15.750 * IN}
    for to_prefab in (False, True):
        out = _follower(amber, 20.000, 24.000, 4.250, to_prefab)
        assert out[0][1] == pytest.approx(19.750 * IN), to_prefab


def test_products_that_already_work_are_untouched():
    """The inset window still answers first, so nothing about AMBER's path changes.

    The slot-row search is a FALLBACK — it only runs when the window finds nothing — so a
    product the window already handles can never be re-decided by it.
    """
    window = hanger_select.find_tab_spacing_candidates(AMBER_LIKE, None, 20.000 * IN)
    assert [c.dim for c in window] == ["D1@Sketch81 [1111-CHASSIS-2]"]
    assert not window[0].by_slot_row
    rules = generate_rules._tab_spacing_offset(_req(AMBER_LIKE), [])
    assert rules[0].offset_meters == pytest.approx(-4.250 * IN)   # already canonical


def test_two_instances_of_one_chassis_are_not_an_ambiguity():
    """A mirrored pair carries the same part-local dim at the same value: one physical row.

    Left uncollapsed this reads as two candidates, the caller refuses to guess, and the product
    gets no link — the exact failure this path exists to remove.
    """
    paired = dict(AMY)
    paired["D2@Sketch53 [12216-CHASSIS-9]"] = 25.500 * IN
    paired["D1@Sketch53 [12216-CHASSIS-9]"] = 1.750 * IN
    cands = hanger_select.find_tab_spacing_by_slot_row(paired, 35.000 * IN)
    assert len(cands) == 1, [c.dim for c in cands]
    stored = generate_rules._tab_spacing_offset(_req(paired), [])[0]
    assert stored.offset_meters == pytest.approx(-9.500 * IN)


def test_two_DIFFERENT_rows_are_still_an_ambiguity():
    """The collapse must not hide a real one. Different values are different rows."""
    two = dict(AMY)
    two["D2@Sketch61 [12216-CHASSIS-1]"] = 21.000 * IN
    two["D1@Sketch61 [12216-CHASSIS-1]"] = 1.750 * IN
    assert len(hanger_select.find_tab_spacing_by_slot_row(two, 35.000 * IN)) == 2
    assert generate_rules._tab_spacing_offset(_req(two), []) == []


def test_a_row_wider_than_its_hanger_is_refused():
    """A span that cannot physically seat in the hanger is not the tab row."""
    absurd = dict(AMY)
    absurd[TAB_DIM] = 60.000 * IN
    assert hanger_select.find_tab_spacing_by_slot_row(absurd, 35.000 * IN) == []


def test_a_sketch_with_no_span_in_it_is_refused():
    """Containing the slot length is not enough — there has to be a ROW."""
    tiny = {HANGER_W_DIM: 35.000 * IN,
            "D1@Sketch99 [12216-CHASSIS-1]": 1.750 * IN,
            "D2@Sketch99 [12216-CHASSIS-1]": 2.000 * IN}
    assert hanger_select.find_tab_spacing_by_slot_row(tiny, 35.000 * IN) == []


def test_a_product_with_no_tabs_stays_silent():
    """MICHELLE/ASTRA have no chassis tabs and must not acquire a link."""
    none = {HANGER_W_DIM: 35.000 * IN,
            "D1@Sketch1 [12454-CHASSIS-ASSEMBLY-1]": 30.000 * IN,
            "D2@Sketch1 [12454-CHASSIS-ASSEMBLY-1]": 40.000 * IN}
    assert hanger_select.find_tab_spacing_by_slot_row(none, 35.000 * IN) == []
    assert generate_rules._tab_spacing_offset(_req(none), []) == []
