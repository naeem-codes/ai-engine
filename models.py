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


class HangerSelection(BaseModel):
    """Which prefab hanger the resized glass calls for (see hanger_select.py).

    The dimension writes are already in `InterpretResponse.changes`; this block carries the
    IDENTITY of the chosen prefab so the app can stamp the `Number` custom property, put the
    part number on the drawing, and suppress the hanger DXF (the resized part reproduces the
    prefab's outline, not its internal hole pattern — hangers are stocked, not fabricated).
    Additive field: older app builds simply ignore it.
    """
    part: str | None = None            # e.g. "1333"
    part_name: str = ""                # e.g. "1333-HANGER"
    fraction: float = 0.0              # chosen area / glass area
    in_band: bool = False              # inside the ideal 20-25%
    under_target: bool = False         # below 20% — accepted, but worth showing
    over_ceiling: bool = False         # no prefab fit under 25% (small panel)
    needs_review: bool = False         # nothing suitable — a human should look
    keep_fitted: bool = False           # the fitted hanger was already in band; nothing written
    resize_fitted: bool = False         # no prefab qualified; the fitted hanger was scaled
    reason: str = ""
    width_dim: str = ""                # dim written with the catalogue width
    height_dim: str = ""               # dim written with the catalogue height
    # Chassis dims shifted to keep tracking the hanger width (the HANGING TAB spacing, so
    # the tabs stay seated in the hanger's slots).
    follower_dims: list[str] = []


class InterpretResponse(BaseModel):
    changes: list[DimensionChange] = []
    explanation: str | None = None
    error: str | None = None
    hanger: HangerSelection | None = None


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


class OffsetRule(BaseModel):
    """A fixed-offset link: `target_dim` is held a constant absolute distance
    (`offset_meters`, may be negative) from `source_dim`. On resize, when the source
    dim changes the target is set to `new_source + offset_meters` — never scaled. Used
    to keep the hanging-tab spacing following the hanger width so the tab stays in its
    slot. `note` documents the intent/derivation of the offset for the UI."""
    component: str = ""
    target_dim: str = ""
    source_dim: str = ""
    offset_meters: float = 0.0
    note: str = ""


class SkipEntry(BaseModel):
    name: str
    reason: str


class GenerateRulesResponse(BaseModel):
    width_rules: list[RulePair] = []
    height_rules: list[RulePair] = []
    skip: list[SkipEntry] = []
    component_labels: dict[str, str] = {}
    # Friendly English name for a SINGLE-PART model (no components to label). Empty for
    # assemblies, which use component_labels instead. Lets the app show "Main Chassis"
    # rather than the raw file name / technical dim name in the rules UI.
    part_label: str = ""
    limits: dict[str, float] = {}
    # Component-multiplication rules (e.g. LED strips that scale in count with size).
    # Kept as free-form dicts so the .NET app owns the schema; the engine only stores
    # and forwards them — it never applies them (component ops happen in SolidWorks).
    pattern_rules: list[dict] = []
    position: list[PositionRule] = []
    # Fixed-offset links (target dim tracks source dim + constant gap).
    offset: list[OffsetRule] = []
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
    offset: list[OffsetRule] = []


# ── Production-drawing plan models (prompt → verb JSON → app ExecutePlan) ──────
# SEPARATE flow from resize. The AI translates English into a list of operations
# (verbs from a FIXED vocabulary); the .NET app validates + executes them. The AI
# NEVER chooses scale or view positions — the app owns geometry.

class DrawingPlanRequest(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    instruction: str
    model_path: str | None = None
    assembly_context: str | None = None


class DrawingOp(BaseModel):
    """One operation from the fixed verb vocabulary. Only fields relevant to the
    verb are set; the rest stay None. The app validates verb + params before use."""
    verb: str
    type: str | None = None     # create_view: front|back|left|right|top|bottom|iso|trimetric|dimetric
    value: str | None = None    # set_units: inch|mm
    scheme: str | None = None   # auto_dimension: ordinate|baseline|chain
    view: str | None = None     # auto_dimension: which view to dimension (default front)
    preset: str | None = None   # add_notes: standard
    lines: list[str] = []       # add_notes: explicit note lines


class DrawingPlanResponse(BaseModel):
    operations: list[DrawingOp] = []
    explanation: str | None = None
    error: str | None = None
