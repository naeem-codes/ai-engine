import json
import chassis_slots
import hanger_select
import resize_policy as policy
import rules_store as store
from hanger_select import select_hanger_meters
from models import (InterpretRequest, DimensionChange, HangerSelection,
                    InterpretResponse)
from rules import (load_rules, parse_rules, validate,
                   expand_positions, expand_offsets)
from llm import call_llm
from prompts import rules_dependent_prompt
from log import log, section


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        nl = text.find("\n")
        text = text[nl + 1:] if nl != -1 else text[3:]
    if text.endswith("```"):
        text = text[: text.rfind("```")].strip()
    return text.strip()


def _axis_of(dim: str, model_rules, labels: dict[str, str]) -> str:
    """Which axis a dim drives: "width" or "height".

    Rule masters are definitive. Beyond them the app's [W]/[H] label is authoritative —
    and that fallback is now the common case, because rules carry ONE master per axis
    (the mirror glass), so a named-component dim belongs to no rule's if_changes.
    Without the label fallback such a dim would silently be treated as height.
    """
    if any(r.if_changes == dim for r in model_rules.width):
        return "width"
    if any(r.if_changes == dim for r in model_rules.height):
        return "height"
    lbl = (labels or {}).get(dim, "")
    if lbl == "W":
        return "width"
    if lbl == "H":
        return "height"
    if any(dim in r.also_change for r in model_rules.width):
        return "width"
    if any(dim in r.also_change for r in model_rules.height):
        return "height"
    return "height"


def _dependent_value(current: float, master_current: float, master_new: float) -> float | None:
    """A dependent's new value, or None if the dim must be LEFT ALONE.

        new_dep = current + (master_new - master_current)

    A CONSTANT ABSOLUTE difference from the master — NOT proportional
    (`current / master_current * master_new`), which was the bug behind the AMBER 36→48
    report. These assemblies are built to fixed borders, not ratios: at 36" the chassis is
    34.000" (master − 2.000") and the LED strip 32.000" (master − 4.000", the 4" enforced by
    the LED-bracket-to-mirror distance mate). Ratio scaling gave 45.333" and 42.667" — each
    short by its own fraction of the growth — which left the strips 1.333" shy of the frosted
    band and pulled every chassis face (and every part mated to one) out of position.
    Constant offset gives 46.000" and 44.000", holding the as-built borders at any size.

    Returns None for a dim that does NOT span the frame — a fixed extrusion profile, whose
    correct new value is its old one. Constant offset alone was not enough: on ALPHA 24→30 it
    added the +6.000" delta to a 1.000" extrusion profile and produced 7.000", a 7x blow-up
    (`resize_policy.is_frame_spanning` documents the numbers). Ratio scaling used to hide
    this class of error by making it small rather than correct.

    With no usable master value, fall back to the target itself (previous behaviour).
    """
    if master_current <= 0:
        return master_new
    if not policy.is_frame_spanning(current, master_current):
        return None
    return current + (master_new - master_current)


