"""AMY's whole tab STACK follows the hanger's slots: the notches AND the tabs.

Live 2026-09-25, AMY 24x48 -> 44x68. No prefab spans the 44in glass, so the bespoke 23.000in
`12214-HANGER` was stretched to 28.500in. Measured in the assembly afterwards:

    hanger slots   (Cut-Extrude1 / Sketch3)    7.750 .. 9.500in off centre   <- moved with the edge
    chassis notch  (Cut-Extrude7 / Sketch53)   5.000 .. 6.750in              <- never moved
    chassis tab    (Boss-Extrude2 / Sketch35)  5.062 .. 6.688in              <- never moved

2.750in a side out, exactly half the 5.500in the hanger grew. `follower_dims: []` and
`offsetRules=0`: the product had no stored link, because its 9.500in row inset is nowhere near the
4.25in window.

`test_tab_row_found_by_slot_length.py` covers finding the ROW (`D2@Sketch53`). This file covers
what that alone left broken on the 24x48 chassis: the tabs carry their OWN spacing,
`D1@Sketch35` = 10.125in between their inner edges (1/16in inside the 10.000in notch edges), so a
row-only link moves the notches out from under tabs that stay put — reported 2026-09-21 as "the
chassis tab on 74 x 98 is not inside the hanger slot".

Both sketches are symmetric about the chassis centreline (4 symmetric relations each, read live),
so writing a span moves both sides equally.
"""

import json
import os
import sys
import types
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.rules import generate_rules
from engine.hangers import hanger_select
from engine.resize import interpret
from engine.core import models
from engine.rules import rules

IN = 0.0254

MIRROR_W = "D1@Sketch1 [AMY-24.00X48.00-1]"
MIRROR_H = "D2@Sketch1 [AMY-24.00X48.00-1]"
CHASSIS_W = "D1@Sketch1 [12213-CHASSIS-1]"
CHASSIS_H = "D2@Sketch1 [12213-CHASSIS-1]"
HANGER_W = "D2@Base-Flange1 [12214-HANGER-1]"
HANGER_H = "D1@Sketch1 [12214-HANGER-1]"
ROW = "D2@Sketch53 [12213-CHASSIS-1]"        # Cut-Extrude7: notch outer-edge span
TAB = "D1@Sketch35 [12213-CHASSIS-1]"        # Boss-Extrude2: tab inner-edge span

SLOT_INSET_IN = 4.750        # hanger slot outer end, in from the hanger edge (DXF + live)
SLOT_LEN_IN = 1.750
TAB_W_IN = 1.625

