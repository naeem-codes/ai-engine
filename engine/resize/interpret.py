import json
import math
from engine.resize import chassis_slots
from engine.hangers import hanger_select
from engine.core import outline
from engine.resize import resize_policy as policy
from engine.rules import rules_store as store
from engine.hangers.hanger_select import select_hanger_meters
from engine.core.models import (InterpretRequest, DimensionChange, HangerSelection,
                    InterpretResponse)
from engine.rules.rules import (load_rules, parse_rules, validate,
                   expand_positions, expand_offsets)
from engine.llm.llm import call_llm
from engine.llm.prompts import rules_dependent_prompt
from engine.core.log import log, section


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

    # A CURVED chassis narrows towards its ends, and the hanging tabs are cut near the top —
    # so "the hanger fits across the chassis" is not the question. The question is whether the
    # TAB ROW the hanger drives still lands on material, and the answer is smaller.
    #
    # Capping the hanger is the right lever, not capping the tabs. The tabs track the hanger at
    # a fixed 4.25" inset, so clamping them alone would leave the brackets sitting inboard of
    # the slots they are supposed to drop into — a part that does not seat, in place of a part
    # that does not build. Since the two move one-for-one, the hanger's ceiling is simply its
    # current width plus whatever growth the row has left.
    #
    # The slot length grows with the chassis too and the two compound, so the headroom below is
    # taken with this turn's new slot length already counted — the slot follower runs first, so
    # it is sitting in `changes` by now.
    tab_cap_w_in = 0.0
    tab_row = _tab_row(req)
    if fitted_w > 0 and _tab_dim_in_row(req, tab_row, fitted_w, model_rules) is not None:
        span = _row_headroom(req, changes)
        if span is not None:
            growth, why = span
            tab_cap_w_in = max(0.0, (fitted_w + growth) / 0.0254)
            log(f"  [OUTLINE] {why}")
            log(f"  [OUTLINE] the tab row may grow {growth / 0.0254:+.3f}\", so the hanger is "
                f"capped at {tab_cap_w_in:.3f}\" — not the {chassis_w / 0.0254:.3f}\" the "
                f"chassis measures across its middle")

    # The hanging bracket follows the hanger, so keeping the bracket off the sides is a limit on
    # the hanger. See `_seat_gap_cap`.
    seat_cap_w_in = _seat_gap_cap(req, fitted_w, glass_w)

    choice = select_hanger_meters(glass_w, glass_h,
                                  fitted_w_in=fitted_w / 0.0254,
                                  fitted_h_in=fitted_h / 0.0254,
                                  chassis_w_in=chassis_w / 0.0254,
                                  tab_cap_w_in=tab_cap_w_in,
                                  seat_cap_w_in=seat_cap_w_in,
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
               to_prefab: bool = False,
               prefab_part: str = "",
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

        `to_prefab` says the hanger arriving is a CATALOGUE part (`prefab_part`), whose slots
        are cut to the catalogue pattern rather than to the fitted hanger's own inset — see
        `_hanger_follower_updates`.
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
        # BACKSTOP, not the main defence. `tab_cap_w_in` above is what should keep the tabs on
        # material, by choosing a hanger whose spacing fits. This catches the paths that reach a
        # spacing the cap never saw: a STORED LINK writes the rules file's offset outright, and
        # the drift correction re-asserts the canonical 4.25" inset on a model that had wandered.
        # Both are right to do that and neither knows about the outline.
        #
        # Clamping here does leave the tabs inboard of the hanger's slots, which is its own
        # problem — so it says so rather than passing silently. A tab row that does not build is
        # worse than one that does not seat: the first takes the whole resize with it.
        row_now = _tab_row(req)
        span = _row_headroom(req, changes)
        for dim, new_val, _cur_inset, applied_in in _hanger_follower_updates(
                req, old_w, new_w - old_w, model_rules, to_prefab, prefab_part):
            # Only the dim that actually drives the measured row, for the reason
            # `_tab_dim_in_row` documents: on most products the row in hand is the MOUNTING
            # slots and the tabs are a separate row the app never reported.
            if span is not None and row_now is not None and _sketch_of(dim) == _sketch_of(row_now.dim):
                growth, why = span
                ceiling = current.get(dim, 0.0) + growth
                if new_val > ceiling:
                    log(f"  [OUTLINE] {why}")
                    log(f"  [OUTLINE] tab spacing {dim} held at {ceiling / 0.0254:.3f}\" "
                        f"instead of {new_val / 0.0254:.3f}\" — the row would have run off the "
                        f"material. The tabs now sit inboard of the hanger's slots; check the "
                        f"hanger size for this shape")
                    new_val = ceiling
            if new_val <= 0 or abs(new_val - current.get(dim, 0.0)) < 1e-9:
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
        return finish([], hanger_w=choice.target_width_m, to_prefab=True,
                      prefab_part=choice.part)

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

    # A part seated in the hanger's slots follows the HANGER on every shape — the slot is cut in
    # the hanger, whatever outline the glass has. Gated behind `is_round` until 2026-09-28, which
    # moved MICHELLE's seated brackets by the glass half-delta even when the hanger stayed put.
    seat_shifts = _hanger_seat_shifts(req, applied, current, hanger)
    # A ROUND product does not play by the edge rule at all — see `_round_shifts`.
    round_shifts: dict[tuple[str, str], float] = {}
    if req.is_round:
        # The hanger seats first: everything else takes its X from what the seated brackets did.
        round_shifts = _round_shifts(req, applied, current, seat_shifts)
        if not round_shifts and not seat_shifts:
            return []
    elif not half_delta and not seat_shifts:
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
        if req.is_round or mate.on_hanger:
            # A part seated in the hanger's slots is governed by the HANGER, never by the glass.
            # The two rules would fight, and the slot wins: a bracket half an inch out of its
            # slot is not fitted, however tidily it sits against the edge.
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
        why = (f"moved {shift / 0.0254:+.3f}\" with the hanger's {mate.axis} edge, so its tab "
               f"stays in the slot"
               if mate.on_hanger else
               f"moved {shift / 0.0254:+.3f}\" along its own radius, so it keeps its share of "
               f"the disc without fouling the LED"
               if req.is_round else
               f"half the {shift * 2 / 0.0254:+.3f}\" master change, keeping its distance from "
               f"the edge")
        log(f"  [MATE] {mate.dim} holds {mate.component} on {mate.axis}: "
            f"{base / 0.0254:.3f}\" → {new_val / 0.0254:.3f}\" ({why})")
        out.append((mate.dim, new_val, base, mate.component))
    return _outline_hold_back(req, changes, out)


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


def _tab_stack_sketches(req: InterpretRequest, model_rules=None) -> set[str]:
    """Every sketch (`SKETCH53 [2222-CHASSIS-1]`) that belongs to the hanging-tab stack.

    From the stored links, and by structure off the live model — the structure test needs no
    alignment, because here it only decides what a MOUNTING-slot follower must keep its hands
    off, not where anything goes.
    """
    keys = {hanger_select.sketch_key(n) for n, _ in _stored_tab_links(req, model_rules)}
    dim_values = {d.name: d.value_meters for d in req.dimensions}
    for row in hanger_select.find_tab_spacing_by_slot_row(dim_values, math.inf,
                                                          req.component_types):
        keys.add(hanger_select.sketch_key(row.dim))
        for m in hanger_select.find_tab_stack_members(dim_values, row, math.inf,
                                                      req.component_types).members:
            keys.add(hanger_select.sketch_key(m.dim))
    return keys


def _slot_follower_updates(req: InterpretRequest, changes: list[DimensionChange],
                           model_rules=None,
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

    NEVER on a hanging-tab row, however the app reported it. The app's row finder cannot tell a
    tab notch from a mounting slot by shape, and live 2026-09-25 (AMY 74x98 -> 24x48) it handed
    over `Cut-Extrude7`'s notches (`D1@Sketch53` = 1.750in, the hanger slot's own length) as the
    mounting slots; this follower then "shortened the mounting slots to 0.500in" and cut the
    notches down to a third of the slot the tab has to pass through. The notch width belongs to
    the hanger's slot, and the tab stack is positioned by `_hanger_follower_updates`.

    Returns (dim_name, new_value_meters, current_value_meters).
    """
    current = {d.name: d.value_meters for d in req.dimensions}
    out: list[tuple[str, float, float]] = []
    seen: set[str] = set()
    if not req.slot_rows:
        return out
    tab_row = _tab_row(req)          # None on every rectangle, and on anything unmeasured
    tab_sketches = _tab_stack_sketches(req, model_rules)
    for row in req.slot_rows:
        if hanger_select.sketch_key(row.dim) in tab_sketches:
            log(f"  [SLOTS] {row.dim} is part of the hanging-tab stack, not a mounting-slot row — "
                f"left to the tab follower")

    for change in changes:
        if (req.dim_axis_labels or {}).get(change.name) != "W":
            continue
        comp = change.name[change.name.index("[") + 1:change.name.rindex("]")] \
            if "[" in change.name and "]" in change.name else ""

        for row in req.slot_rows:
            if row.component != comp:
                continue                    # the row lives on a different part
            if hanger_select.sketch_key(row.dim) in tab_sketches:
                continue                    # a tab notch, not a mounting slot — see above
            if row.dim == change.name:
                continue                    # the slot dim itself is not a trigger
            if row.dim in seen or row.dim not in current:
                continue

            spec = chassis_slots.spec_from_measurement(row)
            if spec is None:
                if chassis_slots.is_hole_row(row) and row.dim not in seen:
                    seen.add(row.dim)
                    log(f"  [SLOTS] {row.dim} is a row of holes, not slots "
                        f"({row.length_meters / 0.0254:.3f}\" long x "
                        f"{row.slot_width_meters / 0.0254:.3f}\" wide) — left as drawn")
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

            # `chassis_slots` measures the room across the part's WIDEST point, which on a
            # curved chassis is not where the row is. Same ceiling as the tab spacing gets, and
            # the same arithmetic: a symmetric row sends half of any length change to each end,
            # so the length may grow by exactly the headroom the spacing may. Asked with the
            # spacing held still, because at this point in the turn it has not moved yet.
            span = (_row_headroom(req, changes, slot_length_m=row.length_meters)
                    if tab_row is not None and tab_row.dim == row.dim else None)
            if span is not None:
                growth, why = span
                if new_val > row.length_meters + growth:
                    log(f"  [OUTLINE] {why}")
                    log(f"  [OUTLINE]   {row.dim} held to "
                        f"{(row.length_meters + growth) / 0.0254:.3f}\" rather than "
                        f"{new_val / 0.0254:.3f}\" — the widest point of the part is not where "
                        f"this row sits")
                    new_val = row.length_meters + growth
            if new_val <= 0:
                continue
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


# ── The curved outline, and the three checks that have to ask it ─────────────────────────────
#
# All three exist because of two failures measured live on CAPSULE 20x40 on 2026-09-17, both
# traceable to the same assumption — see `outline` for the full account. Everything here is
# gated on the app having sent BOTH a curved shape and a shell size; without either, every
# function returns "no ceiling" and the rectangular path runs untouched.


def _outline_hold_back(req: InterpretRequest, changes: list[DimensionChange],
                       out: list[tuple[str, float, float, str]],
                       ) -> list[tuple[str, float, float, str]]:
    """Pull the positioned hardware back in on W until it clears a CURVED shell.

    Holding a part's distance from the top edge and its distance from the side edge is exactly
    right on a rectangle, and both were held on CAPSULE 20x40 -> 40x50. The clip still ended up
    inside `12376-CHASSIS TOP`, because under a curved end the limit is DIAGONAL and neither axis
    check can see it: the clip moved 5.000" up and 6.875" out, each keeping its own clearance,
    while its distance from the arc's centre went from 6.86" to 18.94" against a 19.00" rim. Two
    inches and a fifth of clearance became six hundredths.

    **W only.** The height offset is what holds the part against the top edge, and the client's
    own drawings keep it; sliding the part down the glass to make room would be inventing a new
    position rather than correcting an impossible one. Moving it inboard is the correction that
    matches what the geometry actually ran out of.

    **ONE pull for the whole group**, taken from whichever member needs it most — the lesson
    `_round_shifts` already carries. This hardware is bolted together: CAPSULE's clip rides on
    the hanging bracket, and clamping each by its own size moves them by different amounts and
    pulls the joint apart. The widest part would always come in furthest, purely for being wide.

    Rectangles never reach here, and neither does anything the app has not measured a shell for.

    Neither does a ROUND product, which has its own radial path in `_round_shifts` — one that
    also knows about the LED channel, which this does not. Two clamps on one part would fight,
    and the disc's is the better informed of the two.

    ⚠️ **SWITCHED OFF, and left here on purpose.** Run live on CAPSULE 20x40 -> 40x50 it pulled
    the clip from 10.875" to 10.246" and the clip STILL fouled `12376-CHASSIS TOP`. So the number
    it computes is not the real limit, and a clamp that moves a part 0.6" without fixing anything
    is worse than no clamp: it changes the model for no benefit and makes the next diagnosis
    harder. The app now asks SolidWorks directly, through `ClearMoveInterferences` — real
    interference volumes, bisected until clear — which needs no theory about the outline at all.

    Kept rather than deleted because the SHAPE measurement behind it is sound and the tab-row
    ceiling above still uses it. If interference detection ever turns out to be too slow to run
    on every resize, this is the cheap pre-filter to bring back — but only once something has
    checked its answer against the model's.
    """
    return out
    if req.is_round or not req.is_curved or not out:   # pragma: no cover - see the note above
        return out
    half_w, half_h = _shell_after(req, changes)
    if half_w <= 0:
        return out

    by_dim = {m.dim: m for m in req.mate_positions}
    moved: dict[str, float] = {}          # dim -> the component's NEW distance off centre
    axes: dict[str, dict[str, float]] = {}
    size: dict[str, float] = {}

    for dim, new_val, base, comp in out:
        mate = by_dim.get(dim)
        if mate is None or abs(mate.direction) < 1e-6 or mate.axis not in ("W", "H"):
            continue
        # The inverse of how `new_val` was built: the mate moves its part by
        # (value change) / direction, `direction` carrying the side and the gearing.
        off = abs(mate.offset_meters) + (new_val - base) / mate.direction
        moved[dim] = off
        axes.setdefault(mate.component, {})[mate.axis] = off
        size[mate.component] = max(size.get(mate.component, 0.0), mate.extent_meters / 2.0)

    pull, tightest, detail = 1.0, "", ""
    for comp, ax in axes.items():
        x, y = ax.get("W"), ax.get("H")
        if x is None or x <= 0:
            continue                      # nothing being written on W — nothing to hold back
        if y is None:
            # One mate, so the other axis has to come from the radius the app measured, or the
            # part would be checked as though it sat on the centreline.
            r = max((m.radius_meters for m in req.mate_positions
                     if m.component == comp), default=0.0)
            if r <= abs(x):
                continue
            y = math.sqrt(r * r - x * x)
        allowed = outline.max_x_at(y, half_w, half_h, req.shape, overhang=size.get(comp, 0.0))
        if allowed < x and x > 0 and allowed / x < pull:
            pull, tightest = allowed / x, comp
            detail = (f"{comp} would sit {x / 0.0254:.3f}\" off centre at "
                      f"{y / 0.0254:.3f}\" up, where a {req.shape} shell "
                      f"{half_w * 2 / 0.0254:.3f}\" across its middle leaves it "
                      f"{allowed / 0.0254:.3f}\"")
    if pull >= 1.0:
        return out

    log(f"  [OUTLINE] {detail}")
    log(f"  [OUTLINE] the whole group is held back to {pull:.4f} of its W travel — {tightest} "
        f"is the part that would have fouled the shell. Held together so the parts bolted to "
        f"each other still line up; their height offsets are untouched.")

    held: list[tuple[str, float, float, str]] = []
    for dim, new_val, base, comp in out:
        mate = by_dim.get(dim)
        if mate is None or mate.axis != "W" or dim not in moved:
            held.append((dim, new_val, base, comp))
            continue
        want = moved[dim] * pull
        pulled = base + (want - abs(mate.offset_meters)) * mate.direction
        floor = _mate_position_floor(mate, {c.name: c.value_meters for c in changes},
                                     {d.name: d.value_meters for d in req.dimensions}, req)
        if pulled < floor:
            pulled = floor                # the no-overlap bound still wins over the shell
        log(f"  [OUTLINE]   {dim}: {new_val / 0.0254:.3f}\" → {pulled / 0.0254:.3f}\"")
        held.append((dim, pulled, base, comp))
    return held


def _shell_after(req: InterpretRequest, changes: list[DimensionChange]) -> tuple[float, float]:
    """The shell's half-width and half-height AFTER this resize, in metres. (0, 0) when unknown.

    The shell is the CHASSIS, and the caller must not substitute the glass for it: CAPSULE's clip
    finished 0.06" off the chassis rim while still sitting comfortably inside the glass, so the
    glass would have waved through the collision that was reported.

    It grows by the MASTER'S DELTA, not by the master's ratio. That is the constant-offset law
    the whole resize already runs on, and it is what the models show — the chassis is the glass
    minus a fixed border, 0.5" on MICHELLE and 2" on CAPSULE, at every size the client draws.
    """
    if req.outline_w_meters <= 0 or req.outline_h_meters <= 0:
        return 0.0, 0.0
    applied = {c.name: c.value_meters for c in changes}
    current = {d.name: d.value_meters for d in req.dimensions}

    def delta(dim: str | None) -> float:
        if not dim or dim not in applied or dim not in current:
            return 0.0
        return applied[dim] - current[dim]

    w = req.outline_w_meters + delta(req.master_width_dim)
    # A round product has no height master by design and its one size drives both axes.
    h = req.outline_h_meters + (delta(req.master_width_dim) if req.is_round
                                else delta(req.master_height_dim))
    return (w / 2.0, h / 2.0) if w > 0 and h > 0 else (0.0, 0.0)


def _tab_row(req: InterpretRequest):
    """The chassis slot row the hanging tabs sit in, or None when it cannot be placed.

    "Cannot be placed" is the common case on a model the app has not re-measured yet, and it has
    to stay harmless: every caller treats None as "no outline ceiling".

    A row is only usable if it carries its own position. `row_y_meters` is read out of the
    sketch, so it is trusted only when it lands INSIDE the part — a sketch whose origin is not
    the part's centre reports a row that cannot be where it says it is, and a wrong height is
    worse than no height because it would invent a ceiling nobody asked for.

    With more than one row the highest wins: it is the one closest to the narrowing end, so it
    is the one that fails first.
    """
    best = None
    for row in req.slot_rows or []:
        if row.part_width_meters <= 0 or row.part_height_meters <= 0:
            continue
        if row.outermost_meters <= 0:
            continue
        if abs(row.row_y_meters) >= row.part_height_meters / 2.0:
            log(f"  [OUTLINE] {row.component} slot row sits {row.row_y_meters / 0.0254:.3f}\" "
                f"off centre on a {row.part_height_meters / 0.0254:.3f}\" part — the sketch's "
                f"origin is not the part's centre, so the row's height is not usable here")
            continue
        if not policy.is_chassis(row.dim, req.component_types):
            continue
        if best is None or abs(row.row_y_meters) > abs(best.row_y_meters):
            best = row
    return best


def _sketch_of(dim: str) -> str:
    """`D3@Sketch6 [12375-CHASSIS-1]` -> `SKETCH6 [12375-CHASSIS-1]`. "" when it has no sketch.

    Two dims sharing this are two dims drawn in the same sketch, so they move the SAME contours.
    That is the whole question a spacing ceiling has to answer before it fires.
    """
    at = dim.find("@")
    return dim[at + 1:].strip().upper() if at >= 0 else ""


def _tab_dim_in_row(req: InterpretRequest, row, old_hanger_w: float, model_rules) -> str | None:
    """The hanging-tab SPACING dim, but only if it drives the slot row `row` measured off.

    The gate matters. `MeasureSlotRows` already refuses to return a row it recognises as a
    hanging-tab cut, so on most products the row in hand is the MOUNTING slots and the tabs are a
    separate row the app never reported. Applying a tab ceiling to a row the tabs do not move
    would clamp the wrong thing for the wrong reason.

    On CAPSULE they ARE the same row: `D2@Sketch6` drives the slot length and `D3@Sketch6` the
    spacing, both in `Sketch6`, both cut by `Cut-Extrude2` — the feature that failed. Same sketch
    is exactly the test.

    Identification reuses the follower's own two paths so the two can never disagree about which
    dim this is: the rules file's stored link first, then "sits 4.25in inside the hanger width".
    """
    if row is None:
        return None
    want = _sketch_of(row.dim)
    if not want:
        return None

    stored = _stored_tab_link(req, model_rules)
    if stored is not None:
        return stored[0] if _sketch_of(stored[0]) == want else None

    if old_hanger_w <= 0:
        return None
    for d in req.dimensions:
        if _sketch_of(d.name) != want or d.name == row.dim:
            continue
        inset_in = (old_hanger_w - d.value_meters) / 0.0254
        if abs(inset_in - hanger_select.EXPECTED_TAB_INSET_IN) <= hanger_select.EXPECTED_TAB_INSET_TOL_IN:
            return d.name
    return None


def _row_headroom(req: InterpretRequest, changes: list[DimensionChange],
                  slot_length_m: float | None = None) -> tuple[float, str] | None:
    """How much a slot row may still SPREAD before it runs off the material.

    Returns (metres_of_growth, why) — negative when the row is ALREADY over — or None when there
    is no ceiling to apply, which is every rectangular product and any model the app has not
    re-measured.

    The row's outer end is tracked by DELTA, never rebuilt from the spacing dim. On CAPSULE that
    dim reads 10.000" while the row's own contours put the outer slot end 6.140" off centre, so
    its datum is not the geometry's. Moving a symmetric row by a known change is exact whatever
    the datum is; reconstructing where it starts from is not.

        outer_end(spacing) = outer_end_now + (spacing - spacing_now)/2 + (length - length_now)/2

    Both terms are halved because the row is symmetric about the centre, so each end takes half
    of whatever the pair does.

    The two moves COMPOUND, and checking either on its own lets the pair through: on the CAPSULE
    resize that aborted, the spacing accounted for 2.875" of the overrun and the lengthening slot
    for a further 1.094". So the slot length is picked up from `changes` by default — the slot
    follower runs before both callers, so by the time either asks, the new length is already
    there. Pass `slot_length_m` only to override that, which the slot follower itself does when
    it is still deciding what the length should be.

    ROUND is deliberately left out. A disc narrows towards its top just as an obround does, so
    the same ceiling would apply and probably should — but round products build correctly today
    and this change was not made for them. Widening it is a separate, testable step.
    """
    if req.is_round or not req.is_curved:
        return None
    half_w, half_h = _shell_after(req, changes)
    if half_w <= 0:
        return None
    row = _tab_row(req)
    if row is None:
        return None

    allowed = outline.max_x_at(row.row_y_meters, half_w, half_h, req.shape)
    if allowed <= 0:
        return None
    if slot_length_m is None:
        slot_length_m = next((c.value_meters for c in changes if c.name == row.dim),
                             row.length_meters)
    grow = (slot_length_m - row.length_meters) / 2.0
    headroom = allowed - row.outermost_meters - grow
    across = outline.half_width_at(row.row_y_meters, half_w, half_h, req.shape) * 2
    why = (f"{row.row_y_meters / 0.0254:.3f}\" up a {req.shape} shell "
           f"{half_w * 2 / 0.0254:.3f}\" across its middle, the part is only "
           f"{across / 0.0254:.3f}\" wide, so the outer slot end cannot pass "
           f"{allowed / 0.0254:.3f}\" (it is at {row.outermost_meters / 0.0254:.3f}\" now"
           + (f", and the slot is lengthening by {grow * 2 / 0.0254:.3f}\"" if abs(grow) > 1e-9
              else "") + ")")
    return headroom * 2.0, why


def _stored_tab_link(req: InterpretRequest, model_rules) -> tuple[str, float] | None:
    """The rules file's hanging-tab ROW link as (live_dim_name, offset_meters), if it has one.

    The row is the spacing the rest of the stack is measured from — see `_stored_tab_links`,
    which returns it first.
    """
    links = _stored_tab_links(req, model_rules)
    return links[0] if links else None


def _stored_tab_links(req: InterpretRequest, model_rules) -> list[tuple[str, float]]:
    """Every hanging-tab link in the rules file as (live_dim_name, offset_meters), ROW FIRST.

    Only a link whose TARGET is a chassis dim and whose SOURCE is a hanger dim is taken -- a
    user-authored offset between two unrelated dims must not be mistaken for the tab spacing,
    and it is handled properly by `expand_offsets` anyway.

    `expand_offsets` cannot serve this case, which is why the follower reads the links directly:
    it only fires when the source dim is among the turn's changes, and on a prefab SWAP no hanger
    dimension is written at all (the swapped-in file already carries the catalogue size). The
    live hanger width is known here and nowhere else.

    MORE THAN ONE is normal on AMY 24x48: the notch row (`D2@Sketch53`, Cut-Extrude7) and the
    tabs (`D1@Sketch35`, Boss-Extrude2) each carry their own spacing, and reading only the first
    link moved the notches out from under tabs that stayed put. The generator writes the row
    first; it is re-identified here by the slot length its sketch carries, so a hand-reordered
    file cannot hand the row's role to a tab.
    """
    if model_rules is None:
        return []
    live = [d.name for d in req.dimensions]
    out: list[tuple[str, float]] = []
    seen: set[str] = set()
    for r in getattr(model_rules, "offset", []) or []:
        if not r.target_dim or not r.source_dim:
            continue
        if not policy.is_chassis(r.target_dim, req.component_types):
            continue
        if not policy.is_hanger(r.source_dim, None, req.component_types):
            continue
        resolved = _resolve_stored_dim(r.target_dim, live, req.component_types)
        if resolved and resolved not in seen:
            seen.add(resolved)
            out.append((resolved, r.offset_meters))
    if len(out) > 1:
        slot_m = hanger_select.HANGER_SLOT_LENGTH_IN * 0.0254
        tol_m = hanger_select.HANGER_SLOT_LENGTH_TOL_IN * 0.0254
        row_sketches = {hanger_select.sketch_key(d.name) for d in req.dimensions
                        if abs(d.value_meters - slot_m) <= tol_m}
        rows = [link for link in out if hanger_select.sketch_key(link[0]) in row_sketches]
        if len(rows) == 1:
            out.remove(rows[0])
            out.insert(0, rows[0])
    return out


def _hanger_follower_updates(req: InterpretRequest, old_hanger_w: float, delta: float,
                             model_rules=None, to_prefab: bool = False, prefab_part: str = "",
                             ) -> list[tuple[str, float, float, float]]:
    """Chassis dims that track the hanger width, shifted by the hanger's width delta.

    Yields (dim_name, new_value_meters, current_inset_inches, applied_inset_inches), the tab
    ROW first.

    TWO PATHS, and which one runs matters more than what either does.

    1. STORED LINKS, written once by `generate_rules._tab_spacing_offset` from the model as the
       client authored it. The dims are NAMED, so nothing has to be recognised and drift cannot
       hide them. This is the path that should run on every product generated from now on.
       There is one per dimensioned feature in the tab stack — the row, plus (AMY 24x48) the
       tabs' own spacing — and every one of them moves, or the stack comes apart.

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

    3. A NON-canonical stack (AMY), from stored links or read off the live model: positioned
       against the fitted hanger's own SLOTS, not against a hanger-width offset. See
       `_slot_relative_tab_updates` — a hanger-width offset stops being true the moment a
       different hanger is fitted, which is exactly what broke AMY 74x98 -> 24x48.

    Canonical stored links (the row sits the catalogue 4.25in inside, AMBER/KELLY/PIAZZA/BREAM)
    keep path 1 exactly as it was: verified live, and 4.25in IS the prefab slot pattern.
    """
    links = _stored_tab_links(req, model_rules)
    if links and abs(-links[0][1] / 0.0254 - hanger_select.EXPECTED_TAB_INSET_IN) <= 0.005:
        return _apply_tab_links(req, links, old_hanger_w, delta, to_prefab, "stored link")

    if not links:
        updates = _legacy_tab_follower_updates(req, old_hanger_w, delta)
        if updates:
            return updates

    updates = _slot_relative_tab_updates(req, old_hanger_w, delta, to_prefab, prefab_part, links)
    if updates is not None:
        return updates
    # No slot position anywhere to measure against: the stored offsets are all there is.
    if links:
        return _apply_tab_links(req, links, old_hanger_w, delta, to_prefab, "stored link")
    return []


def _apply_tab_links(req: InterpretRequest, links: list[tuple[str, float]],
                     old_hanger_w: float, delta: float, to_prefab: bool, source: str,
                     ) -> list[tuple[str, float, float, float]]:
    """Write every tab-stack link as `new_hanger_width + offset`, the row (links[0]) first."""
    updates: list[tuple[str, float, float, float]] = []
    current = {d.name: d.value_meters for d in req.dimensions}
    new_hanger_w = old_hanger_w + delta

    # WHOSE slots are the tabs about to sit in? That decides the inset, and nothing else
    # does. A prefab being swapped in brings its own slot pattern, cut the canonical 4.25in
    # total inside its width on all four confirmed client pairs (14.25 -> 10.000,
    # 20 -> 15.750, 30 -> 25.750, 40 -> 35.740), so the row moves onto THAT. A hanger that is
    # kept or merely stretched keeps the slots it has — they travel with its edge, measured
    # on AMY 23.000 -> 28.500in: still 4.750in in from each edge — so the stored inset stands.
    #
    # Getting this wrong in the generous direction is not a near miss: AMY 24x48 -> 34x58
    # keeps its bespoke 23.000in hanger, and forcing the canonical inset moved the tab row
    # 13.500in -> 18.750in, off slots that never moved (live 2026-09-21).
    #
    # The whole stack shifts by ONE amount, taken from the row: the tabs sit a fixed
    # distance inside the notches, whichever hanger the notches are lined up with.
    row_inset_in = -links[0][1] / 0.0254
    shift_in = 0.0
    if to_prefab and abs(row_inset_in - hanger_select.EXPECTED_TAB_INSET_IN) > 0.005:
        shift_in = row_inset_in - hanger_select.EXPECTED_TAB_INSET_IN
        log(f"  [TAB] {links[0][0]} sits {row_inset_in:.3f}\" inside the fitted hanger, but "
            f"a PREFAB is being fitted and its slots are cut "
            f"{hanger_select.EXPECTED_TAB_INSET_IN:.2f}\" inside its width — the tab stack "
            f"moves onto the prefab's pattern")

    for name, offset_m in links:
        inset_in = -offset_m / 0.0254 - shift_in
        new_val = new_hanger_w - inset_in * 0.0254
        if new_val <= 0:
            # All or nothing: the members are one arrangement, and moving the rest without
            # this one pulls the tabs out of their notches.
            log(f"  [TAB] {source} {name} SKIPPED — would go to {new_val * 1000:.2f} mm; "
                f"the rest of the tab stack is left alone with it")
            return []
        # A stored link turns drift from something that DISABLES the follower into something
        # it repairs: the correct value no longer depends on the current one being right.
        was_in = (old_hanger_w - current.get(name, 0.0)) / 0.0254
        if abs(was_in - inset_in) > 0.005 and not to_prefab:
            log(f"  [TAB] {name} is {was_in:.3f}\" inside the {old_hanger_w / 0.0254:.3f}\" "
                f"hanger but the {source} says {inset_in:.3f}\" — this model drifted, and "
                f"this resize corrects it")
        updates.append((name, new_val, was_in, inset_in))
    return updates


def _tab_stack_names(req: InterpretRequest, links: list[tuple[str, float]],
                     hanger_w: float) -> list[str]:
    """The tab stack's live dim names, ROW FIRST: from the stored links, else off the model.

    Off the model is the same identification `generate_rules._tab_spacing_offset` stores — the
    row by the 1.750in slot length its sketch carries, the members by tab width plus footprint —
    so a product gets the fix whether or not its rules were regenerated. Live 2026-09-25 19:03:
    the user reconnected and resized AMY on a rule set with no links, and nothing moved.
    """
    if links:
        return [name for name, _ in links]
    dim_values = {d.name: d.value_meters for d in req.dimensions}
    rows = hanger_select.find_tab_spacing_by_slot_row(dim_values, hanger_w, req.component_types)
    if not rows:
        return []
    if len(rows) > 1:
        log(f"  [TAB] tab stack NOT identified — {len(rows)} chassis sketches carry the slot "
            f"length with a span: {', '.join(r.dim for r in rows)}")
        return []
    scan = hanger_select.find_tab_stack_members(dim_values, rows[0], hanger_w,
                                                req.component_types)
    return [rows[0].dim] + [m.dim for m in scan.members]


def _slot_relative_tab_updates(req: InterpretRequest, old_hanger_w: float, delta: float,
                               to_prefab: bool, prefab_part: str,
                               links: list[tuple[str, float]],
                               ) -> list[tuple[str, float, float, float]] | None:
    """Put the tab stack where the FITTED hanger's slots will be, measured, not assumed.

    The law (see `hanger_select.PREFAB_SLOT_EDGE_INSET_IN` for the survey behind it): every
    hanger dimensions its slot's outer end `D2@Sketch3` in from its edge, so the slots' outer span
    is `width - 2 x D2@Sketch3`, and the tab row sits a fixed DATUM inside that span — 0 for AMY's
    notch row, which is dimensioned to the slots' outer ends. Each other stack member (the tabs'
    own spacing) keeps its distance from the row. So:

        row      = new hanger width - (slot outer inset of the hanger being fitted + datum)
        member   = row + (member - row)

    and the only thing that differs between the three hanger outcomes is the slot inset:
      * kept or stretched — the fitted hanger's own, read off the model (its slots travel with
        its edge: AMY 23 -> 28.5in, still 4.750in in, measured live);
      * a prefab swapped in — the catalogue part's, from the measured table.

    Storing an offset from the hanger WIDTH, as the first version did, is only true while the
    hanger the offset was measured on stays fitted. Live 2026-09-25, AMY 74x98 -> 24x48: the
    stretched bespoke hanger was swapped for prefab #1119, and the row was sent to 10.000in (a
    4.25in inset assumed for the prefab) against slots that need 13.500in.

    Returns None when there is no slot position to measure against at all (the caller falls back
    to the stored offsets); [] when it declines — a misaligned model with nothing to re-seat it
    from, or no stack — and says why.
    """
    if old_hanger_w <= 0:
        return []
    names = _tab_stack_names(req, links, old_hanger_w)
    if not names:
        return []
    current = {d.name: d.value_meters for d in req.dimensions}
    if any(n not in current for n in names):
        return []
    slot_insets = hanger_select.hanger_slot_row_insets(current, req.component_types)
    if not slot_insets:
        if not links:
            log(f"  [TAB] tab stack {', '.join(names)} left alone — the fitted hanger's slots "
                f"carry no position dim, so there is nothing to line the stack up against")
        return None

    row = names[0]
    row_inset_in = (old_hanger_w - current[row]) / 0.0254
    fit = hanger_select.slot_datum(row_inset_in, slot_insets)
    if fit is not None:
        slot_now_in, datum_in = fit
        rel = {n: current[n] - current[row] for n in names}
        basis = "the model, which the hanger's own slot sketch confirms is aligned"
    else:
        # The model has drifted. A stored link can still re-seat it, provided the link was made
        # on a hanger with THIS slot pattern — its own inset must fit the current slots.
        stored = dict(links)
        fit = (hanger_select.slot_datum(-stored[row] / 0.0254, slot_insets)
               if row in stored else None)
        if fit is None:
            detail = " or ".join(f"{e:.3f}\"" for e in sorted({round(e, 3) for e in slot_insets}))
            log(f"  [TAB] tab stack left alone — {row} sits {row_inset_in:.3f}\" inside the hanger "
                f"but its slots are {detail} in: this model's tabs are already off the slots and "
                f"nothing stored says where they belong. Reconnect for a fresh copy")
            return []
        slot_now_in, datum_in = fit
        rel = {n: stored[n] - stored[row] for n in names if n in stored}
        if len(rel) != len(names):
            return []
        basis = "the stored link — the model had drifted and this resize re-seats it"

    if to_prefab:
        slot_new_in = 2 * hanger_select.prefab_slot_edge_inset_in(prefab_part)
        why = f"prefab #{prefab_part or '?'}'s slots, {slot_new_in / 2:.3f}\" in from its edge"
    else:
        slot_new_in = slot_now_in
        why = f"the fitted hanger's slots, {slot_now_in / 2:.3f}\" in from its edge"

    new_hanger_w = old_hanger_w + delta
    row_new = new_hanger_w - (slot_new_in + datum_in) * 0.0254
    updates: list[tuple[str, float, float, float]] = []
    for n in names:
        new_val = row_new + rel[n]
        if new_val <= 0:
            log(f"  [TAB] tab stack left alone — {n} would go to {new_val * 1000:.2f} mm, and the "
                f"stack only moves together")
            return []
        updates.append((n, new_val, (old_hanger_w - current[n]) / 0.0254,
                        (new_hanger_w - new_val) / 0.0254))
    log(f"  [TAB] tab stack lined up with {why} (row datum {datum_in:.3f}\"), from {basis}: "
        f"{', '.join(names)}")
    return updates


def _legacy_tab_follower_updates(req: InterpretRequest, old_hanger_w: float, delta: float,
                                 ) -> list[tuple[str, float, float, float]]:
    """Path 2 of `_hanger_follower_updates`, kept verbatim — see its docstring."""
    updates: list[tuple[str, float, float, float]] = []
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
    for name, value, inset_in in _hanging_bracket_dims(req, old_hanger_w, quiet=False):
        new_val = value + delta
        if new_val <= 0:
            log(f"  [HANGER] bracket {name} SKIPPED — would go to {new_val * 1000:.2f} mm")
            continue
        updates.append((name, new_val, inset_in))
    return updates


def _hanging_bracket_dims(req: InterpretRequest, old_hanger_w: float, quiet: bool = True,
                          ) -> list[tuple[str, float, float]]:
    """(dim_name, current_value_meters, inset_inches) for every hanging-bracket width that seats
    INSIDE the hanger - the dims `_hanger_bracket_follower_updates` moves with it."""
    found: list[tuple[str, float, float]] = []
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
            if not quiet:
                log(f"  [HANGER] bracket candidate {d.name} SKIPPED — sits {inset_in:.3f}\" from "
                    f"the hanger width, outside the 0-{hanger_select.MAX_BRACKET_INSET_IN:.2f}\" a "
                    f"part seating INSIDE the hanger can have. Either it is not the hanging "
                    f"bracket, or this model's bracket is already misaligned — check it in "
                    f"SolidWorks")
            continue
        found.append((d.name, d.value_meters, inset_in))
    return found


def _seat_gap_cap(req: InterpretRequest, hanger_w_now: float, glass_w_new: float) -> float:
    """The widest hanger, in inches, whose hanging bracket still sits at least as far from the
    glass's left and right edges as the product was built with. 0 = no limit.

    The bracket's end tabs drop into slots near the hanger's ends (measured on SUZI: tabs at
    +-17.94..19.56", slots at +-17.87..19.62"), so its width is the hanger's minus a fixed inset
    and moves one-for-one with it. Holding the bracket back on its own would pull the tabs out of
    the slots; limiting the hanger keeps both. The user's rule, 2026-09-29: "mark the original
    distance of the hanger bracket from the left and right side, and it should still keep that
    distance after the resize".

    The glass grows about its centre, so each edge moves half the width change and the bracket's
    ends move half the hanger's change. Keeping a gap of at least `built` on both sides:

        hanger_new <= hanger_now + (glass_new - glass_now) + 2 * min(gap_now - built, per side)

    The frame parts (chassis, chassis corners, the LED strip drawn off them) all hold a constant
    border from the glass edge, so a distance kept from the glass is kept from them too.
    """
    if req.is_round or not req.seat_gaps or hanger_w_now <= 0 or glass_w_new <= 0:
        return 0.0
    glass_w_now = next((d.value_meters for d in req.dimensions
                        if d.name == req.master_width_dim), 0.0)
    if glass_w_now <= 0:
        return 0.0
    gaps = {g.component.upper(): g for g in req.seat_gaps}
    caps: list[float] = []
    for name, _value, _inset in _hanging_bracket_dims(req, hanger_w_now):
        g = gaps.get(policy.component_of(name).upper())
        if g is None:
            log(f"  [SEAT] {name}: no as-built distance from the glass edges was measured — the "
                f"hanger is not limited by it")
            continue
        slack = min(g.left_meters - g.built_left_meters, g.right_meters - g.built_right_meters)
        cap = hanger_w_now + (glass_w_new - glass_w_now) + 2 * slack
        log(f"  [SEAT] {g.component} was built {g.built_left_meters / 0.0254:.3f}\" / "
            f"{g.built_right_meters / 0.0254:.3f}\" from the glass's left / right edges and sits "
            f"{g.left_meters / 0.0254:.3f}\" / {g.right_meters / 0.0254:.3f}\" now — to keep at "
            f"least that on a {glass_w_new / 0.0254:.3f}\" glass the hanger it follows may be at "
            f"most {cap / 0.0254:.3f}\" wide")
        caps.append(cap)
    return max(min(caps), 0.0) / 0.0254 if caps else 0.0


def _warn(text: str) -> None:
    """An ADVISORY for the engine log only — never the chat explanation.

    The user asked (2026-09-25) for no warnings on the main form: the explanation says what the
    resize DID, and caveats about how it was done (a borrowed rule set, a circle the rules do not
    move, a hanger outside its target band) go here, where a support trace still finds them.
    Anything that STOPS a resize is not a warning and still comes back as an error.
    """
    text = (text or "").strip()
    if text:
        log(f"  [WARN] {text}")


def _hanger_note(hanger: HangerSelection | None) -> str:
    """One human-readable clause about the hanger, for the chat explanation.

    What was chosen, only. Whether the choice sits well in the 20-25% band is an advisory and is
    logged instead — see `_warn`.
    """
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
        _warn(f"hanger #{hanger.part} NEEDS REVIEW — well under the 20% target ({pct}).")
    elif hanger.over_ceiling:
        _warn(f"every prefab exceeds 25% at this size; the smallest that fits (#{hanger.part}) "
              f"was used.")
    elif hanger.under_target:
        _warn(f"hanger #{hanger.part} is slightly under the 20% target ({pct}), but the largest "
              f"that stays within 25%.")
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
    shown it regenerated no rules at all. Hence this line — logged as a `[WARN]`, not shown in the
    app's chat (the user asked for no warnings on the main form, 2026-09-25).
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
                   radial_dims: dict[str, int] | None = None,
                   half_frame: set[str] | None = None) -> list[DimensionChange]:
    """One master dim plus its dependents, each moved by the master's CONSTANT OFFSET.

    A `half_frame` dependent sits on a part running from the centre line to ONE edge, so its far
    edge moves half the master delta. That is the radius case exactly (centre to rim), and it
    takes the same path: judged at double its value against the frame-spanning bar, then given
    half the change.
    """
    radial_dims = radial_dims or {}
    half_frame = half_frame or set()
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
        half = kind == 0 and dep in half_frame
        if half:
            kind = policy.RADIAL_RADIUS
        new_val = _dependent_value(current, master_current, value_meters, kind)
        if half and new_val is not None:
            log(f"    [HALF] {dep!r} runs from the centre line to one edge — taking half the "
                f"{(value_meters - master_current) / 0.0254:+.3f}\" change")
        elif kind == policy.RADIAL_RADIUS and new_val is not None:
            log(f"    [ROUND] {dep!r} drives a RADIUS — taking half the "
                f"{(value_meters - master_current) / 0.0254:+.3f}\" diameter change")
        if new_val is None:
            log(f"    SKIP {dep!r} — {current / master_current:.1%} of the master"
                f"{' (a half-frame part, judged at double)' if half else ''}: a "
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
    return _expand_master(other_master, other_value, deps, current_dims, req.radial_dims,
                          set(req.half_frame_dims)), ""


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
    if req.half_frame_dims:
        log("  half-frame dims   : " + ", ".join(req.half_frame_dims))


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
        # Advisory (a borrowed set, dims it names that are missing here) — the log, not the chat.
        _warn(selection.warning)
        rules_note = ""
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
                                 req.radial_dims, set(req.half_frame_dims))
        _warn(_round_circles_left_behind(req, if_changes, also_change, current_dims))

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
        slots = _slot_follower_updates(req, changes, model_rules)
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