def _hanger_changes(req: InterpretRequest, changes: list[DimensionChange],
                    component_labels: dict[str, str] | None,
                    width_deps: list[str] | None = None,
                    ) -> tuple[list[DimensionChange], HangerSelection | None]:
    """Re-select the prefab hanger for the RESIZED glass and write its catalogue dims.

    Runs on every size change, width or height alike: selection is by glass AREA, so a
    width-only resize changes the area and can call for a different prefab.

    Deliberately runs AFTER `_enforce_policy` and appends straight to `changes`. The policy
    blocks the hanger so no proportional rule can ever scale it (that bug drove a 15.000"
    hanger to 20.000"); this selector is the one sanctioned path that may move it, and it
    only ever writes EXACT catalogue sizes from the matrix — it never invents a size.
    """
    labels = req.dim_axis_labels or {}
    current = {d.name: d.value_meters for d in req.dimensions}
    applied = {c.name: c.value_meters for c in changes}

    def master_value(dim: str | None) -> float:
        # Post-resize value if this turn changed it, else its unchanged current value.
        return applied.get(dim, current.get(dim, 0.0)) if dim else 0.0

    glass_w = master_value(req.master_width_dim)
    glass_h = master_value(req.master_height_dim)
    if glass_w <= 0 or glass_h <= 0:
        log("  [HANGER] skipped — no master glass width/height available")
        return [], None

    def hanger_dim(axis: str) -> str | None:
        """The hanger's OUTER size driver on this axis: its largest dim, EXCLUDING dims that
        live on a Sheet-Metal feature.

        A `@Sheet-Metal` dim is flat-pattern / bend metadata, not an outer size. On AMY the
        largest [H] hanger dim was `D2@Sheet-Metal1` = 446.70 mm, so the prefab height was
        written onto that instead of the real driver `D1@Sketch1` = 431.80 mm (which equals
        the hanger's 17.0" bounding-box height exactly). The give-away: 446.70 mm appears
        identically on AMBER's 20x15 hanger AND AMY's 23x17 one, so it cannot be a size.
        """
        cands = [(current[d.name], d.name) for d in req.dimensions
                 if labels.get(d.name) == axis
                 and policy.is_hanger(d.name, component_labels, req.component_types)
                 and "@SHEET-METAL" not in d.name.upper()]
        return max(cands)[1] if cands else None

    w_dim, h_dim = hanger_dim("W"), hanger_dim("H")
    if not w_dim and not h_dim:
        log("  [HANGER] skipped — no labeled hanger dims in this model")
        return [], None

    # The hanger currently fitted to the model, so the selector can keep it when it is
    # already correctly sized, or resize it when no prefab qualifies.
    fitted_w = current.get(w_dim, 0.0) if w_dim else 0.0
    fitted_h = current.get(h_dim, 0.0) if h_dim else 0.0
    # The chassis the hanger bolts to, at its POST-resize width. Every other check sizes the
    # hanger against the GLASS, which hides the difference on a product with a narrow border
    # (AMBER's chassis is glass - 2in) and gets it badly wrong on a wide one: CLARA's is
    # glass - 6in, so a 24in glass leaves an 18in chassis and the 20in #1038 was KEPT, hanging an
    # inch past each end of the part its tabs are cut into.
    #
    # Taken from the WIDTH RULE's dependents, NOT from "the largest [W] dim on a chassis". That
    # heuristic reads plausibly and is wrong: the chassis also carries the hanging-tab spacing on
    # the W axis (D1@Sketch81 = 15.750" on a 36in AMBER), so on any model whose real width dim is
    # missing or smaller it caps the hanger against the TAB SPACING -- the very thing the hanger
    # is supposed to be driving. The rule set already names the chassis width dependent, decided
    # by the labeler at generation time, so use its answer. Missing (0) disables the cap rather
    # than rejecting everything.
    chassis_w = max(
        (master_value(dim) for dim in (width_deps or [])
         if policy.is_chassis(dim, req.component_types)),
        default=0.0)
    if chassis_w > 0:
        log(f"  [HANGER] chassis width after this resize: {chassis_w / 0.0254:.3f}\"")
    else:
        # FALLBACK to the live model when the rule set cannot supply it. The rules are the
        # better source and stay first, but "rules could not answer" must not mean "no cap":
        # `chassis_w_in=0` disables the check inside select_hanger, so the one guard between a
        # wide prefab and a narrow chassis switched off exactly when its input went missing.
        # Live 2026-08-25: a rename left the chassis width dep under a component id that no
        # longer existed, the cap vanished, a 20" #1038 was chosen for a 10" chassis, and the
        # tab follower wrote 15.75" into it — the rebuild aborted on HANGING TAB LOCATIONS.
        #
        # "Largest [W] dim on a chassis" is rejected as the PRIMARY source further up, and
        # rightly: the chassis also carries the hanging-tab dims on the W axis, so a bare max()
        # caps the hanger against the TAB SPACING — the very thing the hanger is meant to be
        # driving. On AMBER 36x36, whose rules carry no chassis width at all, that reads 15.75"
        # and rejects the 20" #1038 the client actually built.
        #
        # So the tab dims are excluded, by the same hint list that identifies the follower, and
        # the two cannot drift apart. What is left is a genuine chassis width or nothing —
        # nothing being the old no-cap behaviour, which is right when there is truly no evidence.
        chassis_w = max(
            (master_value(d.name) for d in req.dimensions
             if labels.get(d.name) == "W"
             and policy.is_chassis(d.name, req.component_types)
             and not any(hint in d.name.upper()
                         for hint in hanger_select.HANGER_FOLLOWER_HINTS)),
            default=0.0)
        if chassis_w > 0:
            log(f"  [HANGER] chassis width missing from the width rule; capped instead against "
                f"the largest [W] chassis dim in the live model: {chassis_w / 0.0254:.3f}\"")
        else:
            log("  [HANGER] no chassis width anywhere - hanger not capped against it")

    choice = select_hanger_meters(glass_w, glass_h,
                                  fitted_w_in=fitted_w / 0.0254,
                                  fitted_h_in=fitted_h / 0.0254,
                                  chassis_w_in=chassis_w / 0.0254)
    for line in choice.log_lines():
        log("  " + line)

    sel = HangerSelection(
        part=choice.part, part_name=choice.part_name, fraction=choice.fraction,
        in_band=choice.in_band, under_target=choice.under_target,
        over_ceiling=choice.over_ceiling, needs_review=choice.needs_review,
        keep_fitted=choice.keep_fitted, resize_fitted=choice.resize_fitted,
        reason=choice.reason, width_dim=w_dim or "", height_dim=h_dim or "",
        target_width_meters=choice.target_width_m,
        target_height_meters=choice.target_height_m,
    )
    def finish(out: list[DimensionChange],
               hanger_w: float | None = None,
               ) -> tuple[list[DimensionChange], HangerSelection]:
        """Append the chassis HANGING TAB follower, then return.

        Every exit goes through here, including the ones where the hanger itself does not move.
        The tabs have to sit on the hanger's slots or they do not seat, and a resize on a model
        whose tabs were ALREADY misaligned used to leave them misaligned — the follower was
        gated behind the hanger changing, and `keep_fitted` returned before it entirely
        (reported 2026-08-06). Every resize now re-asserts the 4.25" inset; a write that comes
        out identical is dropped, so a healthy model still reports no change.

        `hanger_w` is the hanger's width AFTER this turn in metres. It has to be passed in for
        the COMPONENT SWAP path: there the new width arrives as a different part file, not as a
        dimension write, so reading it back out of `out` would find nothing and silently leave
        the tabs at the old hanger's spacing.
        """
        if not w_dim:
            return out, sel
        old_w = current.get(w_dim, 0.0)
        if old_w <= 0:
            return out, sel
        # The hanger width AFTER this turn — from the swapped-in prefab if one is being fitted,
        # else from a dimension write, else unchanged.
        new_w = hanger_w if hanger_w else next(
            (c.value_meters for c in out if c.name == w_dim), old_w)
        for dim, new_val, inset_in in _hanger_follower_updates(req, old_w, new_w - old_w):
            if abs(new_val - current.get(dim, 0.0)) < 1e-9:
                continue              # already correct — nothing to write
            out.append(DimensionChange(name=dim, value_meters=new_val))
            sel.follower_dims.append(dim)
            log(f"  [HANGER] follower {dim}: {current[dim] / 0.0254:.3f}\" → "
                f"{new_val / 0.0254:.3f}\" (holds the "
                f"{hanger_select.EXPECTED_TAB_INSET_IN:.2f}\" inset from the hanger width)")
        return out, sel

    if choice.keep_fitted:
        # A bespoke hanger the client made for this product, already in band — do not swap it
        # for a catalogue part (this is what wrongly replaced JEN's 30x15 with a 12x40).
        return finish([])
    if not choice.part and not choice.resize_fitted:
        # Nothing in the matrix fits and there is no fitted hanger to fall back on.
        log("  [HANGER] no prefab fits — hanger left unchanged (no new hangers by policy)")
        return finish([])

    if choice.part:
        # ── A CATALOGUE PREFAB WAS CHOSEN → the app SWAPS THE COMPONENT ───────────────
        # The real .SLDPRT ships in the app's HANGERS library, so there is nothing to write
        # here: the file already carries the catalogue size AND the genuine internal hole
        # pattern. This replaces the old behaviour of stretching the fitted hanger's
        # `D2@Base-Flange1`/`D1@Sketch1` onto the catalogue outline, which reproduced the
        # prefab's silhouette but not its holes (hence the DXF-suppression caveat, now moot).
        #
        # Idempotence is the APP's call, not ours: only it can see whether the component
        # already points at `<part>-HANGER.SLDPRT`. Comparing dimensions cannot tell a real
        # #1119 from a bespoke hanger someone previously stretched to 14.25x15.
        sel.replace = True
        log(f"  [HANGER] → replace the placed hanger with prefab {choice.part_name} "
            f"({choice.target_width_in:g}x{choice.target_height_in:g}in)")
        return finish([], hanger_w=choice.target_width_m)

    # ── resize_fitted: no prefab spans this glass, so stretch the one that is fitted ──
    out: list[DimensionChange] = []
    for dim, target in ((w_dim, choice.target_width_m), (h_dim, choice.target_height_m)):
        if not dim or target <= 0:
            continue
        if abs(current.get(dim, 0.0) - target) < 1e-6:
            continue      # already at the computed size
        out.append(DimensionChange(name=dim, value_meters=target))
    if not out:
        log("  [HANGER] fitted hanger already at the computed size — no change")
    return finish(out)


