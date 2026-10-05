"""Hanger re-selection wired into interpret(): it must fire on any size change and never be
blocked by the fixed-size policy that guards it.

Two outcomes, and the split matters:
  * a CATALOGUE PREFAB is chosen -> `hanger.replace` is set and NO hanger dimension is written.
    The app swaps the component for the real .SLDPRT out of its HANGERS library, so the size
    arrives as a different file. The chassis tab follower must still fire, driven off the
    prefab's catalogue width rather than off a dimension write that no longer exists.
  * NO PREFAB QUALIFIES -> `hanger.resize_fitted`, and the fitted hanger is stretched through
    `changes` exactly as before, because there is no file to swap in.
"""

import json
import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from engine.rules import rules
from engine.hangers.hanger_select import OBSTACLE_CLEARANCE_IN, select_hanger
from engine.resize.interpret import interpret
from engine.core.models import DimensionIn, InterpretRequest

IN = 0.0254
MIRROR_W = "WIDTH@Sketch1 [1011-MIRROR-CAROL-1]"
MIRROR_H = "HEIGHT@Sketch1 [1011-MIRROR-CAROL-1]"
CHASSIS_H = "D2@Sketch1 [12204-CHASSIS-2]"
HANGER_W = "D2@Base-Flange1 [1038-HANGER-1]"
HANGER_H = "D1@Sketch1 [1038-HANGER-1]"
HANGER_MINOR = "D1@Sketch3 [1038-HANGER-1]"      # small hanger dim — must NOT be chosen
# Flat-pattern / bend metadata that is LARGER than the real height driver — must be ignored.
HANGER_SHEETMETAL = "D2@Sheet-Metal1 [1038-HANGER-1]"
TAB_SPACING = "D1@Sketch81 [12204-CHASSIS-2]"    # chassis HANGING TAB spacing
TAB_WIDTH = "D3@Sketch81 [12204-CHASSIS-2]"      # tab WIDTH — same sketch, must be skipped

# Real AMBER 36x36 values.
DIMS = {
    MIRROR_W: 36 * IN, MIRROR_H: 36 * IN, CHASSIS_H: 34 * IN,
    HANGER_W: 20 * IN, HANGER_H: 15 * IN, HANGER_MINOR: 1.75 * IN,
    HANGER_SHEETMETAL: 17.5866 * IN,          # 446.70 mm — larger than the 15" driver
    TAB_SPACING: 15.75 * IN, TAB_WIDTH: 1.75 * IN,
}
AXIS = {MIRROR_W: "W", MIRROR_H: "H", CHASSIS_H: "H",
        HANGER_W: "W", HANGER_H: "H", HANGER_MINOR: "H",
        HANGER_SHEETMETAL: "H",
        TAB_SPACING: "W", TAB_WIDTH: "W"}


@pytest.fixture
def rules_dir(tmp_path, monkeypatch):
    doc = {
        "model": "AmberTest",
        "width": [{"if_changes": MIRROR_W, "also_change": []}],
        "height": [{"if_changes": MIRROR_H, "also_change": [CHASSIS_H]}],
        "component_labels": {"1038-HANGER-1": "Mirror Hanger",
                             "1011-MIRROR-CAROL-1": "Mirror Glass",
                             "12204-CHASSIS-2": "Main Chassis"},
    }
    d = tmp_path / "rules"
    d.mkdir()
    (d / "AmberTest.rules.json").write_text(json.dumps(doc))
    monkeypatch.setattr(rules, "RULES_DIR", d)
    return tmp_path


async def _resize(rules_dir, if_changes, target_in, dims=None, axis=None, also=None):
    llm = json.dumps({
        "rule": {"if_changes": if_changes,
                 "also_change": also if also is not None else (
                     [CHASSIS_H] if if_changes == MIRROR_H else [])},
        "value_meters": target_in * IN,
        "scope": "overall",
    })
    with patch("engine.resize.interpret.call_llm", new=AsyncMock(return_value=llm)):
        return await interpret(InterpretRequest(
            instruction=f"resize to {target_in}",
            dimensions=[DimensionIn(name=n, value_meters=v)
                        for n, v in (dims or DIMS).items()],
            dim_axis_labels=axis or AXIS,
            master_width_dim=MIRROR_W, master_height_dim=MIRROR_H,
            model_path=str(rules_dir / "AmberTest.SLDASM"),
        ))


