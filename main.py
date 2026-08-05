import os
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

# Configuration baked in at build time by build-release.ps1 (writes _baked.py, git-ignored,
# absent in dev). Applied as DEFAULTS so a real .env always wins — that keeps this machine
# behaving exactly as before, while a client build needs no .env at all.
#
# Scope, not secrecy, is what makes a value safe to bake: the PyInstaller archive is
# compressed, not encrypted, and unpacks with a public script. Safe here are the Supabase URL,
# the public anon key, and a per-client token that unlocks only that client's own rules.
# The Anthropic key is baked at the user's explicit direction (2026-08-04); it is NOT protected
# by being baked, so it should be a spend-capped key, and rotating it requires a new build.
try:
    import _baked                                   # type: ignore[import-not-found]
    BAKED_KEYS = sorted(k for k, v in getattr(_baked, "CONFIG", {}).items() if v)
    for _k, _v in getattr(_baked, "CONFIG", {}).items():
        if _v:
            os.environ.setdefault(_k, str(_v))
except ImportError:
    BAKED_KEYS = []

import json
from contextlib import asynccontextmanager
from fastapi import FastAPI
from models import InterpretRequest, InterpretResponse, GenerateRulesRequest, GenerateRulesResponse, SaveRulesRequest, DrawingPlanRequest, DrawingPlanResponse
from interpret import interpret as run_interpret
from generate_rules import generate_rules as run_generate_rules
from generate_drawing_plan import generate_drawing_plan as run_drawing_plan
import cloud_sync
import rules_store as store
from log import log, section

# Legacy per-stem location (next to the exe). Still read once by store.bootstrap() so an
# existing install migrates itself; the LIVE location is store.data_dir().
RULES_DIR = BASE_DIR / "rules"


@asynccontextmanager
async def lifespan(app: FastAPI):
    section("ENGINE STARTUP")
    info = store.bootstrap()
    log(f"  rules data dir : {info['data_dir']}  ({info['total']} rule set(s))")
    log(f"  cloud sync     : {'supabase' if cloud_sync.enabled() else 'off (local only)'}")
    await cloud_sync.flush_outbox()
    yield


app = FastAPI(title="Lumi Design AI Engine", version="1.1.0", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/sync-status")
async def sync_status(check: bool = False):
    """Is rules sync configured, and (with ?check=true) does the round trip actually work?

    The whole sync path is untestable without a live project, so this makes verification one
    request rather than a failed resize. Returns no secrets — never the key or the token.
    """
    # Names only, never values: enough to answer "did this build get its configuration?"
    # without printing a key into a log or a screenshot.
    settings = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEFAULT_PROVIDER", "RULES_REMOTE",
                "SUPABASE_URL", "SUPABASE_KEY", "SUPABASE_EMAIL", "SUPABASE_PASSWORD",
                "SUPABASE_TOKEN", "CLIENT_ID")
    info = {
        "rules_dir": str(store.data_dir()),
        "rule_sets": len(list(store.data_dir().glob(f"*{store.RULES_SUFFIX}"))),
        "baked_keys": BAKED_KEYS,
        "configured": sorted(k for k in settings if os.getenv(k)),
    }
    return {**info, **(await cloud_sync.check() if check else cloud_sync.status())}


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
    from models import RulePair, PositionRule, OffsetRule
    # The app calls this on every Refresh, i.e. right before any prompt — so it is the natural
    # sync point. Best-effort: a failed pull just leaves the local cache in place.
    await cloud_sync.pull_family(model_path)
    selection = store.select_for_model(model_path, [])
    data = selection.doc
    if data is None:
        legacy = RULES_DIR / f"{Path(model_path).stem}.rules.json"
        if not legacy.exists():
            return GenerateRulesResponse()
        data = json.loads(legacy.read_text())
    def parse(lst):
        return [RulePair(if_changes=r["if_changes"], also_change=r.get("also_change", [])) for r in lst if r.get("if_changes")]
    position = [
        PositionRule(**{k: v for k, v in r.items() if k in PositionRule.model_fields})
        for r in data.get("position", [])
        if r.get("position_dim") and r.get("driver_dim")
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
        offset=offset,
        model_key=selection.key,
        source=selection.source if selection.doc is not None else "legacy",
    )


@app.post("/save-rules")
async def save_rules_endpoint(req: SaveRulesRequest):
    stem = Path(req.model_path).stem
    existing = store.read_for_key_or_family(req.model_path) or {}

    # Preserve pattern_rules / position across saves if the request omits them, so a
    # form that doesn't edit a section can't wipe it. An explicitly-sent (possibly
    # empty) list from a form that DOES edit that section still overwrites on disk.
    def preserve(field_value, key):
        if field_value:
            return [v.model_dump() if hasattr(v, "model_dump") else v for v in field_value]
        return existing.get(key, [])

    # NOTE: a legacy "depth" block is deliberately NOT preserved — thickness (Z) rules
    # were removed, so saving drops it from any rules file written before that.
    pattern_rules = preserve(req.pattern_rules, "pattern_rules")
    position = preserve(req.position, "position")
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
    if offset:
        doc["offset"] = offset

    # Stored under the FAMILY key (so every size of this product resolves to it), then
    # mirrored to the cloud best-effort — a failed push is queued, never lost, and never
    # turns a successful local save into an error the user sees.
    key = store.save_for_model(req.model_path, doc)
    pushed = await cloud_sync.push(key, store.read_key(key) or doc)
    return {"saved": str(store._key_path(key)), "model_key": key,
            "family": store.family_of(req.model_path),
            "synced": pushed, "sync_enabled": cloud_sync.enabled()}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="127.0.0.1", port=port)
