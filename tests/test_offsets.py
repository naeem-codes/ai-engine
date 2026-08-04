"""Tests for fixed-offset dimension links (rules.expand_offsets + load_rules parsing)."""
import json
import pytest
from unittest.mock import patch, AsyncMock

import rules
from rules import ModelRules, OffsetRuleEntry, expand_offsets
from models import InterpretRequest, DimensionIn
from interpret import interpret


def _mr(offsets):
    return ModelRules(model="X", limits=None, offset=offsets)


# ── expand_offsets unit tests ────────────────────────────────────────────────

def test_offset_fires_when_source_changes():
    # A starts 2 cm to the right of B (0.12 vs 0.10). B grows to 0.15 → A follows to 0.17.
    mr = _mr([OffsetRuleEntry(target_dim="A", source_dim="B", offset_meters=0.02)])
    out = expand_offsets(mr, {"B": 0.15}, {"A": 0.12, "B": 0.10})
    assert out == [("A", pytest.approx(0.17))]


def test_offset_skips_when_source_not_changing():
    mr = _mr([OffsetRuleEntry(target_dim="A", source_dim="B", offset_meters=0.02)])
    # The source dim B is NOT among this turn's changes → rule does not fire.
    assert expand_offsets(mr, {"SOMETHING_ELSE": 0.9}, {"A": 0.12, "B": 0.10}) == []


def test_offset_supports_negative_difference():
    # A sits 3 cm to the LEFT of B; the negative offset is preserved absolutely.
    mr = _mr([OffsetRuleEntry(target_dim="A", source_dim="B", offset_meters=-0.03)])
    out = expand_offsets(mr, {"B": 0.20}, {"A": 0.07, "B": 0.10})
    assert out == [("A", pytest.approx(0.17))]


def test_offset_is_absolute_not_ratio():
    # A doubling of B must NOT double the gap — the difference stays fixed.
    mr = _mr([OffsetRuleEntry(target_dim="A", source_dim="B", offset_meters=0.02)])
    out = expand_offsets(mr, {"B": 0.20}, {"A": 0.12, "B": 0.10})
    assert out == [("A", pytest.approx(0.22))]  # 0.20 + 0.02, not 0.24


# ── load_rules parsing ────────────────────────────────────────────────────────

@pytest.fixture()
def offset_rules_dir(tmp_path, monkeypatch):
    doc = {
        "model": "OffsetTest",
        "width": [{"if_changes": "WIDTH@Mirror", "also_change": ["CHASSIS_W@Chassis"]}],
        "height": [],
        "offset": [
            {"component": "A", "target_dim": "D1@DistX",
             "source_dim": "CHASSIS_W@Chassis", "offset_meters": 0.02},
            # Incomplete entries (missing source/target) must be dropped by the parser.
            {"target_dim": "D1@DistY"},
            {"source_dim": "CHASSIS_W@Chassis"},
        ],
    }
    d = tmp_path / "rules"
    d.mkdir()
    (d / "OffsetTest.rules.json").write_text(json.dumps(doc))
    monkeypatch.setattr(rules, "RULES_DIR", d)
    return tmp_path


def test_offset_rules_parsed(offset_rules_dir):
    mr = rules.load_rules(str(offset_rules_dir / "OffsetTest.SLDASM"))
    assert mr is not None
    assert len(mr.offset) == 1          # the two incomplete entries were skipped
    r = mr.offset[0]
    assert r.target_dim == "D1@DistX"
    assert r.source_dim == "CHASSIS_W@Chassis"
    assert r.offset_meters == 0.02


# ── End-to-end: offset change is appended by interpret() ──────────────────────

@pytest.mark.asyncio
async def test_interpret_appends_offset_change(offset_rules_dir):
    dims = [
        DimensionIn(name="WIDTH@Mirror", value_meters=0.6096),   # 24 in
        DimensionIn(name="CHASSIS_W@Chassis", value_meters=0.4318),
        DimensionIn(name="D1@DistX", value_meters=0.1016),       # A's offset dim
    ]
    axis = {"WIDTH@Mirror": "W", "CHASSIS_W@Chassis": "W", "D1@DistX": "W"}
    llm_json = json.dumps({
        "rule": {"if_changes": "WIDTH@Mirror", "also_change": ["CHASSIS_W@Chassis"]},
        "value_meters": 0.762,   # grow to 30 in
    })
    with patch("interpret.call_llm", new=AsyncMock(return_value=llm_json)):
        result = await interpret(InterpretRequest(
            instruction="make it 30 inches wide",
            dimensions=dims,
            dim_axis_labels=axis,
            model_path=str(offset_rules_dir / "OffsetTest.SLDASM"),
        ))
    assert result.error is None
    by_name = {c.name: c.value_meters for c in result.changes}
    # The chassis tracks the master by a CONSTANT offset (not proportionally — see
    # interpret._dependent_value); A is then set to the new chassis value + the fixed 2 cm.
    new_chassis = 0.4318 + (0.762 - 0.6096)
    assert by_name["CHASSIS_W@Chassis"] == pytest.approx(new_chassis)
    assert "D1@DistX" in by_name
    assert by_name["D1@DistX"] == pytest.approx(new_chassis + 0.02)
