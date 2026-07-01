import dataclasses
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Resolve rules/ next to engine.exe when frozen (PyInstaller), else next to this file.
if getattr(sys, "frozen", False):
    RULES_DIR = Path(sys.executable).parent / "rules"
else:
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
class PositionRuleEntry:
    """Keeps a component a constant gap from a moving edge as the parent resizes.

    When `driver_dim` (a width/height dim) changes, `position_dim` (a distance-mate
    value) is shifted by `factor * (new_driver - old_driver)`. factor=0.5 holds the
    gap constant for growth that is symmetric about the centre plane (the common case).
    """
    component: str = ""
    position_dim: str = ""           # the distance-mate display dim to adjust, e.g. "D1@Distance2"
    driver_dim: str = ""             # the width/height dim whose change drives it
    axis: str = "width"              # "width" or "height"
    factor: float = 0.5
    note: str = ""


@dataclass
class ModelRules:
    model: str
    limits: SizeLimits | None
    width: list[RulePairEntry] = field(default_factory=list)
    height: list[RulePairEntry] = field(default_factory=list)
    position: list[PositionRuleEntry] = field(default_factory=list)
    # Map of component-id → human-friendly name (e.g. "LPM-24096A-2" → "Right LED
    # power supply"). Used so the interpret LLM can resolve a named component to the
    # correct rule instead of guessing from cryptic dim names.
    component_labels: dict[str, str] = field(default_factory=dict)


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

    def parse_positions(lst):
        known = {f.name for f in dataclasses.fields(PositionRuleEntry)}
        result = []
        for item in lst:
            if not item.get("position_dim") or not item.get("driver_dim"):
                continue
            result.append(PositionRuleEntry(**{k: v for k, v in item.items() if k in known}))
        return result

    component_labels = data.get("component_labels", {})
    if not isinstance(component_labels, dict):
        component_labels = {}

    return ModelRules(
        model=data.get("model", stem),
        limits=limits,
        width=parse_pairs(data.get("width", [])),
        height=parse_pairs(data.get("height", [])),
        position=parse_positions(data.get("position", [])),
        component_labels=component_labels,
    )


def get_triggers(model_rules: ModelRules) -> list[str]:
    triggers = []
    if model_rules.width:
        triggers.append("width")
    if model_rules.height:
        triggers.append("height")
    return triggers


def validate(model_rules: ModelRules, trigger: str, value_meters: float, check_min: bool = True) -> str | None:
    if model_rules.limits is None:
        return None
    mm = value_meters * 1000
    L = model_rules.limits
    t = trigger.lower()
    if t == "width":
        if check_min and mm < L.min_width_mm:
            return f"Width {mm:.0f}mm ({mm/25.4:.2f}in) is below minimum {L.min_width_mm:.0f}mm ({L.min_width_mm/25.4:.2f}in)"
        if mm > L.max_width_mm:
            return f"Width {mm:.0f}mm ({mm/25.4:.2f}in) exceeds maximum {L.max_width_mm:.0f}mm ({L.max_width_mm/25.4:.2f}in)"
    elif t == "height":
        if check_min and mm < L.min_height_mm:
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


def expand_positions(
    model_rules: ModelRules,
    trigger: str,
    changes_by_name: dict[str, float],
    current_dims: dict[str, float],
) -> list[tuple[str, float]]:
    """Compute distance-mate adjustments so components hold a constant gap from a moving edge.

    A position rule fires only when its `driver_dim` is among the dims already being
    changed this turn (`changes_by_name`). The new mate value is the old value shifted
    by `factor * (new_driver - old_driver)` — NOT proportional scaling, which would
    change the gap. Returns [(position_dim, new_value_meters), ...].
    """
    out: list[tuple[str, float]] = []
    for p in model_rules.position:
        if p.axis.lower() != trigger.lower():
            continue
        if p.driver_dim not in changes_by_name:
            continue  # the driving width/height dim isn't changing this turn
        old_driver = current_dims.get(p.driver_dim)
        old_pos = current_dims.get(p.position_dim)
        if old_driver is None or old_pos is None:
            continue
        delta = changes_by_name[p.driver_dim] - old_driver
        if abs(delta) < 1e-9:
            continue
        out.append((p.position_dim, old_pos + p.factor * delta))
    return out
