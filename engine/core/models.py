from pydantic import BaseModel, ConfigDict

from engine.core import outline


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


class SeatGapIn(BaseModel):
    """How far a part seated in the hanger's slots sits from the glass's LEFT and RIGHT edges —
    now, and as the product was BUILT (measured by the app on the fresh working copy at Connect,
    before anything was written).

    The hanging bracket's tabs drop into slots near the hanger's ends, so its width is always the
    hanger's minus a fixed inset and cannot be set on its own. Keeping it at least as far from the
    sides as it was built is therefore a limit on the HANGER. Live 2026-09-29, SUZI 44x56: #1215
    (40") was chosen, the bracket followed it to 39.125" and ran into the chassis corner and the
    LED strip, which sit 2.5" in from the glass edge.
    """
    component: str = ""
    left_meters: float = 0.0
    right_meters: float = 0.0
    built_left_meters: float = 0.0
    built_right_meters: float = 0.0


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

    # ── HALF-FRAME parts ─────────────────────────────────────────────────────────
    # Dims on a part that runs from the glass's CENTRE LINE out to ONE edge on that axis, as
    # the app measured it off the component's box and origin. Such a part's far edge follows
    # the glass edge, which moves HALF the master delta, so it takes half — the RADIUS rule on a
    # rectangle. SUZI (2026-09-29) builds its chassis corner frame from two of them,
    # `12473-CHASSIS CORNERA` twice with one turned 180°: each is 15.500" of the 36" glass (43%),
    # the frame-spanning bar skipped it as a fixed profile, the corners never grew in height, and
    # the LED strip drawn in context off their edges stayed the same size with them.
    # Empty from an older app, which keeps the old behaviour.
    half_frame_dims: list[str] = []
    # Parts seated in the hanger's slots, with their distance from the glass edges now and as
    # built. Caps the hanger so its bracket never ends up closer to the sides than it was built.
    # Empty from an older app: no cap, the old behaviour.
    seat_gaps: list[SeatGapIn] = []

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

    # ── OVAL ring (MICHELLE) ─────────────────────────────────────────────────────
    # The INNER surface of the oval ring, as half-width and half-height on the glass's W/H axes,
    # read by the app off the ring's elliptical sketch BEFORE this resize. The positioned hardware
    # inside the ring has to stay inside it: live 2026-10-06, MICHELLE 30x42 -> 50x62 moved the
    # bottom hanging brackets +10"/+10" by the rectangle rule and both finished 2.3" OUTSIDE the
    # ring (ISSUE-094). 0 = no oval ring / an older app, which leaves today's behaviour untouched.
    ring_half_w_meters: float = 0.0
    ring_half_h_meters: float = 0.0

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

