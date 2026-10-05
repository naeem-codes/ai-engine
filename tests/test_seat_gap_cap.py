"""The hanging bracket keeps its as-built distance from the glass's sides, via the HANGER.

Live 2026-09-29, SUZI 24x36 -> 44x56. #1215 (40") was chosen; the bracket, whose end tabs drop into
slots near the hanger's ends, followed it to 39.125" and ran into the chassis corner (+-19.500") and
the LED strip drawn off it. As built the bracket is 13.375" inside a 14.250" hanger, 5.3125" from
each glass edge. User: "mark the original distance of the hanger bracket from the left and right
side ... it should still keep the same distance even after resize".

Same run, second fault: `D1@Sketch20` on the chassis corner (two 0.250 x 0.250" cut-outs) was
"lengthened" like a mounting slot to 0.500" and broke the LED strip's in-context sketch plus two
mates. A row of holes keeps its size.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest

from engine.resize import chassis_slots
from engine.resize import interpret
from engine.rules import rules
from engine.hangers.hanger_select import select_hanger
from engine.core.models import DimensionIn, InterpretRequest, SeatGapIn, SlotRowIn
from engine.resize.interpret import interpret as run_interpret

IN = 0.0254

MIRROR_W = "D1@Sketch1 [SUZI-MIRROR-1]"
MIRROR_H = "D2@Sketch1 [SUZI-MIRROR-1]"
CHASSIS_W = "D1@Sketch1 [12476-CHASSIS-5]"
CHASSIS_H = "D2@Sketch1 [12476-CHASSIS-5]"
HANGER_W = "D2@Base-Flange1 [12477-HANGER-1]"
HANGER_H = "D1@Sketch1 [12477-HANGER-1]"
BRACKET_W = "D2@Base-Flange1 [1047-HANGING-BRACKET-1]"
BRACKET = "1047-HANGING-BRACKET-1"
AXIS = {MIRROR_W: "W", MIRROR_H: "H", CHASSIS_W: "W", CHASSIS_H: "H",
        HANGER_W: "W", HANGER_H: "H", BRACKET_W: "W"}
BUILT = 5.3125          # (24 - 13.375) / 2


def _req(glass_w, hanger_w, bracket_w, gap_now, built=BUILT, **kw):
    dims = {MIRROR_W: glass_w, MIRROR_H: 36.0, HANGER_W: hanger_w, HANGER_H: 16.0,
            BRACKET_W: bracket_w, CHASSIS_W: glass_w - 0.25}
    return InterpretRequest(
        instruction="x", master_width_dim=MIRROR_W, master_height_dim=MIRROR_H,
        dimensions=[DimensionIn(name=n, value_meters=v * IN) for n, v in dims.items()],
        dim_axis_labels=AXIS,
        seat_gaps=[SeatGapIn(component=BRACKET, left_meters=gap_now * IN,
                             right_meters=gap_now * IN, built_left_meters=built * IN,
                             built_right_meters=built * IN)], **kw)


def test_cap_straight_from_the_as_built_model():
    """24 -> 44 in one step: the bracket is still where it was built, so the hanger may grow by
    exactly the glass's growth — 14.250 + 20 = 34.250"."""
    cap = interpret._seat_gap_cap(_req(24, 14.25, 13.375, BUILT), 14.25 * IN, 44 * IN)
    assert cap == pytest.approx(34.25)


def test_cap_is_the_same_after_an_intermediate_resize():
    """24 -> 34 -> 44: at 34 the bracket is 19.125" inside #1038 (20"), 7.4375" from each edge.
    The limit at 44 must not depend on the route taken to get there."""
    cap = interpret._seat_gap_cap(_req(34, 20.0, 19.125, 7.4375), 20.0 * IN, 44 * IN)
    assert cap == pytest.approx(34.25)


def test_no_measurement_means_no_cap():
    req = _req(24, 14.25, 13.375, BUILT).model_copy(update={"seat_gaps": []})
    assert interpret._seat_gap_cap(req, 14.25 * IN, 44 * IN) == 0.0


def test_the_40in_prefab_is_ruled_out_and_the_fitted_hanger_is_stretched():
    today = select_hanger(44, 56, fitted_w_in=14.25, fitted_h_in=16, chassis_w_in=43.75)
    assert today.part == "1215", "precondition: what was chosen live"
    capped = select_hanger(44, 56, fitted_w_in=14.25, fitted_h_in=16, chassis_w_in=43.75,
                           seat_cap_w_in=34.25)
    assert capped.part is None and capped.resize_fitted
    bracket = capped.target_width_in - 0.875
    assert (44 - bracket) / 2 >= BUILT
    assert (capped.target_width_in, capped.target_height_in) == (28.5, 19.5)


@pytest.mark.parametrize("glass_w, glass_h, part", [(34, 46, "1038"), (24, 36, "1119")])
def test_sizes_that_already_worked_are_unchanged(glass_w, glass_h, part):
    cap = glass_w - 2 * BUILT + 0.875
    a = select_hanger(glass_w, glass_h, fitted_w_in=14.25, fitted_h_in=16,
                      chassis_w_in=glass_w - 0.25)
    b = select_hanger(glass_w, glass_h, fitted_w_in=14.25, fitted_h_in=16,
                      chassis_w_in=glass_w - 0.25, seat_cap_w_in=cap)
    assert a.part == b.part == part


