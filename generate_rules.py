import json
import hanger_select
import resize_policy as policy
from models import (GenerateRulesRequest, GenerateRulesResponse, OffsetRule, RulePair,
                    SkipEntry)
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


def _hanger_width_dim(req: GenerateRulesRequest) -> tuple[str, float]:
    """The hanger's outer WIDTH driver and its value, or ("", 0.0).

    Largest [W] dim on a hanger, excluding `@Sheet-Metal` dims -- those are flat-pattern / bend
    metadata, not an outer size (on AMY the largest [H] hanger dim was a Sheet-Metal one at
    446.70 mm, against a real 431.80 mm driver). Same rule `interpret._hanger_changes` uses.
    """
    labels = req.dim_axis_labels or {}
    cands = [(d.value_meters, d.name) for d in req.dimensions
             if labels.get(d.name) == "W"
             and policy.is_hanger(d.name, None, None)
             and "@SHEET-METAL" not in d.name.upper()]
    if not cands:
        return "", 0.0
    value, name = max(cands)
    return name, value


def _tab_spacing_offset(req: GenerateRulesRequest, skip: list[SkipEntry]) -> list[OffsetRule]:
    """Derive the hanging-tab -> hanger-width link, ONCE, from the aligned model.

    Why here rather than at resize time, where the follower has always done it: the search
    "which chassis dim sits 4.25in inside the hanger?" only has a right answer while the model is
    still correct. Rule generation runs on the product as the client authored it, so the
    relationship is intact by definition. Resize N runs on a model that may already be drifted,
    where the question is not merely unanswerable but answerable WRONGLY.

    Not hypothetical. Live 2026-09-02, BREAM: a hanger swap left a stale component id in the
    plan, the applier dropped both hanger width writes while the chassis tab write landed, and
    the model was saved with its tabs set for a 15.5in hanger it never received. The inset was
    then 0.750in, outside the +/-1.0in identification window, so the follower stopped recognising
    the dim ENTIRELY -- a ratchet, because nothing remembered what the dim was. Three resizes
    later the hanger was 78.000in and the tabs were still 11.250in apart.

    A stored link cannot ratchet: the dim is named, not searched for. Drift can no longer hide
    it, and becomes something a resize CORRECTS instead of something that silently switches the
    follower off.

    Returns at most one rule. Ambiguity records a `skip` entry and writes nothing, so the rules
    UI shows why the link is absent instead of looking like the generator forgot it.
    """
    hanger_dim, hanger_w = _hanger_width_dim(req)
    if hanger_w <= 0:
        return []

    dim_values = {d.name: d.value_meters for d in req.dimensions}
    cands = hanger_select.find_tab_spacing_candidates(
        dim_values, req.dim_axis_labels, hanger_w)

    if not cands:
        log(f"  [TAB] no chassis dim sits {hanger_select.EXPECTED_TAB_INSET_IN:.2f}"
            f"+/-{hanger_select.GEN_TAB_INSET_TOL_IN:.2f}in inside the "
            f"{hanger_w / 0.0254:.3f}in hanger -- this product has no tab spacing to link "
            f"(a hanging-BRACKET product is expected to land here)")
        return []

    if len(cands) > 1:
        detail = ", ".join(f"{c.dim} ({c.inset_in:.3f}in)" for c in cands)
        log(f"  [TAB] AMBIGUOUS -- {len(cands)} dims sit the right distance inside the hanger "
            f"and nothing separates them: {detail}. No link stored; the resize-time follower "
            f"keeps its own name-gated search for this product.")
        skip.append(SkipEntry(
            name=cands[0].dim,
            reason=(f"hanging-tab link NOT stored: {len(cands)} dims sit "
                    f"{hanger_select.EXPECTED_TAB_INSET_IN:.2f}in inside the hanger and are "
                    f"indistinguishable ({detail}). Confirm which is the tab spacing in "
                    f"SolidWorks.")))
        return []

    best = cands[0]
    # Snap a near-canonical inset so modelling noise is not baked into the product forever, but
    # keep a genuinely different one: the link is per-product, so it is allowed to differ.
    drift = abs(best.inset_in - hanger_select.EXPECTED_TAB_INSET_IN)
    if drift <= hanger_select.GEN_TAB_SNAP_IN:
        inset_in = hanger_select.EXPECTED_TAB_INSET_IN
    else:
        inset_in = best.inset_in
        log(f"  [TAB] inset {inset_in:.3f}in differs from the canonical "
            f"{hanger_select.EXPECTED_TAB_INSET_IN:.2f}in by {drift:.3f}in -- storing the "
            f"MEASURED value, since this link is derived per product")

    # offset is target - source, so the follower writes `new_hanger_width + offset`. Negative
    # because the tabs sit INSIDE the hanger.
    offset_m = -inset_in * 0.0254
    log(f"  [TAB] linked {best.dim} -> {hanger_dim}: tabs {best.value_meters / 0.0254:.3f}in sit "
        f"{inset_in:.3f}in inside the {hanger_w / 0.0254:.3f}in hanger"
        + ("" if best.named else " (identified by relationship -- no name hint matched)"))
    return [OffsetRule(
        component=policy.component_of(best.dim),
        target_dim=best.dim,
        source_dim=hanger_dim,
        offset_meters=offset_m,
        note=(f"hanging-tab spacing seats in the hanger's slots, {inset_in:.3f}in inside its "
              f"width. Derived from the aligned model ({best.value_meters / 0.0254:.3f}in tabs "
              f"in a {hanger_w / 0.0254:.3f}in hanger) so no resize has to re-guess it."),
    )]