def _frost_follower_updates(req: InterpretRequest, changes: list[DimensionChange],
                            component_labels: dict[str, str] | None,
                            ) -> list[tuple[str, float, float]]:
    """Keep the FROSTED (sandblast) band the same length as the LED strip.

    The band is cut into the mirror glass, and the client's rule is simply that the frost and
    the strip match — so that equality is what gets enforced, rather than guessing the band's
    own offset from the frame.

    Identifying the dim: the frost length is INTERNAL to the glass, so it never changes the
    part's bounding box and the labeler leaves it UNLABELED — which is why it was silently
    left behind on AMY (mirror grew 24"→30", strip 20"→26", band stuck at 20"). It is found
    as an unlabeled mirror-glass dim whose current value EQUALS the strip's current length.
    Requiring exact equality makes this self-validating and safe: on AMY exactly one dim
    matches (`D2@Sketch2` = 508.00 mm = the strip's 508.00 mm), and on AMBER — where the
    band's extent has no dim at all and is driven by sketch relations — nothing matches and
    this is a no-op. A looser "large unlabeled glass dim" test would have dragged in AMBER's
    41"/24"/23.25" glass dims, which is exactly what to avoid.

    Returns (dim_name, new_value_meters, strip_length_meters).
    """
    labels = req.dim_axis_labels or {}
    current = {d.name: d.value_meters for d in req.dimensions}
    applied = {c.name: c.value_meters for c in changes}

    # LED strip LENGTH dims moved this turn (cross-sections are excluded by the policy).
    strips = [(n, current.get(n, 0.0), v) for n, v in applied.items()
              if policy.is_led_strip(n, component_labels, req.component_types)
              and not policy.is_led_cross_section(n, current.get(n, 0.0), component_labels)
              and current.get(n, 0.0) > 0]
    if not strips:
        return []

    out: list[tuple[str, float, float]] = []
    seen: set[str] = set()
    for _sname, old_len, new_len in strips:
        if abs(new_len - old_len) < 1e-9:
            continue
        for d in req.dimensions:
            if d.name in applied or d.name in seen:
                continue
            if not policy.is_mirror_glass(d.name, component_labels, req.component_types):
                continue
            if labels.get(d.name) in ("W", "H", "D"):
                continue          # an axis driver — already handled as master/dependent
            if abs(d.value_meters - old_len) > 1e-6:
                continue
            seen.add(d.name)
            out.append((d.name, new_len, old_len))
    return out


def _clip_tab_alignment(hanger, changes: list[DimensionChange]) -> float | None:
    """Where the chassis hanging tabs ended up, as a distance from the centre plane.

    A clip carries the glass at the point the chassis is actually supported, so lining the two
    up puts the clip on the load path instead of somewhere arbitrary. The client's own products
    already do this to within ~1-2": tabs vs clips are ±17.87/±18.50 on the 60", ±12.88/±14.00 on
    the 48", ±5.00/±4.00 on CAROL. Requested 2026-08-06 ("we can make the Hanger Tabs and Clips
    parallel to each other").

    Returns half the tab spacing, or None when the tabs did not move this turn.
    """
    if hanger is None or not getattr(hanger, "follower_dims", None):
        return None
    applied = {c.name: c.value_meters for c in changes}
    for dim in hanger.follower_dims:
        if dim in applied:
            return applied[dim] / 2.0
    return None


