from pydantic import BaseModel, ConfigDict


class DimensionIn(BaseModel):
    name: str
    value_meters: float


class InterpretRequest(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    instruction: str
    dimensions: list[DimensionIn] = []
    assembly_context: str | None = None
    model_path: str | None = None
    dim_axis_labels: dict[str, str] = {}
    master_width_dim: str | None = None
    master_height_dim: str | None = None


class DimensionChange(BaseModel):
    name: str
    value_meters: float


class InterpretResponse(BaseModel):
    changes: list[DimensionChange] = []
    explanation: str | None = None
    error: str | None = None


# ── Generate Rules models ─────────────────────────────────────────────────────

class GenerateRulesRequest(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    assembly_context: str
    dimensions: list[DimensionIn] = []
    dim_axis_labels: dict[str, str] = {}
    master_width_dim: str | None = None
    master_height_dim: str | None = None


class RulePair(BaseModel):
    if_changes: str
    also_change: list[str] = []


class PositionRule(BaseModel):
    """A distance-mate offset that follows a moving edge on resize."""
    component: str = ""
    position_dim: str = ""
    driver_dim: str = ""
    axis: str = "width"
    factor: float = 0.5
    note: str = ""


class SkipEntry(BaseModel):
    name: str
    reason: str


class GenerateRulesResponse(BaseModel):
    width_rules: list[RulePair] = []
    height_rules: list[RulePair] = []
    skip: list[SkipEntry] = []
    component_labels: dict[str, str] = {}
    limits: dict[str, float] = {}
    # Component-multiplication rules (e.g. LED strips that scale in count with size).
    # Kept as free-form dicts so the .NET app owns the schema; the engine only stores
    # and forwards them — it never applies them (component ops happen in SolidWorks).
    pattern_rules: list[dict] = []
    position: list[PositionRule] = []
    error: str | None = None


class SaveRulesRequest(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    model_path: str
    width: list[RulePair] = []
    height: list[RulePair] = []
    component_labels: dict[str, str] = {}
    limits: dict[str, float] = {}
    pattern_rules: list[dict] = []
    position: list[PositionRule] = []
