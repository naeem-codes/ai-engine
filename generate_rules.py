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
        # entry per component. 8192 still truncated mid-string on big assemblies
        # (unterminated-JSON errors). claude-sonnet-4-6 allows up to 128K output;
        # 16000 stays within the non-streaming HTTP timeout while covering assemblies
        # several times larger. Raise further (and stream in llm.py) if this recurs.
        max_tokens=16000,
    )

    cleaned = _strip_fences(raw)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        # An unterminated string / missing closing brace almost always means the
        # model hit max_tokens and the JSON was cut off mid-output — not malformed
        # output. Surface that plainly instead of the raw parser error.
        looks_truncated = "Unterminated" in str(exc) or not cleaned.rstrip().endswith("}")
        if looks_truncated:
            msg = ("AI response was truncated (assembly too large for the current "
                   "token limit). Increase max_tokens in generate_rules.py.")
            log(f"  ERROR: response truncated at {len(cleaned)} chars: {exc}")
            return GenerateRulesResponse(error=msg)
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

    # ── Deterministic axis enforcement ─────────────────────────────────────────
    # The app's [W]/[H] labels are authoritative for WHICH axis a dim scales on;
    # the LLM only decides grouping, and its position heuristic misfires here: a
    # VERTICAL LED strip sits at large X (left/right edge), so "large X → width-
    # dependent" drags the strip's [H] length dim into width_rules — then the strip
    # grows on WIDTH prompts and ignores HEIGHT. It also drops on-axis dims (only
    # one chassis [H] dim captured, so the frame under-grows). Rebuild W/H membership
    # straight from the labels: every same-axis dim is a master that cascades to all
    # other same-axis dims (uniform proportional scale). No dim can land on the wrong
    # axis, and none is missed. depth/skip/labels stay as the LLM produced them; the
    # rules UI still lets the user trim deps before saving.
    labels = req.dim_axis_labels or {}
    w_dims = [d.name for d in req.dimensions if labels.get(d.name) == "W"]
    h_dims = [d.name for d in req.dimensions if labels.get(d.name) == "H"]

    def _rebuild_axis(axis_dims):
        return [
            RulePair(if_changes=m, also_change=[d for d in axis_dims if d != m])
            for m in axis_dims
        ]

    if w_dims:
        width_rules = _rebuild_axis(w_dims)
    if h_dims:
        height_rules = _rebuild_axis(h_dims)
    log(f"  axis-enforced: {len(w_dims)} [W] dims, {len(h_dims)} [H] dims")

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