# ── height change: 36x36 -> 36x48 must re-select #1038 -> #1333 ───────────────

@pytest.mark.asyncio
async def test_height_change_reselects_the_hanger(rules_dir):
    # 36x36 -> 36x48. #1038 drops to 17.4% (under the 18% floor) and #1333 spans only 39.6%
    # of the width, so no prefab qualifies and the fitted hanger is scaled instead.
    # (Under the old AREA-only rule this picked #1333 at 14.25x24 — see
    # test_36x48_no_longer_takes_the_narrow_1333.)
    res = await _resize(rules_dir, MIRROR_H, 48)
    assert res.error is None
    assert res.hanger is not None
    assert res.hanger.resize_fitted is True
    by_name = {c.name: c.value_meters for c in res.changes}
    assert by_name[HANGER_W] / IN >= 0.55 * 36
    assert by_name[HANGER_H] / IN <= 0.60 * 48


@pytest.mark.asyncio
async def test_hanger_write_survives_the_fixed_size_policy_guard(rules_dir):
    # The hanger is in FIXED_SIZE so no RULE can scale it; the selector is the one
    # sanctioned path and its writes must not be stripped by _enforce_policy.
    res = await _resize(rules_dir, MIRROR_H, 48)
    assert HANGER_W in {c.name for c in res.changes}
    assert HANGER_H in {c.name for c in res.changes}


@pytest.mark.asyncio
async def test_only_the_outer_hanger_dims_are_written(rules_dir):
    res = await _resize(rules_dir, MIRROR_H, 48)
    assert HANGER_MINOR not in {c.name for c in res.changes}
    assert res.hanger.width_dim == HANGER_W
    assert res.hanger.height_dim == HANGER_H


@pytest.mark.asyncio
async def test_sheet_metal_flat_dims_are_never_the_hanger_driver(rules_dir):
    """Live regression (AMY, 2026-08-03): D2@Sheet-Metal1 (446.70 mm) is LARGER than the real
    height driver (431.80 mm), so "largest [H] dim" wrote the prefab height onto a
    flat-pattern dim. @Sheet-Metal dims are bend metadata, never an outer size."""
    res = await _resize(rules_dir, MIRROR_H, 48)
    assert res.hanger.height_dim == HANGER_H
    assert HANGER_SHEETMETAL not in {c.name for c in res.changes}


# ── A-2: a WIDTH-only change must also re-select (area changes either way) ────

@pytest.mark.asyncio
async def test_width_only_change_also_reselects(rules_dir):
    # 36x36 -> 24x36 = 864 sq in. The fitted 20x15 becomes 34.7% (over the ceiling, so it
    # cannot simply be kept) and #1119 (14.25x15 = 24.7%) is the largest that qualifies while
    # still spanning 59.4% of the width. Proves selection re-runs on a WIDTH-only change.
    res = await _resize(rules_dir, MIRROR_W, 24)
    assert res.hanger.part == "1119"
    assert res.hanger.replace is True
    assert res.hanger.part_name == "1119-HANGER"
    assert res.hanger.target_width_meters == pytest.approx(14.25 * IN)
    assert res.hanger.target_height_meters == pytest.approx(15 * IN)


@pytest.mark.asyncio
async def test_a_chosen_prefab_writes_no_hanger_dimensions(rules_dir):
    """The contract of the swap: the prefab file already IS the catalogue size, so stretching
    the fitted hanger onto that outline would be redundant AND would leave the wrong internal
    hole pattern behind. `changes` must carry the tab follower and nothing else off the hanger."""
    res = await _resize(rules_dir, MIRROR_W, 24)
    assert res.hanger.replace is True
    written = {c.name for c in res.changes}
    assert HANGER_W not in written
    assert HANGER_H not in written
    assert HANGER_MINOR not in written
    assert HANGER_SHEETMETAL not in written


@pytest.mark.asyncio
async def test_replace_and_resize_fitted_are_mutually_exclusive(rules_dir):
    """A swap and a stretch are the two ways to reach a correct hanger and must never both
    fire — that would write dimensions onto a part the app is about to throw away."""
    for axis_dim, target in [(MIRROR_W, 24), (MIRROR_H, 48), (MIRROR_W, 90), (MIRROR_H, 36)]:
        res = await _resize(rules_dir, axis_dim, target)
        h = res.hanger
        assert sum([h.replace, h.resize_fitted, h.keep_fitted]) <= 1, (axis_dim, target)
        if h.replace:
            assert h.part, "replace with no part number"
            assert not {c.name for c in res.changes} & {HANGER_W, HANGER_H}


