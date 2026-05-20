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