def _mate_position_updates(req: InterpretRequest, changes: list[DimensionChange],
                           hanger=None) -> list[tuple[str, float, float, str]]:
    """Carry absolute-position mates inward when the glass shrinks (and outward when it grows).

    A component pinned a fixed distance from the assembly CENTRE plane does not move when the
    glass resizes, so the edge moves past it: AMBER's mirror clip sits 18.500" from centre, 11.5"
    inside a 60" glass but 0.5" OUTSIDE a 36" one, leaving the clip hanging off the mirror.

    The centre plane does not move and each edge moves by HALF the width change, so shifting the
    mate by half the master delta preserves the component's distance from the edge — the same
    constant-offset invariant the rest of the resize uses. On the real AMBER that turns 18.500"
    into 6.500", within half an inch of the 6.000" the client's own 36x36 carries.

    ⚠️ Writing a mate is normally FORBIDDEN — mates are positions, and a size delta applied to one
    once shifted SUZI's chassis 6" ([[project_mate_dims_never_resized]]). This is a deliberate,
    narrow exception: only mates the app has confirmed are measured against an ASSEMBLY plane, only
    on the axis whose master actually moved, and only by half that master's delta.

    Returns (dim_name, new_value_meters, current_value_meters, component).
    """
    if not req.mate_positions:
        return []

    applied = {c.name: c.value_meters for c in changes}
    current = {d.name: d.value_meters for d in req.dimensions}

    # Half the master's movement on each axis — the distance that axis's edges travelled.
    half_delta: dict[str, float] = {}
    for axis, master in (("W", req.master_width_dim), ("H", req.master_height_dim)):
        if master and master in applied and master in current:
            delta = applied[master] - current[master]
            if abs(delta) > 1e-9:
                half_delta[axis] = delta / 2.0

    if not half_delta:
        return []

    # The MASTER's own component must never be moved by its own resize. The real AMBER carries
    # `Distance7 = 6.000" [assembly plane <-> 1011-MIRROR-CAROL-1]`, so the glass itself is pinned
    # to an assembly plane; shifting that would slide the master and desynchronise everything
    # measured from it. (Today that mate resolves to no axis and is dropped upstream — this does
    # not rely on that staying true.)
    master_comps = {
        name[name.index("[") + 1:name.rindex("]")]
        for name in (req.master_width_dim, req.master_height_dim)
        if name and "[" in name and "]" in name
    }

    out: list[tuple[str, float, float, str]] = []
    seen: set[str] = set()
    for mate in req.mate_positions:
        shift = half_delta.get(mate.axis)
        if shift is None:
            continue
        if mate.dim in applied or mate.dim in seen:
            continue          # already being written — never shift a value twice
        if mate.component in master_comps:
            log(f"  [MATE] {mate.dim} SKIPPED — holds the master component {mate.component}")
            continue
        seen.add(mate.dim)

        base = current.get(mate.dim, mate.value_meters)
        new_val = base + shift

        # A clip lines up with the hanging tabs when those moved — same load path, and it
        # replaces the edge offset rather than adjusting it.
        tab_half = _clip_tab_alignment(hanger, changes) if mate.axis == "W" else None
        if tab_half and policy.is_clip(mate.component, req.component_types):
            log(f"  [MATE] {mate.dim} aligned to the hanging tabs at "
                f"{tab_half / 0.0254:.3f}\" (was heading for {new_val / 0.0254:.3f}\")")
            new_val = tab_half

        floor = _mate_position_floor(mate, applied, current, req)
        if floor > new_val:
            log(f"  [MATE] {mate.dim} floor {floor / 0.0254:.3f}\" applied — holding the edge "
                f"offset would have put {mate.component} at {new_val / 0.0254:.3f}\", too close "
                f"to the centre")
            new_val = floor
        if new_val <= 0:
            log(f"  [MATE] {mate.dim} SKIPPED — would go to {new_val * 1000:.2f} mm")
            continue
        log(f"  [MATE] {mate.dim} holds {mate.component} on {mate.axis}: "
            f"{base / 0.0254:.3f}\" → {new_val / 0.0254:.3f}\" "
            f"(half the {shift * 2 / 0.0254:+.3f}\" master change, keeping its distance "
            f"from the edge)")
        out.append((mate.dim, new_val, base, mate.component))
    return out


def _mate_position_floor(mate, applied: dict[str, float], current: dict[str, float],
                         req: InterpretRequest) -> float:
    """How close to the centre a positioned component may be pushed.

    Holding a constant distance from the EDGE is right until the mirror gets narrow, and then it
    collapses: AMBER's clip, 11.5" in from the edge of a 60" glass, lands 0.5" from the centre of a
    24" one — where the two mirrored clips OVERLAP each other (seen live, 60x36 -> 24x36).

    Two floors, whichever is higher:

    1. **master / 6**, taken from the client's own narrow products, where it is EXACT:
       CAROL 24x36 puts the clip centre at 4.000" = 24/6, and AMBER 36x36 at 6.000" = 36/6. Their
       wider products do not follow it (48 -> 14.000", 60 -> 18.500", i.e. edge insets of 8/12/10/
       11.5" with no single rule), so this is used only as a FLOOR — the edge offset still governs
       wherever it gives the larger value, which is every width the client draws by hand.
    2. **half the component's own width plus 1/4"**, a hard geometric guarantee that a mirrored
       pair can never intersect at the centreline even if the ratio above is ever retuned.
    """
    master = req.master_width_dim if mate.axis == "W" else req.master_height_dim
    if not master:
        return 0.0
    master_new = applied.get(master, current.get(master, 0.0))

    proportional = master_new / 6.0 if master_new > 0 else 0.0
    no_overlap = mate.extent_meters / 2.0 + 0.25 * 0.0254 if mate.extent_meters > 0 else 0.0
    return max(proportional, no_overlap)


def _slot_follower_updates(req: InterpretRequest, changes: list[DimensionChange],
                           ) -> list[tuple[str, float, float]]:
    """Scale the chassis mounting slots with the chassis width, in both directions.

    The slots track the part width by ratio, like any other dependent dim, and the geometric fit
    limit in `chassis_slots` is applied on top as a ceiling. That ceiling is why the feature
    exists: four slots at their drawn 7.78" no longer fit below roughly a 39" chassis,
    `Cut-Extrude4` fails with `swSketchErrorExtRefFail`, and the whole resize aborts. Clamping is
    what makes 60" -> 36" build (measured live; see `chassis_slots`).

    It was shorten-only until 2026-08-11: growing a mirror left the slots behind while everything
    around them grew, and because the reference length is measured live, a saved shrink became the
    new ceiling and ratcheted them permanently shorter.

    Only fires when a WIDTH dim on the slot's own component moved this turn.

    Returns (dim_name, new_value_meters, current_value_meters).
    """
    current = {d.name: d.value_meters for d in req.dimensions}
    out: list[tuple[str, float, float]] = []
    seen: set[str] = set()
    if not req.slot_rows:
        return out

    for change in changes:
        if (req.dim_axis_labels or {}).get(change.name) != "W":
            continue
        comp = change.name[change.name.index("[") + 1:change.name.rindex("]")] \
            if "[" in change.name and "]" in change.name else ""

        for row in req.slot_rows:
            if row.component != comp:
                continue                    # the row lives on a different part
            if row.dim == change.name:
                continue                    # the slot dim itself is not a trigger
            if row.dim in seen or row.dim not in current:
                continue

            spec = chassis_slots.spec_from_measurement(row)
            if spec is None:
                continue
            seen.add(row.dim)

            # The part's new width: it shrinks by the same amount its driving dim does.
            old_driver = current.get(change.name, 0.0)
            if old_driver <= 0:
                continue
            new_part_w_in = (row.part_width_meters + (change.value_meters - old_driver)) / 0.0254

            # Ratio off the PART WIDTH, not the driving dim: the slot row spans the part, and the
            # fit inequality it is clamped against is written in part-width terms. The part moves
            # by the driver's absolute delta, so the two ratios are not the same number.
            old_part_w_in = row.part_width_meters / 0.0254
            if old_part_w_in <= 0:
                continue
            scale = new_part_w_in / old_part_w_in

            target_in = chassis_slots.slot_length_for(new_part_w_in, spec, scale)
            for line in chassis_slots.log_lines(new_part_w_in, spec, target_in,
                                                current[row.dim] / 0.0254, scale):
                log("  " + line)
            if target_in is None:
                continue
            new_val = target_in * 0.0254
            if abs(new_val - current[row.dim]) < 1e-9:
                continue
            out.append((row.dim, new_val, current[row.dim]))
    return out


