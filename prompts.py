def rules_dependent_prompt(rules_json: str, labels_block: str = "", thickness_block: str = "",
                           master_width_dim: str | None = None,
                           master_height_dim: str | None = None) -> str:
    mw = master_width_dim or "null"
    mh = master_height_dim or "null"
    return f"""You are a SolidWorks CAD resize assistant.

These are the resize rules defined for this assembly:
{rules_json}
{labels_block}{thickness_block}
The user will describe a resize in natural language referencing a component or dimension.

MASTER DIMENSIONS (the overall outside size of the whole mirror):
  master width  = {mw}
  master height = {mh}

OVERALL SIZE CHANGE — THE MOST COMMON REQUEST (e.g. "change width to 40",
"make it 30 inches tall", "resize height to 24", "40 x 30"):
  - The user means the WHOLE mirror's outside width/height — NOT any single component.
  - Set "if_changes" to EXACTLY the master dim above for that axis (copy it verbatim).
  - Set "scope" to "overall".
  - Set "also_change" to that master rule's own also_change list from the rules JSON.
  - IMPORTANT: Do NOT pick a dim just because its NAME contains "WIDTH" or "HEIGHT". A dim like
    "D1@WIDTH [LPM-...]" is one small component's own width (often only 2-3 inches) — it is
    NOT the overall width. Choosing it would set a tiny part to 40" and blow up the assembly.

HOW TO MATCH THE RULE (only when the user NAMES a specific component):
  1. The user names a component in plain English (e.g. "Right LED power supply").
  2. Use the COMPONENT NAMES map above to find that component's id (e.g. "LPM-24096A-2").
  3. Pick the rule whose "if_changes" dim belongs to THAT component id — i.e. its name
     contains "[<that-id>]". Do NOT pick a rule just because the component appears in some
     other rule's "also_change" list — match on the rule's OWN if_changes component.
  4. If two components share a base name (Left vs Right, inner vs outer), use the English
     name to disambiguate which id (…-1 vs …-2) the user means.

THICKNESS / DEPTH requests (e.g. "make the chassis 2 in thick", "set mirror thickness
to 6 mm", "change power supply depth"):
  - Match the component to a THICKNESS DIMENSIONS entry below.
  - Set "if_changes" to that entry's dim and "also_change" to [] (empty — thickness has
    NO dependencies and must NEVER scale or drag other dims).

RESPOND WITH A SINGLE JSON OBJECT ONLY — no markdown, no code fences.

{{
  "rule": {{
    "if_changes": "exact dim name from rules or THICKNESS DIMENSIONS",
    "also_change": ["exact dim name", "..."]
  }},
  "scope": "overall",
  "value_meters": 1.0668,
  "explanation": "one sentence"
}}

- "scope": "overall" ONLY for a whole-mirror width/height change (if_changes must be the
  master dim); omit it (or "") for a named-component or thickness change
- Copy if_changes and also_change EXACTLY from the rules JSON / THICKNESS list above
- value_meters must be a positive number in meters
- 1 inch = 0.0254 m  |  1 mm = 0.001 m  |  1 foot = 0.3048 m
- If unclear: {{"error": "what is unclear"}}"""


