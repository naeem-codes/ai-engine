from prompts import rules_prompt, classification_prompt


def test_rules_prompt_contains_all_triggers():
    prompt = rules_prompt(["width", "height"])
    assert "width" in prompt
    assert "height" in prompt
    assert "value_meters" in prompt
    assert "trigger" in prompt


def test_rules_prompt_single_trigger():
    prompt = rules_prompt(["width"])
    assert "width" in prompt


def test_classification_prompt_contains_context():
    prompt = classification_prompt("ASSEMBLY CONTEXT HERE", "  WIDTH@Mirror = 500.00 mm")
    assert "ASSEMBLY CONTEXT HERE" in prompt
    assert "WIDTH@Mirror" in prompt
    assert "width_dims" in prompt
    assert "height_dims" in prompt
    assert "master_width_dim" in prompt