def _hanger_follower_updates(req: InterpretRequest, old_hanger_w: float, delta: float,
                             ) -> list[tuple[str, float, float]]:
    """Chassis dims that track the hanger width, shifted by the hanger's width delta.

    Yields (dim_name, new_value_meters, current_inset_inches). A candidate is skipped when
    its offset from the hanger width is nowhere near the known 4.25" tab inset — that means
    the name hint matched the wrong dim, and writing it would deform the chassis.
    """
    updates: list[tuple[str, float, float]] = []
    seen: set[str] = set()
    for d in req.dimensions:
        upper = d.name.upper()
        if not any(hint in upper for hint in hanger_select.HANGER_FOLLOWER_HINTS):
            continue
        if policy.is_hanger(d.name, None, req.component_types):
            continue                      # the hanger's own dims are handled above
        if d.name in seen:
            continue                      # the dim dump lists every dim twice
        seen.add(d.name)
        inset_m = old_hanger_w - d.value_meters
        inset_in = inset_m / 0.0254
        if abs(inset_in - hanger_select.EXPECTED_TAB_INSET_IN) > hanger_select.EXPECTED_TAB_INSET_TOL_IN:
            log(f"  [HANGER] follower candidate {d.name} SKIPPED — sits {inset_in:.3f}\" "
                f"from the hanger width, not the expected "
                f"{hanger_select.EXPECTED_TAB_INSET_IN:.2f}\". Either it is not the tab spacing, "
                f"or this model's tabs are already misaligned by more than "
                f"{hanger_select.EXPECTED_TAB_INSET_TOL_IN:.2f}\" — check it in SolidWorks")
            continue

        # Write the CANONICAL inset rather than carrying the current one forward.
        #
        # This used to be `d.value_meters + delta`, which preserved whatever inset the model
        # happened to have. That is why resizing a model whose tabs were ALREADY misaligned left
        # them just as misaligned afterwards (reported 2026-08-06): any inset inside the +/-1"
        # identification window was faithfully carried through instead of corrected.
        #
        # Writing 4.25" outright is now safe because it is confirmed on all FOUR client products,
        # not the two it was derived from: 14.25 -> 10.000, 20 -> 15.750, 30 -> 25.750,
        # 40 -> 35.740. The tolerance above still decides WHICH dim this is; it no longer decides
        # the value. On an already-aligned model the result is identical.
        new_hanger_w = old_hanger_w + delta
        new_val = new_hanger_w - hanger_select.EXPECTED_TAB_INSET_IN * 0.0254
        if new_val <= 0:
            log(f"  [HANGER] follower {d.name} SKIPPED — would go to {new_val * 1000:.2f} mm")
            continue

        drift_in = abs(inset_in - hanger_select.EXPECTED_TAB_INSET_IN)
        if drift_in > 0.005:
            log(f"  [HANGER] follower {d.name} inset CORRECTED from {inset_in:.3f}\" to "
                f"{hanger_select.EXPECTED_TAB_INSET_IN:.2f}\" (was drifted by {drift_in:.3f}\")")
        updates.append((d.name, new_val, inset_in))
    return updates


def _hanger_note(hanger: HangerSelection | None) -> str:
    """One human-readable clause about the hanger, for the chat explanation."""
    if hanger is None:
        return ""
    if hanger.keep_fitted:
        return (f" The existing hanger is already correctly sized "
                f"({hanger.fraction * 100:.2f}% of the glass) and was left unchanged.")
    if hanger.resize_fitted:
        return (f" No prefab hanger suited this size, so the existing one was resized to "
                f"{hanger.fraction * 100:.2f}% of the glass, keeping its proportions.")
    if not hanger.part:
        return " No prefab hanger fits this size — the hanger was left unchanged."
    pct = f"{hanger.fraction * 100:.2f}% of the glass"
    # The swap itself is the app's job and can still be refused there (a broken mate aborts it),
    # so this says what was CHOSEN, not what landed — the app reports the outcome separately.
    note = f" Hanger to be replaced with prefab #{hanger.part} ({pct})."
    if hanger.needs_review:
        note += " NEEDS REVIEW — well under the 20% target."
    elif hanger.over_ceiling:
        note += " Note: every prefab exceeds 25% at this size; smallest that fits was used."
    elif hanger.under_target:
        note += " Slightly under the 20% target, but the largest that stays within 25%."
    return note