# AMY 24x48 as the client authored it, read from the 2026-09-25 18:20 Refresh.
AMY_24X48 = {
    MIRROR_W: 24.000 * IN, MIRROR_H: 48.000 * IN,
    "D2@Sketch2 [AMY-24.00X48.00-1]": 20.000 * IN,
    CHASSIS_W: 23.000 * IN, CHASSIS_H: 46.000 * IN,
    "D2@Sheet-Metal1 [12213-CHASSIS-1]": 19.685 * IN,
    "D7@Edge-Flange1 [12213-CHASSIS-1]": 1.375 * IN,
    "D1@Sketch53 [12213-CHASSIS-1]": 1.750 * IN,
    ROW: 13.500 * IN,
    "D3@Sketch53 [12213-CHASSIS-1]": 0.500 * IN,
    "D1@Cut-Extrude7 [12213-CHASSIS-1]": 0.500 * IN,
    TAB: 10.125 * IN,
    "D3@Sketch35 [12213-CHASSIS-1]": 1.625 * IN,
    "D2@Sketch35 [12213-CHASSIS-1]": 0.375 * IN,
    "D1@Sketch36 [12213-CHASSIS-1]": 0.250 * IN,
    "D4@Sketch36 [12213-CHASSIS-1]": 1.000 * IN,
    "D3@Sketch36 [12213-CHASSIS-1]": 20.000 * IN,
    "D2@Hole Thread16 [12213-CHASSIS-1]": 0.190 * IN,
    "D2@Sketch52 [12213-CHASSIS-1]": 5.000 * IN,
    "D3@Sketch52 [12213-CHASSIS-1]": 7.500 * IN,
    # Sketch54: the nearest decoy. 1.500in is a small feature, and 6.750in sits well outside
    # the row's footprint.
    "D1@Sketch54 [12213-CHASSIS-1]": 4.000 * IN,
    "D3@Sketch54 [12213-CHASSIS-1]": 6.750 * IN,
    "D5@Sketch54 [12213-CHASSIS-1]": 3.375 * IN,
    "D6@Sketch54 [12213-CHASSIS-1]": 1.500 * IN,
    HANGER_W: 23.000 * IN, HANGER_H: 17.000 * IN,
    "D2@Sheet-Metal1 [12214-HANGER-1]": 17.5866 * IN,
    "D1@Sketch3 [12214-HANGER-1]": 1.750 * IN,        # the slot
    "D2@Sketch3 [12214-HANGER-1]": SLOT_INSET_IN * IN,
    "D3@Sketch3 [12214-HANGER-1]": 0.1562 * IN,
    "D4@Sketch5 [12214-HANGER-1]": 4.000 * IN,
    "D1@Sketch5 [12214-HANGER-1]": 1.000 * IN,
}
AXIS = {MIRROR_W: "W", MIRROR_H: "H", CHASSIS_W: "W", CHASSIS_H: "H",
        HANGER_W: "W", HANGER_H: "H", "D2@Sheet-Metal1 [12214-HANGER-1]": "H"}


def _gen_req(dims):
    return models.GenerateRulesRequest(
        assembly_context="",
        dimensions=[models.DimensionIn(name=n, value_meters=v) for n, v in dims.items()],
        dim_axis_labels=AXIS)


def _links(dims=AMY_24X48):
    skip = []
    return generate_rules._tab_spacing_offset(_gen_req(dims), skip), skip


# ── generation ────────────────────────────────────────────────────────────────────────────

def test_the_inset_window_misses_amy_24x48_too():
    assert hanger_select.find_tab_spacing_candidates(AMY_24X48, None, 23.000 * IN) == []


def test_generation_stores_the_row_AND_the_tabs():
    out, skip = _links()
    assert [r.target_dim for r in out] == [ROW, TAB], "row first — the clip alignment reads it"
    assert all(r.source_dim == HANGER_W for r in out)
    assert out[0].offset_meters == pytest.approx(-9.500 * IN)     # 23.000 - 13.500
    assert out[1].offset_meters == pytest.approx(-12.875 * IN)    # 23.000 - 10.125
    assert not any("WARNING" in s.reason for s in skip)


def test_the_aligned_model_agrees_with_the_hangers_own_slot_sketch():
    """2 x D2@Sketch3 (4.750in) is the row inset (9.500in): that is what makes this trustworthy."""
    assert 9.500 in [round(v, 3) for v in hanger_select.hanger_slot_row_insets(AMY_24X48)]


def test_the_decoys_on_the_real_chassis_are_not_members():
    scan = hanger_select.find_tab_stack_members(
        AMY_24X48, hanger_select.find_tab_spacing_by_slot_row(AMY_24X48, 23 * IN)[0], 23 * IN)
    assert [m.dim for m in scan.members] == [TAB]
    assert scan.relation_placed == []


def test_a_tab_sketch_with_no_span_is_left_to_its_relations():
    """AMY 36x36's Sketch35 is an outline tied to the notch: 1.625 x 0.375 x 0.0625in."""
    dims = dict(AMY_24X48)
    dims[TAB] = 0.0625 * IN
    out, _ = _links(dims)
    assert [r.target_dim for r in out] == [ROW]


def test_a_tab_width_with_a_span_OUTSIDE_the_footprint_is_not_a_member():
    dims = dict(AMY_24X48)
    dims["D1@Sketch99 [12213-CHASSIS-1]"] = 1.625 * IN
    dims["D2@Sketch99 [12213-CHASSIS-1]"] = 20.000 * IN
    assert [r.target_dim for r in _links(dims)[0]] == [ROW, TAB]


