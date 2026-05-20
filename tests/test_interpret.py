import json
import pytest
from pathlib import Path
from unittest.mock import patch, AsyncMock

import rules
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


# ── Case 2: classification (no rules file) ────────────────────────────────────

@pytest.mark.asyncio
async def test_classification_calculates_ratios(base_dims):
    llm_json = json.dumps({
        "target_width_meters": 0.762,
        "master_width_dim": "WIDTH@Mirror",
        "width_dims": ["WIDTH@Mirror", "LED_WIDTH@LED"],
        "target_height_meters": None,
        "master_height_dim": None,
        "height_dims": [],
        "explanation": "Resizing width to 30 inches",
    })
    with patch("interpret.call_llm", new=AsyncMock(return_value=llm_json)):
        result = await interpret(InterpretRequest(
            instruction="set width to 30 inches",
            dimensions=base_dims,
            assembly_context="ASSEMBLY CONTEXT",
            dim_axis_labels=BASE_AXIS_LABELS,
            model_path=None,
        ))
    assert result.error is None
    width_change = next(c for c in result.changes if c.name == "WIDTH@Mirror")
    led_change = next(c for c in result.changes if c.name == "LED_WIDTH@LED")
    assert width_change.value_meters == pytest.approx(0.762)
    assert led_change.value_meters == pytest.approx((0.45 / 0.5) * 0.762)


@pytest.mark.asyncio
async def test_classification_height_only(base_dims):
    llm_json = json.dumps({
        "target_width_meters": None,
        "master_width_dim": None,
        "width_dims": [],
        "target_height_meters": 1.5,
        "master_height_dim": "HEIGHT@Mirror",
        "height_dims": ["HEIGHT@Mirror"],
        "explanation": "Resizing height to 1.5m",
    })
    with patch("interpret.call_llm", new=AsyncMock(return_value=llm_json)):
        result = await interpret(InterpretRequest(
            instruction="set height to 1.5m",
            dimensions=base_dims,
            assembly_context="ASSEMBLY CONTEXT",
            dim_axis_labels=BASE_AXIS_LABELS,
            model_path=None,
        ))
    assert result.error is None
    assert len(result.changes) == 1
    assert result.changes[0].name == "HEIGHT@Mirror"
    assert result.changes[0].value_meters == pytest.approx(1.5)


@pytest.mark.asyncio
async def test_classification_malformed_json_returns_error(base_dims):
    with patch("interpret.call_llm", new=AsyncMock(return_value="not json {")):
        result = await interpret(InterpretRequest(
            instruction="set width to 30 inches",
            dimensions=base_dims,
            assembly_context="ASSEMBLY CONTEXT",
            dim_axis_labels=BASE_AXIS_LABELS,
            model_path=None,
        ))
    assert result.error is not None
    assert "invalid JSON" in result.error


@pytest.mark.asyncio
async def test_classification_single_scope(base_dims):
    llm_json = json.dumps({
        "changes": [{"dimension": "WIDTH@Mirror", "value_meters": 0.762}],
        "explanation": "Changing only mirror glass width",
    })
    with patch("interpret.call_llm", new=AsyncMock(return_value=llm_json)):
        result = await interpret(InterpretRequest(
            instruction="change mirror glass width to 30 inches",
            dimensions=base_dims,
            dim_axis_labels=BASE_AXIS_LABELS,
            model_path=None,
        ))
    assert result.error is None
    assert len(result.changes) == 1
    assert result.changes[0].name == "WIDTH@Mirror"
    assert result.changes[0].value_meters == pytest.approx(0.762)


@pytest.mark.asyncio
async def test_classification_uses_request_master_when_llm_omits_it(base_dims):
    llm_json = json.dumps({
        "target_width_meters": 0.762,
        "width_dims": ["WIDTH@Mirror", "LED_WIDTH@LED"],
        "target_height_meters": None,
        "height_dims": [],
        "explanation": "Overall resize",
    })
    with patch("interpret.call_llm", new=AsyncMock(return_value=llm_json)):
        result = await interpret(InterpretRequest(
            instruction="set width to 30 inches",
            dimensions=base_dims,
            dim_axis_labels=BASE_AXIS_LABELS,
            master_width_dim="WIDTH@Mirror",
            model_path=None,
        ))
    assert result.error is None
    assert any(c.name == "WIDTH@Mirror" for c in result.changes)


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
