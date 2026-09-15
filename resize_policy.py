"""Which dimensions may be resized, and on which axis — the single source of truth.

Three product policies (requested 2026-07-30), enforced DETERMINISTICALLY rather than
left to the LLM, because the axis-enforcement pass in generate_rules.py rebuilds rule
membership straight from the app's [W]/[H] labels and would otherwise keep re-adding
these dims every time rules are regenerated:

 1. MASTER-ONLY dependent rules — the MIRROR GLASS width/height are the only rule
    masters. Every other same-axis dim is a dependent under them; no other dim gets
    its own `if_changes` entry. See `pick_master`.

 2. FIXED-SIZE components — never resized on any axis, at any cost: LED power supply,
    LED clips, LED brackets. These are catalogue hardware; the assembly's existing
    mates reposition them when the frame grows (clip via Distance+Coincident to the
    chassis/mirror, power supply via chassis+plane distances), so they need no rule.

 3. HEIGHT-ONLY components — the LED strips scale on the HEIGHT axis only. Their
    width-axis dim is the extrusion CROSS-SECTION (a ~12.7 mm profile), which must
    never scale; only their length, which runs vertically, follows the height.

Matching is on the component id carried in a dim name's "[...]" suffix AND on the
friendly component label, so a rename of either still resolves. Patterns live in the
tables below — that is the only place to edit when a new part family is added.
"""

import re

# Dim names look like "D1@Sketch1 [1005-CLIP-1]"; a single-part model has no suffix.
_COMPONENT_RE = re.compile(r"\[([^\]]+)\]\s*$")


def component_of(dim_name: str) -> str:
    """The component id in a dim name's trailing [...] suffix ("" for a single part)."""
    m = _COMPONENT_RE.search(dim_name or "")
    return m.group(1).strip() if m else ""


def _norm(text: str) -> str:
    """Upper-case with separators flattened to single spaces, so an id ("1292-LED-
    BRACKET-2") and a friendly label ("LED Bracket Left") match the same pattern."""
    return re.sub(r"[\s_\-/]+", " ", (text or "").upper()).strip()


# (kind, patterns, reason) — patterns are matched against the normalised component id
# and the normalised friendly label. Keep patterns specific enough not to collide with
# a part that legitimately resizes (e.g. "HANGER" and "CHASSIS" must NOT appear here:
# the hanger width drives the hanging-tab slot, the chassis is the frame).
FIXED_SIZE: list[tuple[str, tuple[str, ...], str]] = [
    ("power supply", ("LPM", "POWER SUPPLY", "POWERSUPPLY", "PSU", "LED DRIVER"),
     "LED power supply is fixed-size hardware — repositioned by its chassis mates, never resized"),
    ("clip", ("CLIP",),
     "LED/mirror clip is fixed-size hardware — repositioned by its chassis + mirror mates, never resized"),
    # ISABELL has no chassis hanging TABS. The part that seats in the hanger's slots is a
    # separate cross-member, `1047-HANGING-BRACKET`, and its width is a MATING dimension against
    # the hanger — not a span across the mirror. Live 2026-08-27: it was named as a width
    # dependent of the mirror, grew +10.000" with the glass while the hanger grew only +5.750"
    # (a #1119 -> #1038 swap), and finished 23.375" WIDER than the 20.000" hanger it seats inside.
    # It was 13.375" against a 14.250" hanger before the resize.
    #
    # So it stays fixed-size — no rule may drive it off the mirror — and the HANGER FOLLOWER
    # writes it instead, the same split the hanger itself uses. This entry must sit AFTER the
    # clip entry: `2004-HANGING-BRACKET-CLIP-1/2` also contain "HANGING BRACKET" and are real
    # fixed hardware, and the first-match loop hands them to "clip" before reaching here.
    ("hanging bracket", ("HANGING BRACKET",),
     "Hanging bracket seats inside the hanger's slots — its width follows the HANGER, not the "
     "mirror, and is written by the hanger follower rather than by a resize rule"),
    ("bracket", ("BRACKET",),
     "LED bracket is fixed-size hardware — repositioned by its mates, never resized"),
    # The hanger is chosen from a PREFAB LEGEND of distinct part numbers (#1004 14.25x10,
    # #1038 20x15, #1119 14.25x15, #1169 12x13, #1215 12x40, #1333 14.25x24, #1417 12x7.5),
    # sized at ~1/3 of the assembly — a part-SELECTION decision, not a scale. Ratio scaling
    # it was the 2026-07-30 bug that took a 15.000" hanger to 20.000".
    ("hanger", ("HANGER",),
     "Hanger is a prefabricated part selected by size (~1/3 of the assembly) — never scaled "
     "by a resize rule"),
]

