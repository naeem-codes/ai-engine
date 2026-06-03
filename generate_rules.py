import json
from models import GenerateRulesRequest, GenerateRulesResponse, RulePair, SkipEntry
from llm import call_llm
from prompts import rules_system_prompt
from log import log, section


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        nl = text.find("\n")
        text = text[nl + 1:] if nl != -1 else text[3:]
    if text.endswith("```"):
        text = text[: text.rfind("```")].strip()
    return text.strip()


async def generate_rules(req: GenerateRulesRequest) -> GenerateRulesResponse:
    section("GENERATE RULES REQUEST")
    log(f"  dimensions        : {len(req.dimensions)} total")
    log(f"  master_width_dim  : {req.master_width_dim or 'null'}")
    log(f"  master_height_dim : {req.master_height_dim or 'null'}")

    large_dims = [d for d in req.dimensions if d.value_meters >= 0.05]
    dim_list = "\n".join(
        f"  [{req.dim_axis_labels.get(d.name, '?')}]  {d.name:<52} = {d.value_meters * 1000:>8.2f} mm  ({d.value_meters / 0.0254:>8.3f} in)"
        for d in large_dims
    ) or "  (no dimensions >= 50 mm found)"

    log(f"  dim_list ({len(large_dims)} dims):\n{dim_list}")

    raw = await call_llm(
        rules_system_prompt(
            req.assembly_context,
            dim_list,
            master_width_dim=req.master_width_dim,
            master_height_dim=req.master_height_dim,
        ),
        "Generate resize rules for this assembly.",
        max_tokens=4096,
    )

    try:
        data = json.loads(_strip_fences(raw))
    except json.JSONDecodeError as exc:
        log(f"  ERROR: invalid JSON: {exc}")
        return GenerateRulesResponse(error=f"LLM returned invalid JSON: {exc}")

    if "error" in data:
        log(f"  ERROR (from AI): {data['error']}")
        return GenerateRulesResponse(error=data["error"])

    width_rules = [
        RulePair(
            if_changes=entry.get("if_changes", ""),
            also_change=entry.get("also_change", []),
        )
        for entry in data.get("width_rules", [])
        if entry.get("if_changes")
    ]

    height_rules = [
        RulePair(
            if_changes=entry.get("if_changes", ""),
            also_change=entry.get("also_change", []),
        )
        for entry in data.get("height_rules", [])
        if entry.get("if_changes")
    ]

    skip = [
        SkipEntry(name=entry.get("name", ""), reason=entry.get("reason", ""))
        for entry in data.get("skip", [])
        if entry.get("name")
    ]

    component_labels = data.get("component_labels", {})
    if not isinstance(component_labels, dict):
        component_labels = {}

    log(f"  width_rules={len(width_rules)}  height_rules={len(height_rules)}  skip={len(skip)}  component_labels={len(component_labels)}")
    return GenerateRulesResponse(width_rules=width_rules, height_rules=height_rules, skip=skip, component_labels=component_labels)