async def generate_rules(req: GenerateRulesRequest) -> GenerateRulesResponse:
    section("GENERATE RULES REQUEST")
    log(f"  dimensions        : {len(req.dimensions)} total")
    log(f"  master_width_dim  : {req.master_width_dim or 'null'}")
    log(f"  master_height_dim : {req.master_height_dim or 'null'}")

    # Keep dims >= 50 mm (noise filter for unclassified dims), but ALWAYS keep
    # dims the app already labeled [W]/[H] — they are axis drivers regardless of size
    # (e.g. a 12.7 mm LED-strip width). [D] thickness dims are NOT kept: rules only
    # cover width/height, so a thin gauge dim is noise here. Generic.
    def _keep(d):
        return d.value_meters >= 0.05 or req.dim_axis_labels.get(d.name, "?") in ("W", "H")
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
        # Large assemblies emit big width/height also_change lists. 8192 still truncated
        # mid-string on big assemblies (unterminated-JSON errors). claude-sonnet-4-6
        # allows up to 128K output; 16000 stays within the non-streaming HTTP timeout
        # while covering assemblies several times larger. Raise further (and stream in
        # llm.py) if this recurs.
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

    skip = [
        SkipEntry(name=entry.get("name", ""), reason=entry.get("reason", ""))
        for entry in data.get("skip", [])
        if entry.get("name")
    ]

    component_labels = data.get("component_labels", {})
    if not isinstance(component_labels, dict):
        component_labels = {}

    # ── Deterministic axis enforcement + resize policy ─────────────────────────
    # The app's [W]/[H] labels are authoritative for WHICH axis a dim scales on;
    # the LLM only decides grouping, and its position heuristic misfires here: a
    # VERTICAL LED strip sits at large X (left/right edge), so "large X → width-
    # dependent" drags the strip's [H] length dim into width_rules — then the strip
    # grows on WIDTH prompts and ignores HEIGHT. It also drops on-axis dims (only
    # one chassis [H] dim captured, so the frame under-grows). So W/H membership is
    # rebuilt straight from the labels rather than trusted from the LLM.
    #
    # Membership is then narrowed by resize_policy (see that module's header):
    #   • fixed-size hardware (power supply / clips / brackets) is dropped from BOTH
    #     axes and recorded in `skip` — mates reposition it, it never resizes;
    #   • LED strips are dropped from the WIDTH axis only (their width dim is the
    #     extrusion cross-section) and keep their height/length dim;
    #   • the surviving dims become ONE rule per axis whose master is the MIRROR GLASS
    #     dim — the only dependent rules the product needs. Every other dim is a
    #     dependent under it instead of also being its own master, which is what the
    #     old every-dim-is-a-master rebuild produced (N pairs for N dims).
    # The rules UI still lets the user trim deps before saving.
    labels = req.dim_axis_labels or {}
    w_labeled = [d.name for d in req.dimensions if labels.get(d.name) == "W"]
    h_labeled = [d.name for d in req.dimensions if labels.get(d.name) == "H"]

    # Values are needed to tell an LED strip's fixed profile from its length (an axis test
    # can't: a horizontally-mounted strip has its LENGTH labeled [W]).
    dim_values = {d.name: d.value_meters for d in req.dimensions}
    w_dims, w_blocked = policy.filter_axis_dims(w_labeled, "width", component_labels, dim_values)
    h_dims, h_blocked = policy.filter_axis_dims(h_labeled, "height", component_labels, dim_values)

    def _rebuild_axis(axis_dims, master):
        # One master (the mirror glass) cascading to every other same-axis dim. With no
        # identifiable master, fall back to every-dim-is-a-master so an unusual model
        # still gets usable rules rather than none.
        if master:
            return [RulePair(if_changes=master,
                             also_change=[d for d in axis_dims if d != master])]
        return [
            RulePair(if_changes=m, also_change=[d for d in axis_dims if d != m])
            for m in axis_dims
        ]

    w_master, w_note = policy.pick_master(w_dims, req.master_width_dim, component_labels)
    h_master, h_note = policy.pick_master(h_dims, req.master_height_dim, component_labels)
    log(f"  width  master : {w_master or '(none — every dim its own master)'}  [{w_note}]")
    log(f"  height master : {h_master or '(none — every dim its own master)'}  [{h_note}]")

    def _scrub(rules, axis):
        # Applied when there are no axis labels to rebuild from, so the LLM's own rules
        # are used as-is: strip blocked dims out of them rather than trusting the prompt.
        out = []
        for r in rules:
            if policy.block_reason(r.if_changes, axis, component_labels, dim_values.get(r.if_changes)):
                log(f"    BLOCK master {r.if_changes} — dropped whole rule")
                continue
            deps = [d for d in r.also_change
                    if not policy.block_reason(d, axis, component_labels, dim_values.get(d))]
            out.append(RulePair(if_changes=r.if_changes, also_change=deps))
        return out

    width_rules = _rebuild_axis(w_dims, w_master) if w_dims else _scrub(width_rules, "width")
    height_rules = _rebuild_axis(h_dims, h_master) if h_dims else _scrub(height_rules, "height")
    log(f"  axis-enforced: {len(w_dims)}/{len(w_labeled)} [W] dims, "
        f"{len(h_dims)}/{len(h_labeled)} [H] dims kept after policy")

    # Record every policy-blocked dim in `skip` so the rules UI shows WHY it is absent
    # instead of looking like the generator forgot it. A dim blocked on both axes is
    # listed once; an LLM skip entry for the same dim wins (it may be more specific).
    already_skipped = {s.name for s in skip}
    for name, reason in w_blocked + h_blocked:
        if name in already_skipped:
            continue
        already_skipped.add(name)
        skip.append(SkipEntry(name=name, reason=reason))
    if w_blocked or h_blocked:
        log(f"  policy-blocked {len(w_blocked)} [W] + {len(h_blocked)} [H] dim(s):")
        for name, reason in w_blocked + h_blocked:
            log(f"    BLOCK {name}  — {reason}")

    # Advisory: a component whose LABEL reads like fixed-size hardware while its part number
    # does not. Never blocks (labels are LLM prose and must not freeze a structural part —
    # that bug froze 12186-MOUNTING-PLATE), but worth surfacing in case a real power supply
    # carries a part number we have not listed.
    for name in dict.fromkeys(w_labeled + h_labeled):
        hint = policy.label_suggests_fixed_size(name, component_labels)
        if hint:
            log(f"    NOTE {name} — {hint}; kept resizable (part number wins)")

    part_label = data.get("part_label", "")
    if not isinstance(part_label, str):
        part_label = ""

    # Derived, not authored: the hanging-tab link is a measured fact about this product, so it
    # is worked out here rather than asked of the LLM or of the user. Wrapped, and done last, so
    # a fault in a brand-new code path can never cost the caller its rules.
    try:
        offset_rules = _tab_spacing_offset(req, skip)
    except Exception as exc:                              # noqa: BLE001 - never fatal
        log(f"  [TAB] link derivation failed ({exc}) -- rules are unaffected and the "
            f"resize-time follower keeps its own search")
        offset_rules = []

    log(f"  width_rules={len(width_rules)}  height_rules={len(height_rules)}  skip={len(skip)}  component_labels={len(component_labels)}  offset={len(offset_rules)}  part_label={part_label or '(none)'}")
    return GenerateRulesResponse(width_rules=width_rules, height_rules=height_rules, skip=skip, component_labels=component_labels, part_label=part_label, offset=offset_rules)
