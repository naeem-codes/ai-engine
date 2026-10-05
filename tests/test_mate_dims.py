"""Assembly MATE dims must never be driven by a size rule.

Live failure (SUZI, 2026-08-03): `D1@Distance4 [12475-CHASSIS-ASSY-1]` received the master's
+6.000" width delta (18.000" -> 24.000") and shifted the whole chassis sub-assembly 6"
sideways. It entered the rules because the app labeler's `(filled)` fallback guessed [W] for
it, and the deterministic axis pass trusts labels.
"""

import json
import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from engine.resize import resize_policy as policy
from engine.rules import rules
from engine.resize.interpret import interpret
from engine.core.models import DimensionIn, InterpretRequest

IN = 0.0254
GLASS_W = "D1@Sketch1 [SUZI-MIRROR-1]"
MATE = "D1@Distance4 [12475-CHASSIS-ASSY-1]"
CHASSIS = "D1@Sketch1 [12476-CHASSIS-5]"


# ── recognising a mate dim ────────────────────────────────────────────────────

@pytest.mark.parametrize("name", [
    "D1@Distance4 [12475-CHASSIS-ASSY-1]",
    "D1@Distance1",
    "D2@Angle3 [X-1]",
    "D1@LimitDistance2 [X-1]",
    "D1@Width1 [12189-1]",
    "D1@Concentric5",
])
def test_mate_dims_are_recognised(name):
    assert policy.is_mate_dim(name) is True


@pytest.mark.parametrize("name", [
    "D1@Sketch1 [SUZI-MIRROR-1]",
    "D1@Boss-Extrude1 [Zortech-LEDS-1]",
    "D2@Base-Flange1 [1038-HANGER-1]",
    "WIDTH@Sketch1 [1011-MIRROR-CAROL-1]",
    "D2@Sheet-Metal1 [12214-HANGER-1]",
    "THICKNESS@Sheet-Metal1 [1005-CLIP-1]",
])
def test_model_dims_are_not_mistaken_for_mates(name):
    assert policy.is_mate_dim(name) is False


def test_a_sketch_named_WIDTH_is_not_a_mate():
    """Real case: ALPHA's `D1@WIDTH [LPM-24060A-2]` is a SKETCH called WIDTH. Mate instance
    names always carry trailing digits (`Width1`), which is what separates them."""
    assert policy.is_mate_dim("D1@WIDTH [LPM-24060A-2]") is False
    assert policy.is_mate_dim("D2@LENGTH/HEIGHT [LPM-24060A-1]") is False
    assert policy.is_mate_dim("D1@Width1 [LPM-24060A-2]") is True


# ── excluded from SIZE-rule membership ────────────────────────────────────────

def test_filter_axis_dims_drops_mate_dims():
    allowed, blocked = policy.filter_axis_dims(
        [GLASS_W, MATE, CHASSIS], "width", None,
        {GLASS_W: 24 * IN, MATE: 18 * IN, CHASSIS: 23.75 * IN})
    assert allowed == [GLASS_W, CHASSIS]
    assert [n for n, _ in blocked] == [MATE]
    assert "mate" in dict(blocked)[MATE].lower()


# ── and skipped at runtime even from a stale rules file ───────────────────────

@pytest.fixture
def rules_dir(tmp_path, monkeypatch):
    doc = {"model": "SuziTest",
           "width": [{"if_changes": GLASS_W, "also_change": [MATE, CHASSIS]}],
           "height": [], "component_labels": {"SUZI-MIRROR-1": "Mirror Glass"}}
    d = tmp_path / "rules"
    d.mkdir()
    (d / "SuziTest.rules.json").write_text(json.dumps(doc))
    monkeypatch.setattr(rules, "RULES_DIR", d)
    return tmp_path


@pytest.mark.asyncio
async def test_stale_rule_listing_a_mate_is_ignored_at_runtime(rules_dir):
    dims = {GLASS_W: 24 * IN, MATE: 18 * IN, CHASSIS: 23.75 * IN}
    llm = json.dumps({"rule": {"if_changes": GLASS_W, "also_change": [MATE, CHASSIS]},
                      "value_meters": 30 * IN, "scope": "overall"})
    with patch("engine.resize.interpret.call_llm", new=AsyncMock(return_value=llm)):
        res = await interpret(InterpretRequest(
            instruction="change width to 30",
            dimensions=[DimensionIn(name=n, value_meters=v) for n, v in dims.items()],
            dim_axis_labels={GLASS_W: "W", MATE: "W", CHASSIS: "W"},
            master_width_dim=GLASS_W,
            model_path=str(rules_dir / "SuziTest.SLDASM")))
    by_name = {c.name: c.value_meters for c in res.changes}
    assert MATE not in by_name, "a mate must never receive a size delta"
    assert by_name[GLASS_W] == pytest.approx(30 * IN)
    assert by_name[CHASSIS] == pytest.approx(29.75 * IN)   # 23.75 + 6.00, still correct
