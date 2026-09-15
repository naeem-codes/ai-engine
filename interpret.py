import json
import math
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


def _dependent_value(current: float, master_current: float, master_new: float,
                     radial_kind: int = 0) -> float | None:
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

    `radial_kind` handles the ROUND case. The offset is still constant, but a dim that drives a
    RADIUS moves the outline twice as fast as the master diameter does, so it takes HALF the
    delta -- 12419-RING's D1@Sketch1 grew the ring 5.08 mm on a 2.54 mm nudge. Giving it the
    full delta would grow that feature to double what the mirror did. A diameter-driven dim
    (kind 1) and a plain linear dim (kind 0) both take the delta as-is.
    """
    if master_current <= 0:
        return master_new
    # A RADIUS dim measures HALF the outline it drives, so a genuinely frame-spanning one is
    # inherently ~0.5 of the master diameter -- landing exactly ON MIN_DEPENDENT_FRACTION, the
    # threshold that separates a frame-spanning dim from a fixed extrusion profile. Comparing its
    # raw value would make that call a coin flip; compare the DIAMETER it stands for instead.
    # This does not loosen the guard for the small stuff: ECLIPSE's 12419-RING D1@Sketch1
    # (50.80 mm) and the LED band's D2@Sketch1 (49.30 mm) are radial, and doubled they still come
    # to 13% of the 762 mm glass, so both stay correctly classed as fixed profiles.
    span_value = current * 2 if radial_kind == policy.RADIAL_RADIUS else current
    if not policy.is_frame_spanning(span_value, master_current):
        return None
    return current + policy.radial_delta(master_new - master_current, radial_kind)


def _current_master_in(req: InterpretRequest) -> float:
    """The glass size the model has RIGHT NOW, in inches, off the width master.

    Not the target: this is the denominator for "what fraction of the disc is the hanger today",
    and using the target instead would make the answer depend on where you are going rather than
    where you are.
    """
    master = req.master_width_dim
    if not master:
        return 0.0
    for d in req.dimensions:
        if d.name == master:
            half = 1.0 if req.master_radial == 2 else 1.0   # a diameter master IS the width
            return d.value_meters * half / 0.0254
    return 0.0


def _hanger_changes(req: InterpretRequest, changes: list[DimensionChange],
                    component_labels: dict[str, str] | None,
                    width_deps: list[str] | None = None,
                    model_rules=None,
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
    if req.is_round and glass_w > 0:
        # A circle has no separate height. Without this glass_h reads 0 (there is no height
        # master by design) and the whole hanger step bails out with "no master glass
        # width/height available" — i.e. a round mirror would silently never get a hanger.
        # The AREA is corrected to pi*d^2/4 inside the selector; passing d twice here only says
        # how big the glass is on each axis, which for a circle is the diameter both times.
        glass_h = glass_w
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

    # An LED bracket is mounted ON the chassis, so the hanger cannot use the full chassis width
    # even though the chassis cap says it may. Reserve a band at each end for it — but only on
    # assemblies that actually carry one, so every other product keeps today's behaviour exactly.
    obstacle_clear_in = 0.0
    if any(policy.is_led_bracket(d.name, req.component_types) for d in req.dimensions):
        obstacle_clear_in = hanger_select.OBSTACLE_CLEARANCE_IN
        log(f"  [HANGER] LED bracket present — reserving {obstacle_clear_in:.2f}\" of chassis at "
            f"each end, so the hanger is capped at "
            f"{chassis_w / 0.0254 - 2 * obstacle_clear_in:.3f}\" not {chassis_w / 0.0254:.3f}\"")

    choice = select_hanger_meters(glass_w, glass_h,
                                  fitted_w_in=fitted_w / 0.0254,
                                  fitted_h_in=fitted_h / 0.0254,
                                  chassis_w_in=chassis_w / 0.0254,
                                  obstacle_clear_in=obstacle_clear_in,
                                  round_glass=req.is_round,
                                  # The diameter the fitted hanger is on TODAY. A round product
                                  # scales it by the change, instead of re-deriving a span from
                                  # the rectangle rules - which is what stopped it coming back.
                                  fitted_glass_w_in=_current_master_in(req))
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
        for dim, new_val, _cur_inset, applied_in in _hanger_follower_updates(
                req, old_w, new_w - old_w, model_rules):
            if abs(new_val - current.get(dim, 0.0)) < 1e-9:
                continue              # already correct — nothing to write
            out.append(DimensionChange(name=dim, value_meters=new_val))
            sel.follower_dims.append(dim)
            log(f"  [HANGER] follower {dim}: {current[dim] / 0.0254:.3f}\" → "
                f"{new_val / 0.0254:.3f}\" (holds the {applied_in:.3f}\" inset from the "
                f"hanger width)")

        # Products with no chassis tabs carry a hanging BRACKET instead. Same relationship,
        # different part — and deliberately NOT added to `sel.follower_dims`, which feeds the
        # clip alignment that reads a tab SPACING. A bracket width is not a spacing.
        for dim, new_val, inset_in in _hanger_bracket_follower_updates(req, old_w, new_w - old_w):
            if abs(new_val - current.get(dim, 0.0)) < 1e-9:
                continue              # already correct — nothing to write
            out.append(DimensionChange(name=dim, value_meters=new_val))
            log(f"  [HANGER] bracket {dim}: {current[dim] / 0.0254:.3f}\" → "
                f"{new_val / 0.0254:.3f}\" (keeps its {inset_in:.3f}\" seat inside the "
                f"{new_w / 0.0254:.3f}\" hanger)")
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

    **None of the above applies to a ROUND product.** A disc has no per-axis edge to hold a
    distance from, so its hardware keeps its SHARE of the disc instead: the radius scales with the
    hanger's own X factor, capped so nothing fouls the LED channel. See `_round_shifts`.

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

    # A ROUND product does not play by this rule at all — see `_round_shifts`.
    round_shifts: dict[tuple[str, str], float] = {}
    seat_shifts: dict[tuple[str, str], float] = {}
    if req.is_round:
        # The hanger seats first: everything else takes its X from what the seated brackets did.
        seat_shifts = _hanger_seat_shifts(req, applied, current, hanger)
        round_shifts = _round_shifts(req, applied, current, seat_shifts)
        if not round_shifts and not seat_shifts:
            return []
    elif not half_delta:
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
        if req.is_round:
            # A part seated in the hanger's slots is governed by the HANGER, never by the disc.
            # The two rules would fight, and the slot wins: a bracket half an inch out of its
            # slot is not fitted, however tidily it sits on the circle.
            shift = (seat_shifts if mate.on_hanger else round_shifts
                     ).get((mate.component, mate.axis))
            if shift is None or abs(shift) < 1e-9:
                log(f"  [MATE] {mate.dim} HELD at {mate.value_meters / 0.0254:.3f}\" — "
                    f"{mate.component} does not need to move on this resize")
                continue
        else:
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
        # SIGNED. `shift` is how far the edge moved; `direction` is how this particular mate has
        # to change for its component to follow it. Adding the shift raw is right only for a mate
        # measured straight off the assembly centre plane -- which was every mate the app could
        # classify at the time, and none of the ones it could not. Getting it backwards drives the
        # part TWICE the wrong way, so this is not a refinement.
        new_val = base + shift * mate.direction

        # A clip lines up with the hanging tabs when those moved — same load path, and it
        # replaces the edge offset rather than adjusting it.
        tab_half = (_clip_tab_alignment(hanger, changes)
                    if mate.axis == "W" and not req.is_round else None)
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
        why = ((f"moved {shift / 0.0254:+.3f}\" with the hanger's {mate.axis} edge, so its tab "
                f"stays in the slot"
                if mate.on_hanger else
                f"moved {shift / 0.0254:+.3f}\" along its own radius, so it keeps its share of "
                f"the disc without fouling the LED")
               if req.is_round else
               f"half the {shift * 2 / 0.0254:+.3f}\" master change, keeping its distance from "
               f"the edge")
        log(f"  [MATE] {mate.dim} holds {mate.component} on {mate.axis}: "
            f"{base / 0.0254:.3f}\" → {new_val / 0.0254:.3f}\" ({why})")
        out.append((mate.dim, new_val, base, mate.component))
    return out


# How much clear space a positioned part keeps between itself and the rim of a round chassis
# before it counts as hanging off it. A quarter inch — the same margin the no-overlap floor uses,
# and it also absorbs the chassis disc being drawn a touch inside the glass (ECLIPSE: 59.500"
# of chassis inside a 60.000" mirror), which is the circle the parts actually have to sit on.
ROUND_RIM_MARGIN_M = 0.25 * 0.0254


def _hanger_seat_shifts(req: InterpretRequest, applied: dict[str, float],
                        current: dict[str, float], hanger) -> dict[tuple[str, str], float]:
    """Carry the parts seated in the hanger's slots along with the hanger's edges.

    ECLIPSE hangs off `1004-HANGER`, and `2867-HANGING-BRACKET-1` and `-2` drop their
    `Edge-Flange1` tabs into slots cut in it. The hanger is 14.250" wide on the Ø30 and its two
    brackets sit 5.875" either side of centre, so each tab is 1.250" in from the hanger's end.

    **The slot is cut in the hanger, so it travels with the hanger.** Resizing the glass to Ø45
    stretches the hanger to 29.250", carrying its slots out to 13.375" off centre, while the disc
    rule was moving the brackets to 8.812" - four and a half inches short, tabs sitting on sheet
    metal instead of in a slot. Reported live 2026-09-14, after the disc rule itself was right
    ("the hanger bracket was inside LED correct place... but not inside hanger slots").

    **The law is a CONSTANT OFFSET from the hanger's edge**, which is the same law the rectangular
    products already use for the mirror of this joint: the chassis HANGING TABS track the hanger's
    width at a fixed 4.250" inset, verified on two of the client's products
    ([[project_hanger_tab_follows_width]] -- and see `HangerSelection.follower_dims`, whose whole
    job is that). So the part moves by exactly as much as the hanger's edge moves on that axis,
    which also keeps a part sitting PAST the hanger's edge at the same distance past it.

    Returns (component, axis) -> metres the part must travel. Components the hanger does not move
    are simply absent, which leaves them held rather than guessed at.
    """
    if hanger is None or not req.mate_positions:
        return {}

    seated = {m.component for m in req.mate_positions if m.on_hanger}
    if not seated:
        return {}

    # How far each of the hanger's edges moved. Half the size change: the hanger is centred, so
    # each end travels half of what its overall dimension does.
    edge: dict[str, float] = {}
    for axis, dim, target in (("W", hanger.width_dim, hanger.target_width_meters),
                              ("H", hanger.height_dim, hanger.target_height_meters)):
        if not dim or target <= 0 or dim not in current:
            continue
        # `applied` wins when the hanger is being STRETCHED (the write is in this batch);
        # `target` covers a catalogue SWAP, which changes the size with no dimension write.
        now = applied.get(dim, target)
        moved = (now - current[dim]) / 2.0
        if abs(moved) > 1e-9:
            edge[axis] = moved

    if not edge:
        log("  [MATE] the hanger is not changing size, so the parts seated in it stay put")
        return {}

    out: dict[tuple[str, str], float] = {}
    for mate in req.mate_positions:
        if mate.component in seated and mate.axis in edge:
            out[(mate.component, mate.axis)] = edge[mate.axis]
    for axis, moved in sorted(edge.items()):
        log(f"  [MATE] the hanger's {axis} edge moves {moved / 0.0254:+.3f}\"; "
            f"{', '.join(sorted(seated))} follow it so their tabs stay in the slots")

    # A seated part is NOT clamped back to the LED keep-out, on purpose. Its slot is cut in the
    # hanger, so pulling the part off the slot to clear the ring leaves it fitted to nothing and
    # still under the ring. If the two genuinely collide the hanger is too big for that disc, and
    # saying so is worth more than quietly splitting the difference.
    master = req.master_width_dim
    if master and master in applied:
        rim = applied[master] / (1.0 if req.master_radial == 2 else 2.0)
        for comp in sorted(seated):
            axes = {m.axis: m.offset_meters + out.get((comp, m.axis), 0.0)
                    for m in req.mate_positions if m.component == comp}
            keep = max((m.keep_out_meters for m in req.mate_positions
                        if m.component == comp), default=0.0)
            if len(axes) < 2 or keep <= 0:
                continue
            grown = rim - current.get(master, rim) / (1.0 if req.master_radial == 2 else 2.0)
            r = math.hypot(axes.get("W", 0.0), axes.get("H", 0.0))
            if r > keep + grown:
                log(f"  [MATE] ⚠ {comp} follows the hanger out to {r / 0.0254:.3f}\" from "
                    f"centre, past the LED channel at {(keep + grown) / 0.0254:.3f}\". The hanger "
                    f"is too wide for this disc - the slot itself is under the ring, so moving "
                    f"the bracket would not fix it. Check the hanger size.")
    return out


def _round_shifts(req: InterpretRequest, applied: dict[str, float], current: dict[str, float],
                  seat_shifts: dict[tuple[str, str], float]) -> dict[tuple[str, str], float]:
    """How far every positioned component travels on each axis, on a ROUND product.

    **X comes from the hanger, Y from the disc.** The hanging brackets that sit in the hanger's
    slots are moved by the hanger's own edge (`_hanger_seat_shifts`); every OTHER positioned part
    then takes the SAME X scale factor those brackets got, and keeps the disc's factor on Y.

    That is the user's rule, and it is what keeps a bolted stack together. ECLIPSE's `1005-CLIP`
    is mated to `2867-HANGING-BRACKET-3`: `Jog4` on the clip has to stay on `Edge-Flange1` on the
    bracket. Two parts in one stack cannot be moved by two different rules and still touch - and
    for the same reason brackets 3 and 4 cannot be moved by a different rule from 1 and 2.

    With no hanger-seated bracket to take a factor from (no hanger, or a hanger that is not
    changing size) X falls back to the disc's own factor, which is the previous behaviour.

    Each component's radius is then checked against the LED channel and the rim exactly as before,
    and if it would reach past either, BOTH of its axes are scaled back together so the part
    travels along its own radius rather than swinging.

    Returns (component, axis) -> metres of travel, as a change in DISTANCE FROM THE CENTRE (the
    caller multiplies by `direction`, which carries the side and the gearing).
    """
    master = req.master_width_dim
    if not master or master not in applied or master not in current:
        return {}
    # `master_radial` 2 means the master dim IS a radius; 1 (and the unset default) a diameter.
    half = 1.0 if req.master_radial == 2 else 2.0
    r_old, r_new = current[master] / half, applied[master] / half
    if r_old <= 0 or abs(r_new - r_old) <= 1e-9:
        return {}
    k_disc = r_new / r_old
    grown = r_new - r_old

    # What the seated brackets did to their X, as a factor. Averaged over them, because they are
    # a symmetric pair and one of them missing its mate should not swing the answer.
    factors = [(abs(m.offset_meters) + seat_shifts[(m.component, m.axis)]) / abs(m.offset_meters)
               for m in req.mate_positions
               if m.on_hanger and m.axis == "W" and abs(m.offset_meters) > 1e-9
               and (m.component, m.axis) in seat_shifts]
    k_x = sum(factors) / len(factors) if factors else k_disc
    if factors:
        log(f"  [MATE] the hanger's brackets moved their X by x{k_x:.4f}; everything else on the "
            f"disc follows that on X (the disc itself is x{k_disc:.4f})")

    # Offsets per component, per axis. A component with only ONE mate still has a radius, so the
    # axis it does not name is recovered from that - without it the move could not stay radial.
    off: dict[str, dict[str, float]] = {}
    radius: dict[str, float] = {}
    overhang: dict[str, float] = {}
    keep_out: dict[str, float] = {}
    for mate in req.mate_positions:
        if mate.on_hanger or mate.radius_meters <= 0 or mate.axis not in ("W", "H"):
            continue
        c = mate.component
        off.setdefault(c, {})[mate.axis] = abs(mate.offset_meters)
        radius[c] = max(radius.get(c, 0.0), mate.radius_meters)
        overhang[c] = max(overhang.get(c, 0.0), mate.extent_meters / 2.0)
        keep_out[c] = max(keep_out.get(c, 0.0), mate.keep_out_meters)

    want: dict[str, tuple[float, float, float, float]] = {}   # comp -> ow, oh, want_w, want_h
    for comp, axes in off.items():
        r = radius[comp]
        ow = axes.get("W")
        oh = axes.get("H")
        if ow is None:
            ow = math.sqrt(max(0.0, r * r - (oh or 0.0) ** 2))
        if oh is None:
            oh = math.sqrt(max(0.0, r * r - ow * ow))
        want[comp] = (ow, oh, ow * k_x, oh * k_disc)

    # ONE hold-back for the whole group, taken from whichever member needs it most.
    #
    # Clamping each part by its own size is what pulls a bolted stack apart, and that is the bug
    # this is here to fix: `1005-CLIP` is 3.000" wide and `2867-HANGING-BRACKET-3` is 1.500", both
    # sit 4.000" off centre, and clamping them separately moved them by different amounts - the
    # clip came in further purely because it is wider. On a disc this hardware is one ring and it
    # keeps its arrangement: the tightest ceiling governs all of it.
    pull = 1.0
    tightest = ""
    for comp, (_ow, _oh, ww, wh) in want.items():
        # The two ceilings: the LED channel holds a CONSTANT inset from the rim, so it travels the
        # full radius change while the hardware travels only its share.
        ceiling = r_new - overhang[comp] - ROUND_RIM_MARGIN_M
        if keep_out[comp] > 0:
            ceiling = min(ceiling, keep_out[comp] + grown - overhang[comp] - ROUND_RIM_MARGIN_M)
        want_r = math.hypot(ww, wh)
        if ceiling > 0 and want_r > ceiling and ceiling / want_r < pull:
            pull, tightest = ceiling / want_r, comp
    if pull < 1.0:
        log(f"  [MATE] the whole group is held back to {pull:.4f} of where it wanted to go - "
            f"{tightest} is the part that would have reached the LED. Held together so the parts "
            f"bolted to each other still line up.")

    out: dict[tuple[str, str], float] = {}
    for comp, (ow, oh, ww, wh) in want.items():
        ww, wh = ww * pull, wh * pull
        if "W" in off[comp] and abs(ww - ow) > 1e-9:
            out[(comp, "W")] = ww - ow
        if "H" in off[comp] and abs(wh - oh) > 1e-9:
            out[(comp, "H")] = wh - oh
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
    # Round: one master for both axes, for the radial reason documented in the half-delta loop.
    master = (req.master_width_dim if (mate.axis == "W" or req.is_round)
              else req.master_height_dim)
    if not master:
        return 0.0
    master_new = applied.get(master, current.get(master, 0.0))

    # `master / 6` is a CLIP number: it was read off the client's own narrow products, and what
    # it protects against is a MIRRORED PAIR meeting at the centreline. Applied to anything else
    # it is just an invented lower bound -- on a 56" mirror it would shove any positioned part to
    # at least 9.33" off centre, which for a single top-mounted part like the hanging bracket is
    # simply wrong. The geometric no-overlap bound below is real regardless, so it always applies.
    # ...and it is a RECTANGLE's rule. It exists because a mirrored PAIR of clips meets at the
    # centreline of a narrow glass, which is a thing that happens when clips grip the glass edge.
    # On a round product they do not: ECLIPSE's 1005-CLIP rides on 2867-HANGING-BRACKET-3, bolted
    # to it. Applying master/6 there pinned the clip at exactly 45/6 = 7.500" while its bracket
    # went to 6.000", and again at 36/6 = 6.000" against 4.800" - so `Jog4` on the clip walked off
    # `Edge-Flange1` on the bracket by a size-dependent amount every single resize (reported live
    # 2026-09-14). The geometric no-overlap bound below is real on any shape and still applies.
    is_clip = policy.is_clip(mate.component, req.component_types) and not req.is_round
    proportional = master_new / 6.0 if (is_clip and master_new > 0) else 0.0
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


def _instance_stem(component: str) -> str:
    """`12204-CHASSIS-2` -> `12204-CHASSIS`. The instance suffix is the volatile part."""
    head, sep, tail = component.rpartition("-")
    return head.upper() if sep and tail.isdigit() else component.upper()


def _resolve_stored_dim(stored: str, live: list[str],
                        types: dict[str, str] | None) -> str | None:
    """Find `stored` among the model's live dim names, tolerating a renamed component.

    A rules file records `D1@Sketch81 [12204-CHASSIS-2]`, but the bracketed half is an INSTANCE
    id and instance ids do not survive. A chassis rename changes the part number; a hanger swap
    renumbers the instance. That staleness is what dropped BREAM's two hanger writes on
    2026-09-02 ("no component matches that id"), so a stored link that only ever matched exactly
    would inherit the very bug it exists to fix.

    Three tiers, strongest first, and it gives up rather than guessing wide:
      1. the exact name;
      2. the same dim on the same PART (instance suffix ignored) -- survives re-instancing;
      3. the same dim on any chassis -- survives a part renumber, which the app does routinely
         (`[RENAME] resized: 12226-CHASSIS-2 ... Number='1003'`).
    """
    if stored in live:
        return stored
    head = stored.split(" [", 1)[0].strip().upper()
    if not head:
        return None

    def parts(name):
        h, sep, rest = name.partition(" [")
        return h.strip().upper(), (rest[:-1] if rest.endswith("]") else rest)

    want_comp = parts(stored)[1]
    same_head = [n for n in live if parts(n)[0] == head]
    if not same_head:
        return None

    same_part = [n for n in same_head
                 if _instance_stem(parts(n)[1]) == _instance_stem(want_comp)]
    if len(same_part) == 1:
        log(f"  [TAB] stored link {stored} resolved to {same_part[0]} (re-instanced)")
        return same_part[0]

    on_chassis = [n for n in same_head if policy.is_chassis(n, types)]
    if len(on_chassis) == 1:
        log(f"  [TAB] stored link {stored} resolved to {on_chassis[0]} (component renamed)")
        return on_chassis[0]

    log(f"  [TAB] stored link {stored} matches no single live dim "
        f"({len(same_head)} share its feature) — falling back to the geometric search")
    return None


def _stored_tab_link(req: InterpretRequest, model_rules) -> tuple[str, float] | None:
    """The rules file's hanging-tab link as (live_dim_name, offset_meters), if it has one.

    Only a link whose TARGET is a chassis dim and whose SOURCE is a hanger dim is taken -- a
    user-authored offset between two unrelated dims must not be mistaken for the tab spacing,
    and it is handled properly by `expand_offsets` anyway.

    `expand_offsets` cannot serve this case, which is why the follower reads the link directly:
    it only fires when the source dim is among the turn's changes, and on a prefab SWAP no hanger
    dimension is written at all (the swapped-in file already carries the catalogue size). The
    live hanger width is known here and nowhere else.
    """
    if model_rules is None:
        return None
    live = [d.name for d in req.dimensions]
    for r in getattr(model_rules, "offset", []) or []:
        if not r.target_dim or not r.source_dim:
            continue
        if not policy.is_chassis(r.target_dim, req.component_types):
            continue
        if not policy.is_hanger(r.source_dim, None, req.component_types):
            continue
        resolved = _resolve_stored_dim(r.target_dim, live, req.component_types)
        if resolved:
            return resolved, r.offset_meters
    return None


def _hanger_follower_updates(req: InterpretRequest, old_hanger_w: float, delta: float,
                             model_rules=None,
                             ) -> list[tuple[str, float, float, float]]:
    """Chassis dims that track the hanger width, shifted by the hanger's width delta.

    Yields (dim_name, new_value_meters, current_inset_inches, applied_inset_inches).

    TWO PATHS, and which one runs matters more than what either does.

    1. A STORED LINK, written once by `generate_rules._tab_spacing_offset` from the model as the
       client authored it. The dim is NAMED, so nothing has to be recognised and drift cannot
       hide it. This is the path that should run on every product generated from now on.

    2. The legacy geometric search below, kept verbatim for rule sets written before the link
       existed. It identifies the dim by "sits 4.25" +/-1.0" inside the current hanger width",
       which is only answerable while the model is still aligned. Once it isn't, the dim stops
       being recognised and the tabs freeze wherever they were -- see the BREAM note in
       `generate_rules._tab_spacing_offset`. Regenerating a product's rules moves it to path 1
       and retires this for that model.

    The search is deliberately NOT widened to cover path 2's failures. Dropping its name gate
    was tried and is worse than doing nothing: with the gate removed, a chassis whose real tab
    dim has drifted OUT of the window can leave a decoy as the sole candidate, and the follower
    then confidently writes to a dim it has never touched before. Measured on 12204 at a 24"
    hanger: the real `D1@Sketch81` (15.750") falls outside, `D5@Sketch105` (20.000") is left
    alone in the window. Doing nothing is the correct failure here; the fix is path 1.
    """
    updates: list[tuple[str, float, float, float]] = []

    stored = _stored_tab_link(req, model_rules)
    if stored is not None:
        name, offset_m = stored
        current = {d.name: d.value_meters for d in req.dimensions}.get(name, 0.0)
        new_hanger_w = old_hanger_w + delta
        new_val = new_hanger_w + offset_m
        inset_in = -offset_m / 0.0254
        if new_val <= 0:
            log(f"  [TAB] stored link {name} SKIPPED — would go to {new_val * 1000:.2f} mm")
            return []
        # A stored link turns drift from something that DISABLES the follower into something it
        # repairs: the correct value no longer depends on the current one being right.
        was_in = (old_hanger_w - current) / 0.0254
        if abs(was_in - inset_in) > 0.005:
            log(f"  [TAB] {name} is {was_in:.3f}\" inside the {old_hanger_w / 0.0254:.3f}\" "
                f"hanger but the stored link says {inset_in:.3f}\" — this model drifted, and "
                f"this resize corrects it")
        return [(name, new_val, was_in, inset_in)]

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
        updates.append((d.name, new_val, inset_in, hanger_select.EXPECTED_TAB_INSET_IN))
    return updates


def _hanger_bracket_follower_updates(req: InterpretRequest, old_hanger_w: float, delta: float,
                                     ) -> list[tuple[str, float, float]]:
    """A HANGING BRACKET's width, shifted by the hanger's width delta.

    Yields (dim_name, new_value_meters, current_inset_inches).

    The tab follower above covers products whose tabs are cut into the chassis. ISABELL has no
    tabs: a separate cross-member seats inside the hanger's slots, and it is the WHOLE part's
    width that has to track the hanger.

    Live 2026-08-27, resizing 24x36 -> 34x46: the bracket was named as a width dependent of the
    MIRROR, so it grew +10.000" while the hanger grew only +5.750" (a #1119 -> #1038 swap). It
    finished 23.375" wide against a 20.000" hanger — WIDER than the part it has to seat inside,
    having started 13.375" inside a 14.250" one. Following the hanger instead gives 19.125".

    Unlike the tab inset this CARRIES THE LIVE OFFSET FORWARD rather than re-asserting a
    canonical one: 4.25" is confirmed across four client products, this relationship on one.

    This is also what makes the obstacle clearance work at all — capping the hanger only keeps
    the bracket away from the LED brackets because the bracket tracks the hanger.
    """
    updates: list[tuple[str, float, float]] = []
    seen: set[str] = set()
    for d in req.dimensions:
        if not policy.is_hanging_bracket(d.name, req.component_types):
            continue
        if (req.dim_axis_labels or {}).get(d.name) != "W":
            continue                      # only the dim that spans the hanger
        if d.name in seen:
            continue                      # the dim dump lists every dim twice
        seen.add(d.name)

        inset_in = (old_hanger_w - d.value_meters) / 0.0254
        if not (-1e-9 <= inset_in <= hanger_select.MAX_BRACKET_INSET_IN):
            log(f"  [HANGER] bracket candidate {d.name} SKIPPED — sits {inset_in:.3f}\" from "
                f"the hanger width, outside the 0-{hanger_select.MAX_BRACKET_INSET_IN:.2f}\" a "
                f"part seating INSIDE the hanger can have. Either it is not the hanging "
                f"bracket, or this model's bracket is already misaligned — check it in "
                f"SolidWorks")
            continue

        new_val = d.value_meters + delta
        if new_val <= 0:
            log(f"  [HANGER] bracket {d.name} SKIPPED — would go to {new_val * 1000:.2f} mm")
            continue
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


def _round_circles_left_behind(req: InterpretRequest, master: str, also_change: list[str],
                               current_dims: dict[str, float]) -> str:
    """Warn when a ROUND product leaves one of its own concentric circles out of the resize.

    On a disc every concentric circle is the same size axis seen from a different radius, so one
    that does not move is not a design choice — it is a rule set that has gone stale, and the
    damage is invisible until SolidWorks refuses to solve.

    Live on ECLIPSE, 2026-09-14. `12419-RING` has no size dimension at all: it is driven IN
    CONTEXT off an EDGE OF THE GLASS, and the chassis's `Sketch46` in turn CONVERTS ~25 of the
    ring's edges while carrying `D15@Sketch46` (12.980") and `D17@Sketch46` (13.079") as its own
    dimensions of that same geometry. At the drawn Ø30 the two agree. Grow the glass and the
    converted edges travel with it while the dimensions stay put, so `Sketch45` and `Sketch46` go
    OVER-DEFINED — reported by SolidWorks as
    `swSketchErrorExtRefFail`, and in the UI as "the sketch is overdefined, consider deleting some
    overdefining dimensions or relations".

    The app now labels those two dims (it measures them against the ring's own radius), but a
    RULE SET SAVED BEFORE THAT still lists three dependants and none of them is the channel — so
    the fix looks applied and changes nothing. Exactly what happened: the run that should have
    shown it regenerated no rules at all. Hence this line, which says so in the chat.
    """
    if not req.is_round or not req.radial_dims:
        return ""
    covered = {master} | set(also_change)
    missed = [d for d in req.radial_dims
              if d not in covered and d in current_dims and current_dims[d] > 0]
    if not missed:
        return ""
    for d in missed:
        log(f"  [ROUND] {d!r} draws a circle on this product and no rule moves it "
            f"({current_dims[d] / 0.0254:.3f}\")")
    listed = ", ".join(sorted(missed)[:4]) + (" …" if len(missed) > 4 else "")
    return (f" ⚠ {len(missed)} dimension(s) that draw a circle on this model are not in its "
            f"rule set, so they will not move: {listed}. On a round product that leaves the "
            f"sketches that reference them over-defined — regenerate the rules (⚙ Generate Rules) "
            f"and try again.")


def _expand_master(master: str, value_meters: float, also_change: list[str],
                   current_dims: dict[str, float],
                   radial_dims: dict[str, int] | None = None) -> list[DimensionChange]:
    """One master dim plus its dependents, each moved by the master's CONSTANT OFFSET."""
    radial_dims = radial_dims or {}
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
        kind = radial_dims.get(dep, 0)
        new_val = _dependent_value(current, master_current, value_meters, kind)
        if kind == policy.RADIAL_RADIUS and new_val is not None:
            log(f"    [ROUND] {dep!r} drives a RADIUS — taking half the "
                f"{(value_meters - master_current) / 0.0254:+.3f}\" diameter change")
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
    if req.is_round:
        # A circle has one size. The prompt already tells the model this, so reaching here means
        # it produced a second number anyway -- applying it would write a diameter onto whatever
        # the labeller happened to leave on [H], which on a round assembly is hardware.
        log("  [2-AXIS] ignored — a round mirror has ONE size axis (the diameter)")
        return [], (" This is a round mirror, so it has a single size: the diameter. "
                    "Only one value was applied.")
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
    return _expand_master(other_master, other_value, deps, current_dims, req.radial_dims), ""


# How close a dimension has to be to the master's own value before it is treated as THE SAME
# CIRCLE. 0.5% - on a 30in disc that is 0.15in, while the nearest thing on HALO that is not the
# same circle (its chassis ring) is 4.6% away and on ECLIPSE (its chassis) 1.7%.
SAME_CIRCLE_FRACTION = 0.005


def _is_the_glass_circle(dim: str, req: InterpretRequest,
                         current: dict[str, float] | None) -> bool:
    """Is this dim drawing the GLASS'S OWN circle, whatever the part is called?

    `resize_policy` classifies by NAME, and on a round product that gets HALO wrong.
    `12646-LED BRACKET-BOTTOM` is an ARC of the disc - its `D1@Sketch1` is 762.00 mm, the glass
    diameter to the hundredth - but the name says "LED BRACKET", so the policy calls it fixed-size
    hardware and drops it after the rule has already correctly listed it:

        [POLICY] DROP D1@Sketch1 [12646-LED BRACKET-BOTTOM-1] (1143.00 mm)
                 - LED bracket is fixed-size hardware - repositioned by its mates, never resized

    So the two LED bracket assemblies stayed at their drawn size through every resize, while the
    rule set looked right.

    **Measurement beats a name.** Two independent facts have to agree: the app nudged this dim and
    watched it grow a CIRCLE (`radial_dims`), and its value IS the master's. A hole or a fillet on
    the same part passes neither. Round products only - on a rectangle nothing here applies.
    """
    if not req.is_round or dim not in (req.radial_dims or {}):
        return False
    master = req.master_width_dim
    if not master or not current:
        return False
    here, there = current.get(dim), current.get(master)
    if not here or not there or there <= 0:
        return False
    return abs(here - there) <= there * SAME_CIRCLE_FRACTION


def _enforce_policy(changes: list[DimensionChange], labels: dict[str, str],
                    component_labels: dict[str, str] | None,
                    axis_hint: str = "",
                    current: dict[str, float] | None = None,
                    types: dict[str, str] | None = None,
                    req: InterpretRequest | None = None,
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
        if reason and req is not None and _is_the_glass_circle(c.name, req, current):
            log(f"    [POLICY] KEPT {c.name} — the policy calls it fixed-size hardware by name, "
                f"but it is drawn at the glass's own diameter and the nudge proved it drives a "
                f"circle. It IS the frame; measurement wins.")
            reason = ""
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
    if req.is_round:
        kind = ("a RADIUS — targets are halved" if req.master_radial == policy.RADIAL_RADIUS
                else "a DIAMETER")
        log(f"  shape             : ROUND (master is {kind}, "
            f"{len(req.radial_dims)} radial dim(s))")
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
                                   master_height_dim=req.master_height_dim,
                                   is_round=req.is_round),
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

            # The master is supposed to BE a rule trigger. When it is not, the app has handed us
            # a dim the rule set treats as a DEPENDENT, and anchoring there quietly drops the real
            # master: it appears in no `also_change`, so nothing writes it.
            #
            # Live on ECLIPSE, 2026-09-14. Its glass `D1@Sketch1 [1026-MIRROR-ECLIPSE-1]` and its
            # lit ring `D1@Sketch1 [LED FLEX EXTRUSION-ECLIPSE  30]` are BOTH exactly 762.00 mm,
            # because the ring is drawn at the glass diameter, so whichever the app read first won
            # the tie -- the glass on one connect, the LED on the next. With the LED as master the
            # net below could not fire (no rule is keyed on the LED), the chassis and the LED went
            # to Ø45 and the mirror stayed at Ø30. Every mate solved, SolidWorks reported nothing,
            # and the crossing check refused the whole resize.
            #
            # Follow the dependency back to its trigger: that is the dim the rule was written
            # around, and it is the one the user means by "the size".
            if master and not any(r.if_changes == master for r in axis_rules):
                owner = next((r for r in axis_rules if master in r.also_change), None)
                if owner is not None:
                    log(f"  [OVERALL] master {master!r} is a DEPENDENT of {owner.if_changes!r} "
                        f"- anchoring on the trigger instead")
                    master = owner.if_changes
                elif len(axis_rules) == 1:
                    # The set's own TRIGGER names something this model no longer has, while its
                    # dependants still name real dims: a fork whose anchor went stale. The anchor
                    # is the recoverable half - the app measures the master off the model - so
                    # take the rule's dependants and put them on the master we can see.
                    #
                    # HALO 2026-09-15: `#2` listed the six `1111-*` dims the model actually had,
                    # under a trigger that still said `HALO-60-MIRROR` after a rename to
                    # `HALO-70-MIRROR`. Without this the whole set was unusable for the sake of
                    # one name.
                    live_names = {d.name for d in req.dimensions}
                    only = axis_rules[0]
                    if (only.if_changes not in live_names
                            and any(d in live_names for d in only.also_change)):
                        log(f"  [OVERALL] the rule's own trigger {only.if_changes!r} is not in "
                            f"this model; re-anchoring its {len(only.also_change)} dependant(s) "
                            f"onto the measured master {master!r}")
                        if_changes = master
                        also_change = list(only.also_change)

            if master and if_changes != master:
                mrule = next((r for r in axis_rules if r.if_changes == master), None)
                if mrule is not None:
                    log(f"  [OVERALL] re-anchored if_changes {if_changes!r} → master {master!r}")
                    if_changes = master
                    also_change = list(mrule.also_change)

        if not if_changes or value_meters <= 0:
            return InterpretResponse(error="AI returned invalid rule response")

        # ROUND, and the master dim is a RADIUS rather than a diameter. The user always speaks in
        # diameters ("make it 40 inches" = a 40" circle), so the value has to be halved before it
        # is written or the mirror comes out twice the size asked for. Only ever applied to the
        # master itself, and only on an overall change: a named-component request is already in
        # that dim's own terms. Not seen on ECLIPSE -- its glass is dimensioned Ø762 mm, ratio
        # 1.00 -- but the app measures the difference per product, so it has to be honoured.
        if (req.is_round and scope == "overall"
                and req.master_radial == policy.RADIAL_RADIUS
                and if_changes == req.master_width_dim):
            log(f"  [ROUND] master {if_changes!r} is a RADIUS dim — halving the requested "
                f"{value_meters / 0.0254:.3f}\" diameter to {value_meters / 2 / 0.0254:.3f}\"")
            value_meters = value_meters / 2.0

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

        # A rule that names dependants of which NOT ONE exists here is not describing this model.
        # Writing the master on its own is the worst possible outcome - it rebuilds, it saves, and
        # the whole frame is left at the old size. That is exactly what HALO did on 2026-09-15
        # (0 of 7 dims present), and only the crossing check caught it, after the fact.
        if also_change and not any(d in current_dims for d in also_change):
            missing = ", ".join(sorted(also_change)[:3])
            return InterpretResponse(error=(
                f"This model's rule set does not match it: none of the {len(also_change)} "
                f"dimension(s) it wants to change exist here ({missing}"
                f"{' …' if len(also_change) > 3 else ''}). Resizing the master on its own would "
                f"leave the frame behind, so nothing was written. Regenerate the rules "
                f"(⚙ Generate Rules) for this model."))

        changes = _expand_master(if_changes, value_meters, also_change, current_dims,
                                 req.radial_dims)
        rules_note += _round_circles_left_behind(req, if_changes, also_change, current_dims)

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
                                           current_dims, req.component_types, req)
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
                                                 width_deps, model_rules)
        changes.extend(hanger_changes)
        explanation += _hanger_note(hanger)

        # Last, so it sees every size change already decided and never double-shifts a dim
        # another step is writing.
        mates = _mate_position_updates(req, changes, hanger)
        for dim, new_val, _old, comp in mates:
            changes.append(DimensionChange(name=dim, value_meters=new_val))
        if mates:
            why = ("back inside the rim, which has come in past them" if req.is_round
                   else "to keep the same distance from the edge")
            explanation += (f" Moved {len(mates)} positioned component(s) "
                            f"({', '.join(sorted({m[3] for m in mates}))}) {why}.")

        _log_changes("CASE 2 rules changes", changes)
        return InterpretResponse(changes=changes, explanation=explanation, hanger=hanger)

    # ── Case 3: no rules, no context ─────────────────────────────────────────
    log("  CASE 3 (no rules, no context) — returning error")
    return InterpretResponse(error="Assembly context not loaded. Please click Refresh Dimensions first.")


def _log_changes(label: str, changes: list[DimensionChange]) -> None:
    log(f"  [{label}]  {len(changes)} change(s):")
    for c in changes:
        log(f"    {c.name:<55} →  {c.value_meters * 1000:>8.2f} mm  ({c.value_meters / 0.0254:>8.3f} in)")