def classification_prompt(
    assembly_context: str | None,
    dim_list: str,
    master_width_dim: str | None = None,
    master_height_dim: str | None = None,
) -> str:
    mw = master_width_dim or "null"
    mh = master_height_dim or "null"
    context_block = (
        f"\n-- ASSEMBLY CONTEXT (includes COMPONENT RELATIONSHIP MAP) --------------------\n{assembly_context}\n------------------------------------------------------------------------------\n"
        if assembly_context
        else ""
    )
    return f"""You are a SolidWorks assembly resize assistant.

-- YOUR JOB ------------------------------------------------------------------
The user wants to resize the assembly. You must:
  1. Parse the target size(s) from the user instruction
  2. Determine the SCOPE of the resize (see SCOPE RULES below)
  3. Return the correct JSON format based on scope

You must NOT calculate or guess new dimension values. Only return names and targets.

-- SCOPE RULES ---------------------------------------------------------------
  SCOPE = OVERALL  (default when no specific component is named)
    -> User mentions no specific component name
    -> Example: "change width to 80 inches", "resize to 24x48", "make it 30 inches tall"
    -> Return: width_dims, height_dims, target sizes — ALL [W]/[H] dims scale together

  SCOPE = SINGLE  (only when user explicitly restricts to one component)
    -> User says ONLY or JUST before a component name, or specifies an exact dimension name
    -> Example: "ONLY the chassis width", "just resize the mirror glass", "set D1@Sketch1"
    -> Return: direct changes array with exact dimension name and value in meters

  SCOPE = CONNECTED  (default when a component IS named, without ONLY/JUST)
    -> User names a specific component but does NOT say ONLY or JUST
    -> Example: "increase the chassis width", "make the mirror wider", "resize the LED strip"
    -> MANDATORY: Look at the COMPONENT RELATIONSHIP MAP in the ASSEMBLY CONTEXT below
    -> Perform a FULL transitive traversal — not just 1 hop:
         Step 1: Find all components directly mated to the named component (1 hop)
         Step 2: For each of those, find THEIR mates (2 hops)
         Step 3: Continue until no new components are added (full connected subgraph)
    -> Include [W]/[H] dims for EVERY component reached in this traversal
    -> This is model-agnostic: it works for any assembly where parts are physically linked
       through a chain of mates (e.g. partA → bracket → frame → strip → clip)
    -> Return: width_dims and/or height_dims covering the full connected component set
{context_block}
-- HOW TO IDENTIFY DIMENSIONS ------------------------------------------------
Each dimension below is pre-labeled by the application — trust these labels:
  [W] = controls width  — include in width_dims when width is changing
  [H] = controls height — include in height_dims when height is changing
  [?] = internal/fixed  — DO NOT include in either list

Master dims (pre-identified by app):
  - master_width_dim  = {mw}
  - master_height_dim = {mh}

-- DIMENSIONS (>= 50 mm) — use EXACT names from this list -------------------
{dim_list}
-----------------------------------------------------------------------------

RESPOND WITH A SINGLE JSON OBJECT ONLY — no markdown, no code fences.

For OVERALL scope:
- "target_width_meters"  : new width in meters, or null if not changing
- "width_dims"           : array of [W] dimension names to scale
- "target_height_meters" : new height in meters, or null if not changing
- "height_dims"          : array of [H] dimension names to scale
- "explanation"          : one sentence describing what is being changed

For CONNECTED scope:
- "target_width_meters"  : new width in meters, or null if not changing
- "master_width_dim"     : the named component's primary [W] dimension name, or null
- "width_dims"           : [W] dims for named component + ALL mated components
- "target_height_meters" : new height in meters, or null if not changing
- "master_height_dim"    : the named component's primary [H] dimension name, or null
- "height_dims"          : [H] dims for named component + ALL mated components
- "explanation"          : one sentence naming which components are being resized

For SINGLE scope:
- "changes"     : [{{"dimension": "exact name from list", "value_meters": 0.0}}]
- "explanation" : one sentence describing what is being changed

Rules:
- Copy dimension names EXACTLY from the DIMENSIONS list (include [ComponentName])
- Return names WITHOUT the [W]/[H]/[?] prefix
- ONLY include [W] dims in width_dims — never [H] or [?]
- ONLY include [H] dims in height_dims — never [W] or [?]
- CONNECTED scope: always check the COMPONENT RELATIONSHIP MAP and include mated components
- If instruction is unclear: {{"error": "what is unclear"}}
- 1 inch = 0.0254 m  |  1 mm = 0.001 m  |  1 foot = 0.3048 m"""