def test_a_capped_custom_width_snaps_down_not_past_the_cap():
    """Rounding to the nearest 1/4" used to push a capped width over the cap, which then threw
    the resized hanger away. 30.1" of room must give 30.0", not fail."""
    c = select_hanger(60, 30, fitted_w_in=20, fitted_h_in=15, chassis_w_in=59.75,
                      seat_cap_w_in=30.1)
    assert c.resize_fitted and c.target_width_in <= 30.1


@pytest.fixture
def suzi_rules(tmp_path, monkeypatch):
    doc = {"model": "SuziTest",
           "width": [{"if_changes": MIRROR_W, "also_change": [CHASSIS_W, BRACKET_W]}],
           "height": [{"if_changes": MIRROR_H, "also_change": [CHASSIS_H]}],
           "component_labels": {BRACKET: "Hanging Bracket", "12477-HANGER-1": "Mirror Hanger"}}
    d = tmp_path / "rules"
    d.mkdir()
    (d / "SuziTest.rules.json").write_text(json.dumps(doc))
    monkeypatch.setattr(rules, "RULES_DIR", d)
    return tmp_path


@pytest.mark.asyncio
async def test_end_to_end_the_bracket_keeps_its_distance_from_the_sides(suzi_rules):
    dims = {MIRROR_W: 24, MIRROR_H: 36, CHASSIS_W: 23.75, CHASSIS_H: 35.75,
            HANGER_W: 14.25, HANGER_H: 16, BRACKET_W: 13.375}
    llm = json.dumps({"rule": {"if_changes": MIRROR_W, "also_change": [CHASSIS_W, BRACKET_W]},
                      "value_meters": 44 * IN, "other_axis_meters": 56 * IN, "scope": "overall"})
    with patch("engine.resize.interpret.call_llm", new=AsyncMock(return_value=llm)):
        res = await run_interpret(InterpretRequest(
            instruction="Resize to 44 x 56",
            dimensions=[DimensionIn(name=n, value_meters=v * IN) for n, v in dims.items()],
            dim_axis_labels=AXIS, master_width_dim=MIRROR_W, master_height_dim=MIRROR_H,
            model_path=str(suzi_rules / "SuziTest.SLDASM"),
            seat_gaps=[SeatGapIn(component=BRACKET, left_meters=BUILT * IN,
                                 right_meters=BUILT * IN, built_left_meters=BUILT * IN,
                                 built_right_meters=BUILT * IN)]))
    assert res.error is None, res.error
    assert res.hanger.part != "1215"
    by = {c.name: c.value_meters / IN for c in res.changes}
    bracket = by[BRACKET_W]
    assert bracket == pytest.approx(res.hanger.target_width_meters / IN - 0.875)
    assert (44 - bracket) / 2 >= BUILT - 1e-9


# ── holes are not slots ───────────────────────────────────────────────────────

def _row(length_in, width_in, dim="D1@Sketch20 [12473-CHASSIS CORNERA-1]",
         comp="12473-CHASSIS CORNERA-1", part_w=19.0, inset=0.125):
    return SlotRowIn(dim=dim, length_meters=length_in * IN, count=2,
                     slot_width_meters=width_in * IN, inset_meters=inset * IN,
                     part_width_meters=part_w * IN, component=comp)


def test_square_cutouts_and_dimple_holes_are_holes():
    assert chassis_slots.is_hole_row(_row(0.25, 0.25))
    assert chassis_slots.is_hole_row(_row(0.145, 0.145))
    assert chassis_slots.spec_from_measurement(_row(0.25, 0.25)) is None


def test_real_mounting_slots_are_still_slots():
    for length, width in [(6.0, 0.28), (7.5, 0.28), (4.0, 0.28)]:
        assert not chassis_slots.is_hole_row(_row(length, width))
        assert chassis_slots.spec_from_measurement(_row(length, width)) is not None


def test_the_corner_cutouts_are_not_lengthened_on_resize():
    """24 -> 44: the chassis slots still grow, the corner's cut-outs keep 0.250"."""
    corner_w = "D1@Sketch1 [12473-CHASSIS CORNERA-1]"
    chassis_slot = "D8@Sketch108 [12476-CHASSIS-5]"
    req = InterpretRequest(
        instruction="x",
        dimensions=[DimensionIn(name=corner_w, value_meters=19.0 * IN),
                    DimensionIn(name="D1@Sketch20 [12473-CHASSIS CORNERA-1]", value_meters=0.25 * IN),
                    DimensionIn(name=CHASSIS_W, value_meters=23.75 * IN),
                    DimensionIn(name=chassis_slot, value_meters=6.0 * IN)],
        dim_axis_labels={corner_w: "W", CHASSIS_W: "W"},
        slot_rows=[_row(0.25, 0.25),
                   _row(6.0, 0.28, dim=chassis_slot, comp="12476-CHASSIS-5", part_w=23.75,
                        inset=4.235)])
    from engine.core.models import DimensionChange
    out = {d: v for d, v, _cur in interpret._slot_follower_updates(
        req, [DimensionChange(name=corner_w, value_meters=39.0 * IN),
              DimensionChange(name=CHASSIS_W, value_meters=43.75 * IN)])}
    assert "D1@Sketch20 [12473-CHASSIS CORNERA-1]" not in out
    assert chassis_slot in out and out[chassis_slot] > 6.0 * IN
