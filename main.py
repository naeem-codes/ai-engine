import sys
from pathlib import Path
from dotenv import load_dotenv

# Base dir = folder of engine.exe when frozen (PyInstaller), else this file's folder.
# This makes .env and rules/ resolve next to the exe regardless of the working directory.
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent

load_dotenv(BASE_DIR / ".env")

import json
from fastapi import FastAPI
from models import InterpretRequest, InterpretResponse, GenerateRulesRequest, GenerateRulesResponse, SaveRulesRequest, DrawingPlanRequest, DrawingPlanResponse
from interpret import interpret as run_interpret
from generate_rules import generate_rules as run_generate_rules
from generate_drawing_plan import generate_drawing_plan as run_drawing_plan

RULES_DIR = BASE_DIR / "rules"

app = FastAPI(title="Lumi Design AI Engine", version="1.0.0")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/interpret", response_model=InterpretResponse)
async def interpret_endpoint(req: InterpretRequest) -> InterpretResponse:
    return await run_interpret(req)


@app.post("/generate-rules", response_model=GenerateRulesResponse)
async def generate_rules_endpoint(req: GenerateRulesRequest) -> GenerateRulesResponse:
    return await run_generate_rules(req)


@app.post("/drawing-plan", response_model=DrawingPlanResponse)
async def drawing_plan_endpoint(req: DrawingPlanRequest) -> DrawingPlanResponse:
    return await run_drawing_plan(req)


@app.get("/get-rules", response_model=GenerateRulesResponse)
async def get_rules_endpoint(model_path: str) -> GenerateRulesResponse:
    from models import RulePair, PositionRule, ThicknessRule, OffsetRule
    stem = Path(model_path).stem
    candidate = RULES_DIR / f"{stem}.rules.json"
    if not candidate.exists():
        return GenerateRulesResponse()
    data = json.loads(candidate.read_text())
    def parse(lst):
        return [RulePair(if_changes=r["if_changes"], also_change=r.get("also_change", [])) for r in lst if r.get("if_changes")]
    position = [
        PositionRule(**{k: v for k, v in r.items() if k in PositionRule.model_fields})
        for r in data.get("position", [])
        if r.get("position_dim") and r.get("driver_dim")
    ]
    depth = [
        ThicknessRule(**{k: v for k, v in r.items() if k in ThicknessRule.model_fields})
        for r in data.get("depth", [])
        if r.get("dim")
    ]
    offset = [
        OffsetRule(**{k: v for k, v in r.items() if k in OffsetRule.model_fields})
        for r in data.get("offset", [])
        if r.get("target_dim") and r.get("source_dim")
    ]
    return GenerateRulesResponse(
        width_rules=parse(data.get("width", [])),
        height_rules=parse(data.get("height", [])),
        component_labels=data.get("component_labels", {}),
        limits=data.get("limits", {}),
        pattern_rules=data.get("pattern_rules", []),
        position=position,
        depth=depth,
        offset=offset,
    )


@app.post("/save-rules")
async def save_rules_endpoint(req: SaveRulesRequest):
    stem = Path(req.model_path).stem
    RULES_DIR.mkdir(exist_ok=True)
    out_path = RULES_DIR / f"{stem}.rules.json"

    # Preserve pattern_rules / position across saves if the request omits them, so a
    # form that doesn't edit a section can't wipe it. An explicitly-sent (possibly
    # empty) list from a form that DOES edit that section still overwrites on disk.
    def preserve(field_value, key):
        if field_value:
            return [v.model_dump() if hasattr(v, "model_dump") else v for v in field_value]
        if out_path.exists():
            try:
                return json.loads(out_path.read_text()).get(key, [])
            except (json.JSONDecodeError, OSError):
                return []
        return []

    pattern_rules = preserve(req.pattern_rules, "pattern_rules")
    position = preserve(req.position, "position")
    depth = preserve(req.depth, "depth")
    offset = preserve(req.offset, "offset")

    doc = {
        "model": stem,
        "width": [{"if_changes": r.if_changes, "also_change": r.also_change} for r in req.width],
        "height": [{"if_changes": r.if_changes, "also_change": r.also_change} for r in req.height],
        "component_labels": req.component_labels,
    }
    if req.limits:
        doc["limits"] = req.limits
    if pattern_rules:
        doc["pattern_rules"] = pattern_rules
    if position:
        doc["position"] = position
    if depth:
        doc["depth"] = depth
    if offset:
        doc["offset"] = offset
    out_path.write_text(json.dumps(doc, indent=2))
    return {"saved": str(out_path)}


if __name__ == "__main__":
    import os
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="127.0.0.1", port=port)
