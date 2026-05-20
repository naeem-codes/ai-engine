import json
from models import InterpretRequest, DimensionChange, InterpretResponse
from rules import load_rules, get_triggers, validate, expand
from llm import call_llm
from prompts import rules_prompt, classification_prompt


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        # Skip past the opening fence line (e.g. "```json\n" or "```\n")
        nl = text.find("\n")
        text = text[nl + 1:] if nl != -1 else text[3:]
    if text.endswith("```"):
        text = text[: text.rfind("```")].strip()
    return text.strip()


async def interpret(req: InterpretRequest) -> InterpretResponse:
    model_rules = load_rules(req.model_path)

    if model_rules is not None:
        # Case 1: rules-based — AI identifies trigger + value, we expand
        triggers = get_triggers(model_rules)
        raw = await call_llm(rules_prompt(triggers), req.instruction, max_tokens=256)
        try:
            data = json.loads(_strip_fences(raw))
        except json.JSONDecodeError as exc:
            return InterpretResponse(error=f"LLM returned invalid JSON: {exc}")

        if "error" in data:
            return InterpretResponse(error=data["error"])

        if "changes" in data:
            changes = [
                DimensionChange(name=c["dimension"], value_meters=c["value_meters"])
                for c in data["changes"]
                if c.get("value_meters", 0) > 0
            ]
            if not changes:
                return InterpretResponse(error="AI returned an empty changes list")
            return InterpretResponse(changes=changes, explanation=data.get("explanation"))

        trigger = data.get("trigger", "")
        value_meters = float(data.get("value_meters", 0))
        if not trigger or value_meters <= 0:
            return InterpretResponse(error="AI returned invalid trigger response")

        limit_error = validate(model_rules, trigger, value_meters)
        if limit_error:
            return InterpretResponse(error=limit_error)

        expanded = expand(model_rules, trigger, value_meters)
        changes = [DimensionChange(name=n, value_meters=v) for n, v in expanded]
        explanation = data.get("explanation") or (
            f"Applying {trigger} = {value_meters * 1000:.2f} mm via smart rules ({len(changes)} dimensions)"
        )
        return InterpretResponse(changes=changes, explanation=explanation)

    if req.dim_axis_labels:
        # Case 2: classification — AI classifies dims by axis, we calculate ratios
        large_dims = [d for d in req.dimensions if d.value_meters >= 0.05]
        dim_list = "\n".join(
            f"  [{req.dim_axis_labels.get(d.name, '?')}]  {d.name:<52} = {d.value_meters * 1000:>8.2f} mm  ({d.value_meters / 0.0254:>8.3f} in)"
            for d in large_dims
        ) or "  (no dimensions >= 50 mm found)"

        _dependent_keywords = ("related", "corresponding", "dependent", "and everything")
        is_dependent = req.assembly_context and any(
            kw in req.instruction.lower() for kw in _dependent_keywords
        )
        context_for_prompt = req.assembly_context if is_dependent else None

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
            return InterpretResponse(error=f"LLM returned invalid JSON: {exc}")

        if "error" in data:
            return InterpretResponse(error=data["error"])

        # SINGLE scope: AI returned direct changes with "dimension" key
        if "changes" in data:
            changes = [
                DimensionChange(name=c["dimension"], value_meters=c["value_meters"])
                for c in data["changes"]
                if c.get("value_meters", 0) > 0
            ]
            if not changes:
                return InterpretResponse(error="AI returned an empty changes list")
            return InterpretResponse(changes=changes, explanation=data.get("explanation"))

        # OVERALL / DEPENDENT scope: ratio-based scaling
        dims_by_name = {d.name: d.value_meters for d in req.dimensions}
        changes: list[DimensionChange] = []
        axis_errors: list[str] = []

        target_w = data.get("target_width_meters")
        master_w = data.get("master_width_dim") or req.master_width_dim
        width_dims: list[str] = data.get("width_dims") or []
        if target_w and master_w and width_dims:
            master_current = dims_by_name.get(master_w, 0.0)
            if master_current <= 0:
                axis_errors.append(f"Master width dim '{master_w}' not found in loaded dimensions")
            else:
                for dname in width_dims:
                    current = dims_by_name.get(dname)
                    if current is None:
                        continue
                    changes.append(DimensionChange(name=dname, value_meters=(current / master_current) * target_w))

        target_h = data.get("target_height_meters")
        master_h = data.get("master_height_dim") or req.master_height_dim
        height_dims: list[str] = data.get("height_dims") or []
        if target_h and master_h and height_dims:
            master_current = dims_by_name.get(master_h, 0.0)
            if master_current <= 0:
                axis_errors.append(f"Master height dim '{master_h}' not found in loaded dimensions")
            else:
                for dname in height_dims:
                    current = dims_by_name.get(dname)
                    if current is None:
                        continue
                    changes.append(DimensionChange(name=dname, value_meters=(current / master_current) * target_h))

        if not changes:
            error_msg = "; ".join(axis_errors) if axis_errors else "AI classified no dimensions — check assembly context"
            return InterpretResponse(error=error_msg)

        return InterpretResponse(changes=changes, explanation=data.get("explanation"))

    # Case 3: no rules, no context
    return InterpretResponse(error="Assembly context not loaded. Please click Refresh Dimensions first.")