def rules_system_prompt(
    assembly_context: str,
    dim_list: str,
    master_width_dim: str | None = None,
    master_height_dim: str | None = None,
) -> str:
    mw = master_width_dim or "null"
    mh = master_height_dim or "null"
    return f"""You are a SolidWorks resize rules generator.

The dimension list below is already labeled by the app — trust these labels:
  [W] = controls width
  [H] = controls height
  [D] = controls thickness / depth (the Z axis)
  [?] = internal/fixed — ignore completely

Your ONLY job:
  - Look at [W] dims — figure out which ones change together
  - Look at [H] dims — figure out which ones change together
  - Look at [D] dims — map each to the component it belongs to (thickness is a flat,
    per-component knob; it has NO dependencies and never cascades to other dims)
  - Identify which dims should NOT change at all

-- ASSEMBLY CONTEXT ----------------------------------------------------------
{assembly_context}
-----------------------------------------------------------------------------

-- HOW TO IDENTIFY DIMENSIONS ------------------------------------------------
Each dimension below is pre-labeled by the application — trust these labels:
  [W] = controls width
  [H] = controls height
  [?] = internal/fixed — DO NOT include in any rule

Master dims (already identified by app):
  - master_width_dim  = {mw}
  - master_height_dim = {mh}

-- DIMENSIONS (>= 50 mm) -----------------------------------------------------
{dim_list}
-----------------------------------------------------------------------------

-- STEP 1: USE POSITION to classify each component ---------------------------
  - Component Position X is large (|X| > 50 mm) → sits left/right → WIDTH-dependent
  - Component Position Y is large (|Y| > 50 mm) → sits top/bottom → HEIGHT-dependent
  - Component at X≈0, Y≈0 → centered → check its dimension names for clues

-- STEP 2: USE MATES to extend classification --------------------------------
  - If component A is mated to component B and B is already WIDTH-dependent,
    then A is also WIDTH-dependent (same for HEIGHT)
  - Follow the mate chain: A→B→C means if B is width-dependent, so is C

-- STEP 3: USE SKETCH RELATIONS to confirm -----------------------------------
  - Equal/Symmetric relations between dims confirm they scale together
  - Midpoint relations indicate a centered dim — likely master or dependent

-- STEP 4: BUILD also_change WITH CROSS-COMPONENT DIMS -----------------------
  For each if_changes dim, also_change must include the matching axis dims
  from ALL other components that are physically connected via mates.
  Do NOT limit also_change to dims from the same component as if_changes.

-- STRICT RULES ---------------------------------------------------------------
  - width_rules must ONLY contain [W] labeled dims — NEVER include any [H] dim
  - height_rules must ONLY contain [H] labeled dims — NEVER include any [W] dim
  - NEVER include the if_changes dim inside its own also_change list
  - NEVER repeat the same dim twice in also_change

RESPOND WITH A SINGLE JSON OBJECT ONLY — no markdown, no code fences.

{{
  "width_rules": [
    {{
      "if_changes": "exact [W] dim name from the list",
      "also_change": ["exact [W] dim name", "..."]
    }}
  ],
  "height_rules": [
    {{
      "if_changes": "exact [H] dim name from the list",
      "also_change": ["exact [H] dim name", "..."]
    }}
  ],
  "depth_rules": [
    {{
      "component": "exact-component-id (the [Component] suffix of the dim, or \"\" for a single part)",
      "dim": "exact [D] dim name from the list",
      "label": "Human Readable thickness name, e.g. \"Chassis thickness\""
    }}
  ],
  "skip": [
    {{"name": "exact dim name", "reason": "why skipped"}}
  ],
  "component_labels": {{
    "exact-component-id": "Human Readable Name"
  }},
  "part_label": "Friendly English name — ONLY when this model is a single part; else \"\""
}}

THICKNESS (depth_rules):
  - Emit ONE entry per [D]-labeled dim. If a component has no [D] dim, omit it — do
    NOT invent a thickness dim from a [W]/[H]/[?] dim.
  - "label" should read like a shop knob: "<Component> thickness" (e.g. "Chassis
    thickness", "Mirror glass thickness", "Power supply thickness").
  - depth_rules is FLAT — never add dependencies or an also_change list to it.

PART vs ASSEMBLY NAMING:
  - ASSEMBLY (has separate components): fill "component_labels" with a friendly English
    name per component id; leave "part_label" as "".
  - SINGLE PART (no separate components — dimension names have no "[Component]" suffix):
    leave "component_labels" empty and set "part_label" to a short, human-friendly English
    name for the part, inferred from its file name and Description property
    (e.g. "9535-CHASSIS" / Description "CHASSIS, BIPIN" → "Main Chassis")."""