def test_a_feature_well_under_the_slot_width_is_not_a_tab():
    """1.500in leaves 0.250in of play in a 1.750in slot — that is not a seated tab."""
    dims = dict(AMY_24X48)
    dims["D1@Sketch98 [12213-CHASSIS-1]"] = 1.500 * IN
    dims["D2@Sketch98 [12213-CHASSIS-1]"] = 11.000 * IN      # inside the footprint on purpose
    assert [r.target_dim for r in _links(dims)[0]] == [ROW, TAB]


def test_a_member_on_ANOTHER_chassis_part_is_not_this_rows():
    dims = dict(AMY_24X48)
    dims["D1@Sketch35 [99999-CHASSIS-TOP-1]"] = 11.000 * IN
    dims["D3@Sketch35 [99999-CHASSIS-TOP-1]"] = 1.625 * IN
    assert [r.target_dim for r in _links(dims)[0]] == [ROW, TAB]


def test_products_on_the_inset_window_get_no_extra_members():
    """AMBER/KELLY/PIAZZA are verified live with the row alone; this change must not touch them."""
    amber = {
        "D2@Base-Flange1 [1038-HANGER-1]": 20.000 * IN,
        "D1@Sketch81 [1111-CHASSIS-2]": 15.750 * IN,
        "D3@Sketch81 [1111-CHASSIS-2]": 1.750 * IN,
        "D1@Sketch99 [1111-CHASSIS-2]": 12.500 * IN,     # would pass both member tests
        "D2@Sketch99 [1111-CHASSIS-2]": 1.625 * IN,
    }
    req = models.GenerateRulesRequest(
        assembly_context="",
        dimensions=[models.DimensionIn(name=n, value_meters=v) for n, v in amber.items()],
        dim_axis_labels={"D2@Base-Flange1 [1038-HANGER-1]": "W"})
    out = generate_rules._tab_spacing_offset(req, [])
    assert [r.target_dim for r in out] == ["D1@Sketch81 [1111-CHASSIS-2]"]


def test_generating_on_an_ALREADY_MISALIGNED_model_warns():
    """Generate Rules pressed on the 44x68 result: row 15.000in inside a hanger whose slots say
    9.500in. The link is still stored — nothing here can say which number is right — but the
    rules UI is told, because every later resize would preserve the misalignment."""
    dims = dict(AMY_24X48)
    dims[HANGER_W] = 28.500 * IN
    out, skip = _links(dims)
    assert out[0].offset_meters == pytest.approx(-15.000 * IN)
    assert any("WARNING" in s.reason and s.name == ROW for s in skip)


# ── resize ────────────────────────────────────────────────────────────────────────────────

def _stack_rules(order=(ROW, TAB)):
    off = {ROW: -9.500 * IN, TAB: -12.875 * IN}
    return types.SimpleNamespace(offset=[types.SimpleNamespace(
        target_dim=d, source_dim=HANGER_W, offset_meters=off[d]) for d in order])


def _follow(new_w_in, to_prefab=False, rules_=None, dims=AMY_24X48, part="", old_w_in=23.000):
    req = models.InterpretRequest(
        instruction="", shape="rect",
        dimensions=[models.DimensionIn(name=n, value_meters=v) for n, v in dims.items()])
    out = interpret._hanger_follower_updates(
        req, old_w_in * IN, (new_w_in - old_w_in) * IN, rules_ or _stack_rules(), to_prefab, part)
    return {d: v / IN for d, v, _w, _a in out}, [d for d, *_ in out]


# The catalogue hangers' slots, measured 2026-09-25 by opening each part read-only: the slot's
# outer end sits D2@Sketch3 in from the edge — 0.375in on all of them, 0.380in on #1215.
def _prefab_outer_span(part, width_in):
    return width_in - 2 * hanger_select.prefab_slot_edge_inset_in(part)


