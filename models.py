from pydantic import BaseModel, ConfigDict

import outline


class DimensionIn(BaseModel):
    name: str
    value_meters: float


class MatePositionIn(BaseModel):
    """A distance mate pinning one component a fixed distance from an ASSEMBLY plane.

    An absolute position, not a gap between parts, which is why a shrink has to carry it inward:
    AMBER's mirror clip is held 18.500" from the centre plane, fine inside a 60" glass but 0.5"
    OUTSIDE a 36" one. The dim name alone ("D1@Distance8") says nothing about what it positions,
    so the app resolves the owning component and the axis and sends them here.
    """

    dim: str
    value_meters: float
    component: str
    axis: str                     # "W", "H" or "D"
    extent_meters: float = 0.0    # the component's own size on that axis
    # Where the component actually SITS, measured by the app against the mirror's centre:
    # `offset_meters` is signed, on this mate's own axis; `radius_meters` is its distance from the
    # centre in the W/H plane. Only a ROUND product uses them -- a disc has no per-axis edge to
    # hold a distance from, so its containment check has to be radial. 0 means "the app did not
    # measure it", which leaves an older app behaving exactly as it used to.
    offset_meters: float = 0.0
    radius_meters: float = 0.0
    # The radius this component must stay INSIDE on a round product -- the inner face of the
    # first concentric ring outboard of it, which on ECLIPSE is the LED channel. Measured by the
    # app; 0 means nothing rings it and only the chassis rim limits the part.
    keep_out_meters: float = 0.0
    # This component is MATED TO THE HANGER: its tab drops into one of the hanger's slots, so it
    # travels with the HANGER's edge, not with the glass. Stretching the hanger without moving it
    # slides the slot out from under the tab.
    on_hanger: bool = False
    # What to multiply the master's half-delta by. Measured by the app, which nudges the mate and
    # watches which way the component actually goes, so it carries BOTH the side of the glass the
    # component sits on and how hard the mate drives it (a mate that moves its part at half rate
    # needs twice the change). A plain distance-from-the-centre-plane mate measures +1 on either
    # side of the glass, which is what this code assumed before it was measured -- so the default
    # keeps an older app, which sends no direction at all, behaving exactly as it used to.
    direction: float = 1.0


class SlotRowIn(BaseModel):
    """A row of mounting slots MEASURED off the model by the app.

    Replaces a table keyed to `12211-CHASSIS`, which silently did nothing for `12204-CHASSIS` on
    the next product. Every field comes from the geometry, so any chassis in any product works.
    """

    dim: str                       # the dim driving the slot length
    length_meters: float           # its current value
    count: int                     # slots in the row
    slot_width_meters: float = 0.0 # overall slot length = length + this
    inset_meters: float = 0.0      # part edge to the outermost slot
    part_width_meters: float = 0.0 # the part the row lives on
    component: str = ""

    # ── Where the row actually SITS on the part ─────────────────────────────────
    # A row near the top of a curved part has far less width available than the part's widest
    # point, and until these arrived there was no way to ask. All three default to 0, which
    # `outline` reads as "unknown" and answers with the full width — today's behaviour.
    #
    # `outermost_meters` is MEASURED, not derived from the spacing dim. That is deliberate: on
    # CAPSULE the tab dim `D3@Sketch6` reads 10.000" while the row's own contours put the outer
    # slot end 6.140" off centre, so the dim's datum is not the one the geometry uses. Moving
    # the row by a known DELTA is exact whatever the datum, and reconstructing its position from
    # the dim is not.
    part_height_meters: float = 0.0
    row_y_meters: float = 0.0      # row centre, from the part's centre; sign carries nothing
    outermost_meters: float = 0.0  # centre to the far end of the outermost slot


