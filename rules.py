import dataclasses
import json
from dataclasses import dataclass, field
from pathlib import Path

RULES_DIR = Path(__file__).parent / "rules"


@dataclass
class RulePairEntry:
    if_changes: str
    also_change: list[str] = field(default_factory=list)


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
    width: list[RulePairEntry] = field(default_factory=list)
    height: list[RulePairEntry] = field(default_factory=list)


def load_rules(model_path: str | None) -> ModelRules | None:
    if not model_path:
        return None
    stem = Path(model_path).stem
    candidate = RULES_DIR / f"{stem}.rules.json"
    if not candidate.exists():
        return None
    data = json.loads(candidate.read_text())

    # Must be new format (has "width" or "height" keys)
    if "width" not in data and "height" not in data:
        return None

    limits_data = data.get("limits")
    limits = None
    if limits_data:
        known = {f.name for f in dataclasses.fields(SizeLimits)}
        limits = SizeLimits(**{k: v for k, v in limits_data.items() if k in known})

    def parse_pairs(lst):
        result = []
        for item in lst:
            ic = item.get("if_changes", "")
            ac = item.get("also_change", [])
            if ic:
                result.append(RulePairEntry(if_changes=ic, also_change=ac))
        return result

    return ModelRules(
        model=data.get("model", stem),
        limits=limits,
        width=parse_pairs(data.get("width", [])),
        height=parse_pairs(data.get("height", [])),
    )


def get_triggers(model_rules: ModelRules) -> list[str]:
    triggers = []
    if model_rules.width:
        triggers.append("width")
    if model_rules.height:
        triggers.append("height")
    return triggers


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


def expand(
    model_rules: ModelRules,
    trigger: str,
    value_meters: float,
    current_dims: dict[str, float] | None = None,
    master_dim: str | None = None,
) -> list[tuple[str, float]]:
    pairs = model_rules.width if trigger.lower() == "width" else model_rules.height

    # Collect all unique dim names from all pairs (preserving order)
    all_dims: list[str] = []
    seen: set[str] = set()
    for pair in pairs:
        for name in [pair.if_changes] + pair.also_change:
            if name not in seen:
                all_dims.append(name)
                seen.add(name)

    # Compute proportional scaling relative to master dim
    master_current = 0.0
    if master_dim and current_dims:
        master_current = current_dims.get(master_dim, 0.0)

    result: list[tuple[str, float]] = []
    for name in all_dims:
        if current_dims and master_current > 0:
            current = current_dims.get(name)
            if current is None:
                continue
            new_val = (current / master_current) * value_meters
        else:
            new_val = value_meters
        result.append((name, new_val))

    return result
