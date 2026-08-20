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
├── rules_store.py          # WHERE rules live + WHICH set applies (family keying)
├── cloud_sync.py           # optional Supabase mirror (off by default)
├── prompts.py              # the four system prompts
├── llm.py                  # streaming Claude + OpenAI clients, call_llm() router
├── models.py               # all pydantic request/response schemas
├── log.py                  # ai-engine.log writer (log / section)
├── supabase/               # CLI project: migrations/ + config.toml (dev-only, never shipped)
├── tools/
│   └── create_client_user.py # per-client Auth user + client_id claim (needs the service key)
├── rules/                  # LEGACY location — migrated to the data dir on startup
├── tests/                  # 13 files / ~1.7k lines, LLM mocked
├── ai-engine.spec          # PyInstaller → single dist/ai-engine.exe  (engine.spec = stale)
├── .env                    # ANTHROPIC_API_KEY / OPENAI_API_KEY / DEFAULT_PROVIDER / PORT
└── start.bat               # venv + uvicorn on :8000
```

`generate_drawing.py` is **dead code** — it imports `DrawingRequest`/`DrawingRecipe`, which no
longer exist in `models.py`, and nothing imports it. The live drawing path is
`generate_drawing_plan.py`. Delete it or restore the models; don't extend it as-is.

`main.py` resolves `.env` next to `sys.executable` when frozen, so the client can edit it beside
`ai-engine.exe`. Preserve that when touching path logic.

---

## Where rules live, and which set applies (`rules_store.py`)

**Live location is `%PROGRAMDATA%\LumiDesignAI\rules`** (override with `LUMI_DATA_DIR`) —
deliberately outside the distribution, because rules used to sit next to `ai-engine.exe`, i.e.
inside the folder the client deletes on upgrade, so every new build wiped them.
`rules-seed/` in the build holds factory defaults, copied in only when nothing exists for that
key. `store.bootstrap()` runs at startup (FastAPI lifespan): migrate legacy `rules/` → seed →
flush the sync outbox. Idempotent, safe every boot.

**Rules are keyed by PRODUCT FAMILY, not by file stem.** `family_of()` strips the size token:
`KELLY-24.00X48.00-LED` → `KELLY-LED`. This is why it matters: the app's `SaveSizedVariant`
(step 1 of its two-button build-review-export drawing flow) renames only size-bearing files,
so component ids — and therefore dim names — are identical at
24×48 and 30×48. Keying by stem meant every sized variant looked like an unknown model and lost
the rule set that made the resize correct (it then fell through to LLM classification, which
silently resized it anyway; today it would be refused outright). One generation now covers every
size of a product.

A genuine part swap (AMBER 36 vs 60 — different chassis part number, 6 B-slots vs 8) gets its
own set as `FAMILY#2`, `#3`, assigned automatically on save by comparing component-id sets. Which
set applies is decided by **dim coverage** against the live model:

| Tier | Source | Notes |
|---|---|---|
| 1 | exact `<stem>.rules.json` | per-size override; back-compat with pre-family files |
| 2 | best-covering set in the family | `usable` requires every master dim to exist |
| 3 | legacy `rules/<stem>.rules.json` | for an install that hasn't bootstrapped |

A set whose **master** dim is absent is rejected (it would resize nothing while reporting
success). Missing **dependents** are tolerated — the rules UI lets the user trim deps — but they
are now listed in the chat explanation via `Selection.warning`, because the old failure mode was
silent: unknown deps were skipped with only a log line, so an under-grown frame looked like a
resize bug.

`family_from_stem()` exists separately from `family_of()` for a real trap: these stems contain
dots as part of the size, so `Path("KELLY-24.00X48.00-LED").stem` chops at the last dot and
yields family `KELLY`. Only a real path may go through `Path.stem`. The same derivation is
duplicated in `build-release.ps1` (`Get-RulesFamily`) for seed filtering — keep the two in step.

---

## Cloud sync (`cloud_sync.py`, opt-in)