def _expand_master(master: str, value_meters: float, also_change: list[str],
                   current_dims: dict[str, float]) -> list[DimensionChange]:
    """One master dim plus its dependents, each moved by the master's CONSTANT OFFSET."""
    master_current = current_dims.get(master, 0.0)
    out = [DimensionChange(name=master, value_meters=value_meters)]
    for dep in also_change:
        if dep == master:
            continue
        if policy.is_mate_dim(dep):
            # A stale rules file (or a hand-added dep) can still list a mate; a size
            # delta on a mate MOVES the component instead of resizing it.
            log(f"    SKIP {dep!r} — {policy.MATE_DIM_REASON}")
            continue
        current = current_dims.get(dep)
        if current is None:
            log(f"    SKIP {dep!r} — not in dims")
            continue
        new_val = _dependent_value(current, master_current, value_meters)
        if new_val is None:
            log(f"    SKIP {dep!r} — {current / master_current:.1%} of the master: a "
                f"fixed profile, not a frame-spanning dim (left at "
                f"{current / 0.0254:.3f}\")")
            continue
        if new_val <= 0:
            log(f"    SKIP {dep!r} — constant offset would give {new_val*1000:.2f} mm")
            continue
        out.append(DimensionChange(name=dep, value_meters=new_val))
    return out


def _second_axis_changes(data: dict, scope: str, primary_dim: str, model_rules,
                         req: InterpretRequest, current_dims: dict[str, float],
                         ) -> tuple[list[DimensionChange], str]:
    """The OTHER axis of a "24 x 36" request.

    The response schema carries a single rule, so a two-number request could only ever move one
    axis. `other_axis_meters` carries the second value; which axis it belongs to is decided HERE,
    not by the LLM — it is simply whichever master the primary is not, and the rule comes from
    this model's own rules file. That keeps the LLM's job to reading two numbers off the prompt.

    Returns (changes, note) — the note explains a no-op so the chat never claims something moved
    that did not.
    """
    raw = data.get("other_axis_meters")
    if raw in (None, "") or scope != "overall":
        return [], ""
    try:
        other_value = float(raw)
    except (TypeError, ValueError):
        log(f"  [2-AXIS] ignored unparseable other_axis_meters={raw!r}")
        return [], ""
    if other_value <= 0:
        return [], ""

    on_width = primary_dim == req.master_width_dim
    other_master = req.master_height_dim if on_width else req.master_width_dim
    other_rules = model_rules.height if on_width else model_rules.width
    other_label = "height" if on_width else "width"
    if not other_master:
        log(f"  [2-AXIS] no master for the {other_label} — skipped")
        return [], ""
    if other_master == primary_dim:
        return [], ""

    current = current_dims.get(other_master)
    if current is not None and abs(current - other_value) < 1e-6:
        log(f"  [2-AXIS] {other_label} already {other_value / 0.0254:.3f}\" — nothing to change")
        return [], (f" The {other_label} is already {other_value / 0.0254:.0f}\", so only the "
                    f"{'width' if on_width else 'height'} changed.")

    rule = next((r for r in other_rules if r.if_changes == other_master), None)
    deps = list(rule.also_change) if rule is not None else []
    log(f"  [2-AXIS] also setting the {other_label} master {other_master!r} → "
        f"{other_value / 0.0254:.3f}\" with {len(deps)} dependent(s)")
    return _expand_master(other_master, other_value, deps, current_dims), ""


def _enforce_policy(changes: list[DimensionChange], labels: dict[str, str],
                    component_labels: dict[str, str] | None,
                    axis_hint: str = "",
                    current: dict[str, float] | None = None,
                    types: dict[str, str] | None = None,
                    ) -> tuple[list[DimensionChange], list[str]]:
    """Drop any change that resize_policy forbids. Returns (kept, [reason lines]).

    This is the last line of defence on the resize path, and it also catches what the position,
    offset and follower passes add. Fixed-size hardware (power supply, clips, brackets, hanger)
    and an LED strip's extrusion profile must not move even if the LLM picks them directly or a
    stale rules file lists them in also_change.

    `current` holds each dim's CURRENT value — deliberately not the proposed new one, so a
    cross-section dim being wrongly scaled to mirror width is still recognised as a
    cross-section and rejected.
    """
    kept: list[DimensionChange] = []
    notes: list[str] = []
    for c in changes:
        lbl = (labels or {}).get(c.name, "")
        axis = "width" if lbl == "W" else "height" if lbl == "H" else axis_hint
        reason = policy.block_reason(c.name, axis, component_labels,
                                     (current or {}).get(c.name), types)
        if reason:
            log(f"    [POLICY] DROP {c.name} ({c.value_meters * 1000:.2f} mm) — {reason}")
            notes.append(f"{c.name}: {reason}")
        else:
            kept.append(c)
    return kept, notes


