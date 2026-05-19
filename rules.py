import dataclasses
import json
from dataclasses import dataclass, field
from pathlib import Path

RULES_DIR = Path(__file__).parent / "rules"


@dataclass
class DimensionRule:
    name: str
    ratio: float


@dataclass
class TriggerRule:
    trigger: str
    dimensions: list[DimensionRule] = field(default_factory=list)


@dataclass
class SizeLimits:
    min_width_mm: float = 0.0
    max_width_mm: float = float("inf")
    min_height_mm: float = 0.0
    max_height_mm: float = float("inf")


@dataclass
class ModelRules:
    model: str
    limits: SizeLimits | None
    rules: list[TriggerRule] = field(default_factory=list)


def load_rules(model_path: str | None) -> ModelRules | None:
    if not model_path:
        return None
    stem = Path(model_path).stem
    candidate = RULES_DIR / f"{stem}.rules.json"
    if not candidate.exists():
        return None
    data = json.loads(candidate.read_text())
    limits_data = data.get("limits")
    if limits_data:
        known = {f.name for f in dataclasses.fields(SizeLimits)}
        limits = SizeLimits(**{k: v for k, v in limits_data.items() if k in known})
    else:
        limits = None
    trigger_rules = [
        TriggerRule(
            trigger=r["trigger"],
            dimensions=[DimensionRule(name=d["name"], ratio=d["ratio"]) for d in r.get("dimensions", [])],
        )
        for r in data.get("rules", [])
    ]
    return ModelRules(model=data.get("model", stem), limits=limits, rules=trigger_rules)


def get_triggers(model_rules: ModelRules) -> list[str]:
    return [r.trigger for r in model_rules.rules]


def validate(model_rules: ModelRules, trigger: str, value_meters: float) -> str | None:
    if model_rules.limits is None:
        return None
    mm = value_meters * 1000
    L = model_rules.limits
    t = trigger.lower()
    if t == "width":
        if mm < L.min_width_mm:
            return f"Width {mm:.0f}mm ({mm/25.4:.2f}in) is below minimum {L.min_width_mm:.0f}mm ({L.min_width_mm/25.4:.2f}in)"
        if mm > L.max_width_mm:
            return f"Width {mm:.0f}mm ({mm/25.4:.2f}in) exceeds maximum {L.max_width_mm:.0f}mm ({L.max_width_mm/25.4:.2f}in)"
    elif t == "height":
        if mm < L.min_height_mm:
            return f"Height {mm:.0f}mm ({mm/25.4:.2f}in) is below minimum {L.min_height_mm:.0f}mm ({L.min_height_mm/25.4:.2f}in)"
        if mm > L.max_height_mm:
            return f"Height {mm:.0f}mm ({mm/25.4:.2f}in) exceeds maximum {L.max_height_mm:.0f}mm ({L.max_height_mm/25.4:.2f}in)"
    return None


def expand(model_rules: ModelRules, trigger: str, value_meters: float) -> list[tuple[str, float]]:
    for rule in model_rules.rules:
        if rule.trigger.lower() == trigger.lower():
            return [(d.name, value_meters * d.ratio) for d in rule.dimensions]
    return []
