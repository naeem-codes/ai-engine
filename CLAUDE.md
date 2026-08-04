# CLAUDE.md — ai-engine (Lumi Design AI Engine)

## What this is

The Python/FastAPI brain of the SolidWorks CAD automation tool. It receives a model snapshot
(dimensions, axis labels, assembly context) plus a natural-language instruction and returns a
flat list of `(dimension_name, new_value_meters)` pairs — or a rules file, or a drawing plan.

**No SolidWorks dependency.** Every COM operation lives in the companion .NET app; this side is
pure text/JSON and fully unit-testable.

**Companion app:** the WinForms app at
`C:\Users\a\Desktop\Prototype Copy\Auto CAD version 5 Part 3\AutoCAD\SolidWorksAI` — that is the
one the client build ships from, and its `CLAUDE.md` documents the COM half. (The WPF app under
`SolidWorks CAD Automation Production`, which carries its own restructured `ai-engine/src/` copy,
is **not** what ships.)

**Division of labour:** the LLM makes only *naming and grouping* decisions — which rule, which
component, which target value, which views. Every numeric consequence (dependent values, policy
blocks, hanger choice, drawing geometry) is computed deterministically in Python or in the app.
Where an old bug is encoded as a rule, the module docstring cites the live case; keep those
comments intact when editing.

---

## Layout

```
ai-engine/
├── main.py                 # FastAPI app + all endpoints; frozen-aware BASE_DIR
├── interpret.py            # THE core: resize orchestration + all followers/guards
├── resize_policy.py        # what may resize, on which axis — single source of truth
├── rules.py                # load rules JSON; expand_positions / expand_offsets; validate
├── hanger_select.py        # prefab-hanger selection by glass AREA (+ height cap)
├── generate_rules.py       # /generate-rules: LLM grouping → deterministic axis enforcement
├── generate_drawing_plan.py# /drawing-plan: English → fixed verb vocabulary
├── prompts.py              # the four system prompts
├── llm.py                  # streaming Claude + OpenAI clients, call_llm() router
├── models.py               # all pydantic request/response schemas
├── log.py                  # ai-engine.log writer (log / section)
├── rules/                  # <ModelStem>.rules.json — GENERATED, never hand-edited
├── tests/                  # 13 files / ~1.7k lines, LLM mocked
├── ai-engine.spec          # PyInstaller → single dist/ai-engine.exe  (engine.spec = stale)
├── .env                    # ANTHROPIC_API_KEY / OPENAI_API_KEY / DEFAULT_PROVIDER / PORT
└── start.bat               # venv + uvicorn on :8000
```

`generate_drawing.py` is **dead code** — it imports `DrawingRequest`/`DrawingRecipe`, which no
longer exist in `models.py`, and nothing imports it. The live drawing path is
`generate_drawing_plan.py`. Delete it or restore the models; don't extend it as-is.

`main.py` and `rules.py` resolve `.env` and `rules/` next to `sys.executable` when frozen, so the
client can edit both beside `ai-engine.exe`. Preserve that when touching path logic.

---

## Run / test / build

```powershell
# dev server (auto-reload)
.\.venv\Scripts\python.exe -m uvicorn main:app --reload --port 8000
# or: start.bat     # creates .venv, installs requirements, runs uvicorn on :8000

# tests — no API key needed, every LLM call mocked
.\.venv\Scripts\python.exe -m pytest -q

# client exe (embeds Python; the app's build-release.ps1 calls this)
.\.venv310\Scripts\python.exe -m PyInstaller ai-engine.spec --noconfirm   # → dist\ai-engine.exe
```

Swagger at `http://localhost:8000/docs`. `.venv310` is the canonical build venv (PyInstaller),
`.venv` the dev one.

**Rules are read from disk per request, but code is not — restart the engine after any `.py`
edit.** The frozen exe never reloads.

---

## Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /health` | `{"status":"ok"}` — the app polls this before Connect; `START.bat` waits on it |
| `POST /interpret` | instruction + model snapshot → `changes[]`, `explanation`, `hanger` |
| `POST /generate-rules` | assembly context → proposed width/height rules + `skip` + `component_labels` (the app reviews before saving) |
| `GET /get-rules?model_path=` | read back the saved rules file for a model |
| `POST /save-rules` | write `rules/<stem>.rules.json`; **preserves** `pattern_rules`/`position`/`offset` when the caller omits them (a form that doesn't edit a section can't wipe it) — but deliberately drops any legacy `depth` block |
| `POST /drawing-plan` | English → validated drawing verb list |

