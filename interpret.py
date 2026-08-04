import json
import hanger_select
import resize_policy as policy
from hanger_select import select_hanger_meters
from models import (InterpretRequest, DimensionChange, HangerSelection,
                    InterpretResponse)
from rules import load_rules, validate, expand_positions, expand_offsets
from llm import call_llm
from prompts import classification_prompt, rules_dependent_prompt
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
                 if labels.get(d.name) == axis and policy.is_hanger(d.name, component_labels)
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
    choice = select_hanger_meters(glass_w, glass_h,
                                  fitted_w_in=fitted_w / 0.0254,
                                  fitted_h_in=fitted_h / 0.0254)
    for line in choice.log_lines():
        log("  " + line)

    sel = HangerSelection(
        part=choice.part, part_name=choice.part_name, fraction=choice.fraction,
        in_band=choice.in_band, under_target=choice.under_target,
        over_ceiling=choice.over_ceiling, needs_review=choice.needs_review,
        keep_fitted=choice.keep_fitted, resize_fitted=choice.resize_fitted,
        reason=choice.reason, width_dim=w_dim or "", height_dim=h_dim or "",
    )
    if choice.keep_fitted:
        # A bespoke hanger the client made for this product, already in band — do not swap it
        # for a catalogue part (this is what wrongly replaced JEN's 30x15 with a 12x40).
        return [], sel
    if not choice.part and not choice.resize_fitted:
        # Nothing in the matrix fits and there is no fitted hanger to fall back on.
        log("  [HANGER] no prefab fits — hanger left unchanged (no new hangers by policy)")
        return [], sel

    out: list[DimensionChange] = []
    for dim, target in ((w_dim, choice.target_width_m), (h_dim, choice.target_height_m)):
        if not dim or target <= 0:
            continue
        if abs(current.get(dim, 0.0) - target) < 1e-6:
            continue      # already the right catalogue size (e.g. #1038 on a 36x36)
        out.append(DimensionChange(name=dim, value_meters=target))
    if not out:
        log(f"  [HANGER] #{choice.part} already fitted at the correct size — no change")
        return out, sel

    # The chassis HANGING TAB spacing has to follow the hanger width or the tabs stop
    # seating in the hanger's slots. Follow by the width DELTA, which preserves whatever
    # constant inset the aligned model already has (nothing hard-coded).
    if w_dim:
        old_w = current.get(w_dim, 0.0)
        delta = choice.target_width_m - old_w
        if old_w > 0 and abs(delta) > 1e-6:
            for dim, new_val, inset_in in _hanger_follower_updates(req, old_w, delta):
                out.append(DimensionChange(name=dim, value_meters=new_val))
                sel.follower_dims.append(dim)
                log(f"  [HANGER] follower {dim}: {current[dim] / 0.0254:.3f}\" → "
                    f"{new_val / 0.0254:.3f}\" (holds the {inset_in:.3f}\" inset from the "
                    f"hanger width)")
    return out, sel


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
              if policy.is_led_strip(n, component_labels)
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
            if not policy.is_mirror_glass(d.name, component_labels):
                continue
            if labels.get(d.name) in ("W", "H", "D"):
                continue          # an axis driver — already handled as master/dependent
            if abs(d.value_meters - old_len) > 1e-6:
                continue
            seen.add(d.name)
            out.append((d.name, new_len, old_len))
    return out