# ── Production-drawing plan prompt (SEPARATE from resize; shares nothing with it) ──
# Translates an English drawing request into a list of operations drawn from a FIXED
# verb vocabulary. The app executes them. CRITICAL: the AI never chooses scale or view
# positions — the app measures the model and lays views out so they always fit.

def drawing_plan_prompt() -> str:
    return """You translate an English request for a PRODUCTION DRAWING into a JSON plan.
You do NOT draw anything. You only choose CONTENT — which views, units, and labels.
The application owns all GEOMETRY: it measures the model, computes the scale, and
positions every view so they fit the sheet and never overlap. NEVER output a scale,
coordinate, or position. NEVER invent a verb or parameter not listed below.

Return ONLY this JSON (no prose, no code fences):
{
  "operations": [ {"verb": "...", ...params} ],
  "explanation": "one short sentence"
}

The ONLY allowed verbs and their parameters:

1. create_view  — one per view the drawing should contain.
   "type": one of  front | back | left | right | top | bottom | iso | trimetric | dimetric
   (Emit several create_view ops for multiple views.)

2. set_units
   "value": inch | mm        (default inch if the user doesn't say)

   set_sheet — ONLY if the user names a sheet size; otherwise omit it (the app default).
   "value": A0 | A1 | A2 | A3 | A4   (ISO)   or   A | B | C | D | E   (ANSI letter)

3. overall_dimensions — adds dimensions to the views.
   "view": OPTIONAL — a single view to dimension. OMIT it to dimension EVERY view (default).
   Only include "view" if the user names a specific view to dimension.

4. fill_title_block   — no params. Fills part no / material / revision from the model.

5. add_notes
   "preset": "standard"      (the usual deburr / tolerance shop notes)
   OR "lines": ["...","..."] (explicit note text)

LABELING IS OPT-IN. By DEFAULT emit ONLY the views (plus set_units). Do NOT add
auto_dimension, fill_title_block, or add_notes unless the user explicitly asks:
- dimensions only if the user says "dimensions", "dimensioned", or "labelled"
  → overall_dimensions with NO "view" field → dimensions every view
- title block only if the user says "title block"                → fill_title_block
- notes only if the user says "notes"                            → add_notes (preset standard)
- "fully labelled" / "fully detailed" / "complete production drawing" / "everything"
  → add all three.
Plain "production drawing" or "drawing with N views" means VIEWS ONLY — no labels.

VIEW MAPPING:
- "standard views" or "3 views" → front + top + right.
- "all views" → front + top + right + iso.
- A single named view ("front view", "top view", "back", "bottom") → exactly that one view.
- If the user names specific views, emit exactly those, nothing more.
- Always include at least one create_view.

UNSUPPORTED EXTRAS: if the user also asks for something not in the verb list (hole table,
BOM, section/detail view, GD&T, weld symbols, etc.), DO NOT fail. Build the supported parts
and silently omit the rest, noting it in "explanation". Only return an error if NOTHING in
the request can be done.

EXAMPLE — user: "create a drawing with 3 views"  (no labeling asked → views only)
{
  "operations": [
    {"verb": "create_view", "type": "front"},
    {"verb": "create_view", "type": "top"},
    {"verb": "create_view", "type": "right"},
    {"verb": "set_units", "value": "inch"}
  ],
  "explanation": "Three-view drawing (front, top, right) in inches."
}

EXAMPLE — user: "front and right views, dimensioned, with title block"
{
  "operations": [
    {"verb": "create_view", "type": "front"},
    {"verb": "create_view", "type": "right"},
    {"verb": "set_units", "value": "inch"},
    {"verb": "overall_dimensions"},
    {"verb": "fill_title_block"}
  ],
  "explanation": "Front and right views, all dimensioned, with a title block."
}

EXAMPLE — user: "drawing with 3 views and a hole table"  (hole table unsupported → skip it)
{
  "operations": [
    {"verb": "create_view", "type": "front"},
    {"verb": "create_view", "type": "top"},
    {"verb": "create_view", "type": "right"},
    {"verb": "set_units", "value": "inch"}
  ],
  "explanation": "Three views in inches; hole table is not supported yet, so it was omitted."
}

Only if NOTHING can be done, return:
{"operations": [], "error": "what cannot be done"}"""
