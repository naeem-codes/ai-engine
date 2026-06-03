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


class SkipEntry(BaseModel):
    name: str
    reason: str


class GenerateRulesResponse(BaseModel):
    width_rules: list[RulePair] = []
    height_rules: list[RulePair] = []
    skip: list[SkipEntry] = []
    component_labels: dict[str, str] = {}
    error: str | None = None


class SaveRulesRequest(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    model_path: str
    width: list[RulePair] = []
    height: list[RulePair] = []
    component_labels: dict[str, str] = {}