# ── no-op when the fitted hanger is already right ─────────────────────────────

@pytest.mark.asyncio
async def test_fitted_hanger_already_in_band_is_kept_untouched(rules_dir):
    """Height 36 -> 36: the fitted 20x15 is 23.15% of the glass, inside the band, so it is
    KEPT and nothing is written. This is checked before prefab selection so a bespoke hanger
    the client purpose-built is never swapped for a catalogue part (the JEN failure)."""
    res = await _resize(rules_dir, MIRROR_H, 36)
    assert res.hanger.keep_fitted is True
    assert res.hanger.part is None
    assert res.hanger.in_band is True
    assert HANGER_W not in {c.name for c in res.changes}
    assert HANGER_H not in {c.name for c in res.changes}
    assert "already correctly sized" in res.explanation


# ── never invents a size: every write is an exact catalogue dimension ────────

@pytest.mark.asyncio
@pytest.mark.parametrize("target_in", [30, 40, 48, 54, 60, 72])
async def test_every_hanger_write_is_a_catalogue_or_clean_custom_size(rules_dir, target_in):
    """A prefab write must be an EXACT catalogue size. When no prefab qualifies and the fitted
    hanger is resized instead, the value is computed — but must still be a clean 0.25"
    increment, since someone has to make that part."""
    from engine.hangers.hanger_select import PREFAB_HANGERS, RESIZE_ROUND_TO_IN
    widths = {round(w, 4) for _p, w, _h in PREFAB_HANGERS}
    heights = {round(h, 4) for _p, _w, h in PREFAB_HANGERS}
    res = await _resize(rules_dir, MIRROR_H, target_in)
    resized = res.hanger is not None and res.hanger.resize_fitted
    for c in res.changes:
        if c.name not in (HANGER_W, HANGER_H):
            continue
        inches = round(c.value_meters / IN, 4)
        if resized:
            assert abs(inches / RESIZE_ROUND_TO_IN - round(inches / RESIZE_ROUND_TO_IN)) < 1e-6
        else:
            assert inches in (widths if c.name == HANGER_W else heights)


# ── the chassis HANGING TAB must follow the hanger width ─────────────────────

@pytest.mark.asyncio
async def test_tab_spacing_lands_on_the_known_good_value(rules_dir):
    """The headline check: AMBER's 15.75" tab spacing must land on 10.000" when the hanger
    goes 20" -> 14.25" — exactly the spacing KELLY really carries for that same 14.25"
    hanger, both keeping the 4.25" slot inset.

    Driven by a WIDTH change to 24" (which selects #1119 at 14.25") rather than the height
    change that used to select #1333; the cross-validated 14.25 -> 10.000 pair is the point,
    not which axis moved.

    #1119 is a SWAP, so the new hanger width never appears in `changes`. The follower has to
    take it from the prefab's catalogue width instead — reading it back out of the change list
    would find nothing and silently leave the tabs at the old 20" hanger's 15.75" spacing.
    """
    res = await _resize(rules_dir, MIRROR_W, 24)
    by_name = {c.name: c.value_meters for c in res.changes}
    assert res.hanger.replace is True
    assert res.hanger.target_width_meters == pytest.approx(14.25 * IN)
    assert by_name[TAB_SPACING] == pytest.approx(10.0 * IN)
    assert res.hanger.follower_dims == [TAB_SPACING]


@pytest.mark.asyncio
@pytest.mark.parametrize("axis_dim,target", [(MIRROR_W, 24), (MIRROR_H, 48), (MIRROR_W, 90)])
async def test_the_4_25_inch_inset_is_preserved(rules_dir, axis_dim, target):
    """Holds on all four client products, so it must hold whatever the selector picks —
    prefab or scaled, narrow or very wide.

    The width the tabs must track comes from `target_width_meters`, which is set on BOTH
    outcomes. Reading it out of `changes` instead would silently skip every swap, since a
    swapped-in prefab writes no dimensions at all."""
    res = await _resize(rules_dir, axis_dim, target)
    by_name = {c.name: c.value_meters for c in res.changes}
    new_hanger_w = res.hanger.target_width_meters
    assert new_hanger_w > 0
    if TAB_SPACING not in by_name:
        pytest.skip("hanger width did not move, so no follower is expected")
    inset = (new_hanger_w - by_name[TAB_SPACING]) / IN
    assert inset == pytest.approx(4.25)


