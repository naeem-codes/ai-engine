"""Two dims of identical value must not decide the master between them by read order.

Live on ECLIPSE, 2026-09-14. The glass `D1@Sketch1 [1026-MIRROR-ECLIPSE-1]` and the lit ring
`D1@Sketch1 [LED FLEX EXTRUSION-ECLIPSE  30]` are BOTH exactly 762.00 mm, because the ring is
drawn at the glass diameter. The app kept whichever it read first, and that flipped between
connects — the glass at 14:29, the LED extrusion at 15:35 and 16:08.

With the LED as master the damage is silent. No rule is keyed on the LED, so the `[OVERALL]`
safety net could not fire; the LLM's own anchor went through unchecked and the glass — which
appears in no `also_change`, only as the rule's trigger — was never written. The chassis and the
LED went to Ø45 while the mirror stayed at Ø30. Every mate solved, SolidWorks reported no error,
and the crossing check refused the whole resize:

    12418-CHASSIS-CIRCLINE-2 — was 0.250in inside, now 7.250in outside

The app now prefers the mirror glass outright (`PreferGlassMaster`). This file pins the engine
half: a master that is some rule's DEPENDENT is followed back to that rule's trigger, so the
family still moves together even if the app hands over the wrong end of it.
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
GLASS = "D1@Sketch1 [1026-MIRROR-ECLIPSE-1]"            # 30.000" — the real master
LED = "D1@Sketch1 [LED FLEX EXTRUSION-ECLIPSE  30]"     # 30.000" — ties with it, exactly
CHASSIS = "D1@Sketch1 [12418-CHASSIS-CIRCLINE-2]"       # 29.500"
SLOTS = "D2@Sketch40 [12418-CHASSIS-CIRCLINE-2]"        # 6.000"

DIMS = {GLASS: 30 * IN, LED: 30 * IN, CHASSIS: 29.5 * IN, SLOTS: 6 * IN}
AXIS = {GLASS: "W", LED: "W", CHASSIS: "W", SLOTS: "W"}
LABELS = {"1026-MIRROR-ECLIPSE-1": "Mirror Glass",
          "LED FLEX EXTRUSION-ECLIPSE  30": "LED Flex Extrusion",
          "12418-CHASSIS-CIRCLINE-2": "Chassis Circline"}


@pytest.fixture
def rules_dir(tmp_path, monkeypatch):
    # The real generated set: keyed on the GLASS, with the LED as one of its dependants.
    doc = {"model": "EclipseTest",
           "width": [{"if_changes": GLASS, "also_change": [CHASSIS, LED]}],
           "height": [], "component_labels": LABELS}
    d = tmp_path / "rules"
    d.mkdir()
    (d / "EclipseTest.rules.json").write_text(json.dumps(doc))
    monkeypatch.setattr(rules, "RULES_DIR", d)
    return tmp_path


async def _resize(rules_dir, master, llm_anchor, target_in=45):
    """Resize with `master` reported by the app and `llm_anchor` chosen by the model.

    The stub mirrors what the model really does: anchored on the GLASS it echoes the stored rule,
    and anchored anywhere else it improvises a short list -- which is exactly the shape of the
    16:08 response that dropped the mirror.
    """
    also = [CHASSIS, LED] if llm_anchor == GLASS else [CHASSIS]
    llm = json.dumps({"rule": {"if_changes": llm_anchor, "also_change": also},
                      "value_meters": target_in * IN, "scope": "overall"})
    with patch("interpret.call_llm", new=AsyncMock(return_value=llm)):
        return await interpret(InterpretRequest(
            instruction=f"Resize to {target_in}.00",
            dimensions=[DimensionIn(name=n, value_meters=v) for n, v in DIMS.items()],
            dim_axis_labels=AXIS, master_width_dim=master, master_height_dim=None,
            shape="round", master_radial=1,
            radial_dims={GLASS: 1, LED: 1, CHASSIS: 1},
            model_path=str(rules_dir / "EclipseTest.SLDASM")))


@pytest.mark.asyncio
async def test_the_glass_is_written_even_when_the_led_is_handed_over_as_master(rules_dir):
    """The reported failure: master = the LED, and the mirror silently never moved."""
    res = await _resize(rules_dir, master=LED, llm_anchor=LED)
    by_name = {c.name: round(c.value_meters / IN, 3) for c in res.changes}
    assert by_name.get(GLASS) == 45.0, "the mirror was left behind again"
    assert by_name.get(LED) == 45.0
    assert by_name.get(CHASSIS) == 44.5      # constant 0.5" offset from the glass


@pytest.mark.asyncio
async def test_the_normal_case_is_unchanged(rules_dir):
    """Master = the glass, which is what the app now always reports."""
    res = await _resize(rules_dir, master=GLASS, llm_anchor=GLASS)
    by_name = {c.name: round(c.value_meters / IN, 3) for c in res.changes}
    assert by_name.get(GLASS) == 45.0
    assert by_name.get(LED) == 45.0
    assert by_name.get(CHASSIS) == 44.5


@pytest.mark.asyncio
async def test_an_llm_anchor_on_a_dependent_is_still_corrected(rules_dir):
    """The master is right but the model picked a dependant. The existing net handles this."""
    res = await _resize(rules_dir, master=GLASS, llm_anchor=CHASSIS)
    by_name = {c.name: round(c.value_meters / IN, 3) for c in res.changes}
    assert by_name.get(GLASS) == 45.0
    assert by_name.get(LED) == 45.0
    assert by_name.get(CHASSIS) == 44.5


@pytest.mark.asyncio
async def test_nothing_is_dropped_when_the_master_is_in_no_rule_at_all(rules_dir):
    """A master that is neither a trigger nor a dependant leaves the anchor alone rather than
    guessing — the same as before this fix."""
    res = await _resize(rules_dir, master=SLOTS, llm_anchor=GLASS)
    by_name = {c.name: round(c.value_meters / IN, 3) for c in res.changes}
    assert by_name.get(GLASS) == 45.0
