"""A "24 x 36" request must set BOTH axes.

The response schema carries one rule, so before `other_axis_meters` a two-number request silently
moved only the width. Seen live 2026-08-06: "Change Width to 24.00 X 36.00 inches" resized the
width and told the user to submit the height separately — which looked correct only because that
model's height already happened to be 36".
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
MIRROR_W = "WIDTH@Sketch1 [1011-MIRROR-CAROL-1]"
MIRROR_H = "HEIGHT@Sketch1 [1011-MIRROR-CAROL-1]"
CHASSIS_W = "D1@Sketch1 [12211-CHASSIS-2]"
CHASSIS_H = "D2@Sketch1 [12211-CHASSIS-2]"

DIMS = {MIRROR_W: 60 * IN, MIRROR_H: 36 * IN, CHASSIS_W: 59 * IN, CHASSIS_H: 34 * IN}
AXIS = {MIRROR_W: "W", MIRROR_H: "H", CHASSIS_W: "W", CHASSIS_H: "H"}


@pytest.fixture
def rules_dir(tmp_path, monkeypatch):
    doc = {
        "model": "TwoAxis",
        "width": [{"if_changes": MIRROR_W, "also_change": [CHASSIS_W]}],
        "height": [{"if_changes": MIRROR_H, "also_change": [CHASSIS_H]}],
        "component_labels": {},
    }
    (tmp_path / "TwoAxis.rules.json").write_text(json.dumps(doc))
    monkeypatch.setattr(rules, "RULES_DIR", tmp_path, raising=False)
    import rules_store
    monkeypatch.setattr(rules_store, "RULES_DIR", tmp_path, raising=False)
    return tmp_path


async def _run(rules_dir, llm_payload, dims=None):
    with patch("interpret.call_llm", new=AsyncMock(return_value=json.dumps(llm_payload))):
        return await interpret(InterpretRequest(
            instruction="change to 24 x 36",
            dimensions=[DimensionIn(name=n, value_meters=v)
                        for n, v in (dims or DIMS).items()],
            dim_axis_labels=AXIS,
            master_width_dim=MIRROR_W, master_height_dim=MIRROR_H,
            model_path=str(rules_dir / "TwoAxis.SLDASM"),
        ))


def _both_axes(width_m, height_m):
    return {
        "rule": {"if_changes": MIRROR_W, "also_change": [CHASSIS_W]},
        "scope": "overall",
        "value_meters": width_m,
        "other_axis_meters": height_m,
        "explanation": "setting width and height",
    }


@pytest.mark.asyncio
async def test_both_axes_are_applied(rules_dir):
    res = await _run(rules_dir, _both_axes(24 * IN, 48 * IN))
    by = {c.name: c.value_meters for c in res.changes}
    assert by[MIRROR_W] / IN == pytest.approx(24)
    assert by[MIRROR_H] / IN == pytest.approx(48), "the height was silently dropped"


@pytest.mark.asyncio
async def test_the_second_axis_brings_its_own_dependents(rules_dir):
    res = await _run(rules_dir, _both_axes(24 * IN, 48 * IN))
    by = {c.name: c.value_meters for c in res.changes}
    assert by[CHASSIS_W] / IN == pytest.approx(23)     # 59 + (24-60)
    assert by[CHASSIS_H] / IN == pytest.approx(46)     # 34 + (48-36)


@pytest.mark.asyncio
async def test_an_unchanged_second_axis_is_a_no_op_and_says_so(rules_dir):
    """The live case: height already 36. Nothing should move, and the explanation must not
    claim otherwise or tell the user to resubmit it."""
    res = await _run(rules_dir, _both_axes(24 * IN, 36 * IN))
    by = {c.name: c.value_meters for c in res.changes}
    assert MIRROR_H not in by
    assert CHASSIS_H not in by
    assert "already" in res.explanation.lower()
    assert "separate" not in res.explanation.lower()


@pytest.mark.asyncio
async def test_single_axis_request_is_unaffected(rules_dir):
    res = await _run(rules_dir, {
        "rule": {"if_changes": MIRROR_W, "also_change": [CHASSIS_W]},
        "scope": "overall",
        "value_meters": 24 * IN,
        "explanation": "width only",
    })
    by = {c.name: c.value_meters for c in res.changes}
    assert by[MIRROR_W] / IN == pytest.approx(24)
    assert MIRROR_H not in by


@pytest.mark.asyncio
async def test_height_first_request_also_works(rules_dir):
    """If the LLM anchors on the height master, the other axis must resolve to the WIDTH."""
    res = await _run(rules_dir, {
        "rule": {"if_changes": MIRROR_H, "also_change": [CHASSIS_H]},
        "scope": "overall",
        "value_meters": 48 * IN,
        "other_axis_meters": 24 * IN,
        "explanation": "height and width",
    })
    by = {c.name: c.value_meters for c in res.changes}
    assert by[MIRROR_H] / IN == pytest.approx(48)
    assert by[MIRROR_W] / IN == pytest.approx(24)


@pytest.mark.asyncio
async def test_garbage_second_value_is_ignored_not_fatal(rules_dir):
    for bad in ("", None, "abc", -5, 0):
        payload = _both_axes(24 * IN, 48 * IN)
        payload["other_axis_meters"] = bad
        res = await _run(rules_dir, payload)
        by = {c.name: c.value_meters for c in res.changes}
        assert by[MIRROR_W] / IN == pytest.approx(24), f"width broke on {bad!r}"
        assert MIRROR_H not in by, f"{bad!r} should not move the height"


@pytest.mark.asyncio
async def test_second_axis_ignored_when_scope_is_not_overall(rules_dir):
    payload = _both_axes(24 * IN, 48 * IN)
    payload["scope"] = ""
    res = await _run(rules_dir, payload)
    by = {c.name: c.value_meters for c in res.changes}
    assert MIRROR_H not in by