@pytest.mark.asyncio
async def test_tab_width_on_the_same_sketch_is_not_touched(rules_dir):
    # D3@Sketch81 matches the same name hint but sits 18.25" from the hanger width, nowhere
    # near the 4.25" inset — it must be rejected, not deformed.
    res = await _resize(rules_dir, MIRROR_H, 48)
    assert TAB_WIDTH not in {c.name for c in res.changes}


@pytest.mark.asyncio
async def test_a_drifted_tab_inset_is_CORRECTED_not_carried_forward(rules_dir):
    """Reported 2026-08-06: resizing a model whose tabs were already misaligned left them just
    as misaligned. The follower used to shift by the hanger's width delta, which faithfully
    preserved any inset inside the +/-1" identification window instead of fixing it."""
    dims = dict(DIMS)
    dims[TAB_SPACING] = 15.00 * IN            # drifted: 5.00" inset instead of 4.25"
    res = await _resize(rules_dir, MIRROR_W, 24, dims=dims)
    by_name = {c.name: c.value_meters for c in res.changes}
    assert res.hanger.target_width_meters == pytest.approx(14.25 * IN)
    assert by_name[TAB_SPACING] == pytest.approx(10.0 * IN), "the drift was carried forward"


@pytest.mark.asyncio
async def test_a_drifted_tab_is_fixed_even_when_the_hanger_does_not_move(rules_dir):
    """36x36 keeps its #1038 hanger, so nothing about the hanger changes — but a drifted tab
    must still be pulled back onto the 4.25" inset."""
    dims = dict(DIMS)
    dims[TAB_SPACING] = 16.50 * IN            # drifted: 3.50" inset
    res = await _resize(rules_dir, MIRROR_H, 36.0001, dims=dims)
    by_name = {c.name: c.value_meters for c in res.changes}
    assert by_name.get(TAB_SPACING) == pytest.approx(15.75 * IN)


@pytest.mark.asyncio
async def test_an_already_correct_tab_reports_no_change(rules_dir):
    """The corollary: re-asserting the inset must stay a no-op on a healthy model, or every
    resize would show a spurious tab write."""
    res = await _resize(rules_dir, MIRROR_H, 36.0001)
    assert TAB_SPACING not in {c.name for c in res.changes}


@pytest.mark.asyncio
async def test_duplicated_dim_dump_yields_one_follower_change(rules_dir):
    """The app's dim dump lists every dim TWICE, which made the follower emit the same write
    twice (seen live on AMBER 60x36). Duplicate writes made the second undo entry record the
    already-written value as its "previous", so only the reverse-order rollback saved the
    model — the follower must be deduped by name instead."""
    llm = json.dumps({
        "rule": {"if_changes": MIRROR_H, "also_change": [CHASSIS_H]},
        "value_meters": 48 * IN,
        "scope": "overall",
    })
    doubled = [DimensionIn(name=n, value_meters=v) for n, v in DIMS.items() for _ in range(2)]
    with patch("engine.resize.interpret.call_llm", new=AsyncMock(return_value=llm)):
        res = await interpret(InterpretRequest(
            instruction="resize to 48",
            dimensions=doubled,
            dim_axis_labels=AXIS,
            master_width_dim=MIRROR_W, master_height_dim=MIRROR_H,
            model_path=str(rules_dir / "AmberTest.SLDASM"),
        ))
    assert res.hanger.follower_dims == [TAB_SPACING]
    assert [c.name for c in res.changes].count(TAB_SPACING) == 1


@pytest.mark.asyncio
async def test_no_follower_change_when_only_the_hanger_height_moves(rules_dir):
    # #1004 (14.25x10) and #1119 (14.25x15) share a width. Starting from a 24x24 glass on
    # #1004 and growing to 24x40 (960 sq in) selects #1119: the height changes but the width
    # does not, so the tab spacing must be left completely alone.
    dims = {MIRROR_W: 24 * IN, MIRROR_H: 24 * IN, CHASSIS_H: 22 * IN,
            HANGER_W: 14.25 * IN, HANGER_H: 10 * IN,
            TAB_SPACING: 10 * IN, TAB_WIDTH: 1.75 * IN}
    res = await _resize(rules_dir, MIRROR_H, 40, dims=dims)
    assert res.hanger.part == "1119"
    assert res.hanger.replace is True
    by_name = {c.name: c.value_meters for c in res.changes}
    assert res.hanger.target_height_meters == pytest.approx(15 * IN)   # height did move
    assert res.hanger.target_width_meters == pytest.approx(14.25 * IN)  # width did not
    assert TAB_SPACING not in by_name                    # so the tabs stay put
    assert res.hanger.follower_dims == []