# LED strips: the extruded PROFILE never scales, the LENGTH does — on whichever axis the
# strip happens to run along.
LED_STRIP: tuple[str, ...] = ("ZORTECH", "LEDS", "LED STRIP")

# A strip's cross-section is a small extruded profile (Zortech: 12.70 mm wide x 15.88 mm
# deep); its length spans the mirror (812.80 mm at 36"). 50 mm cleanly separates them and
# is the same noise threshold the rest of the codebase uses.
#
# Keying on VALUE rather than on axis is deliberate. The original rule was "LED strips scale
# on HEIGHT only", which held for AMBER/KELLY where the strips run vertically — but a model
# with the strips mounted HORIZONTALLY (parallel to the width, seen 2026-07-30) has its
# length dim labeled [W], and an axis-based rule would refuse to grow it with the width. The
# app's labeler is already orientation-aware (it perturbs each dim and measures which
# bounding-box axis actually moves), so the length simply scales on whatever axis it is
# labeled, and only the profile is pinned.
LED_CROSS_SECTION_MAX_M = 0.05

# ── Round geometry ───────────────────────────────────────────────────────────
# How a dim that drives a circle is dimensioned, as PROVEN by the app's nudge rather than read
# off a name: grow the dim by d and watch the bounding box. A DIAMETER dim moves the outline by
# d (ratio 1); a RADIUS dim moves it by 2d (ratio 2), because it pushes both sides at once.
# Live on ECLIPSE 2026-09-11: the mirror, chassis and LED-ring diameters all read 1.00, while
# 12419-RING's D1@Sketch1 and the LED band's D2@Sketch1/D2@Sketch2 read 2.00.
RADIAL_DIAMETER = 1
RADIAL_RADIUS = 2


def radial_delta(delta: float, kind: int) -> float:
    """How far a RADIAL dim moves when the master DIAMETER moves by `delta`.

    A diameter-driven dim tracks the master one-for-one. A radius-driven one drives the outline
    twice as fast, so it takes HALF -- give it the full delta and the feature grows to double
    what the mirror did.
    """
    return delta / 2.0 if kind == RADIAL_RADIUS else delta

LED_CROSS_SECTION_REASON = (
    "LED strip cross-section (fixed extrusion profile) — only the strip's LENGTH scales, "
    "and it follows whichever axis the strip runs along"
)

# ── Frame-spanning vs fixed-profile dependents ────────────────────────────────
# A dependent scales by a CONSTANT OFFSET only if it actually SPANS THE FRAME, i.e. it
# means "master minus a fixed border". A dim that is a small fixed PROFILE must not move
# at all: adding the master's delta to it is catastrophic, not merely inaccurate.
#
# Live evidence (ALPHA 24" -> 30", delta +6.000"), fraction = dependent / master:
#     12181-COVER          21.188" -> 27.188"   0.883  frame-spanning, correct
#     4967-CHASSIS         21.500" -> 27.500"   0.896  frame-spanning, correct
#     12186-MOUNTING-PLATE 21.000" -> 27.000"   0.875  frame-spanning, correct
#     12189 extrusion x2    1.000" ->  7.000"   0.042  PROFILE — a 7x blow-up
# and from AMBER: chassis 0.944, LED strip length 0.889, LED cross-section 0.014.
#
# Real frame dims cluster at 0.875-0.944 and profiles at 0.014-0.042, so 0.5 sits in a wide
# empty gap — nothing observed is anywhere near it. Below the threshold the dim is LEFT
# ALONE rather than scaled some other way: a fixed profile's correct new value is its old
# one. (Ratio scaling hid this class of bug by making the error small — 1.000" -> 1.250" —
# instead of correct.)
MIN_DEPENDENT_FRACTION = 0.5

FIXED_PROFILE_REASON = (
    "fixed profile, not a frame-spanning dim — it is only {fraction:.1%} of the master, so "
    "adding the master's delta would blow it up"
)