Off unless `RULES_REMOTE=supabase` **and** `SUPABASE_URL` + `SUPABASE_KEY` + `SUPABASE_TOKEN` are
set (`CLIENT_ID` is read from the token's claim when omitted). Table is
`rules (client_id, model_key, doc, family, updated_at)` with `(client_id, model_key)` as the
primary key, so one client legitimately holds many rule sets; `model_key` is `FAMILY` or
`FAMILY#n`, and `family` is a **generated** column (`split_part(model_key,'#',1)`) so it cannot
drift. Everything else (variant, generated_for, the rules) lives inside `doc`, and a set's dim
fingerprint is computed from `doc` at read time.

**Authentication: the engine SIGNS IN as a per-client Supabase Auth user.**

| Value | Role | Safe to ship? |
|---|---|---|
| `SUPABASE_KEY` (anon) | identifies the project; grants nothing by itself | yes — public by design |
| `SUPABASE_EMAIL` + `SUPABASE_PASSWORD` | the client's Auth user; Supabase issues tokens carrying its `app_metadata.client_id` | yes — unlocks only that client's own rows |
| `SUPABASE_TOKEN` | optional pre-issued token used *instead* of signing in (dev smoke test, or a legacy-secret project). **Ignored when email+password are set** | only if scoped — never a service key |

**Why sign-in rather than a locally minted token:** this project signs JWTs with an
**asymmetric ES256 key** whose private half is not exportable, so a self-signed HS256 token is
rejected with `PGRST301 "No suitable key or wrong key type"` — verified live. Only Supabase can
issue an acceptable token. (An earlier `tools/mint_client_token.py` assumed the legacy shared
secret and was deleted.) A side benefit: revoking a client is banning one Auth user, instead of
rotating the project secret and invalidating every client at once.

`tools/create_client_user.py` creates/updates that user and stamps the claim (needs the service
key, dev machine only, idempotent, and verifies sign-in afterwards). `app_metadata` is
deliberate — unlike `user_metadata` a signed-in user cannot edit it, so a client cannot rewrite
their own `client_id`.

Session handling in `cloud_sync.py`: token cached in memory only (the password is already there,
so persisting a refresh token buys nothing), re-authenticated 60 s before expiry, and `_request`
retries once after re-authenticating on a 401 — access tokens last about an hour while the app
can stay open all day.

- **Pull** happens in `/get-rules`, which the app calls on every Refresh, i.e. immediately before
  any prompt. Filters `family=eq.…` (an earlier `model_key=like.FAMILY*` also matched the
  different product `KELLY-LED-HO`). Newest-wins on the DB's own `updated_at`, stamped into the
  local doc so comparison never depends on client clocks.
- **Push** happens in `/save-rules`; a failure queues the doc in `<data>/outbox` and
  `flush_outbox()` retries at startup.
- **`/interpret` never touches the network.** Any sync failure degrades to the local cache, which
  is exactly the local-only engine. Keep it that way — Part 5 of the roadmap is fully-local
  operation, and a rules file carries the client's part numbers and size limits.
- **`GET /sync-status`** reports config (key *names* only, never values), the data dir, rule-set
  count and outbox depth; `?check=true` does a live round trip and maps 401/403 → "claim or RLS
  problem", 404 → "has the migration been applied?". This exists because the sync path can't be
  tested without a live project.

Schema lives in `supabase/migrations/`. Apply with `supabase db push`, or paste into the SQL
editor. The `supabase/` folder is dev-only and never shipped.

---

## Baked configuration (client builds carry no `.env`)

`build-release.ps1` writes `_baked.py` into the engine source before PyInstaller runs, and
`main.py` applies it via `os.environ.setdefault` — so **a real `.env` always wins** and dev
machines behave exactly as before. The file is git-ignored and deleted after the build, even if
the build fails. Verified end to end: a frozen exe with no `.env` beside it reports its baked
keys and derives `client_id` from the baked token.

What may be baked, and why it is not about secrecy: **the PyInstaller archive is compressed, not
encrypted** — plain `strings` finds nothing, but `pyinstxtractor` unpacks it in one command. So
scope is the only real control. The Supabase URL, the anon key and a per-client token are safe
because reading them grants nothing the holder doesn't already own.

`ANTHROPIC_API_KEY` is baked **by the user's explicit decision (2026-08-04)** after being told it
is not protected by baking. Consequences to remember: use a spend-capped key, and rotating it
requires shipping a new build. `BAKED_KEYS` in `main.py` reports which keys a build received.

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
| `POST /interpret` | instruction + model snapshot → `changes[]`, `explanation`, `hanger`. Local rules only, never the network |
| `POST /generate-rules` | assembly context → proposed width/height rules + `skip` + `component_labels` (the app reviews before saving) |
| `GET /get-rules?model_path=` | pull the family from the cloud (best-effort), then return the selected set + `model_key`/`source` |
| `POST /save-rules` | write the family's set to the data dir and push it; **preserves** `pattern_rules`/`position`/`offset` when the caller omits them (a form that doesn't edit a section can't wipe it) — but deliberately drops any legacy `depth` block |
| `POST /drawing-plan` | English → validated drawing verb list |

`InterpretRequest` carries `instruction`, `dimensions[]`, `assembly_context`,
`model_path` (used to locate the rules file), `dim_axis_labels` (`W`/`H`/`D`/`?`, computed by the
app), `master_width_dim`, `master_height_dim`. All lengths are **meters** everywhere.

---

## `/interpret` — one resize path, two refusals (`interpret.py`)

**Case 1 — rules file exists** (the ONLY path that resizes). `rules_dependent_prompt` gets the
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

**Case 2 — no rules file** → refuse, **before any LLM call**: `error` names the model and points
at ⚙ Generate Rules, with `needs_rules: true` so the app raises a dialog instead of printing a
chat line. There used to be a `classification_prompt` fallback here that resized a model with no
rules at all by having the LLM choose the scope and the dim list itself (OVERALL / CONNECTED /
SINGLE). It ran silently — BREAM resized through it for months with nobody having reviewed what
moves with what — so it is **deleted**, prompt included. Do not reintroduce a resize path that
runs without a stored set; `tests/test_prompts.py` asserts the prompt stays gone.

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
stays under `MAX_AREA_FRACTION` (25% of glass **area**), under `MAX_HEIGHT_FRACTION` (60% of
glass height), and **no wider than the CHASSIS** (`chassis_w_in`, clearance
`CHASSIS_CLEARANCE_IN` = 0). 20% is a soft target used only for flagging. Reproduces every known data point
(AMBER 36×36 → #1038, KELLY 24×48 → #1119, AMBER 36×48 → #1333). A bespoke fitted hanger already
in band is kept untouched. `HangerChoice.log_lines()` makes the whole decision auditable.

**The chassis cap applies at ALL THREE steps, and `keep_fitted` is the one that matters.**
The hanger bolts to the chassis and its tabs are cut into it, but every other check sizes it
against the GLASS. AMBER hides this (chassis = glass - 2"); CLARA does not (glass - 6"). At a
24x60 CLARA glass the chassis is 18" and `keep_fitted` held the 20" #1038 - 300/1440 = 20.83%, a
fine share of the glass - so it overhung the part carrying its tabs by an inch each side (live
2026-08-18). Checking only during prefab substitution would never have run.

The chassis width comes from the **width rule's dependents** (`interpret._hanger_changes`
`width_deps`), *not* from "the largest `[W]` dim on a chassis" - the chassis also carries the
hanging-tab spacing on the W axis (`D1@Sketch81` = 15.750" on a 36" AMBER), so that heuristic caps
the hanger against the tab spacing the hanger is supposed to be driving. Clearance is deliberately
0: CLARA's chassis is 12" at an 18" glass and every 12" prefab would fail any positive margin,
pushing that whole size range onto custom hangers.

**The choice reaches the app in one of two shapes, and they are mutually exclusive:**

| outcome | `changes` carries | what the app does |
|---|---|---|
| `replace` — a prefab qualified | the tab follower **only** | swaps the component for `HANGERS\<part>-HANGER.SLDPRT` |
| `resize_fitted` — nothing qualified | the two hanger dims + follower | stretches the fitted hanger, as before |
| `keep_fitted` — bespoke, in band | the tab follower only | nothing |

The client supplied the real prefab parts, so a chosen prefab is now **swapped in as a component,
not written as dimensions** — `interpret._hanger_changes` emits no hanger dim writes on that path.
The old behaviour stretched whatever hanger was placed onto the catalogue *outline*, which got the
silhouette right and the internal hole pattern wrong (hence the since-dropped DXF-suppression
caveat). `resize_fitted` survives because the catalogue tops out at 20" wide and a 90" mirror needs
~58": there is no file to swap in, so stretching is the only option left.

Idempotence is deliberately the **app's** call — only it can see whether the component already
points at that file. Dimensions can't answer it: a bespoke hanger previously stretched to 14.25×15
measures exactly like a real #1119.

The chassis hanging-tab follower runs on every path (`HANGER_FOLLOWER_HINTS = ("SKETCH81",)`,
cross-checked against a 4.25" inset — a sketch *number* is the fragile part, so verify per product).
On the swap path its driving width comes from `target_width_meters`, **not** from `changes`, which
no longer holds a hanger dim — reading it from there would silently leave the tabs at the old
hanger's spacing.

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

**Never hand-edit a stored rule set.** The user regenerates them through the app's Generate
Rules flow every time, so a patch is thrown away. Fix the generator: this module, `prompts.py`, or
the app's labeler (`GetDimAxisLabels` / `LabelComponentDims` / `TryPerturbDrivenAxis` /
`FindMasterDims`). After a labeler change the user must rebuild → reconnect → **regenerate**.

`derive_rules()` in `rules.py` computes the same membership without storing anything. It propped
up the OVERALL branch of classification; with that path deleted it is **no longer called in
production** — kept and unit-tested (`tests/test_derived_rules.py`) as the honest expression of
"membership from labels", and a candidate building block for a Generate-Rules preview. It was
never a substitute for a stored set: no limits, component labels, offsets or position rules.

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
| `LUMI_DATA_DIR` | overrides the rules data dir | `%PROGRAMDATA%\LumiDesignAI` |
| `RULES_REMOTE` | `none` \| `supabase` | `none` |
| `SUPABASE_URL` / `SUPABASE_KEY` / `SUPABASE_EMAIL` / `SUPABASE_PASSWORD` | cloud sync (see below) | — |

In a client build these come from `_baked.py` rather than a file; `.env` overrides either way.

Models are pinned in `llm.py`: `claude-sonnet-4-6` / `gpt-5.1`. Both calls **stream** so the read
timeout applies per chunk instead of to the whole generation (`STREAM_TIMEOUT` = 600 s total,
15 s connect, 120 s read idle). The OpenAI path multiplies `max_tokens` (≥4096 floor) because
reasoning tokens count against `max_completion_tokens`. `call_llm` logs the full system prompt,
user message and raw response to `ai-engine.log` — that log is the first place to look.

---

## Testing

`pytest.ini` sets `asyncio_mode = auto`; no API key needed. `tests/conftest.py` points
`LUMI_DATA_DIR` at a temp dir and forces `RULES_REMOTE=none` for **every** test — without it the
suite would read and write the developer's real `%PROGRAMDATA%` rules.

Known pre-existing debt (unrelated to storage): `tests/fixture.rules.json` is still the old
`trigger`/`ratio` schema and `test_prompts.py` imports a `rules_prompt` that no longer exists, so
20 tests fail and one module fails to collect. Everything else passes.

Suites map to the invariants above:
`test_policy.py` (blocking, labels-vs-part-numbers, LED axis cases), `test_hanger_select.py` +
`test_hanger_wiring.py` (every legend data point, height cap, keep/resize-fitted),
`test_generate_rules_policy.py` (one rule per axis, mastered on the glass),
`test_mate_dims.py`, `test_offsets.py`, `test_positions.py`, `test_frost_follower.py`,
`test_interpret.py`, `test_rules.py`, `test_prompts.py`, `test_main.py`,
`test_rules_store.py` (family keying, coverage selection, migration/seed),
`test_derived_rules.py` (label-derived membership, OVERALL vs CONNECTED),
`test_cloud_sync.py` (pull/push/outbox with httpx faked).

**When you change a policy number or a selection rule, update the test that cites the live case
it came from** — those tests are the record of which bug each constant prevents.