# ── graceful when the model has no hanger at all ──────────────────────────────

@pytest.mark.asyncio
async def test_model_without_a_hanger_is_unaffected(rules_dir):
    dims = {MIRROR_W: 36 * IN, MIRROR_H: 36 * IN, CHASSIS_H: 34 * IN}
    res = await _resize(rules_dir, MIRROR_H, 48, dims=dims)
    assert res.error is None
    assert res.hanger is None
    assert {c.name for c in res.changes} == {MIRROR_H, CHASSIS_H}


# ── the chassis cap must not switch off when the RULES cannot supply the width ──
#
# Live 2026-08-25 (CLARA 16x36 -> 60x36). A rename left the chassis width dep naming a
# component id that no longer existed, so `width_deps` yielded nothing and `chassis_w_in`
# arrived as 0 — which meant "no cap" rather than "unknown". A 20" #1038 was selected for a
# 10" chassis, the tab follower wrote 15.75" into it, and HANGING TAB LOCATIONS failed the
# rebuild. The width was in the live dim dump the whole time; only the rules had lost it.

CHASSIS_W = "D1@Sketch1 [12204-CHASSIS-2]"   # the real chassis WIDTH, 10" — narrow, like CLARA


@pytest.mark.asyncio
async def test_chassis_width_is_recovered_from_LIVE_dims_when_the_rules_lack_it(rules_dir):
    dims = dict(DIMS, **{CHASSIS_W: 10 * IN})
    axis = dict(AXIS, **{CHASSIS_W: "W"})
    # The rules name no chassis width — exactly the state the rename left behind.
    res = await _resize(rules_dir, MIRROR_H, 36, dims=dims, axis=axis, also=[])

    assert res.error is None
    # The fitted 20" hanger is WIDER than the 10" chassis it bolts to, so it cannot be kept.
    assert res.hanger.keep_fitted is False
    assert res.hanger.part != "1038"
    chosen = res.hanger.target_width_meters / IN
    assert chosen <= 10 + 1e-9, f"hanger {chosen}\" overhangs a 10\" chassis"


@pytest.mark.asyncio
async def test_the_TAB_SPACING_is_never_mistaken_for_the_chassis_width(rules_dir):
    """The trap the fallback has to dodge, and the reason it is not a bare max().

    The chassis carries its hanging-tab dims on the W axis too. AMBER 36x36 has NO chassis
    width dim at all — only TAB_SPACING at 15.75" — so a bare "largest [W] chassis dim" reads
    15.75" and rejects the 20" #1038 the client actually built. The tabs are cut INTO the
    chassis; their spacing is never its width.
    """
    res = await _resize(rules_dir, MIRROR_H, 36, also=[])

    assert res.error is None
    assert res.hanger.keep_fitted is True, "the 20\" hanger was capped against its own tabs"


# ── a dim the model does not have must never reach the app ───────────────────

@pytest.mark.asyncio
async def test_a_rule_naming_a_MISSING_dim_is_dropped_not_shipped(rules_dir):
    """`selection.warning` promised these were "left unchanged" — nothing enforced it.

    The change list carried them to the app, whose resolver dropped the unmatched
    `[ComponentId]` and wrote the value to whatever bare dim name matched instead. Live
    2026-08-25: 60" bound for the mirror landed on a 1" dim of the top-level assembly.
    """
    ghost = "D1@Sketch1 [12393-CHASSIS-RENAMED-AWAY-2]"
    res = await _resize(rules_dir, MIRROR_H, 48, also=[CHASSIS_H, ghost])

    assert res.error is None
    names = {c.name for c in res.changes}
    assert ghost not in names
    assert CHASSIS_H in names, "the real dep must still be applied"



