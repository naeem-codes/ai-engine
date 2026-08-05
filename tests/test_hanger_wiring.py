"""Hanger re-selection wired into interpret(): it must fire on any size change, write only
exact catalogue dims, and never be blocked by the fixed-size policy that guards it."""

import json
import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import rules
from interpret import interpret
from models import DimensionIn, InterpretRequest

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


async def _resize(rules_dir, if_changes, target_in, dims=None):
    llm = json.dumps({
        "rule": {"if_changes": if_changes,
                 "also_change": [CHASSIS_H] if if_changes == MIRROR_H else []},
        "value_meters": target_in * IN,
        "scope": "overall",
    })
    with patch("interpret.call_llm", new=AsyncMock(return_value=llm)):
        return await interpret(InterpretRequest(
            instruction=f"resize to {target_in}",
            dimensions=[DimensionIn(name=n, value_meters=v)
                        for n, v in (dims or DIMS).items()],
            dim_axis_labels=AXIS,
            master_width_dim=MIRROR_W, master_height_dim=MIRROR_H,
            model_path=str(rules_dir / "AmberTest.SLDASM"),
        ))


# ── height change: 36x36 -> 36x48 must re-select #1038 -> #1333 ───────────────

@pytest.mark.asyncio
async def test_height_change_reselects_the_hanger(rules_dir):
    res = await _resize(rules_dir, MIRROR_H, 48)
    assert res.error is None
    assert res.hanger is not None
    assert res.hanger.part == "1333"
    by_name = {c.name: c.value_meters for c in res.changes}
    assert by_name[HANGER_W] == pytest.approx(14.25 * IN)
    assert by_name[HANGER_H] == pytest.approx(24 * IN)


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
    # 36x36 -> 30x36 = 1080 sq in. The fitted 20x15 becomes 27.78% (over the ceiling, so it
    # cannot simply be kept) and #1119 (14.25x15 = 19.79%) is the largest that qualifies.
    # Proves selection re-runs on a WIDTH-only change, since area moves either way.
    res = await _resize(rules_dir, MIRROR_W, 30)
    assert res.hanger.part == "1119"
    by_name = {c.name: c.value_meters for c in res.changes}
    assert by_name[HANGER_W] == pytest.approx(14.25 * IN)


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
    from hanger_select import PREFAB_HANGERS, RESIZE_ROUND_TO_IN
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
    hanger, both keeping the 4.25" slot inset."""
    res = await _resize(rules_dir, MIRROR_H, 48)
    by_name = {c.name: c.value_meters for c in res.changes}
    assert by_name[HANGER_W] == pytest.approx(14.25 * IN)
    assert by_name[TAB_SPACING] == pytest.approx(10.0 * IN)
    assert res.hanger.follower_dims == [TAB_SPACING]


@pytest.mark.asyncio
async def test_the_4_25_inch_inset_is_preserved(rules_dir):
    res = await _resize(rules_dir, MIRROR_H, 48)
    by_name = {c.name: c.value_meters for c in res.changes}
    inset = (by_name[HANGER_W] - by_name[TAB_SPACING]) / IN
    assert inset == pytest.approx(4.25)


@pytest.mark.asyncio
async def test_tab_width_on_the_same_sketch_is_not_touched(rules_dir):
    # D3@Sketch81 matches the same name hint but sits 18.25" from the hanger width, nowhere
    # near the 4.25" inset — it must be rejected, not deformed.
    res = await _resize(rules_dir, MIRROR_H, 48)
    assert TAB_WIDTH not in {c.name for c in res.changes}


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
    by_name = {c.name: c.value_meters for c in res.changes}
    assert by_name[HANGER_H] == pytest.approx(15 * IN)   # height did move
    assert HANGER_W not in by_name                       # width did not
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