def is_frame_spanning(current_meters: float, master_current_meters: float) -> bool:
    """True if a dependent is large enough to mean "master minus a fixed border".

    False for a fixed extrusion/profile dim, which must keep its as-built value.
    """
    if master_current_meters <= 0:
        return True          # cannot judge; leave the decision to the caller's fallback
    return current_meters / master_current_meters >= MIN_DEPENDENT_FRACTION

# A dim belongs to the mirror glass if its component id says MIRROR (clean across
# products: the clip is "1005-CLIP", the hanger "1038-HANGER" — neither carries it) or
# its friendly label is explicitly the mirror glass. The label alone is ambiguous —
# "Mirror Clip Left" / "Mirror Hanger" also contain MIRROR — hence the exact phrase.
_MIRROR_ID_PATTERNS = ("MIRROR",)
_MIRROR_LABEL_PATTERNS = ("MIRROR GLASS",)


# ── DECLARED part type, which beats guessing from the name ────────────────────────────────
#
# Everything below identifies a part by finding a keyword in its component id. That works only
# while the id keeps the keyword, and renaming is allowed to remove it: a chassis renamed
# `Custom-Chasis-12323` (one S) stops matching "CHASSIS", `is_chassis` returns False, and the
# hanger's chassis-width cap silently switches off. No error, no warning — the guard just goes.
#
# So a part may DECLARE what it is, in a `PartType` custom property the app stamps when it
# renames one (at which point it still knows, from the name it is about to destroy). The
# declaration wins; the keyword match stays as the fallback for every part nobody has renamed,
# which by definition still has its original keyword-bearing name. An empty map therefore
# behaves exactly as before.
#
# This is NOT the label-matching that was rejected in `_hits` below. That was the LLM's prose,
# regenerated non-deterministically on every rule generation. This is a fixed vocabulary written
# once by the app.
PART_TYPES: tuple[str, ...] = (
    "CHASSIS", "HANGER", "MIRROR", "LED_STRIP", "CLIP", "BRACKET", "PSU",
)

# FIXED_SIZE is keyed by a human "kind"; map those onto the declared vocabulary.
_KIND_TO_TYPE: dict[str, str] = {
    "power supply": "PSU",
    "clip": "CLIP",
    "bracket": "BRACKET",
    "hanger": "HANGER",
}


def declared_type(dim_name: str, types: dict[str, str] | None) -> str:
    """The part type this component declares, or "" when it declares nothing."""
    t = (types or {}).get(component_of(dim_name), "")
    return t.strip().upper().replace(" ", "_")


def _is_kind(dim_name: str, kind: str, patterns: tuple[str, ...],
             types: dict[str, str] | None) -> bool:
    """Declared type if there is one, else the keyword match."""
    d = declared_type(dim_name, types)
    if d:
        return d == kind
    return _hits(dim_name, patterns)


_WORD_RE_CACHE: dict[str, "re.Pattern[str]"] = {}


def _word_re(pattern: str) -> "re.Pattern[str]":
    """`pattern` as whole words inside a normalised component id.

    The neighbours may not be LETTERS. Deliberately looser than `\\b`, which also rejects a
    digit neighbour and so would stop matching an id written without a separator
    ("1005-CLIP2" -> "1005 CLIP2"). What has to be refused is the pattern buried inside a longer
    WORD, which is exactly the ECLIPSE/CLIP collision.
    """
    rx = _WORD_RE_CACHE.get(pattern)
    if rx is None:
        rx = _WORD_RE_CACHE[pattern] = re.compile(
            r"(?<![A-Z])" + re.escape(pattern) + r"(?![A-Z])")
    return rx