def _hanger_follower_updates(req: InterpretRequest, old_hanger_w: float, delta: float,
                             ) -> list[tuple[str, float, float]]:
    """Chassis dims that track the hanger width, shifted by the hanger's width delta.

    Yields (dim_name, new_value_meters, current_inset_inches). A candidate is skipped when
    its offset from the hanger width is nowhere near the known 4.25" tab inset — that means
    the name hint matched the wrong dim, and writing it would deform the chassis.
    """
    updates: list[tuple[str, float, float]] = []
    for d in req.dimensions:
        upper = d.name.upper()
        if not any(hint in upper for hint in hanger_select.HANGER_FOLLOWER_HINTS):
            continue
        if policy.is_hanger(d.name, None):
            continue                      # the hanger's own dims are handled above
        inset_m = old_hanger_w - d.value_meters
        inset_in = inset_m / 0.0254
        if abs(inset_in - hanger_select.EXPECTED_TAB_INSET_IN) > hanger_select.EXPECTED_TAB_INSET_TOL_IN:
            log(f"  [HANGER] follower candidate {d.name} SKIPPED — sits {inset_in:.3f}\" "
                f"from the hanger width, not the expected "
                f"{hanger_select.EXPECTED_TAB_INSET_IN:.2f}\"; likely not the tab spacing")
            continue
        new_val = d.value_meters + delta
        if new_val <= 0:
            log(f"  [HANGER] follower {d.name} SKIPPED — would go to {new_val * 1000:.2f} mm")
            continue
        updates.append((d.name, new_val, inset_in))
    return updates


def _hanger_note(hanger: HangerSelection | None,
                 hanger_changes: list[DimensionChange]) -> str:
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
    if not hanger_changes:
        return f" Hanger #{hanger.part} is already the correct prefab ({pct})."
    note = f" Hanger resized to prefab #{hanger.part} ({pct})."
    if hanger.needs_review:
        note += " NEEDS REVIEW — well under the 20% target."
    elif hanger.over_ceiling:
        note += " Note: every prefab exceeds 25% at this size; smallest that fits was used."
    elif hanger.under_target:
        note += " Slightly under the 20% target, but the largest that stays within 25%."
    return note