def _seats(new_w_in, vals):
    """The physical check, in half-spans off the centreline: the notch's outer edge is the slot's
    outer end, and the whole tab lies inside the slot."""
    slot_outer = new_w_in / 2 - SLOT_INSET_IN
    slot_inner = slot_outer - SLOT_LEN_IN
    tab_inner = vals[TAB] / 2
    return (vals[ROW] / 2 == pytest.approx(slot_outer)
            and slot_inner <= tab_inner and tab_inner + TAB_W_IN <= slot_outer)


def test_the_live_stretch_23_to_28_5_puts_notches_and_tabs_on_the_slots():
    vals, order = _follow(28.500)
    assert vals[ROW] == pytest.approx(19.000)     # notch outer ends at 9.500in = slot outer ends
    assert vals[TAB] == pytest.approx(15.625)     # tabs 7.8125 .. 9.4375in inside 7.750 .. 9.500
    assert order == [ROW, TAB]
    assert _seats(28.500, vals)


@pytest.mark.parametrize("new_w", [16.0, 23.0, 28.5, 35.0, 47.0, 59.0, 74.0])
def test_every_stretch_keeps_the_stack_seated(new_w):
    """Including the client's own 35/47/59in AMY hangers, which carry the same 4.750in inset."""
    vals, _ = _follow(new_w)
    assert _seats(new_w, vals)


def test_the_moves_are_equal_not_proportional():
    """"Adjust proportionally" in the ticket means WITH the slots. A ratio would not seat: the
    tab spacing times 28.5/23 is 12.546in, still 1.5in a side inside a slot that moved 2.75in."""
    vals, _ = _follow(28.500)
    assert vals[ROW] - 13.500 == pytest.approx(5.500)
    assert vals[TAB] - 10.125 == pytest.approx(5.500)


def test_a_kept_hanger_moves_nothing():
    vals, _ = _follow(23.000)
    assert vals[ROW] == pytest.approx(13.500)
    assert vals[TAB] == pytest.approx(10.125)


def test_a_PREFAB_swap_moves_the_whole_stack_onto_the_prefabs_slots():
    """23in custom -> 40in #1215, whose slots end 0.380in in from its edge: the notch row is the
    slots' OUTER span, 39.240in, and the tabs keep their 3.375in inside the notches.

    NOT the catalogue 4.25in, which is the INNER span — the datum AMBER/KELLY dimension to. Using
    it for AMY's outer-span row was the 2026-09-25 shrink bug (row at 10.000in on #1119)."""
    vals, _ = _follow(40.000, to_prefab=True, part="1215")
    assert vals[ROW] == pytest.approx(39.240)
    assert vals[TAB] == pytest.approx(39.240 - 3.375)
    assert vals[ROW] - vals[TAB] == pytest.approx(13.500 - 10.125)


def test_the_live_74x98_to_24x48_shrink_lands_on_1119s_slots():
    """The run that broke. State before it: the bespoke hanger stretched to 48.000in (slots still
    4.750in in), row 38.500in, tabs 35.125in — aligned. The shrink swaps in #1119 (14.25in), whose
    slots were measured live in the assembly at 5.000 .. 6.750in off centre. Row 13.500in puts the
    notches exactly there; tabs 10.125in put them 5.0625 .. 6.6875in — the client's own AMY 24x48
    values, recovered by arithmetic."""
    at_74 = dict(AMY_24X48)
    at_74[HANGER_W] = 48.000 * IN
    at_74[ROW] = 38.500 * IN
    at_74[TAB] = 35.125 * IN
    for rules_ in (_stack_rules(), _NO_LINKS):
        vals, order = _follow(14.250, to_prefab=True, rules_=rules_, dims=at_74, part="1119",
                              old_w_in=48.000)
        assert order == [ROW, TAB]
        assert vals[ROW] == pytest.approx(13.500)
        assert vals[TAB] == pytest.approx(10.125)