def _hits(dim_name: str, patterns: tuple[str, ...]) -> bool:
    """Match patterns against the component ID only — NEVER the friendly label.

    Labels are generated fresh by the LLM on every rule generation, so basing a BLOCKING
    decision on them makes the policy non-deterministic. That is not hypothetical: the same
    physical part `12186-MOUNTING-PLATE` was labeled "Mounting Plate" on ALPHA-BLACK and
    "Power Supply Mounting Plate" on ALPHA-WHITE. The latter matched the "POWER SUPPLY"
    pattern, so a STRUCTURAL plate was classified as the power supply and frozen — and since
    the vertical extrusions are located off that plate by a Width mate, they came away from
    the top of the frame on resize (reported 2026-08-03).

    Component IDs are the client's own part numbers: stable, meaningful, verifiable. Use
    `label_suggests_fixed_size` to LOG a label-only match for human review instead.

    Matched as WHOLE WORDS, not as raw substrings. "ECLIPSE" contains "CLIP" -- E-CLIP-SE -- so
    a plain `in` test classified every part of the round ECLIPSE product as fixed-size clip
    hardware, `1026-MIRROR-ECLIPSE` included. That made the mirror glass ineligible to be a rule
    master (fixed-size beats `is_mirror_glass`), so the product could not be resized at all, by
    any path, with no error to explain why -- the dim was simply reported as blocked.
    """
    text = _norm(component_of(dim_name))
    return any(_word_re(p).search(text) for p in patterns)


def label_suggests_fixed_size(dim_name: str,
                              component_labels: dict[str, str] | None) -> str | None:
    """The friendly label hints at fixed-size hardware, but the part number does NOT.

    Advisory only — never blocks. Surfaces a possible unlisted part number (e.g. a power
    supply whose number isn't "LPM-…") without letting LLM prose freeze a structural part.
    """
    comp = component_of(dim_name)
    label = (component_labels or {}).get(comp)
    if not label or _hits(dim_name, tuple(p for _k, pats, _r in FIXED_SIZE for p in pats)):
        return None
    norm = _norm(label)
    for _kind, patterns, _reason in FIXED_SIZE:
        for p in patterns:
            if p in norm:
                return f"label {label!r} contains {p!r} but part number {comp!r} does not"
    return None


def fixed_size_reason(dim_name: str,
                      component_labels: dict[str, str] | None = None,
                      types: dict[str, str] | None = None) -> str | None:
    """Why this dim must never be resized on ANY axis, or None if it may resize."""
    d = declared_type(dim_name, types)
    if d:
        # A declaration is the whole answer: it says what the part IS, so a part declared
        # something else is NOT fixed-size no matter what its name happens to contain.
        for kind, _patterns, reason in FIXED_SIZE:
            if _KIND_TO_TYPE.get(kind) == d:
                return reason
        return None
    for _kind, patterns, reason in FIXED_SIZE:
        if _hits(dim_name, patterns):
            return reason
    return None


def is_led_strip(dim_name: str,
                 component_labels: dict[str, str] | None = None,
                 types: dict[str, str] | None = None) -> bool:
    """True if this dim belongs to an LED strip."""
    if fixed_size_reason(dim_name, component_labels, types):
        return False          # fixed-size wins (e.g. an LED BRACKET is not a strip)
    return _is_kind(dim_name, "LED_STRIP", LED_STRIP, types)


def is_led_cross_section(dim_name: str, value_meters: float,
                         component_labels: dict[str, str] | None = None) -> bool:
    """True if this is an LED strip's fixed extrusion profile rather than its length."""
    return (is_led_strip(dim_name, component_labels)
            and value_meters < LED_CROSS_SECTION_MAX_M)


import re as _re

# Assembly MATE dimensions, e.g. "D1@Distance4 [12475-CHASSIS-ASSY-1]". A mate value is a
# POSITION, not a size, so a size rule must never write one: on SUZI, `D1@Distance4` was
# handed the master's +6.000" width delta (18.000" -> 24.000") and shifted the whole chassis
# sub-assembly 6" sideways (reported live 2026-08-03).
#
# How it got in: an earlier pass logged "skipped D1@Distance4 — assembly mate distance,
# labeled [?]", but the app labeler's `(filled)` FALLBACK — its guess when perturbation and
# extent-matching both fail — then tagged it [W], and the deterministic axis pass trusts
# labels. Hence this check: deterministic, independent of what the labeler guessed.
#
# The trailing digits matter. SolidWorks names mate instances `Distance4`, `Angle1`,
# `Width1`; a SKETCH named "WIDTH" produces `D1@WIDTH` with NO digits (real case: ALPHA's
# `D1@WIDTH [LPM-24060A-2]`), so requiring digits keeps sketches out of this net.
_MATE_DIM_RE = _re.compile(
    r"^D\d+@(?:DISTANCE|ANGLE|LIMITDISTANCE|LIMITANGLE|WIDTH|PARALLEL|PERPENDICULAR"
    r"|COINCIDENT|CONCENTRIC|TANGENT|SYMMETRIC)\d+$", _re.IGNORECASE)