# ── LED bracket collision: the hanger is capped so the hanging bracket clears it ──
#
# Live 2026-08-27, ISABELL 44x56. The chassis is 42.750" and #1215 (40x12) was chosen — 19.5% of
# the AREA, so it passed every rule there was. The hanging bracket follows the hanger's width, so
# it grew to 39.125" and left 1.81" per side, straight into `12296-LED-BRACKET`.
#
# The chain the fix relies on: cap the HANGER -> the bracket follows the hanger -> the bracket
# never reaches the LED brackets. Both halves are needed; capping alone does nothing if the
# bracket is driven off the mirror instead.

ISA_MIRROR_W = "D1@Sketch1 [1046-MIRROR-KAREN-1]"
ISA_MIRROR_H = "D2@Sketch1 [1046-MIRROR-KAREN-1]"
ISA_CHASSIS_W = "D1@Sketch2 [12295-CHASSIS-1]"
ISA_HANGER_W = "D2@Base-Flange1 [1119-HANGER-1]"
ISA_HANGER_H = "D1@Sketch1 [1119-HANGER-1]"
ISA_BRACKET_W = "D2@Base-Flange1 [1047-HANGING-BRACKET-1]"
ISA_LED_L = "D7@Edge-Flange1 [12296-LED-BRACKET-1]"
ISA_LED_R = "D7@Edge-Flange1 [12296-LED-BRACKET-2]"
ISA_CLIP = "D2@Base-Flange1 [2004-HANGING-BRACKET-CLIP-1]"

ISA_AXIS = {ISA_MIRROR_W: "W", ISA_MIRROR_H: "H", ISA_CHASSIS_W: "W", ISA_HANGER_W: "W",
            ISA_HANGER_H: "H", ISA_BRACKET_W: "W", ISA_LED_L: "W", ISA_LED_R: "W", ISA_CLIP: "W"}


def _isa_dims(mw, mh, cw, hw, bw, with_led=True):
    d = {ISA_MIRROR_W: mw * IN, ISA_MIRROR_H: mh * IN, ISA_CHASSIS_W: cw * IN,
         ISA_HANGER_W: hw * IN, ISA_HANGER_H: 15 * IN, ISA_BRACKET_W: bw * IN,
         ISA_CLIP: 1.5 * IN}
    if with_led:
        d[ISA_LED_L] = 2 * IN
        d[ISA_LED_R] = 2 * IN
    return d


@pytest.fixture
def isabell_rules(tmp_path, monkeypatch):
    """The rule set as the generator really wrote it — bracket listed as a width dependent."""
    doc = {"model": "IsabellTest",
           "width": [{"if_changes": ISA_MIRROR_W, "also_change": [ISA_CHASSIS_W, ISA_BRACKET_W]}],
           "height": [{"if_changes": ISA_MIRROR_H, "also_change": []}],
           "component_labels": {"1047-HANGING-BRACKET-1": "Hanging Bracket"}}
    d = tmp_path / "rules"
    d.mkdir()
    (d / "IsabellTest.rules.json").write_text(json.dumps(doc))
    monkeypatch.setattr(rules, "RULES_DIR", d)
    return tmp_path


async def _isa_resize(rules_dir, dims, target_in):
    llm = json.dumps({
        "rule": {"if_changes": ISA_MIRROR_W, "also_change": [ISA_CHASSIS_W, ISA_BRACKET_W]},
        "value_meters": target_in * IN, "scope": "overall"})
    with patch("engine.resize.interpret.call_llm", new=AsyncMock(return_value=llm)):
        return await interpret(InterpretRequest(
            instruction=f"resize to {target_in}",
            dimensions=[DimensionIn(name=n, value_meters=v) for n, v in dims.items()],
            dim_axis_labels=ISA_AXIS, master_width_dim=ISA_MIRROR_W,
            master_height_dim=ISA_MIRROR_H,
            model_path=str(rules_dir / "IsabellTest.SLDASM")))


def test_the_offending_prefab_is_rejected_by_the_clearance():
    """#1215 at 40in leaves 1.81in of chassis per side once the bracket follows it."""
    without = select_hanger(44, 56, fitted_w_in=20, fitted_h_in=15, chassis_w_in=42.75)
    assert without.part == "1215", "precondition: this is what was chosen live"
    assert (42.75 - (without.target_width_in - 0.875)) / 2 < 2.0

    with_clear = select_hanger(44, 56, fitted_w_in=20, fitted_h_in=15, chassis_w_in=42.75,
                               obstacle_clear_in=OBSTACLE_CLEARANCE_IN)
    assert with_clear.part != "1215"
    assert (42.75 - (with_clear.target_width_in - 0.875)) / 2 >= OBSTACLE_CLEARANCE_IN