async def interpret(req: InterpretRequest) -> InterpretResponse:
    # ── Log incoming request ──────────────────────────────────────────────────
    section(f"INTERPRET REQUEST")
    log(f"  instruction       : {req.instruction}")
    log(f"  model_path        : {req.model_path or '(none)'}")
    log(f"  dimensions        : {len(req.dimensions)} total")
    log(f"  dim_axis_labels   : {len(req.dim_axis_labels)} entries")
    log(f"  master_width_dim  : {req.master_width_dim or 'null'}")
    log(f"  master_height_dim : {req.master_height_dim or 'null'}")
    log(f"  assembly_context  : {len(req.assembly_context or '')} chars")
    if req.dimensions:
        log("  [W] dims: " + ", ".join(
            d.name for d in req.dimensions
            if req.dim_axis_labels.get(d.name) == "W"
        ) or "  [W] dims: none")
        log("  [H] dims: " + ", ".join(
            d.name for d in req.dimensions
            if req.dim_axis_labels.get(d.name) == "H"
        ) or "  [H] dims: none")


    # ── Resize: rules are REQUIRED ────────────────────────────────────────────
    if req.dim_axis_labels:
        # ── Which rule set applies? Two tiers, strongest first ─────────────────
        # 1. a stored set for this model's FAMILY (or an exact-stem file) — authored and
        #    human-reviewed, so it wins whenever its masters exist in the live model;
        # 2. the legacy per-stem path, for an install whose data dir has not bootstrapped.
        # There is no third tier: without a rule set the request is refused (see below).
        live_dims = [d.name for d in req.dimensions]
        selection = store.select_for_model(req.model_path, live_dims)
        model_rules = parse_rules(selection.doc, store.stem_of(req.model_path))
        rules_note = selection.warning
        if model_rules is None:
            model_rules = load_rules(req.model_path)
            if model_rules is not None:
                log("  [STORE] using the legacy per-stem rules path")
        # A resize may ONLY run from an authored, human-reviewed rule set. There used to be
        # an LLM-classification fallback here that resized a model with no rules at all by
        # asking the model to pick the scope and the dim list itself. It ran silently — a
        # brand-new product would resize with nobody having approved what moves with what
        # (BREAM went through it without anyone noticing). Refuse instead, and tell the app
        # to send the user to Generate Rules.
        if model_rules is None:
            log("  REFUSED — no rule set resolves for this model; rules are required")
            return InterpretResponse(
                error=(f"No resize rules exist for {store.stem_of(req.model_path) or 'this model'}. "
                       "Click \u2699 Generate Rules to create and review them, then try again."),
                needs_rules=True)

        rules_json = json.dumps({
            "width": [{"if_changes": p.if_changes, "also_change": p.also_change} for p in model_rules.width],
            "height": [{"if_changes": p.if_changes, "also_change": p.also_change} for p in model_rules.height],
        }, indent=2)

        # Friendly component names so the LLM can resolve "Right LED power supply" to the
        # right component id (e.g. LPM-24096A-2) and pick that component's OWN rule —
        # instead of guessing from cryptic dim names and grabbing an unrelated rule.
        labels_block = ""
        if model_rules.component_labels:
            lines = "\n".join(f"  {cid}  =  {name}" for cid, name in model_rules.component_labels.items())
            labels_block = (
                "\nCOMPONENT NAMES (english name ⇄ component id — use to resolve which "
                f"component the user means):\n{lines}\n"
            )

        log(f"  CASE 2 (rules)  rules_json={len(rules_json)} chars  component_labels={len(model_rules.component_labels)}")
        raw = await call_llm(
            rules_dependent_prompt(rules_json, labels_block,
                                   master_width_dim=req.master_width_dim,
                                   master_height_dim=req.master_height_dim),
            req.instruction, max_tokens=512)
        try:
            data = json.loads(_strip_fences(raw))
        except json.JSONDecodeError as exc:
            return InterpretResponse(error=f"LLM returned invalid JSON: {exc}")
        if "error" in data:
            return InterpretResponse(error=data["error"])

        rule_data = data.get("rule", {})
        if_changes = rule_data.get("if_changes", "")
        also_change = rule_data.get("also_change", [])
        value_meters = float(data.get("value_meters", 0))
        scope = str(data.get("scope", "")).strip().lower()
        log(f"  if_changes={if_changes!r}  value_meters={value_meters}  scope={scope!r}")

        # SAFETY NET — re-anchor an overall resize to the true master dim. A component dim
        # literally named "WIDTH"/"HEIGHT" (e.g. a 59mm power-supply D1@WIDTH) can get picked
        # for a plain "change width to 40" and, since value_meters is applied to if_changes,
        # the master then scales by (target / tiny-component) → the whole assembly blows up
        # (observed 17x). For an overall change, if_changes MUST be the master dim. The axis
        # comes from the picked dim (still the right AXIS even if the wrong dim); we then swap
        # to that axis's master and use the master rule's own also_change.
        if scope == "overall" and if_changes:
            # Axis via _axis_of, NOT via width-rule membership: rules now hold one
            # master per axis, so a wrongly-picked component dim is in no width rule
            # and the old membership test would re-anchor it to the HEIGHT master.
            on_width = _axis_of(if_changes, model_rules, req.dim_axis_labels) == "width"
            master = req.master_width_dim if on_width else req.master_height_dim
            axis_rules = model_rules.width if on_width else model_rules.height
            if master and if_changes != master:
                mrule = next((r for r in axis_rules if r.if_changes == master), None)
                if mrule is not None:
                    log(f"  [OVERALL] re-anchored if_changes {if_changes!r} → master {master!r}")
                    if_changes = master
                    also_change = list(mrule.also_change)

        if not if_changes or value_meters <= 0:
            return InterpretResponse(error="AI returned invalid rule response")

        # Refuse outright when the user targeted a fixed-size component, rather than
        # silently applying nothing: the master dim is NOT a substitute for it.
        trigger = _axis_of(if_changes, model_rules, req.dim_axis_labels)
        dim_values = {d.name: d.value_meters for d in req.dimensions}
        target_block = policy.block_reason(if_changes, trigger,
                                           model_rules.component_labels,
                                           dim_values.get(if_changes),
                                           req.component_types)
        if target_block:
            log(f"  [POLICY] REFUSE target {if_changes!r} — {target_block}")
            return InterpretResponse(error=f"Cannot resize {if_changes} — {target_block}.")

        # Validate against limits — skip min check if rule has no dependencies
        limit_error = validate(model_rules, trigger, value_meters, check_min=bool(also_change))
        if limit_error:
            return InterpretResponse(error=limit_error)

        current_dims = {d.name: d.value_meters for d in req.dimensions}
        changes = _expand_master(if_changes, value_meters, also_change, current_dims)

        # SECOND AXIS — "24 x 36" names both. The response carries one rule, so without this
        # the other axis was silently dropped and the explanation told the user to submit it
        # separately (seen live: "change to 24.00 X 36.00" resized width only; it looked right
        # only because the height already happened to be 36").
        second, second_note = _second_axis_changes(
            data, scope, if_changes, model_rules, req, current_dims)
        changes.extend(second)
        rules_note += second_note

        # Position rules: shift distance-mate offsets so components hold a constant
        # gap from a moving edge. Applied AFTER proportional scaling, and override
        # any proportional value for the same dim (a mate offset must not be scaled).
        changes_by_name = {c.name: c.value_meters for c in changes}
        pos_changes = expand_positions(model_rules, trigger, changes_by_name, current_dims)
        for pos_dim, pos_val in pos_changes:
            existing = next((c for c in changes if c.name == pos_dim), None)
            if existing is not None:
                log(f"    [POS] {pos_dim} override {existing.value_meters*1000:.2f} → {pos_val*1000:.2f} mm")
                existing.value_meters = pos_val
            else:
                log(f"    [POS] {pos_dim} = {pos_val*1000:.2f} mm (edge-follow)")
                changes.append(DimensionChange(name=pos_dim, value_meters=pos_val))

        # Fixed-offset links: hold a target dim a constant absolute distance from a
        # source dim (e.g. hanging-tab spacing follows the hanger width so the tab
        # stays in its slot). Runs LAST so it sees the scaled source value, and
        # overrides any proportional value for the target (a rigid gap must not scale).
        changes_by_name = {c.name: c.value_meters for c in changes}
        off_changes = expand_offsets(model_rules, changes_by_name, current_dims)
        for off_dim, off_val in off_changes:
            existing = next((c for c in changes if c.name == off_dim), None)
            if existing is not None:
                log(f"    [OFFSET] {off_dim} override {existing.value_meters*1000:.2f} → {off_val*1000:.2f} mm")
                existing.value_meters = off_val
            else:
                log(f"    [OFFSET] {off_dim} = {off_val*1000:.2f} mm (linked to source + offset)")
                changes.append(DimensionChange(name=off_dim, value_meters=off_val))

        # A rule set can name a dim this model does not have — a stale set, or one whose
        # component ids no longer match after a rename. `selection.warning` already tells the
        # user those were "left unchanged", but until now nothing MADE that true: the change
        # list still carried them all the way to the app, whose resolver dropped the
        # unmatched `[ComponentId]` and wrote the value to whatever bare dim name matched
        # instead. Live 2026-08-25: 60" bound for the mirror landed on a 1" dim.
        #
        # Filtered HERE, before the followers, so frost/slots/hanger/mates never read a size
        # that was never going to be applied.
        known = ({d.name for d in req.dimensions}
                 | {m.dim for m in req.mate_positions}
                 | {s.dim for s in req.slot_rows})
        unknown = [c.name for c in changes if c.name not in known]
        if unknown:
            changes = [c for c in changes if c.name in known]
            for name in unknown:
                log(f"    [DROP] {name} — no such dimension in this model")
            if not changes:
                return InterpretResponse(error=(
                    f"None of the dimensions in rule set '{selection.key or 'in use'}' exist "
                    f"in this model ({len(unknown)} checked), so there is nothing to resize. "
                    "Click ⚙ Generate Rules to rebuild the rules for this version."))

        # Final policy guard — a stale rules file (generated before the fixed-size
        # policy) can still list a clip/bracket/power-supply dim in also_change, and
        # expand_positions/expand_offsets can add one too. Nothing gets past here.
        changes, dropped = _enforce_policy(changes, req.dim_axis_labels,
                                           model_rules.component_labels, trigger,
                                           current_dims, req.component_types)
        if not changes:
            return InterpretResponse(error="Every dimension in this change is fixed-size "
                                           "hardware that cannot be resized.")

        explanation = data.get("explanation") or f"Applied rule for {if_changes} with {len(changes)} dimensions"
        if dropped:
            explanation += f" (left unchanged: {len(dropped)} fixed-size dim(s))"
        # Coverage gap / derived-membership notice. Previously silent: an unmatched dep was
        # skipped with only an engine-log line, so an under-grown frame looked like a bug.
        explanation += rules_note

        # Frost band before the hanger: it follows the LED strip, which is already in
        # `changes`, and it must not be confused with the hanger's own followers.
        frost = _frost_follower_updates(req, changes, model_rules.component_labels)
        for dim, new_val, old_val in frost:
            changes.append(DimensionChange(name=dim, value_meters=new_val))
            log(f"    [FROST] {dim}: {old_val / 0.0254:.3f}\" → {new_val / 0.0254:.3f}\" "
                f"(matches the LED strip length)")
        if frost:
            explanation += (f" Frosted band followed the LED strip to "
                            f"{frost[0][1] / 0.0254:.3f}\".")

        # Slots before the hanger: this reads the chassis width that the rules just set, and
        # must be in `changes` before the app's inside-out ordering sequences the batch.
        slots = _slot_follower_updates(req, changes)
        for dim, new_val, old_val in slots:
            changes.append(DimensionChange(name=dim, value_meters=new_val))
        if slots:
            grew = slots[0][1] > slots[0][2]
            explanation += (f" Chassis mounting slots {'lengthened' if grew else 'shortened'} to "
                            f"{slots[0][1] / 0.0254:.3f}\" to track the chassis width.")

        # The width rule's dependents include the chassis width dim, which caps the hanger.
        width_deps = [d for r in model_rules.width for d in r.also_change]
        hanger_changes, hanger = _hanger_changes(req, changes, model_rules.component_labels,
                                                 width_deps)
        changes.extend(hanger_changes)
        explanation += _hanger_note(hanger)

        # Last, so it sees every size change already decided and never double-shifts a dim
        # another step is writing.
        mates = _mate_position_updates(req, changes, hanger)
        for dim, new_val, _old, comp in mates:
            changes.append(DimensionChange(name=dim, value_meters=new_val))
        if mates:
            explanation += (f" Moved {len(mates)} positioned component(s) "
                            f"({', '.join(sorted({m[3] for m in mates}))}) to keep the same "
                            f"distance from the edge.")

        _log_changes("CASE 2 rules changes", changes)
        return InterpretResponse(changes=changes, explanation=explanation, hanger=hanger)

    # ── Case 3: no rules, no context ─────────────────────────────────────────
    log("  CASE 3 (no rules, no context) — returning error")
    return InterpretResponse(error="Assembly context not loaded. Please click Refresh Dimensions first.")


def _log_changes(label: str, changes: list[DimensionChange]) -> None:
    log(f"  [{label}]  {len(changes)} change(s):")
    for c in changes:
        log(f"    {c.name:<55} →  {c.value_meters * 1000:>8.2f} mm  ({c.value_meters / 0.0254:>8.3f} in)")