MATE_DIM_REASON = (
    "assembly mate distance — a POSITION, not a size; driving it with a size delta moves the "
    "component bodily. Use an offset/position rule if it must follow something"
)


def is_mate_dim(dim_name: str) -> bool:
    """True if this is an assembly MATE value rather than a model dimension."""
    clean = (dim_name or "").split(" [")[0].strip()
    return bool(_MATE_DIM_RE.match(clean))


def is_chassis(dim_name: str, types: dict[str, str] | None = None) -> bool:
    """True if this dim belongs to the chassis.

    Not a blocking decision — the chassis is a normal resize dependent. This exists so
    `interpret._hanger_changes` can find the chassis WIDTH and cap the hanger against it: the
    hanger bolts to the chassis and its tabs are cut into it, so a hanger wider than the chassis
    overhangs the part carrying its tabs (CLARA 24x60, live 2026-08-18). Matched on the component
    id like every other check here, never on the LLM's friendly label.
    """
    return _is_kind(dim_name, "CHASSIS", ("CHASSIS",), types)


def is_hanger(dim_name: str,
              component_labels: dict[str, str] | None = None,
              types: dict[str, str] | None = None) -> bool:
    """True if this dim belongs to the hanger.

    The hanger is in FIXED_SIZE so no resize RULE can scale it, but it is not truly fixed:
    it is re-selected from the prefab matrix by glass area and then written to that prefab's
    exact catalogue dims. `interpret._hanger_changes` uses this to find the dims to write.
    """
    return _is_kind(dim_name, "HANGER", ("HANGER",), types)


def is_hanging_bracket(name: str, types: dict[str, str] | None = None) -> bool:
    """True for the cross-member that seats inside the hanger's slots (ISABELL's
    `1047-HANGING-BRACKET`) — and NOT for the clips that carry the same words in their number.

    Accepts either a dim name or a bare component id, like `is_clip`.

    The part stays FIXED_SIZE and this never unfreezes it. It exists so the HANGER FOLLOWER can
    identify the one part whose width has to track the hanger, the way the chassis tab spacing
    does on products that have tabs. Patterns come from the FIXED_SIZE entry itself, so there is
    one definition of what a hanging bracket is.
    """
    if is_clip(name, types):
        return False
    patterns = next((p for k, p, _r in FIXED_SIZE if k == "hanging bracket"), ())
    return any(p in _norm(component_of(name) or name) for p in patterns)


def is_led_bracket(name: str, types: dict[str, str] | None = None) -> bool:
    """True for an LED bracket — fixed hardware the hanger has to make room for.

    ⚠️ NARROW BY REQUEST. The generic form is "any component already in FIXED_SIZE is something
    the hanger must clear", which needs no part name at all. That needs the app to send component
    bounding boxes so the clearance can be MEASURED; until then the obstacle is the one part we
    know collides, named explicitly rather than guessed at.

    Excludes the HANGING bracket, which is the part being positioned, not an obstacle to it.
    """
    if is_hanging_bracket(name, types) or is_clip(name, types):
        return False
    return "LED BRACKET" in _norm(component_of(name) or name)


def is_clip(name: str, types: dict[str, str] | None = None) -> bool:
    """True for a mirror clip, given EITHER a dim name or a bare component id.

    Unlike the blocking checks this accepts a bare id ("1005-CLIP-1"), because mate positions
    identify their component directly rather than through a "[...]" dim suffix — `component_of`
    returns "" for those, so `_hits` alone would silently never match.

    Clips stay FIXED_SIZE; this never unfreezes one. It exists so a clip's POSITION mate can be
    lined up with the chassis hanging tabs, which is a placement decision, not a resize.
    """
    d = declared_type(name, types) or declared_type(f"x [{name}]", types)
    if d:
        return d == "CLIP"
    return "CLIP" in _norm(component_of(name) or name)