def test_the_clearance_binds_every_outcome_not_just_prefabs():
    """It is folded into `chassis_cap`, so kept / substituted / custom all honour one number."""
    for fw, fh in [(20, 15), (41, 12), (14.25, 15)]:
        c = select_hanger(44, 56, fitted_w_in=fw, fitted_h_in=fh, chassis_w_in=42.75,
                          obstacle_clear_in=OBSTACLE_CLEARANCE_IN)
        assert c.target_width_in <= 42.75 - 2 * OBSTACLE_CLEARANCE_IN + 1e-9, (fw, fh, c.reason)


def test_a_product_with_no_LED_bracket_is_completely_unaffected():
    """The narrow half of the fix: clearance is 0 unless the obstacle is really there."""
    a = select_hanger(44, 56, fitted_w_in=20, fitted_h_in=15, chassis_w_in=42.75)
    b = select_hanger(44, 56, fitted_w_in=20, fitted_h_in=15, chassis_w_in=42.75,
                      obstacle_clear_in=0.0)
    assert (a.part, a.target_width_in, a.target_height_in) == \
           (b.part, b.target_width_in, b.target_height_in)


@pytest.mark.asyncio
async def test_end_to_end_the_bracket_clears_the_led_brackets(isabell_rules):
    """The whole chain, on the live numbers."""
    dims = _isa_dims(34, 46, 32.75, 20.0, 19.125)
    res = await _isa_resize(isabell_rules, dims, 44)
    by = {c.name: c.value_meters for c in res.changes}
    bracket = by[ISA_BRACKET_W] / IN
    chassis = by[ISA_CHASSIS_W] / IN
    assert res.hanger.part != "1215"
    assert bracket == pytest.approx(res.hanger.target_width_meters / IN - 0.875, abs=1e-6)
    assert (chassis - bracket) / 2 >= OBSTACLE_CLEARANCE_IN - 1e-9


@pytest.mark.asyncio
async def test_without_the_follower_the_cap_would_be_pointless(isabell_rules):
    """The bracket must take the HANGER's delta, not the mirror's — mirror +10.000in vs hanger
    +5.750in is how it ended up wider than the part it seats inside."""
    dims = _isa_dims(24, 36, 22.75, 14.25, 13.375)
    res = await _isa_resize(isabell_rules, dims, 34)
    bracket = next(c.value_meters for c in res.changes if c.name == ISA_BRACKET_W) / IN
    hanger = res.hanger.target_width_meters / IN
    assert bracket == pytest.approx(hanger - 0.875, abs=1e-6)
    assert bracket != pytest.approx(13.375 + 10.0), "that is the mirror-driven value — the bug"


@pytest.mark.asyncio
async def test_the_mirror_rule_can_no_longer_drive_the_bracket(isabell_rules):
    """The rule still names it — rule files are regenerated, not hand-edited — so the POLICY is
    what has to refuse it, or the follower's value gets overwritten."""
    dims = _isa_dims(24, 36, 22.75, 14.25, 13.375)
    res = await _isa_resize(isabell_rules, dims, 34)
    written = [c.value_meters / IN for c in res.changes if c.name == ISA_BRACKET_W]
    assert len(written) == 1, f"expected exactly one write, got {written}"


@pytest.mark.asyncio
async def test_the_clips_are_never_written(isabell_rules):
    """They share "HANGING BRACKET" in their part number and are real fixed hardware."""
    res = await _isa_resize(isabell_rules, _isa_dims(24, 36, 22.75, 14.25, 13.375), 34)
    assert ISA_CLIP not in {c.name for c in res.changes}


@pytest.mark.asyncio
async def test_a_bracket_already_wider_than_its_hanger_is_flagged_not_followed(isabell_rules):
    """The sanity bound — a bracket that does not sit inside the hanger is either not the
    bracket, or a model already broken by the old behaviour."""
    dims = _isa_dims(34, 46, 32.75, 20.0, 23.375)      # the value the old bug produced
    res = await _isa_resize(isabell_rules, dims, 44)
    assert ISA_BRACKET_W not in {c.name for c in res.changes}