def test_the_NEXT_resize_on_a_swapped_in_prefab_uses_the_prefabs_slots():
    """After that swap the fitted hanger is #1119 (D2@Sketch3 = 0.375in), but the stored links
    still carry offsets measured on the 23in bespoke hanger. A width offset would send the row to
    14.25 - 9.5 = 4.75in on a kept hanger; lining up with the slots keeps it at 13.500in."""
    on_1119 = dict(AMY_24X48)
    on_1119[HANGER_W] = 14.250 * IN
    on_1119["D2@Sketch3 [12214-HANGER-1]"] = 0.375 * IN
    vals, _ = _follow(14.250, dims=on_1119, old_w_in=14.250)
    assert vals[ROW] == pytest.approx(13.500)
    assert vals[TAB] == pytest.approx(10.125)


def test_the_row_is_found_even_if_the_file_lists_the_tab_first():
    vals, order = _follow(28.500, rules_=_stack_rules(order=(TAB, ROW)))
    assert order == [ROW, TAB]
    vals_p, _ = _follow(40.000, to_prefab=True, rules_=_stack_rules(order=(TAB, ROW)),
                        part="1215")
    assert vals_p[ROW] == pytest.approx(39.240)


def test_the_mounting_slot_follower_never_touches_the_notch_width():
    """Live 2026-09-25: the app reported `Cut-Extrude7`'s notches as the chassis mounting slots
    ("2 slots of 1.750in ... driven by D1@Sketch53") and the slot follower cut them to 0.500in on
    the 74x98 -> 24x48 shrink."""
    at_74 = dict(AMY_24X48)
    at_74[CHASSIS_W] = 73.000 * IN
    req = models.InterpretRequest(
        instruction="", shape="rect",
        dimensions=[models.DimensionIn(name=n, value_meters=v) for n, v in at_74.items()],
        dim_axis_labels=AXIS,
        slot_rows=[models.SlotRowIn(
            dim="D1@Sketch53 [12213-CHASSIS-1]", length_meters=1.750 * IN, count=2,
            slot_width_meters=0.500 * IN, inset_meters=17.250 * IN,
            part_width_meters=73.000 * IN, component="12213-CHASSIS-1")])
    out = interpret._slot_follower_updates(
        req, [models.DimensionChange(name=CHASSIS_W, value_meters=23.000 * IN)])
    assert out == []


def test_the_stack_moves_together_or_not_at_all():
    """A shrink that would take the tabs through zero must not move the notches alone."""
    vals, order = _follow(12.000)     # tab: 12 - 12.875 < 0
    assert vals == {} and order == []


def test_links_survive_the_rename_the_app_does_after_every_save():
    """`[RENAME] 12213-CHASSIS.SLDPRT -> 1111-CHASSIS.SLDPRT` — the stored names go stale."""
    renamed = {k.replace("12213-CHASSIS", "1111-CHASSIS"): v for k, v in AMY_24X48.items()}
    vals, order = _follow(28.500, dims=renamed)
    assert order == ["D2@Sketch53 [1111-CHASSIS-1]", "D1@Sketch35 [1111-CHASSIS-1]"]
    assert vals["D1@Sketch35 [1111-CHASSIS-1]"] == pytest.approx(15.625)


# ── a rule set with NO stored link: the stack is read off the live model ─────────────────
#
# Live 2026-09-25 19:03: the user reconnected and resized AMY 24x48 -> 54x68 on the rule set
# saved at 18:22 (`offsetRules=0`) without regenerating it, so the stored-link fix had nothing
# to read and the tabs stayed at 13.500 / 10.125in beside 35in hanger slots. The stack is now
# read off the model — but only once the hanger's own slot sketch proves it is aligned.

_NO_LINKS = types.SimpleNamespace(offset=[])


def test_without_a_stored_link_the_live_stack_is_used():
    vals, order = _follow(35.000, rules_=_NO_LINKS)
    assert order == [ROW, TAB]
    assert vals[ROW] == pytest.approx(25.500)     # what the client's own AMY 36x36 carries
    assert vals[TAB] == pytest.approx(22.125)     # proven in SolidWorks: 11.062 .. 12.688in
    assert _seats(35.000, vals)


