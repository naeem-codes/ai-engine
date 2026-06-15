"""Tests for edge-follow position rules (rules.expand_positions + load_rules parsing)."""
import json
import pytest
from unittest.mock import patch, AsyncMock

import rules
from models import InterpretRequest, DimensionIn
from interpret import interpret


def _kelly():
    # Loads the real seeded model rules from the engine's rules/ dir.
    mr = rules.load_rules("KELLY-24.00X48.00-LED.SLDASM")
    assert mr is not None, "KELLY rules file should load"
    return mr


def test_position_rules_parsed():
    mr = _kelly()
    assert len(mr.position) == 2
    by_dim = {p.position_dim: p for p in mr.position}
    assert by_dim["D1@Distance2"].axis == "width"
    assert by_dim["D1@Distance2"].driver_dim == "D1@Sketch1 [9535-CHASSIS-2]"
    assert by_dim["D1@Distance2"].factor == 0.5
    assert by_dim["D1@Distance3"].axis == "height"


def test_expand_positions_width_follows_edge():
    mr = _kelly()
    current = {
        "D1@Sketch1 [9535-CHASSIS-2]": 0.4318,   # chassis width 431.8 mm
        "D1@Distance2": 0.1016,                   # LPM x-offset 101.6 mm
        "D1@Distance3": 0.1143,
        "D2@Sketch1 [9535-CHASSIS-2]": 1.1811,
    }
    # Chassis width grew by 100 mm → edge moved out 50 mm → LPM should move +50 mm.
    changes = {"D1@Sketch1 [9535-CHASSIS-2]": 0.5318}
    out = rules.expand_positions(mr, "width", changes, current)
    assert out == [("D1@Distance2", pytest.approx(0.1516))]


def test_expand_positions_height_follows_edge():
    mr = _kelly()
    current = {
        "D2@Sketch1 [9535-CHASSIS-2]": 1.1811,
        "D1@Distance3": 0.1143,
    }
    changes = {"D2@Sketch1 [9535-CHASSIS-2]": 1.2811}  # +100 mm
    out = rules.expand_positions(mr, "height", changes, current)
    assert out == [("D1@Distance3", pytest.approx(0.1643))]


def test_expand_positions_skips_when_driver_not_changing():
    mr = _kelly()
    current = {"D1@Sketch1 [9535-CHASSIS-2]": 0.4318, "D1@Distance2": 0.1016}
    # The chassis width driver is NOT among this turn's changes.
    changes = {"SOME_OTHER_DIM": 0.9}
    assert rules.expand_positions(mr, "width", changes, current) == []


def test_expand_positions_wrong_axis_ignored():
    mr = _kelly()
    current = {"D1@Sketch1 [9535-CHASSIS-2]": 0.4318, "D1@Distance2": 0.1016}
    changes = {"D1@Sketch1 [9535-CHASSIS-2]": 0.5318}
    # Width-driver change under a height trigger must not fire the width rule.
    assert rules.expand_positions(mr, "height", changes, current) == []


# ── End-to-end: position change is appended by interpret() ────────────────────

@pytest.fixture()
def pos_rules_dir(tmp_path, monkeypatch):
    doc = {
        "model": "PosTest",
        "width": [{"if_changes": "WIDTH@Mirror", "also_change": ["CHASSIS_W@Chassis"]}],
        "height": [],
        "position": [{
            "component": "PSU", "position_dim": "D1@DistX",
            "driver_dim": "CHASSIS_W@Chassis", "axis": "width", "factor": 0.5,
        }],
    }
    d = tmp_path / "rules"
    d.mkdir()
    (d / "PosTest.rules.json").write_text(json.dumps(doc))
    monkeypatch.setattr(rules, "RULES_DIR", d)
    return tmp_path


@pytest.mark.asyncio
async def test_interpret_appends_position_change(pos_rules_dir):
    dims = [
        DimensionIn(name="WIDTH@Mirror", value_meters=0.6096),   # 24 in
        DimensionIn(name="CHASSIS_W@Chassis", value_meters=0.4318),
        DimensionIn(name="D1@DistX", value_meters=0.1016),       # PSU offset from centre
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
            model_path=str(pos_rules_dir / "PosTest.SLDASM"),
        ))
    assert result.error is None
    by_name = {c.name: c.value_meters for c in result.changes}
    # Chassis grew proportionally; the PSU offset followed by half that delta.
    new_chassis = (0.4318 / 0.6096) * 0.762
    expected_pos = 0.1016 + 0.5 * (new_chassis - 0.4318)
    assert "D1@DistX" in by_name
    assert by_name["D1@DistX"] == pytest.approx(expected_pos)
