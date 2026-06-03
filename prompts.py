def rules_dependent_prompt(rules_json: str) -> str:
    return f"""You are a SolidWorks CAD resize assistant.

These are the resize rules defined for this assembly:
{rules_json}

The user will describe a resize in natural language referencing a component or dimension.
Find the ONE rule from the list above that best matches what the user wants to change, and extract the target size.

RESPOND WITH A SINGLE JSON OBJECT ONLY — no markdown, no code fences.

{{
  "rule": {{
    "if_changes": "exact dim name from rules",
    "also_change": ["exact dim name", "..."]
  }},
  "value_meters": 1.0668,
  "explanation": "one sentence"
}}

- Copy if_changes and also_change EXACTLY from the rules JSON above
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
  [?] = internal/fixed — ignore completely

Your ONLY job:
  - Look at [W] dims — figure out which ones change together
  - Look at [H] dims — figure out which ones change together
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
  "skip": [
    {{"name": "exact dim name", "reason": "why skipped"}}
  ],
  "component_labels": {{
    "exact-component-id": "Human Readable Name"
  }}
}}"""