class InterpretRequest(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    instruction: str
    dimensions: list[DimensionIn] = []
    assembly_context: str | None = None
    model_path: str | None = None
    dim_axis_labels: dict[str, str] = {}
    master_width_dim: str | None = None
    master_height_dim: str | None = None
    mate_positions: list[MatePositionIn] = []
    slot_rows: list[SlotRowIn] = []
    # What each component DECLARES itself to be ("12393-CHASSIS-2" -> "CHASSIS"), read from the
    # part's `PartType` custom property. Empty for a model nobody has stamped, in which case the
    # policy falls back to matching keywords in the component id exactly as before. Exists because
    # renaming a part can delete the keyword the policy identifies it by, silently switching off
    # whichever guard depended on it.
    component_types: dict[str, str] = {}

    # ── ROUND mirrors ────────────────────────────────────────────────────────────
    # `shape` is "round" or "rect", decided by the APP from geometry it already measures: one dim
    # whose nudge grows TWO bounding extents EQUALLY is driving a circle, where a square part's
    # width dim moves one axis alone and its height dim the other. A square BOUNDING BOX proves
    # nothing either way (CLARA 36x36 is genuinely square), which is why the app sends a verdict
    # instead of the engine guessing from the numbers.
    #
    # A round product has exactly ONE size axis and it is carried on the WIDTH master, so every
    # width mechanism -- rule selection, constant-offset dependents, the mate half-delta follower
    # -- applies unchanged. `master_height_dim` comes back None deliberately: on a round assembly
    # the [H] dims all belong to the hardware (on ECLIPSE the nearest [H] dim to the 762 mm glass
    # is the hanger's own 254 mm one), so a height master would aim "change the height" at the
    # hanger.
    #
    # `master_radial`: 1 = the master IS the diameter, 2 = it is a RADIUS, so a diameter target
    # must be halved before it is written. `radial_dims` carries the same 1/2 verdict for every
    # dim proven to drive a circle, so a radius-driven dependent takes HALF the master delta.
    # All three default to the rectangular behaviour, so an older app is byte-for-byte unchanged.
    shape: str = "rect"
    master_radial: int = 0
    radial_dims: dict[str, int] = {}

    # ── CURVED mirrors that are not discs (obround, ellipse) ────────────────────
    # `shape` now also carries "obround" and "ellipse", measured by the app as a part's real area
    # over its bounding rectangle's (1.000 / 0.847 / 0.785 at 30 x 42 — eight percent apart). The
    # verdict is taken off the MASTER's own component, i.e. the glass.
    #
    # `outline_w/h_meters` is the SHELL the hardware has to stay inside: the chassis, not the
    # glass. It has to be the chassis. CAPSULE's clip ended up 0.06" from the chassis rim while
    # still comfortably inside the glass, so a glass-based check would have passed the very
    # collision that was reported. It is the chassis's CURRENT bounding box; the new one follows
    # the constant-offset law the rest of the resize uses (shell delta == master delta), not a
    # ratio — the chassis is glass minus a fixed border, 0.5" on MICHELLE and 2" on CAPSULE.
    #
    # 0 means "the app did not send it", which every check reads as "no outline ceiling" and so
    # behaves exactly as it did before.
    outline_w_meters: float = 0.0
    outline_h_meters: float = 0.0

    @property
    def is_round(self) -> bool:
        return (self.shape or "").strip().lower() == "round"

    @property
    def is_curved(self) -> bool:
        """The outline narrows towards the ends, so "how wide" needs a height to answer."""
        return outline.is_curved(self.shape)


class DimensionChange(BaseModel):
    name: str
    value_meters: float


class HangerSelection(BaseModel):
    """Which prefab hanger the resized glass calls for (see hanger_select.py).

    Two different outcomes reach the app through this block:

    * `replace` — a CATALOGUE prefab was chosen. The real `.SLDPRT` for it exists in the app's
      bundled `HANGERS\\` library, so the app SWAPS THE COMPONENT rather than stretching the
      one already fitted. No hanger dimension writes are emitted in this case; `changes` holds
      only the chassis tab follower. The swapped-in part carries the prefab's genuine internal
      hole pattern, so its DXF is real and needs no suppression.
    * `resize_fitted` — nothing in the 7-part catalogue spans the glass (a 90in mirror needs a
      ~58in hanger and the widest prefab is 20in). There is nothing to swap in, so the fitted
      hanger is stretched via `changes` exactly as before.

    `keep_fitted` writes nothing at all: a bespoke hanger already in band is never traded for a
    catalogue part. Additive field — older app builds simply ignore the whole block.
    """
    part: str | None = None            # e.g. "1333"
    part_name: str = ""                # e.g. "1333-HANGER" — the library file's stem
    fraction: float = 0.0              # chosen area / glass area
    in_band: bool = False              # inside the ideal 20-25%
    under_target: bool = False         # below 20% — accepted, but worth showing
    over_ceiling: bool = False         # no prefab fit under 25% (small panel)
    needs_review: bool = False         # nothing suitable — a human should look
    keep_fitted: bool = False           # the fitted hanger was already in band; nothing written
    resize_fitted: bool = False         # no prefab qualified; the fitted hanger was scaled
    # Swap the placed hanger for `part_name` out of the app's prefab library. Mutually
    # exclusive with `keep_fitted`/`resize_fitted`, and never set together with hanger
    # dimension writes — the prefab file already IS the right size.
    replace: bool = False
    reason: str = ""
    width_dim: str = ""                # hanger width driver (informational when `replace`)
    height_dim: str = ""               # hanger height driver (informational when `replace`)
    # Catalogue size of the chosen prefab, so the app can sanity-check the swapped-in file
    # really is the part the selector reasoned about.
    target_width_meters: float = 0.0
    target_height_meters: float = 0.0
    # Chassis dims shifted to keep tracking the hanger width (the HANGING TAB spacing, so
    # the tabs stay seated in the hanger's slots).
    follower_dims: list[str] = []


class InterpretResponse(BaseModel):
    changes: list[DimensionChange] = []
    explanation: str | None = None
    error: str | None = None
    hanger: HangerSelection | None = None
    # Set with `error` when the refusal is specifically "this model has no rule set". The app
    # raises a dialog pointing at ⚙ Generate Rules rather than printing it in the chat log,
    # because it is an action the user must take, not a message to scroll past.
    needs_rules: bool = False


# ── Generate Rules models ─────────────────────────────────────────────────────

class GenerateRulesRequest(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    assembly_context: str
    dimensions: list[DimensionIn] = []
    dim_axis_labels: dict[str, str] = {}
    master_width_dim: str | None = None
    master_height_dim: str | None = None
    # Same round context as InterpretRequest -- the generator needs it too, or it rebuilds a
    # height axis out of the hardware dims the labeller left on [H].
    shape: str = "rect"
    master_radial: int = 0
    radial_dims: dict[str, int] = {}

    @property
    def is_round(self) -> bool:
        return (self.shape or "").strip().lower() == "round"


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
    # Which stored rule set answered a /get-rules call, and how it was found
    # ("exact-stem" | "family" | "legacy"). Additive: older app builds ignore them.
    model_key: str = ""
    source: str = ""
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
