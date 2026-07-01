import json
from models import DrawingPlanRequest, DrawingPlanResponse, DrawingOp
from llm import call_llm
from prompts import drawing_plan_prompt
from log import log, section


# The fixed verb vocabulary the app can execute. The LLM is told only these exist;
# we also validate here so a hallucinated verb never reaches the .NET app.
ALLOWED_VERBS = {
    "create_view", "set_units", "set_sheet", "overall_dimensions", "auto_dimension",
    "fill_title_block", "add_notes", "export_pdf",
}  # auto_dimension kept as a back-compat alias for overall_dimensions


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        nl = text.find("\n")
        text = text[nl + 1:] if nl != -1 else text[3:]
    if text.endswith("```"):
        text = text[: text.rfind("```")].strip()
    return text.strip()


async def generate_drawing_plan(req: DrawingPlanRequest) -> DrawingPlanResponse:
    section("DRAWING PLAN REQUEST")
    log(f"  instruction : {req.instruction}")
    log(f"  model_path  : {req.model_path or '(none)'}")

    raw = await call_llm(drawing_plan_prompt(), req.instruction, max_tokens=1024)
    try:
        data = json.loads(_strip_fences(raw))
    except json.JSONDecodeError as exc:
        log(f"  ERROR: invalid JSON: {exc}")
        return DrawingPlanResponse(error=f"AI returned invalid JSON: {exc}")

    if "error" in data and data["error"]:
        log(f"  ERROR (from AI): {data['error']}")
        return DrawingPlanResponse(error=data["error"])

    ops: list[DrawingOp] = []
    for raw_op in data.get("operations", []):
        verb = (raw_op.get("verb") or "").strip().lower()
        if verb not in ALLOWED_VERBS:
            log(f"  DROP unknown verb: {verb!r}")
            continue
        ops.append(DrawingOp(
            verb=verb,
            type=raw_op.get("type"),
            value=raw_op.get("value"),
            scheme=raw_op.get("scheme"),
            view=raw_op.get("view"),
            preset=raw_op.get("preset"),
            lines=raw_op.get("lines", []) or [],
        ))

    if not ops:
        return DrawingPlanResponse(error="AI returned no valid drawing operations.")

    log(f"  PLAN: {len(ops)} op(s) → " + ", ".join(o.verb for o in ops))
    return DrawingPlanResponse(operations=ops, explanation=data.get("explanation"))
