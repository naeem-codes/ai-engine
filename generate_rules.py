import json
from models import GenerateRulesRequest, GenerateRulesResponse, RulePair, SkipEntry, ThicknessRule
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

    # Keep dims >= 50 mm (noise filter for unclassified dims), but ALWAYS keep
    # dims the app already labeled [W]/[H]/[D] — they are axis drivers regardless of
    # size (a 12.7 mm LED-strip width, or a 1.5 mm sheet-metal thickness). Generic.
    def _keep(d):
        return d.value_meters >= 0.05 or req.dim_axis_labels.get(d.name, "?") in ("W", "H", "D")
    large_dims = [d for d in req.dimensions if _keep(d)]
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
        # Large assemblies emit big width/height also_change lists PLUS a depth_rules
        # entry per component — 4096 truncated mid-array. Claude Sonnet allows far more.
        max_tokens=8192,
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

    # Thickness (Z) rules — a FLAT list, one entry per component with a [D]-labeled
    # thickness dim. No dependencies (thickness never cascades). The app lets the user
    # add/remove/correct these in the rules UI, so partial detection is fine.
    depth_rules = [
        ThicknessRule(
            component=entry.get("component", ""),
            dim=entry.get("dim", ""),
            label=entry.get("label", ""),
        )
        for entry in data.get("depth_rules", [])
        if entry.get("dim")
    ]

    component_labels = data.get("component_labels", {})
    if not isinstance(component_labels, dict):
        component_labels = {}

    part_label = data.get("part_label", "")
    if not isinstance(part_label, str):
        part_label = ""

    log(f"  width_rules={len(width_rules)}  height_rules={len(height_rules)}  depth_rules={len(depth_rules)}  skip={len(skip)}  component_labels={len(component_labels)}  part_label={part_label or '(none)'}")
    return GenerateRulesResponse(width_rules=width_rules, height_rules=height_rules, depth=depth_rules, skip=skip, component_labels=component_labels, part_label=part_label)