def test_without_a_stored_link_a_MISALIGNED_model_is_left_alone():
    """The 44x68 result: hanger already 28.5in, stack still 13.5/10.125. Offsets measured off
    it would preserve the misalignment, and the hanger's 4.750in slot dim says so."""
    dims = dict(AMY_24X48)
    dims[HANGER_W] = 28.500 * IN
    req = models.InterpretRequest(
        instruction="", shape="rect",
        dimensions=[models.DimensionIn(name=n, value_meters=v) for n, v in dims.items()])
    assert interpret._hanger_follower_updates(req, 28.5 * IN, 6.5 * IN, _NO_LINKS) == []


def test_without_a_stored_link_or_a_slot_position_nothing_is_guessed():
    dims = {k: v for k, v in AMY_24X48.items() if k != "D2@Sketch3 [12214-HANGER-1]"}
    vals, order = _follow(35.000, rules_=_NO_LINKS, dims=dims)
    assert order == []


def test_without_a_stored_link_a_prefab_swap_still_lands_on_its_slots():
    vals, _ = _follow(40.000, to_prefab=True, rules_=_NO_LINKS, part="1215")
    assert vals[ROW] == pytest.approx(39.240)
    assert vals[TAB] == pytest.approx(39.240 - 3.375)


# ── end to end through interpret() ────────────────────────────────────────────────────────

@pytest.fixture(params=["stored links", "no links (not regenerated)"])
def amy_rules(tmp_path, monkeypatch, request):
    out, _ = _links() if request.param == "stored links" else ([], [])
    doc = {
        "model": "AmyTest",
        "width": [{"if_changes": MIRROR_W, "also_change": [CHASSIS_W]}],
        "height": [{"if_changes": MIRROR_H, "also_change": [CHASSIS_H]}],
        "component_labels": {"12214-HANGER-1": "Hanger", "12213-CHASSIS-1": "Chassis",
                             "AMY-24.00X48.00-1": "Mirror Glass"},
        "offset": [r.model_dump() for r in out],
    }
    d = tmp_path / "rules"
    d.mkdir()
    (d / "AmyTest.rules.json").write_text(json.dumps(doc))
    monkeypatch.setattr(rules, "RULES_DIR", d)
    return tmp_path


async def _resize(root, master, also, target_in):
    llm = json.dumps({"rule": {"if_changes": master, "also_change": [also]},
                      "value_meters": target_in * IN, "scope": "overall"})
    with patch("engine.resize.interpret.call_llm", new=AsyncMock(return_value=llm)):
        return await interpret.interpret(models.InterpretRequest(
            instruction=f"resize to {target_in}",
            dimensions=[models.DimensionIn(name=n, value_meters=v) for n, v in AMY_24X48.items()],
            dim_axis_labels=AXIS,
            master_width_dim=MIRROR_W, master_height_dim=MIRROR_H,
            model_path=str(root / "AmyTest.SLDASM"),
        ))


@pytest.mark.asyncio
@pytest.mark.parametrize("target", [30.0, 44.0, 60.0])
async def test_a_real_width_resize_carries_the_tabs_with_the_hanger(amy_rules, target):
    res = await _resize(amy_rules, MIRROR_W, CHASSIS_W, target)
    assert res.error is None, res.error
    by = {c.name: c.value_meters / IN for c in res.changes}
    h = res.hanger
    assert h is not None
    new_w = (h.target_width_meters / IN if h.replace
             else by.get(HANGER_W, AMY_24X48[HANGER_W] / IN))
    if abs(new_w - 23.000) < 1e-6:
        assert ROW not in by and TAB not in by       # hanger did not move -> nothing to do
        return
    expected_row = (_prefab_outer_span(h.part, new_w) if h.replace else new_w - 9.500)
    assert by[ROW] == pytest.approx(expected_row)
    assert by[TAB] == pytest.approx(by[ROW] - 3.375)
    assert h.follower_dims[:2] == [ROW, TAB]
