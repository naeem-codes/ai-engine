"""The frosted (sandblast) band must stay the same length as the LED strip.

Real AMY-24.00X48 numbers. The band is cut into the glass by Sketch2, so it never changes the
part's bounding box and the labeler leaves it unlabeled — which is how it was silently left
behind when the mirror grew 24"→30".
"""

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
GLASS_W = "D1@Sketch1 [AMY-24.00X48]"          # 24.000" outer width — master
GLASS_H = "D2@Sketch1 [AMY-24.00X48]"          # 48.000" outer height
FROST_LEN = "D2@Sketch2 [AMY-24.00X48]"        # 20.000" frost band LENGTH  <-- follower
FROST_BAND_W = "D1@Sketch2 [AMY-24.00X48]"     # 2.500"  frost band WIDTH — must NOT move
FROST_INSET = "D3@Sketch2 [AMY-24.00X48]"      # 2.000"  inset — must NOT move
GLASS_OTHER = "D1@Sketch3 [AMY-24.00X48]"      # 7.000"  unrelated — must NOT move
CHASSIS_W = "D1@Sketch1 [12213-CHASSIS-1]"     # 23.000"
STRIP = "D1@Boss-Extrude1 [Zortech-Low-Profile-1900-Lumens-1]"   # 20.000" length

DIMS = {GLASS_W: 24 * IN, GLASS_H: 48 * IN, FROST_LEN: 20 * IN, FROST_BAND_W: 2.5 * IN,
        FROST_INSET: 2 * IN, GLASS_OTHER: 7 * IN, CHASSIS_W: 23 * IN, STRIP: 20 * IN}
# Only the outer dims and the strip are labeled; everything inside Sketch2 is unlabeled.
AXIS = {GLASS_W: "W", GLASS_H: "H", CHASSIS_W: "W", STRIP: "W"}

LABELS = {"AMY-24.00X48": "Mirror Glass", "12213-CHASSIS-1": "Main Chassis",
          "Zortech-Low-Profile-1900-Lumens-1": "LED Strip 1"}


@pytest.fixture
def rules_dir(tmp_path, monkeypatch):
    doc = {"model": "AmyTest",
           "width": [{"if_changes": GLASS_W, "also_change": [CHASSIS_W, STRIP]}],
           "height": [], "component_labels": LABELS}
    d = tmp_path / "rules"
    d.mkdir()
    (d / "AmyTest.rules.json").write_text(json.dumps(doc))
    monkeypatch.setattr(rules, "RULES_DIR", d)
    return tmp_path


async def _resize(rules_dir, target_in, dims=None):
    llm = json.dumps({"rule": {"if_changes": GLASS_W, "also_change": [CHASSIS_W, STRIP]},
                      "value_meters": target_in * IN, "scope": "overall"})
    with patch("interpret.call_llm", new=AsyncMock(return_value=llm)):
        return await interpret(InterpretRequest(
            instruction=f"change width to {target_in}",
            dimensions=[DimensionIn(name=n, value_meters=v) for n, v in (dims or DIMS).items()],
            dim_axis_labels=AXIS, master_width_dim=GLASS_W, master_height_dim=GLASS_H,
            model_path=str(rules_dir / "AmyTest.SLDASM")))


@pytest.mark.asyncio
async def test_frost_band_follows_the_led_strip(rules_dir):
    res = await _resize(rules_dir, 30)
    by_name = {c.name: round(c.value_meters / IN, 4) for c in res.changes}
    assert by_name[STRIP] == 26.0          # 20 + (30 - 24)
    assert by_name[FROST_LEN] == 26.0      # was silently left at 20.000"


@pytest.mark.asyncio
async def test_frost_and_strip_end_up_identical(rules_dir):
    res = await _resize(rules_dir, 30)
    by_name = {c.name: c.value_meters for c in res.changes}
    assert by_name[FROST_LEN] == pytest.approx(by_name[STRIP])


@pytest.mark.asyncio
async def test_the_symmetric_insets_still_add_up(rules_dir):
    # 2.000" + band + 2.000" must equal the new glass width.
    res = await _resize(rules_dir, 30)
    by_name = {c.name: c.value_meters for c in res.changes}
    assert (2 * 2 * IN + by_name[FROST_LEN]) == pytest.approx(by_name[GLASS_W])


@pytest.mark.asyncio
async def test_the_other_sketch2_dims_are_untouched(rules_dir):
    # The 2.500" band width and the 2.000" insets are fixed; only the length follows.
    res = await _resize(rules_dir, 30)
    names = {c.name for c in res.changes}
    assert FROST_BAND_W not in names
    assert FROST_INSET not in names
    assert GLASS_OTHER not in names


@pytest.mark.asyncio
async def test_no_frost_change_when_the_strip_does_not_move(rules_dir):
    res = await _resize(rules_dir, 24)      # same size — nothing moves
    assert FROST_LEN not in {c.name for c in res.changes}


@pytest.mark.asyncio
async def test_noop_when_no_glass_dim_matches_the_strip(rules_dir):
    """AMBER's case: the band's extent has no dim (driven by sketch relations), so no glass
    dim equals the strip length and this must do nothing."""
    dims = dict(DIMS)
    dims[FROST_LEN] = 17 * IN               # no longer equal to the 20" strip
    res = await _resize(rules_dir, 30, dims=dims)
    assert FROST_LEN not in {c.name for c in res.changes}


@pytest.mark.asyncio
async def test_a_matching_dim_on_a_NON_glass_part_is_ignored(rules_dir):
    dims = dict(DIMS)
    dims["D9@Sketch1 [12213-CHASSIS-1]"] = 20 * IN   # equals the strip, but not the glass
    res = await _resize(rules_dir, 30, dims=dims)
    assert "D9@Sketch1 [12213-CHASSIS-1]" not in {c.name for c in res.changes}
