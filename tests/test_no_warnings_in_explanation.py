"""No warnings in the chat explanation — they go to the engine log.

User, 2026-09-25, after LUCY 70 -> 48: "don't throw warning on main form UI please". The main form
prints the explanation verbatim, and it was carrying "⚠ 1 dimension(s) that draw a circle on this
model are not in its rule set…". The explanation now says what the resize DID; caveats about it
are `[WARN]` lines in the engine log. A resize that cannot run still returns an ERROR — that is
not a warning, and nothing here hides it.
"""

import json
import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from engine.resize import interpret
from engine.rules import rules
from engine.core.models import DimensionIn, HangerSelection, InterpretRequest

IN = 0.0254
GLASS = "D1@Sketch1 [1026-MIRROR-ECLIPSE-1]"
CHASSIS = "D1@Sketch1 [12418-CHASSIS-CIRCLINE-2]"
D17 = "D17@Sketch46 [12418-CHASSIS-CIRCLINE-2]"
D15 = "D15@Sketch46 [12418-CHASSIS-CIRCLINE-2]"
DIMS = {GLASS: 30 * IN, CHASSIS: 29.5 * IN, D17: 13.079 * IN, D15: 12.980 * IN}


@pytest.fixture
def stale_round_rules(tmp_path, monkeypatch):
    """A rule set saved before the LED channel was labelled: the channel is not a dependant."""
    doc = {"model": "EclipseTest",
           "width": [{"if_changes": GLASS, "also_change": [CHASSIS]}],
           "height": [], "component_labels": {}}
    d = tmp_path / "rules"
    d.mkdir()
    (d / "EclipseTest.rules.json").write_text(json.dumps(doc))
    monkeypatch.setattr(rules, "RULES_DIR", d)
    return tmp_path


@pytest.mark.asyncio
async def test_a_stale_round_rule_set_warns_in_the_LOG_not_the_explanation(stale_round_rules):
    logged = []
    llm = json.dumps({"rule": {"if_changes": GLASS, "also_change": [CHASSIS]},
                      "value_meters": 45 * IN, "scope": "overall"})
    with patch("engine.resize.interpret.call_llm", new=AsyncMock(return_value=llm)), \
            patch("engine.resize.interpret.log", new=lambda msg, *a, **k: logged.append(str(msg))):
        res = await interpret.interpret(InterpretRequest(
            instruction="Resize to 45",
            dimensions=[DimensionIn(name=n, value_meters=v) for n, v in DIMS.items()],
            dim_axis_labels={GLASS: "W", CHASSIS: "W"},
            master_width_dim=GLASS, master_height_dim=None,
            shape="round", master_radial=1,
            radial_dims={GLASS: 1, CHASSIS: 1, D17: 2, D15: 2},
            model_path=str(stale_round_rules / "EclipseTest.SLDASM")))
    assert res.error is None
    assert res.changes, "the resize itself still happens"
    assert "⚠" not in res.explanation
    assert "draw a circle" not in res.explanation
    assert any("[WARN]" in l and "draw a circle" in l for l in logged)


def test_the_hanger_band_remarks_are_logged_not_shown():
    logged = []
    for flags in ({"needs_review": True}, {"over_ceiling": True}, {"under_target": True}):
        sel = HangerSelection(part="1119", part_name="1119-HANGER", fraction=0.1855, **flags)
        with patch("engine.resize.interpret.log", new=lambda msg, *a, **k: logged.append(str(msg))):
            note = interpret._hanger_note(sel)
        assert note == " Hanger to be replaced with prefab #1119 (18.55% of the glass)."
    assert len([l for l in logged if "[WARN]" in l]) == 3
