"""Production-drawing planning — the brain side of automated drawing generation.

Mirrors the resize pipeline's brain/hands split: this module NEVER touches
SolidWorks. It only returns a DrawingRecipe (a plan). The .NET app builds the
actual .SLDDRW from the approved recipe.

Two paths, in priority order:
  1. SAVED recipe — rules/<ModelStem>.drawing.json exists → return it verbatim.
     Instant, free, identical every time (the Case-1 equivalent for drawings).
     A saved recipe is the approved-and-frozen plan for a known product.
  2. AI recipe — no saved recipe (or force_ai) → ask the LLM to propose one from
     the drawing context. Human reviews it in the app before anything is built;
     once approved it can be saved back as the model's fixed recipe.
"""

import json
import sys
from pathlib import Path

from models import DrawingRequest, DrawingRecipe
from llm import call_llm
from prompts import drawing_prompt
from log import log, section

# Resolve rules/ next to engine.exe when frozen (PyInstaller), else next to this file.
if getattr(sys, "frozen", False):
    RULES_DIR = Path(sys.executable).parent / "rules"
else:
    RULES_DIR = Path(__file__).parent / "rules"


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        nl = text.find("\n")
        text = text[nl + 1:] if nl != -1 else text[3:]
    if text.endswith("```"):
        text = text[: text.rfind("```")].strip()
    return text.strip()


def _recipe_path(model_path: str | None) -> Path | None:
    if not model_path:
        return None
    return RULES_DIR / f"{Path(model_path).stem}.drawing.json"


def load_drawing_recipe(model_path: str | None) -> DrawingRecipe | None:
    """Return the saved (approved) recipe for this model, or None if there isn't one."""
    path = _recipe_path(model_path)
    if path is None or not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        recipe = DrawingRecipe(**data)
        recipe.source = "saved"
        return recipe
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        log(f"  [DRAWING] saved recipe unreadable ({path.name}): {exc}")
        return None


def save_drawing_recipe(model_path: str, recipe: DrawingRecipe) -> str:
    """Persist an approved recipe so future drawings of this model skip the AI."""
    RULES_DIR.mkdir(exist_ok=True)
    path = RULES_DIR / f"{Path(model_path).stem}.drawing.json"
    out = recipe.model_dump(exclude={"source", "error"})
    path.write_text(json.dumps(out, indent=2))
    log(f"  [DRAWING] saved recipe → {path}")
    return str(path)


async def generate_drawing(req: DrawingRequest) -> DrawingRecipe:
    section("DRAWING RECIPE REQUEST")
    log(f"  model_path  : {req.model_path or '(none)'}")
    log(f"  instruction : {req.instruction or '(none)'}")
    log(f"  force_ai    : {req.force_ai}")
    log(f"  context     : {len(req.drawing_context or '')} chars")

    # ── Path 1: saved recipe (unless the caller forces a fresh AI plan, or adds a
    #    natural-language tweak that the frozen recipe can't express) ────────────
    if not req.force_ai and not req.instruction:
        saved = load_drawing_recipe(req.model_path)
        if saved is not None:
            log("  PATH 1 (saved recipe) — returning frozen plan, no LLM call")
            return saved

    # ── Path 2: ask the AI to propose a recipe ─────────────────────────────────
    if not req.drawing_context or not req.drawing_context.strip():
        return DrawingRecipe(error="No drawing context provided. Click Refresh first.")

    log("  PATH 2 (AI) — asking LLM for a drawing recipe")
    raw = await call_llm(
        drawing_prompt(req.drawing_context, req.instruction),
        req.instruction or "Design the production drawing for this model.",
        max_tokens=1500,
        provider=req.provider,
    )
    try:
        data = json.loads(_strip_fences(raw))
    except json.JSONDecodeError as exc:
        log(f"  ERROR: LLM returned invalid JSON: {exc}")
        return DrawingRecipe(error=f"LLM returned invalid JSON: {exc}")

    if "error" in data:
        log(f"  ERROR (from AI): {data['error']}")
        return DrawingRecipe(error=data["error"])

    try:
        recipe = DrawingRecipe(**data)
    except ValueError as exc:
        log(f"  ERROR: recipe did not match schema: {exc}")
        return DrawingRecipe(error=f"Recipe did not match schema: {exc}")

    recipe.source = "ai"
    log(f"  AI recipe: {len(recipe.views)} view(s), "
        f"{len(recipe.dimensions_to_show)} dim(s), bom={recipe.bom.enabled}")
    return recipe
