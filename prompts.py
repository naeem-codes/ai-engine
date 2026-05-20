def rules_prompt(triggers: list[str]) -> str:
    t = ", ".join(triggers)
    return f"""You are a SolidWorks CAD dimension assistant.
The engineer will describe a resize operation on the open CAD model.

This model has smart rules defined for these triggers: {t}

RESPOND WITH A SINGLE JSON OBJECT ONLY — no markdown, no code fences, no explanation outside the JSON.

If the user mentions a trigger keyword ({t}):
{{"trigger": "width", "value_meters": 0.762, "explanation": "Setting width to 30 inches"}}

If the user specifies an exact dimension name (contains @ symbol):
{{"changes": [{{"dimension": "ExactName@Feature [Component]", "value_meters": 0.5}}], "explanation": "Direct dimension change"}}

If the instruction is ambiguous:
{{"error": "Brief explanation of what is unclear"}}

Rules:
- trigger must exactly match one of: {t}
- value_meters must be a positive number in meters
- 1 inch = 0.0254 m  |  1 mm = 0.001 m  |  1 foot = 0.3048 m"""


def classification_prompt(
    assembly_context: str | None,
    dim_list: str,
    master_width_dim: str | None = None,
    master_height_dim: str | None = None,
) -> str:
    mw = master_width_dim or "null"
    mh = master_height_dim or "null"
    context_block = (
        f"\n-- ASSEMBLY CONTEXT --\n{assembly_context}\n---------------------\n"
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
  SCOPE = OVERALL (default)
    -> User mentions no specific component name
    -> Example: "change width to 80 inches", "resize to 24x48"
    -> Return: width_dims, height_dims, target sizes

  SCOPE = SINGLE
    -> User mentions a specific component but no others
    -> Example: "change chassis width to 60 inches", "only resize the mirror glass"
    -> Return: direct changes array with exact dimension name and value in meters

  SCOPE = DEPENDENT
    -> User mentions a component AND words like "related", "corresponding", "dependent"
    -> Example: "change chassis width and its corresponding components"
    -> Use the 3-step process below with ASSEMBLY CONTEXT to find all dependent dims
    -> Return: width_dims, height_dims, target sizes for all affected components
{context_block}
-- DEPENDENT SCOPE: 3-STEP DEPENDENCY IDENTIFICATION ------------------------
(Only apply when SCOPE = DEPENDENT)

STEP 1 — Use POSITION to classify each component:
  - Component Position X is large (|X| > 50 mm) → sits left/right → WIDTH-dependent
  - Component Position Y is large (|Y| > 50 mm) → sits top/bottom → HEIGHT-dependent
  - Component at X≈0, Y≈0 → centered, check its dimension names for clues

STEP 2 — Use MATES to extend classification:
  - If component A is mated to component B and B is already WIDTH-dependent,
    then A is also WIDTH-dependent (same for HEIGHT)
  - Follow the mate chain: A→B→C means if B is width-dependent, so is C

STEP 3 — Use SKETCH RELATIONS to confirm:
  - Equal/Symmetric relations between dims confirm they scale together
  - Midpoint relations indicate a centered dim — likely master or dependent
-----------------------------------------------------------------------------

-- HOW TO IDENTIFY DIMENSIONS ------------------------------------------------
Each dimension below is pre-labeled by the application — trust these labels:
  [W] = controls width  — include in width_dims
  [H] = controls height — include in height_dims
  [?] = internal/fixed  — DO NOT include in either list

Master dims (pre-identified by app — do NOT return in JSON for OVERALL scope):
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

For DEPENDENT scope:
- "target_width_meters"  : new width in meters, or null if not changing
- "master_width_dim"     : the target component's primary [W] dimension name, or null
- "width_dims"           : array of [W] dimension names to scale
- "target_height_meters" : new height in meters, or null if not changing
- "master_height_dim"    : the target component's primary [H] dimension name, or null
- "height_dims"          : array of [H] dimension names to scale
- "explanation"          : one sentence describing what is being changed

For SINGLE scope:
- "changes"     : [{{"dimension": "exact name from list", "value_meters": 0.0}}]
- "explanation" : one sentence describing what is being changed

Rules:
- Copy dimension names EXACTLY from the DIMENSIONS list (include [ComponentName])
- Return names WITHOUT the [W]/[H]/[?] prefix
- ONLY include [W] dims in width_dims — never [H] or [?]
- ONLY include [H] dims in height_dims — never [W] or [?]
- If instruction is unclear: {{"error": "what is unclear"}}
- 1 inch = 0.0254 m  |  1 mm = 0.001 m  |  1 foot = 0.3048 m"""