`InterpretRequest` carries `instruction`, `dimensions[]`, `assembly_context`,
`model_path` (used to locate the rules file), `dim_axis_labels` (`W`/`H`/`D`/`?`, computed by the
app), `master_width_dim`, `master_height_dim`. All lengths are **meters** everywhere.

---

## `/interpret` — three paths (`interpret.py`)

**Case 1 — rules file exists** (the normal production path). `rules_dependent_prompt` gets the
rules JSON + friendly component labels + the master dims; the LLM returns only
`{rule, scope, value_meters, explanation}`. Then, deterministically:

1. **`scope == "overall"` re-anchor** — if `if_changes` isn't the axis master, swap it for the
   master and take that rule's own `also_change`. (A 59 mm power-supply dim literally named
   `D1@WIDTH` got picked for "change width to 40" and blew the assembly up 17×.)
2. **Target policy check** — refuse outright if the user targeted fixed-size hardware, rather
   than silently resizing something else.
3. **`validate()`** against the rules file's min/max limits (min skipped when the rule has no deps).
4. **Dependents** via `_dependent_value` — constant offset, with fixed profiles left alone.
5. **`expand_positions`** — distance-mate offsets shifted by `factor × driver delta`.
6. **`expand_offsets`** — `target = new_source + offset_meters`, absolute, never scaled; runs last
   so it sees the scaled source and overrides any proportional value.