def is_mirror_glass(dim_name: str,
                    component_labels: dict[str, str] | None = None,
                    types: dict[str, str] | None = None) -> bool:
    """True if this dim belongs to the mirror glass (the only legal rule master).

    This one DOES consult the friendly label, unlike the blocking checks. The asymmetry is
    deliberate: a label can only make a dim eligible to be the master (permissive), never
    freeze it (restrictive). A wrong master is caught immediately — the whole assembly
    resizes off the wrong dim — whereas a wrongly frozen part fails silently.
    """
    if fixed_size_reason(dim_name, component_labels, types):
        return False
    d = declared_type(dim_name, types)
    if d:
        return d == "MIRROR"
    comp = _norm(component_of(dim_name))
    if any(p in comp for p in _MIRROR_ID_PATTERNS):
        return True
    label = _norm((component_labels or {}).get(component_of(dim_name), ""))
    return any(p in label for p in _MIRROR_LABEL_PATTERNS)


def block_reason(dim_name: str, axis: str,
                 component_labels: dict[str, str] | None = None,
                 value_meters: float | None = None,
                 types: dict[str, str] | None = None) -> str | None:
    """Why `dim_name` must not be changed on `axis`, or None if the change is allowed.

    `axis` is "width" / "height"; anything else means the caller could not determine the
    axis, in which case only the axis-independent bans apply.

    `value_meters` is the dim's CURRENT value, needed to tell an LED strip's fixed profile
    from its length. Without it, no LED dim can be classified, so every LED dim is blocked —
    the conservative choice: refusing to resize a strip is recoverable, stretching its
    12.70 mm profile to mirror width is not.
    """
    fixed = fixed_size_reason(dim_name, component_labels, types)
    if fixed:
        return fixed
    if is_led_strip(dim_name, component_labels, types):
        if value_meters is None:
            return LED_CROSS_SECTION_REASON + " (dim value unavailable — not classified)"
        if value_meters < LED_CROSS_SECTION_MAX_M:
            return LED_CROSS_SECTION_REASON
    return None


def filter_axis_dims(dim_names: list[str], axis: str,
                     component_labels: dict[str, str] | None = None,
                     values: dict[str, float] | None = None,
                     types: dict[str, str] | None = None,
                     ) -> tuple[list[str], list[tuple[str, str]]]:
    """Split an axis's dim list into (allowed, [(blocked_dim, reason), ...]).

    `values` maps dim name → current metres; required to classify LED strip dims.
    """
    allowed: list[str] = []
    blocked: list[tuple[str, str]] = []
    for name in dim_names:
        # Mate dims are excluded HERE rather than in `block_reason`, deliberately: this
        # function decides SIZE-rule membership, whereas block_reason also gates the runtime
        # guard, and the offset/position mechanisms legitimately DO write mate values.
        reason = (MATE_DIM_REASON if is_mate_dim(name)
                  else block_reason(name, axis, component_labels,
                                    (values or {}).get(name), types))
        if reason:
            blocked.append((name, reason))
        else:
            allowed.append(name)
    return allowed, blocked


def pick_master(axis_dims: list[str], app_master: str | None,
                component_labels: dict[str, str] | None = None,
                ) -> tuple[str | None, str]:
    """Choose the single rule master for one axis: the MIRROR GLASS dim.

    Returns (master_dim_or_None, note_for_the_log). Preference order:
      1. the app's detected master dim, when it is a mirror-glass dim (the normal case
         — FindMasterDims prefers a dim literally named WIDTH/HEIGHT on the glass);
      2. the sole mirror-glass dim on this axis, when the app's master is something else
         (honours "dependent rules only for mirror glass" without guessing between
         several candidates);
      3. the app's master dim even though it is not the glass — better a real overall
         driver than none;
      4. None → caller keeps its previous every-dim-is-a-master behaviour.
    """
    if not axis_dims:
        return None, "no dims on this axis"
    if app_master and app_master in axis_dims and is_mirror_glass(app_master, component_labels):
        return app_master, "app master dim is the mirror glass"
    mirror_dims = [d for d in axis_dims if is_mirror_glass(d, component_labels)]
    if len(mirror_dims) == 1:
        return mirror_dims[0], (
            f"app master {app_master!r} is not a mirror-glass dim — using the sole "
            f"mirror-glass dim on this axis instead"
        )
    if app_master and app_master in axis_dims:
        return app_master, (
            f"no single mirror-glass dim found ({len(mirror_dims)} candidates) — "
            f"falling back to the app master dim"
        )
    return None, (
        f"neither a mirror-glass dim ({len(mirror_dims)} candidates) nor the app master "
        f"{app_master!r} is on this axis — leaving every dim as its own master"
    )
