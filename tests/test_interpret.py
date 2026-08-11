import json
import pytest
from pathlib import Path
from unittest.mock import patch, AsyncMock

import rules
import rules_store as store
from models import InterpretRequest, DimensionIn
from interpret import interpret

FIXTURE = Path(__file__).parent / "fixture.rules.json"

BASE_AXIS_LABELS = {
    "WIDTH@Mirror": "W",
    "HEIGHT@Mirror": "H",
    "LED_WIDTH@LED": "W",
}


@pytest.fixture()
def rules_dir(tmp_path, monkeypatch):
    d = tmp_path / "rules"
    d.mkdir()
    (d / "TestMirror.rules.json").write_text(FIXTURE.read_text())
    monkeypatch.setattr(rules, "RULES_DIR", d)
    return tmp_path


@pytest.fixture()
def base_dims():
    return [
        DimensionIn(name="WIDTH@Mirror", value_meters=0.5),
        DimensionIn(name="HEIGHT@Mirror", value_meters=1.0),
        DimensionIn(name="LED_WIDTH@LED", value_meters=0.45),
    ]


# ── Case 1: rules trigger ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rules_trigger_expands_correctly(rules_dir, base_dims):
    llm_json = '{"trigger": "width", "value_meters": 0.762, "explanation": "30 inches"}'
    with patch("interpret.call_llm", new=AsyncMock(return_value=llm_json)):
        result = await interpret(InterpretRequest(
            instruction="set width to 30 inches",
            dimensions=base_dims,
            model_path=str(rules_dir / "TestMirror.SLDASM"),
        ))
    assert result.error is None
    assert len(result.changes) == 2
    assert result.changes[0].name == "WIDTH@Mirror"
    assert result.changes[0].value_meters == pytest.approx(0.762)
    assert result.changes[1].name == "LED_WIDTH@LED"
    assert result.changes[1].value_meters == pytest.approx(0.762 * 0.9)


@pytest.mark.asyncio
async def test_rules_trigger_blocked_by_limit(rules_dir, base_dims):
    llm_json = '{"trigger": "width", "value_meters": 0.05}'
    with patch("interpret.call_llm", new=AsyncMock(return_value=llm_json)):
        result = await interpret(InterpretRequest(
            instruction="set width to 2 inches",
            dimensions=base_dims,
            model_path=str(rules_dir / "TestMirror.SLDASM"),
        ))
    assert result.error is not None
    assert "below minimum" in result.error
    assert result.changes == []


@pytest.mark.asyncio
async def test_rules_error_from_llm(rules_dir, base_dims):
    llm_json = '{"error": "unclear instruction"}'
    with patch("interpret.call_llm", new=AsyncMock(return_value=llm_json)):
        result = await interpret(InterpretRequest(
            instruction="do something",
            dimensions=base_dims,
            model_path=str(rules_dir / "TestMirror.SLDASM"),
        ))
    assert result.error == "unclear instruction"


@pytest.mark.asyncio
async def test_rules_malformed_json_returns_error(rules_dir, base_dims):
    with patch("interpret.call_llm", new=AsyncMock(return_value="not valid json {")):
        result = await interpret(InterpretRequest(
            instruction="set width to 30 inches",
            dimensions=base_dims,
            model_path=str(rules_dir / "TestMirror.SLDASM"),
        ))
    assert result.error is not None
    assert "invalid JSON" in result.error


@pytest.mark.asyncio
async def test_rules_direct_changes_path(rules_dir, base_dims):
    llm_json = '{"changes": [{"dimension": "WIDTH@Mirror", "value_meters": 0.5}], "explanation": "direct"}'
    with patch("interpret.call_llm", new=AsyncMock(return_value=llm_json)):
        result = await interpret(InterpretRequest(
            instruction="set WIDTH@Mirror to 500mm",
            dimensions=base_dims,
            model_path=str(rules_dir / "TestMirror.SLDASM"),
        ))
    assert result.error is None
    assert len(result.changes) == 1
    assert result.changes[0].name == "WIDTH@Mirror"


# ── No rules → refuse (there is no LLM-classification fallback) ───────────────
#
# A resize may only run from an authored, human-reviewed rule set. The old fallback asked the
# LLM to pick the scope and the dim list itself for a model nobody had generated rules for,
# and it ran silently — BREAM resized through it unnoticed.

@pytest.mark.asyncio
async def test_no_rules_is_refused(base_dims):
    llm = AsyncMock(return_value="{}")
    with patch("interpret.call_llm", new=llm):
        result = await interpret(InterpretRequest(
            instruction="set width to 30 inches",
            dimensions=base_dims,
            assembly_context="ASSEMBLY CONTEXT",
            dim_axis_labels=BASE_AXIS_LABELS,
            master_width_dim="WIDTH@Mirror",
            model_path=r"C:\models\NORULES-24.00X36.00.SLDASM",
        ))
    assert result.changes == []
    assert result.needs_rules is True
    assert "Generate Rules" in (result.error or "")
    # Refused BEFORE the LLM is called — no tokens spent deciding something we will not apply.
    llm.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_rules_refusal_names_the_model(base_dims):
    with patch("interpret.call_llm", new=AsyncMock(return_value="{}")):
        result = await interpret(InterpretRequest(
            instruction="resize to 40 x 40 inches",
            dimensions=base_dims,
            dim_axis_labels=BASE_AXIS_LABELS,
            model_path=r"C:\models\BREAM-24.00X36.00-LED.SLDASM",
        ))
    assert result.needs_rules is True
    assert "BREAM" in (result.error or "")


@pytest.mark.asyncio
async def test_rules_path_does_not_set_needs_rules(base_dims):
    """needs_rules means "generate rules first" specifically — not a generic failure flag."""
    store.write_key("HASRULES", {
        "model": "HASRULES", "family": "HASRULES",
        "width": [{"if_changes": "WIDTH@Mirror", "also_change": ["LED_WIDTH@LED"]}],
        "height": [],
    })
    with patch("interpret.call_llm", new=AsyncMock(return_value='{"error": "unclear"}')):
        result = await interpret(InterpretRequest(
            instruction="do something",
            dimensions=base_dims,
            dim_axis_labels=BASE_AXIS_LABELS,
            master_width_dim="WIDTH@Mirror",
            model_path=r"C:\models\HASRULES.SLDASM",
        ))
    assert result.error == "unclear"
    assert not result.needs_rules



# ── Case 3: no context ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_context_returns_error():
    result = await interpret(InterpretRequest(
        instruction="set width to 30 inches",
        dimensions=[],
        assembly_context=None,
        model_path=None,
    ))
    assert result.error is not None
    assert "Refresh" in result.error
