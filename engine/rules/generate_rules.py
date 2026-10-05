import json
from engine.hangers import hanger_select
from engine.resize import resize_policy as policy
from engine.core.models import (GenerateRulesRequest, GenerateRulesResponse, OffsetRule, RulePair,
                    SkipEntry)
from engine.llm.llm import call_llm
from engine.llm.prompts import rules_system_prompt
from engine.core.log import log, section


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

    Returns the ROW link first, then one link per dimensioned member of the tab stack (see
    `_tab_stack_links`). The order matters: `interpret._clip_tab_alignment` reads the first
    follower as the tab spacing. Ambiguity records a `skip` entry and writes nothing, so the
    rules UI shows why the link is absent instead of looking like the generator forgot it.
    """
    hanger_dim, hanger_w = _hanger_width_dim(req)
    if hanger_w <= 0:
        return []

    dim_values = {d.name: d.value_meters for d in req.dimensions}
    cands = hanger_select.find_tab_spacing_candidates(
        dim_values, req.dim_axis_labels, hanger_w)

    if not cands:
        # The inset window only answers while the FITTED hanger is a prefab. AMY's is custom,
        # with its slots 4.750in in from each edge, so its tab row is 9.500in inside the hanger
        # and invisible to that window. Fall back to the slot-length structure, which does not
        # care what the fitted hanger's inset happens to be.
        log(f"  [TAB] no chassis dim sits {hanger_select.EXPECTED_TAB_INSET_IN:.2f}"
            f"+/-{hanger_select.GEN_TAB_INSET_TOL_IN:.2f}in inside the "
            f"{hanger_w / 0.0254:.3f}in hanger -- looking for the tab row by the "
            f"{hanger_select.HANGER_SLOT_LENGTH_IN:.3f}in slot it is cut to instead")
        cands = hanger_select.find_tab_spacing_by_slot_row(dim_values, hanger_w)

    if not cands:
        log("  [TAB] no tab row found either way -- this product has no tab spacing to link "
            "(a hanging-BRACKET product is expected to land here)")
        return []

    if len(cands) > 1:
        detail = ", ".join(f"{c.dim} ({c.inset_in:.3f}in)" for c in cands)
        what = (f"chassis sketches carry the {hanger_select.HANGER_SLOT_LENGTH_IN:.3f}in slot "
                f"length with a span in them" if cands[0].by_slot_row else
                f"dims sit {hanger_select.EXPECTED_TAB_INSET_IN:.2f}in inside the hanger")
        log(f"  [TAB] AMBIGUOUS -- {len(cands)} {what} and nothing separates them: {detail}. "
            f"No link stored; the resize-time follower keeps its own name-gated search for "
            f"this product.")
        skip.append(SkipEntry(
            name=cands[0].dim,
            reason=(f"hanging-tab link NOT stored: {len(cands)} {what} and are "
                    f"indistinguishable ({detail}). Confirm which is the tab spacing in "
                    f"SolidWorks.")))
        return []

    best = cands[0]
    # Snap a near-canonical inset so modelling noise is not baked into the product forever, but
    # keep a genuinely different one: the link records what THIS product's hanger actually has.
    #
    # It must not be overridden with the canonical 4.25in here, however tempting. The inset is a
    # property of the FITTED HANGER, and the link is read on every resize -- including the ones
    # that KEEP that hanger. Overriding it was tried on 2026-09-21 and broke a correct model:
    # AMY 24x48 -> 34x58 keeps its bespoke 23.000in `12214-HANGER`, whose slots give a 13.500in
    # tab row, and the stored 4.250in moved the row to 18.750in -- lifting the tabs out of slots
    # that had not moved at all. Whether the canonical inset applies is a RESIZE-time question,
    # because only the resize knows if a prefab is being fitted; `interpret` decides it.
    drift = abs(best.inset_in - hanger_select.EXPECTED_TAB_INSET_IN)
    if drift <= hanger_select.GEN_TAB_SNAP_IN:
        inset_in = hanger_select.EXPECTED_TAB_INSET_IN
    else:
        inset_in = best.inset_in
        log(f"  [TAB] inset {inset_in:.3f}in differs from the canonical "
            f"{hanger_select.EXPECTED_TAB_INSET_IN:.2f}in by {drift:.3f}in -- storing the "
            f"MEASURED value, since this link is derived per product. A resize that SWAPS IN a "
            f"prefab will use the prefab's canonical inset instead; one that keeps or stretches "
            f"this hanger keeps this value")

    # offset is target - source, so the follower writes `new_hanger_width + offset`. Negative
    # because the tabs sit INSIDE the hanger.
    offset_m = -inset_in * 0.0254
    log(f"  [TAB] linked {best.dim} -> {hanger_dim}: tabs {best.value_meters / 0.0254:.3f}in sit "
        f"{inset_in:.3f}in inside the {hanger_w / 0.0254:.3f}in hanger"
        + ("" if best.named else " (identified by relationship -- no name hint matched)"))
    rules = [OffsetRule(
        component=policy.component_of(best.dim),
        target_dim=best.dim,
        source_dim=hanger_dim,
        offset_meters=offset_m,
        note=(f"hanging-tab spacing seats in the hanger's slots, {inset_in:.3f}in inside its "
              f"width. Derived from the aligned model ({best.value_meters / 0.0254:.3f}in tabs "
              f"in a {hanger_w / 0.0254:.3f}in hanger) so no resize has to re-guess it."),
    )]

    # Only on the slot-row path. The inset-window products (AMBER, KELLY, PIAZZA, BREAM) are
    # verified live with the row alone, and their tabs are cut in the row's own sketch; searching
    # them for extra members would add writes nobody has checked.
    if best.by_slot_row:
        _check_row_against_hanger_slots(dim_values, best, inset_in, skip)
        rules += _tab_stack_links(dim_values, best, hanger_dim, hanger_w)
    return rules


def _tab_stack_links(dim_values: dict[str, float], row, hanger_dim: str,
                     hanger_w: float) -> list[OffsetRule]:
    """One link per chassis feature that sits in the row's slots AND carries its own span.

    Live 2026-09-21 and again 2026-09-25, AMY: the row link moved the notches (`Cut-Extrude7`,
    `D2@Sketch53`) onto the hanger's slots, but on the 24x48 chassis the tabs themselves
    (`Boss-Extrude2`, `D1@Sketch35` = 10.125in) have their own spacing dimension, so they stayed
    where the old hanger had them - "the chassis tab on 74 x 98 is not inside the hanger slot".
    The stack is one rigid arrangement: every member keeps its offset from the hanger width, so
    it moves by exactly as much as the slots do.
    """
    scan = hanger_select.find_tab_stack_members(dim_values, row, hanger_w)
    for sketch in scan.relation_placed:
        log(f"  [TAB] {sketch} carries a tab width but no span of its own -- it is placed by "
            f"sketch relations, so it is not linked (on AMY 36x36 those relations tie it to the "
            f"row, and it follows the row for free)")
    rules: list[OffsetRule] = []
    for m in scan.members:
        member_inset = m.inset_in
        log(f"  [TAB] linked {m.dim} -> {hanger_dim}: a tab positioned by its own spacing, "
            f"{m.value_meters / 0.0254:.3f}in, sits in the same slots as {row.dim} and moves "
            f"with them ({member_inset:.3f}in inside the {hanger_w / 0.0254:.3f}in hanger)")
        rules.append(OffsetRule(
            component=policy.component_of(m.dim),
            target_dim=m.dim,
            source_dim=hanger_dim,
            offset_meters=-member_inset * 0.0254,
            note=(f"hanging tab seated in the same slots as {row.dim}; its own spacing sits "
                  f"{member_inset:.3f}in inside the hanger width. Derived from the aligned model "
                  f"({m.value_meters / 0.0254:.3f}in in a {hanger_w / 0.0254:.3f}in hanger)."),
        ))
    return rules


def _check_row_against_hanger_slots(dim_values: dict[str, float], row, inset_in: float,
                                    skip: list[SkipEntry]) -> None:
    """Warn when the tab row does not sit where the HANGER'S slot sketch says it should.

    The stored inset is measured, so it is only right on a model whose tabs are in their slots.
    The generator trusts that because it normally runs on the product as the client authored it
    - but Generate Rules can be pressed after a resize too, and on AMY 24x48 -> 44x68 that model
    has its row 15.000in inside a 28.500in hanger whose slots say 9.500in. Storing that would make
    every later resize faithfully PRESERVE the misalignment.

    A warning, never a gate: the prefabs carry no slot position dim at all, and nothing here can
    say which of the two numbers is right - only that they disagree.
    """
    expected = hanger_select.hanger_slot_row_insets(dim_values)
    if not expected:
        return
    if hanger_select.slot_datum(inset_in, expected) is not None:
        log(f"  [TAB] row inset {inset_in:.3f}in matches the hanger's own slot sketch -- the "
            f"model is aligned, so the stored link is right")
        return
    detail = " or ".join(f"{e:.3f}in" for e in sorted(set(round(e, 3) for e in expected)))
    log(f"  [TAB] WARNING -- {row.dim} sits {inset_in:.3f}in inside the hanger, but the hanger's "
        f"slot sketch puts its slots {detail} inside. This model's tabs are probably NOT in "
        f"their slots right now; generate rules from a fresh Connect (an unmodified copy)")
    skip.append(SkipEntry(
        name=row.dim,
        reason=(f"WARNING: the tab row sits {inset_in:.3f}in inside the hanger but the hanger's "
                f"slots are {detail} inside - the tabs look misaligned in this model. The "
                f"stored link keeps whatever offset the model has now; reconnect (fresh copy) "
                f"and generate rules again before resizing.")))


async def generate_rules(req: GenerateRulesRequest) -> GenerateRulesResponse:
    section("GENERATE RULES REQUEST")
    log(f"  dimensions        : {len(req.dimensions)} total")
    log(f"  master_width_dim  : {req.master_width_dim or 'null'}")
    log(f"  master_height_dim : {req.master_height_dim or 'null'}")
    if req.is_round:
        log(f"  shape             : ROUND — one size axis (the diameter), "
            f"{len(req.radial_dims)} radial dim(s)")

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
            is_round=req.is_round,
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

    # `skip` is NOT asked of the LLM any more, and is built from the model instead.
    #
    # It used to be the single most expensive thing in this call. Measured 2026-09-11 on MOMO:
    # the response was 9,413 chars and `skip` was 8,401 of them - 89% of the output, and output
    # is generated one token at a time, so it was ~40 s of a 45 s call. For 79 entries carrying
    # 14 distinct reasons, repeated over and over.
    #
    # Nothing consumed it. It is not saved (no `skip` key in any .rules.json), /get-rules does
    # not return it, no prompt receives it back, and no resize logic reads it - it is drawn once
    # as grey rows in the Generate Rules dialog, logged, and dropped. And it never protected the
    # fixed-size hardware either: LPM / clips / brackets / hanger are excluded below by
    # policy.filter_axis_dims, in Python, whatever the LLM says.
    #
    # So the same rows are now assembled from what is already known for free (see below).
    skip: list[SkipEntry] = []

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

    # ROUND: one size axis, and it is the diameter on the [W] side. Every radial dim of every
    # part was pinned to that axis by the app's labeller, so the [H] dims that remain belong to
    # the hardware — on ECLIPSE they are the hanger's own height and the hanging brackets'. A
    # height rule built from those would name the hanger as a resize target under a master the
    # app deliberately sends as null, i.e. a rule that can only ever be wrong.
    if req.is_round:
        if h_dims:
            log(f"  [ROUND] dropping {len(h_dims)} [H] dim(s) — a round mirror has one size "
                f"axis (the diameter); these are hardware dims, not a height: "
                f"{', '.join(h_dims)}")
            for name in h_dims:
                if name not in {s.name for s in skip}:
                    skip.append(SkipEntry(
                        name=name,
                        reason="round mirror: there is no height axis — the diameter is the "
                               "only size, and this dim belongs to hardware the mates "
                               "reposition"))
        h_dims, h_master = [], None

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
    # `_scrub` is the no-labels fallback and would hand a round model the LLM's own invented
    # height rules straight back, undoing the drop above.
    height_rules = ([] if req.is_round
                    else _rebuild_axis(h_dims, h_master) if h_dims
                    else _scrub(height_rules, "height"))
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

    # The second half of the skip list: a dim the app labelled [W] or [H] that policy did not
    # block yet still did not make it into a rule. Those are the ones actually worth an eye in
    # the UI - a real axis dim that no rule will drive. Everything else that is absent is
    # unlabelled internal detail, which is the normal case and not interesting one row at a
    # time, so it is summarised on a single line instead of listing hundreds.
    in_rules = set()
    for r in width_rules + height_rules:
        in_rules.add(r.if_changes)
        in_rules.update(r.also_change)
    already_skipped = {s.name for s in skip}
    for name in dict.fromkeys(w_labeled + h_labeled):
        if name in in_rules or name in already_skipped:
            continue
        already_skipped.add(name)
        skip.append(SkipEntry(
            name=name,
            reason=f"labelled [{labels.get(name, '?')}] but not picked up by any rule"))

    unlisted = [d.name for d in req.dimensions
                if d.name not in in_rules and d.name not in already_skipped]
    if unlisted:
        skip.append(SkipEntry(
            name=f"+ {len(unlisted)} more dimension(s)",
            reason="internal detail - unlabelled and not on a resize axis"))

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
