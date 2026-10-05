import json
import pytest
from pathlib import Path
from engine.rules import rules


FIXTURE = Path(__file__).parent / "fixture.rules.json"


@pytest.fixture()
def model_rules(tmp_path, monkeypatch):
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "TestMirror.rules.json").write_text(FIXTURE.read_text())
    monkeypatch.setattr(rules, "RULES_DIR", rules_dir)
    return rules.load_rules(str(tmp_path / "TestMirror.SLDASM"))


def test_load_rules_returns_none_when_no_file():
    assert rules.load_rules(None) is None
    assert rules.load_rules("C:\\missing\\Unknown.SLDASM") is None


def test_load_rules_finds_by_model_stem(model_rules):
    assert model_rules is not None
    assert model_rules.model == "TestMirror"


def test_get_triggers(model_rules):
    assert rules.get_triggers(model_rules) == ["width", "height"]


def test_expand_width(model_rules):
    result = rules.expand(model_rules, "width", 0.762)
    assert len(result) == 2
    assert result[0] == ("WIDTH@Mirror", pytest.approx(0.762))
    assert result[1] == ("LED_WIDTH@LED", pytest.approx(0.762 * 0.9))


def test_expand_returns_empty_for_unknown_trigger(model_rules):
    assert rules.expand(model_rules, "depth", 0.5) == []


def test_validate_passes_within_limits(model_rules):
    assert rules.validate(model_rules, "width", 0.762) is None


def test_validate_fails_below_min_width(model_rules):
    error = rules.validate(model_rules, "width", 0.1)
    assert error is not None
    assert "below minimum" in error


def test_validate_fails_above_max_width(model_rules):
    error = rules.validate(model_rules, "width", 2.0)
    assert error is not None
    assert "exceeds maximum" in error


def test_validate_returns_none_when_no_limits():
    no_limit_rules = rules.ModelRules(model="X", limits=None, rules=[])
    assert rules.validate(no_limit_rules, "width", 99.0) is None


def test_validate_fails_below_min_height(model_rules):
    error = rules.validate(model_rules, "height", 0.1)
    assert error is not None
    assert "below minimum" in error


def test_validate_fails_above_max_height(model_rules):
    error = rules.validate(model_rules, "height", 2.5)
    assert error is not None
    assert "exceeds maximum" in error
