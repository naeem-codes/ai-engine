import json
from models import InterpretRequest, DimensionChange, InterpretResponse
from rules import load_rules, validate, expand_positions, find_depth_rule
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

            # Thickness (Z) dims the user can address by name, e.g. "make the chassis 2in thick".
            thickness_block = ""
            if model_rules.depth:
                lines = "\n".join(
                    f"  {d.label or d.dim}  =  dim {d.dim}" + (f"  (component {d.component})" if d.component else "")
                    for d in model_rules.depth
                )
                thickness_block = (
                    "\nTHICKNESS DIMENSIONS (human name ⇄ exact thickness dim — use for depth/thickness "
                    f"requests; changing one NEVER changes another):\n{lines}\n"
                )

            log(f"  CASE 2 (rules)  rules_json={len(rules_json)} chars  component_labels={len(model_rules.component_labels)}  depth={len(model_rules.depth)}")
            raw = await call_llm(rules_dependent_prompt(rules_json, labels_block, thickness_block), req.instruction, max_tokens=512)
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
            log(f"  if_changes={if_changes!r}  value_meters={value_meters}")

            if not if_changes or value_meters <= 0:
                return InterpretResponse(error="AI returned invalid rule response")

            # Thickness (Z) change: standalone, single dim. No proportional scaling, no
            # width/height limit check, no position expansion — thickness never cascades.
            depth_rule = find_depth_rule(model_rules, if_changes)
            if depth_rule is not None:
                change = DimensionChange(name=if_changes, value_meters=value_meters)
                explanation = data.get("explanation") or (
                    f"Set {depth_rule.label or if_changes} to {value_meters / 0.0254:.3f} in"
                )
                _log_changes("CASE 2 thickness change", [change])
                return InterpretResponse(changes=[change], explanation=explanation)

            # Validate against limits — skip min check if rule has no dependencies
            trigger = "width" if any(r.if_changes == if_changes for r in model_rules.width) else "height"
            limit_error = validate(model_rules, trigger, value_meters, check_min=bool(also_change))
            if limit_error:
                return InterpretResponse(error=limit_error)

            current_dims = {d.name: d.value_meters for d in req.dimensions}
            master_current = current_dims.get(if_changes, 0.0)
            changes = [DimensionChange(name=if_changes, value_meters=value_meters)]
            for dep in also_change:
                if dep == if_changes:
                    continue
                current = current_dims.get(dep)
                if current is None:
                    log(f"    SKIP {dep!r} — not in dims")
                    continue
                new_val = (current / master_current) * value_meters if master_current > 0 else value_meters
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

            explanation = data.get("explanation") or f"Applied rule for {if_changes} with {len(changes)} dimensions"
            _log_changes("CASE 2 rules changes", changes)
            return InterpretResponse(changes=changes, explanation=explanation)

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
                    current = dims_by_name.get(dname)
                    if current is None:
                        log(f"    [W] SKIP {dname!r} — not in dims")
                        continue
                    new_val = (current / master_current) * target_w
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
                    current = dims_by_name.get(dname)
                    if current is None:
                        log(f"    [H] SKIP {dname!r} — not in dims")
                        continue
                    new_val = (current / master_current) * target_h
                    log(f"    [H] {dname}  {current * 1000:.2f} mm  →  {new_val * 1000:.2f} mm")
                    changes.append(DimensionChange(name=dname, value_meters=new_val))

        if not changes:
            error_msg = "; ".join(axis_errors) if axis_errors else "AI classified no dimensions — check assembly context"
            log(f"  ERROR: {error_msg}")
            return InterpretResponse(error=error_msg)

        _log_changes("CASE 2 OVERALL/CONNECTED final changes", changes)
        return InterpretResponse(changes=changes, explanation=data.get("explanation"))

    # ── Case 3: no rules, no context ─────────────────────────────────────────
    log("  CASE 3 (no rules, no context) — returning error")
    return InterpretResponse(error="Assembly context not loaded. Please click Refresh Dimensions first.")


def _log_changes(label: str, changes: list[DimensionChange]) -> None:
    log(f"  [{label}]  {len(changes)} change(s):")
    for c in changes:
        log(f"    {c.name:<55} →  {c.value_meters * 1000:>8.2f} mm  ({c.value_meters / 0.0254:>8.3f} in)")