def _enforce_policy(changes: list[DimensionChange], labels: dict[str, str],
                    component_labels: dict[str, str] | None,
                    axis_hint: str = "",
                    current: dict[str, float] | None = None,
                    ) -> tuple[list[DimensionChange], list[str]]:
    """Drop any change that resize_policy forbids. Returns (kept, [reason lines]).

    This is the last line of defence and runs on EVERY path: rules, SINGLE scope and
    OVERALL/CONNECTED classification. Fixed-size hardware (power supply, clips, brackets,
    hanger) and an LED strip's extrusion profile must not move even if the LLM picks them
    directly or an overall sweep includes them.

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
                                    (current or {}).get(c.name))
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


    # ── Case 2: classification ────────────────────────────────────────────────
    if req.dim_axis_labels:
        # >= 50 mm noise filter, but always keep app-labeled [W]/[H]/[D] axis drivers
        # (a small LED-strip width, or a thin sheet-metal thickness). Generic — no
        # model-specific names.
        large_dims = [
            d for d in req.dimensions
            if d.value_meters >= 0.05 or req.dim_axis_labels.get(d.name, "?") in ("W", "H", "D")
        ]
        dim_list = "\n".join(
            f"  [{req.dim_axis_labels.get(d.name, '?')}]  {d.name:<52} = {d.value_meters * 1000:>8.2f} mm  ({d.value_meters / 0.0254:>8.3f} in)"
            for d in large_dims
        ) or "  (no dimensions >= 50 mm found)"

        # Rules file exists → always use rules_dependent_prompt, ignore keywords
        model_rules = load_rules(req.model_path)
        if model_rules is not None:
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
                                               dim_values.get(if_changes))
            if target_block:
                log(f"  [POLICY] REFUSE target {if_changes!r} — {target_block}")
                return InterpretResponse(error=f"Cannot resize {if_changes} — {target_block}.")

            # Validate against limits — skip min check if rule has no dependencies
            limit_error = validate(model_rules, trigger, value_meters, check_min=bool(also_change))
            if limit_error:
                return InterpretResponse(error=limit_error)

            current_dims = {d.name: d.value_meters for d in req.dimensions}
            master_current = current_dims.get(if_changes, 0.0)
            changes = [DimensionChange(name=if_changes, value_meters=value_meters)]
            for dep in also_change:
                if dep == if_changes:
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
                changes.append(DimensionChange(name=dep, value_meters=new_val))

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

            # Final policy guard — a stale rules file (generated before the fixed-size
            # policy) can still list a clip/bracket/power-supply dim in also_change, and
            # expand_positions/expand_offsets can add one too. Nothing gets past here.
            changes, dropped = _enforce_policy(changes, req.dim_axis_labels,
                                               model_rules.component_labels, trigger,
                                               current_dims)
            if not changes:
                return InterpretResponse(error="Every dimension in this change is fixed-size "
                                               "hardware that cannot be resized.")

            explanation = data.get("explanation") or f"Applied rule for {if_changes} with {len(changes)} dimensions"
            if dropped:
                explanation += f" (left unchanged: {len(dropped)} fixed-size dim(s))"

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

            hanger_changes, hanger = _hanger_changes(req, changes, model_rules.component_labels)
            changes.extend(hanger_changes)
            explanation += _hanger_note(hanger, hanger_changes)

            _log_changes("CASE 2 rules changes", changes)
            return InterpretResponse(changes=changes, explanation=explanation, hanger=hanger)

        # No rules file → use keywords to decide context
        _dependent_keywords = ("related", "corresponding", "dependent", "and everything", "dependencies")
        is_dependent = any(kw in req.instruction.lower() for kw in _dependent_keywords)
        context_for_prompt = req.assembly_context if is_dependent else None

        log(f"  CASE 2 (classification)  large_dims={len(large_dims)}  context_in_prompt={'yes' if context_for_prompt else 'no (OVERALL/SINGLE scope)'}")
        log(f"  dim_list sent to prompt:\n{dim_list}")

        raw = await call_llm(
            classification_prompt(
                context_for_prompt,
                dim_list,
                master_width_dim=req.master_width_dim,
                master_height_dim=req.master_height_dim,
            ),
            req.instruction,
            max_tokens=2048,
        )
        try:
            data = json.loads(_strip_fences(raw))
        except json.JSONDecodeError as exc:
            log(f"  ERROR: LLM returned invalid JSON: {exc}")
            return InterpretResponse(error=f"LLM returned invalid JSON: {exc}")

        if "error" in data:
            log(f"  ERROR (from AI): {data['error']}")
            return InterpretResponse(error=data["error"])

        # SINGLE scope
        if "changes" in data:
            changes = [
                DimensionChange(name=c["dimension"], value_meters=c["value_meters"])
                for c in data["changes"]
                if c.get("value_meters", 0) > 0
            ]
            if not changes:
                log("  ERROR: AI returned empty changes list (SINGLE scope)")
                return InterpretResponse(error="AI returned an empty changes list")
            changes, dropped = _enforce_policy(
                changes, req.dim_axis_labels, None, "",
                {d.name: d.value_meters for d in req.dimensions})
            if not changes:
                return InterpretResponse(
                    error="That component is fixed-size hardware and cannot be resized — "
                          + (dropped[0].split(": ", 1)[-1] if dropped else ""))
            _log_changes("CASE 2 SINGLE scope changes", changes)
            return InterpretResponse(changes=changes, explanation=data.get("explanation"))

        # OVERALL / CONNECTED scope: ratio-based scaling
        dims_by_name = {d.name: d.value_meters for d in req.dimensions}
        changes: list[DimensionChange] = []
        axis_errors: list[str] = []

        target_w = data.get("target_width_meters")
        master_w = data.get("master_width_dim") or req.master_width_dim
        width_dims: list[str] = data.get("width_dims") or []
        log(f"  target_w={target_w}  master_w={master_w!r}  width_dims={width_dims}")
        if target_w and master_w and width_dims:
            master_current = dims_by_name.get(master_w, 0.0)
            if master_current <= 0:
                axis_errors.append(f"Master width dim '{master_w}' not found in loaded dimensions")
            else:
                for dname in width_dims:
                    if dname != master_w and policy.is_mate_dim(dname):
                        log(f"    [W] SKIP {dname!r} — {policy.MATE_DIM_REASON}")
                        continue
                    current = dims_by_name.get(dname)
                    if current is None:
                        log(f"    [W] SKIP {dname!r} — not in dims")
                        continue
                    new_val = (target_w if dname == master_w
                               else _dependent_value(current, master_current, target_w))
                    if new_val is None:
                        log(f"    [W] SKIP {dname!r} — {current / master_current:.1%} of the "
                            f"master: fixed profile, left at {current / 0.0254:.3f}\"")
                        continue
                    if new_val <= 0:
                        log(f"    [W] SKIP {dname!r} — constant offset would give {new_val*1000:.2f} mm")
                        continue
                    log(f"    [W] {dname}  {current * 1000:.2f} mm  →  {new_val * 1000:.2f} mm")
                    changes.append(DimensionChange(name=dname, value_meters=new_val))

        target_h = data.get("target_height_meters")
        master_h = data.get("master_height_dim") or req.master_height_dim
        height_dims: list[str] = data.get("height_dims") or []
        log(f"  target_h={target_h}  master_h={master_h!r}  height_dims={height_dims}")
        if target_h and master_h and height_dims:
            master_current = dims_by_name.get(master_h, 0.0)
            if master_current <= 0:
                axis_errors.append(f"Master height dim '{master_h}' not found in loaded dimensions")
            else:
                for dname in height_dims:
                    if dname != master_h and policy.is_mate_dim(dname):
                        log(f"    [H] SKIP {dname!r} — {policy.MATE_DIM_REASON}")
                        continue
                    current = dims_by_name.get(dname)
                    if current is None:
                        log(f"    [H] SKIP {dname!r} — not in dims")
                        continue
                    new_val = (target_h if dname == master_h
                               else _dependent_value(current, master_current, target_h))
                    if new_val is None:
                        log(f"    [H] SKIP {dname!r} — {current / master_current:.1%} of the "
                            f"master: fixed profile, left at {current / 0.0254:.3f}\"")
                        continue
                    if new_val <= 0:
                        log(f"    [H] SKIP {dname!r} — constant offset would give {new_val*1000:.2f} mm")
                        continue
                    log(f"    [H] {dname}  {current * 1000:.2f} mm  →  {new_val * 1000:.2f} mm")
                    changes.append(DimensionChange(name=dname, value_meters=new_val))

        if not changes:
            error_msg = "; ".join(axis_errors) if axis_errors else "AI classified no dimensions — check assembly context"
            log(f"  ERROR: {error_msg}")
            return InterpretResponse(error=error_msg)

        # An overall/connected sweep scales every [W]/[H] dim it was handed, so without
        # this guard a model with no rules file would still stretch the clips and
        # power supply. No component_labels available on this path — id patterns only.
        changes, dropped = _enforce_policy(changes, req.dim_axis_labels, None, "", dims_by_name)
        if not changes:
            return InterpretResponse(error="All classified dimensions are fixed-size hardware "
                                           "that cannot be resized.")
        explanation = data.get("explanation")
        if dropped:
            explanation = (explanation or "") + f" (left unchanged: {len(dropped)} fixed-size dim(s))"

        frost = _frost_follower_updates(req, changes, None)
        for dim, new_val, old_val in frost:
            changes.append(DimensionChange(name=dim, value_meters=new_val))
            log(f"    [FROST] {dim}: {old_val / 0.0254:.3f}\" → {new_val / 0.0254:.3f}\" "
                f"(matches the LED strip length)")
        if frost:
            explanation = (explanation or "") + (
                f" Frosted band followed the LED strip to {frost[0][1] / 0.0254:.3f}\".")

        hanger_changes, hanger = _hanger_changes(req, changes, None)
        changes.extend(hanger_changes)
        explanation = (explanation or "") + _hanger_note(hanger, hanger_changes)

        _log_changes("CASE 2 OVERALL/CONNECTED final changes", changes)
        return InterpretResponse(changes=changes, explanation=explanation, hanger=hanger)

    # ── Case 3: no rules, no context ─────────────────────────────────────────
    log("  CASE 3 (no rules, no context) — returning error")
    return InterpretResponse(error="Assembly context not loaded. Please click Refresh Dimensions first.")


def _log_changes(label: str, changes: list[DimensionChange]) -> None:
    log(f"  [{label}]  {len(changes)} change(s):")
    for c in changes:
        log(f"    {c.name:<55} →  {c.value_meters * 1000:>8.2f} mm  ({c.value_meters / 0.0254:>8.3f} in)")
