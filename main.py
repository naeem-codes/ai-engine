from dotenv import load_dotenv
load_dotenv()

import json
from pathlib import Path
from fastapi import FastAPI
from models import InterpretRequest, InterpretResponse, GenerateRulesRequest, GenerateRulesResponse, SaveRulesRequest
from interpret import interpret as run_interpret
from generate_rules import generate_rules as run_generate_rules

RULES_DIR = Path(__file__).parent / "rules"

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


@app.post("/save-rules")
async def save_rules_endpoint(req: SaveRulesRequest):
    stem = Path(req.model_path).stem
    RULES_DIR.mkdir(exist_ok=True)
    out_path = RULES_DIR / f"{stem}.rules.json"
    doc = {
        "model": stem,
        "width": [{"if_changes": r.if_changes, "also_change": r.also_change} for r in req.width],
        "height": [{"if_changes": r.if_changes, "also_change": r.also_change} for r in req.height],
    }
    out_path.write_text(json.dumps(doc, indent=2))
    return {"saved": str(out_path)}
