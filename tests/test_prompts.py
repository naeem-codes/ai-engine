"""The two surviving prompt builders.

`rules_prompt` and `classification_prompt` are both gone. The first was removed before this
module was last touched (so these tests had been failing to import); the second went with the
LLM-classification fallback — a resize now REQUIRES a rule set, so there is no prompt for
"figure the scope and the dim list out yourself".
"""

import prompts
from prompts import rules_dependent_prompt, rules_system_prompt


def test_no_classification_prompt_survives():
    """Guards the deletion: nothing may reintroduce a resize prompt that runs without rules."""
    assert not hasattr(prompts, "classification_prompt")


def test_rules_dependent_prompt_carries_the_rules_and_masters():
    prompt = rules_dependent_prompt(
        '{"width": [{"if_changes": "WIDTH@Mirror", "also_change": ["LED_WIDTH@LED"]}]}',
        master_width_dim="WIDTH@Mirror",
        master_height_dim="HEIGHT@Mirror",
    )
    assert "WIDTH@Mirror" in prompt
    assert "LED_WIDTH@LED" in prompt
    assert "master width  = WIDTH@Mirror" in prompt
    assert "master height = HEIGHT@Mirror" in prompt
    # The response shape the engine parses
    assert "if_changes" in prompt
    assert "value_meters" in prompt
    assert "scope" in prompt


def test_rules_dependent_prompt_includes_component_labels():
    prompt = rules_dependent_prompt("{}", labels_block="\n  LPM-24096A-2  =  Right power supply\n")
    assert "Right power supply" in prompt


def test_rules_dependent_prompt_tolerates_missing_masters():
    prompt = rules_dependent_prompt("{}")
    assert "master width  = null" in prompt
    assert "master height = null" in prompt


def test_rules_system_prompt_contains_the_dim_list_and_context():
    prompt = rules_system_prompt("ASSEMBLY CONTEXT HERE", "  [W]  WIDTH@Mirror = 500.00 mm",
                                 master_width_dim="WIDTH@Mirror")
    assert "ASSEMBLY CONTEXT HERE" in prompt
    assert "WIDTH@Mirror" in prompt