7. **`_enforce_policy`** — final guard; a stale rules file can still name a clip or a mate.
8. **`_frost_follower_updates`** then **`_hanger_changes`** (order matters: the frost band follows
   the strip, and must not be confused with the hanger's followers).

**Case 2 — no rules file** → `classification_prompt` with OVERALL / SINGLE / CONNECTED scope
(CONNECTED does a full transitive walk of the COMPONENT RELATIONSHIP MAP the app builds).
Dependents use the same constant-offset helper; the same policy guard, frost and hanger passes run.

**Case 3 — no `dim_axis_labels`** → error telling the user to click Refresh first.

---

## Resize invariants (`resize_policy.py`)

These are enforced in code, not prompted, because `generate_rules.py` rebuilds rule membership
from the app's labels on every regeneration and would otherwise keep re-adding blocked dims.

- **Only the mirror glass W/H may be a rule master** (`pick_master`); everything else on that axis
  is a dependent under it. One width rule, one height rule.
- **Dependents move by CONSTANT OFFSET:** `new = current + (master_new − master_current)`. These
  products are built to fixed borders, not ratios (the AMBER 36→48 report).
- Below `MIN_DEPENDENT_FRACTION` (0.5 × master) a dim is a **fixed extrusion profile → left alone**;
  offsetting ALPHA's 1.000" profile by +6" was a 7× blow-up. Real frame dims cluster at 0.875–0.944,
  profiles at 0.014–0.042.
- **Never resized:** power supply (`LPM`/`PSU`/`LED DRIVER`), clips, brackets, hanger — the
  assembly's mates reposition them.
- **LED strips:** the length scales on whichever axis it's labeled (strips can be horizontal); the
  <50 mm cross-section never scales. Classification is by **value**, not axis.
- **Mate dims are positions, not sizes** — `is_mate_dim` matches `D<n>@Distance4`/`Width1`/… (digits
  required, so a sketch named `WIDTH` is safe). Excluded from size rules in `filter_axis_dims` and
  both dependent loops, *not* in `block_reason`, because offset/position rules legitimately write
  mate values. Writing one shifted SUZI's chassis 6".
- **Blocking matches the component part number** in the dim's trailing `[...]`, never the LLM's
  friendly label: the same physical `12186-MOUNTING-PLATE` was labeled "Mounting Plate" on one
  product and "Power Supply Mounting Plate" on another, and the label match froze a structural part.
  `label_suggests_fixed_size` reports such cases as advisory only.
- `is_mirror_glass` is the one check that *does* consult the label — a label may make a dim eligible
  to be master (a wrong master fails loudly) but never freeze one (a wrong freeze fails silently).

### Hanger (`hanger_select.py`)
Selected from the 7-part prefab legend, never scaled: the **largest** prefab that physically fits,
stays under `MAX_AREA_FRACTION` (25% of glass **area**) and under `MAX_HEIGHT_FRACTION` (60% of
glass height). 20% is a soft target used only for flagging. Reproduces every known data point
(AMBER 36×36 → #1038, KELLY 24×48 → #1119, AMBER 36×48 → #1333). A bespoke fitted hanger already
in band is kept untouched; if nothing qualifies the fitted one is scaled to mid-band. Only exact
catalogue sizes are ever written, plus the chassis hanging-tab follower
(`HANGER_FOLLOWER_HINTS = ("SKETCH81",)`, cross-checked against a 4.25" inset — a sketch *number* is
the fragile part, so verify per product). `HangerChoice.log_lines()` makes the whole decision
auditable.

---

## `/generate-rules`

The LLM (`rules_system_prompt`) proposes grouping; then Python overrules it:

- Axis membership is **rebuilt from the app's `[W]`/`[H]` labels** — the prompt's position
  heuristic ("large X → width-dependent") drags a *vertical* LED strip's `[H]` length into
  width_rules and drops on-axis chassis dims, so the frame under-grows.
- `filter_axis_dims` then removes policy-blocked dims and every removal is appended to `skip` with
  its reason, so the app's rules UI shows *why* a dim is absent rather than looking buggy.
- `_rebuild_axis` collapses each axis to one master (the glass) + dependents; with no identifiable
  master it falls back to every-dim-its-own-master. `_scrub` handles the no-labels case by
  stripping blocked dims out of the LLM's own rules.
- `max_tokens=16000` — 8192 truncated mid-JSON on large assemblies; a truncated response is
  reported as such rather than as a parse error.

**Never hand-edit `rules/*.rules.json`.** The user regenerates them through the app's Generate
Rules flow every time, so a patch is thrown away. Fix the generator: this module, `prompts.py`, or
the app's labeler (`GetDimAxisLabels` / `LabelComponentDims` / `TryPerturbDrivenAxis` /
`FindMasterDims`). After a labeler change the user must rebuild → reconnect → **regenerate**.

Rules-file shape: `width`/`height` (`if_changes` + `also_change`), `component_labels`, `limits`,
`pattern_rules` (component multiplication — stored and forwarded only; the app applies them),
`position`, `offset`.

---

## `/drawing-plan`

`drawing_plan_prompt` translates English into ops from a fixed vocabulary, re-validated here
against `ALLOWED_VERBS` so a hallucinated verb never reaches the app: `create_view`, `set_units`,
`set_sheet`, `overall_dimensions`, `auto_dimension` (back-compat alias), `fill_title_block`,
`add_notes`, `export_pdf`. **The AI never emits a scale, coordinate or view position** — the app
measures the model and lays the sheet out. Labelling is opt-in: plain "production drawing" means
views only. Unsupported extras (hole table, BOM, section views, GD&T) are silently omitted and
noted in `explanation` rather than failing.

---

## LLM config (`llm.py`)

| Var | Meaning | Default |
|---|---|---|
| `ANTHROPIC_API_KEY` | required when `DEFAULT_PROVIDER=claude` | — |
| `OPENAI_API_KEY` | required when `DEFAULT_PROVIDER=openai` | — |
| `DEFAULT_PROVIDER` | `claude` \| `openai` | `claude` |
| `PORT` | server port | `8000` |

Models are pinned in `llm.py`: `claude-sonnet-4-6` / `gpt-5.1`. Both calls **stream** so the read
timeout applies per chunk instead of to the whole generation (`STREAM_TIMEOUT` = 600 s total,
15 s connect, 120 s read idle). The OpenAI path multiplies `max_tokens` (≥4096 floor) because
reasoning tokens count against `max_completion_tokens`. `call_llm` logs the full system prompt,
user message and raw response to `ai-engine.log` — that log is the first place to look.

---

## Testing

`pytest.ini` sets `asyncio_mode = auto`; no API key needed. Suites map to the invariants above:
`test_policy.py` (blocking, labels-vs-part-numbers, LED axis cases), `test_hanger_select.py` +
`test_hanger_wiring.py` (every legend data point, height cap, keep/resize-fitted),
`test_generate_rules_policy.py` (one rule per axis, mastered on the glass),
`test_mate_dims.py`, `test_offsets.py`, `test_positions.py`, `test_frost_follower.py`,
`test_interpret.py`, `test_rules.py`, `test_prompts.py`, `test_main.py`.

**When you change a policy number or a selection rule, update the test that cites the live case
it came from** — those tests are the record of which bug each constant prevents.
