# Lumi Design AI Engine

Python FastAPI service that handles all AI logic for the SolidWorks CAD automation tool. Runs independently on Windows, Mac, or Linux — no SolidWorks required.

The companion .NET app (`CAD-Automation/`) connects to SolidWorks, collects dimensions, and calls this service. This service handles all prompt engineering, LLM calls, rules validation, and returns a flat list of dimension changes to apply.

---

## Requirements

- Python 3.11+
- An Anthropic API key (or OpenAI key if using GPT)

---

## Setup

```bash
cd ai-engine

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
nano .env                          # Add your ANTHROPIC_API_KEY
```

---

## Running

```bash
source .venv/bin/activate
python -m uvicorn main:app --port 8000
```

> Always use `python -m uvicorn` (not just `uvicorn`) to ensure the venv's packages are used, not the system ones.

Open **http://localhost:8000/docs** to access the Swagger UI.  
Open **http://localhost:8000/health** to verify the server is up.

On Windows you can also double-click **`start.bat`** — it creates the venv, installs packages, and starts the server automatically.

---

## Testing

```bash
source .venv/bin/activate
python -m pytest -v
```

All tests mock LLM calls — no API key needed to run the test suite.

---

## Configuration

Edit `ai-engine/.env`:

| Variable | Description | Default |
|---|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key | required |
| `OPENAI_API_KEY` | OpenAI API key | optional |
| `DEFAULT_PROVIDER` | `claude` or `openai` | `claude` |
| `PORT` | Server port | `8000` |

---

## API

### `GET /health`
Returns `{"status": "ok"}`. Called by the .NET connector on startup.

### `POST /interpret`

**Request:**
```json
{
  "instruction": "set width to 30 inches",
  "dimensions": [
    { "name": "WIDTH@Mirror", "value_meters": 0.5 },
    { "name": "HEIGHT@Mirror", "value_meters": 1.0 }
  ],
  "assembly_context": "... context string from SolidWorks ...",
  "model_path": "C:\\models\\Mirror.SLDASM"
}
```

**Response (success):**
```json
{
  "changes": [
    { "name": "WIDTH@Mirror", "value_meters": 0.762 },
    { "name": "LED_WIDTH@LED", "value_meters": 0.686 }
  ],
  "explanation": "Setting width to 30in via smart rules (2 dimensions)",
  "error": null
}
```

**Response (error):**
```json
{
  "changes": [],
  "explanation": null,
  "error": "Width 200mm is below minimum 300mm"
}
```

---

## How It Works

The engine applies three cases in order:

1. **Rules-based** — if a `.rules.json` file exists for the active model (`model_path` stem matched against `rules/`), the LLM identifies the trigger keyword and value, the engine validates size limits and expands the trigger into all dependent dimensions using predefined ratios.

2. **Classification** — if no rules file exists but `assembly_context` is provided, the LLM classifies which dimensions belong to each axis (width/height) and determines the resize scope:
   - **OVERALL** — no component named → all `[W]`/`[H]` dims scale proportionally
   - **CONNECTED** — a component is named (without ONLY/JUST) → the LLM looks up that component in the **COMPONENT RELATIONSHIP MAP** embedded in `assembly_context` and includes all directly-mated components' dimensions in the change set. This is how physically connected parts (e.g. mirror glass + chassis frame + LED strips) all resize correctly when any one of them is mentioned.
   - **SINGLE** — user says ONLY/JUST → only that component changes

3. **Error fallback** — if neither rules nor context are available, an error is returned asking the user to refresh dimensions first.

---

## Adding Rules for a New Model

Create `rules/<ModelName>.rules.json` where `<ModelName>` matches the SolidWorks filename stem (e.g. `Mirror.SLDASM` → `Mirror.rules.json`):

```json
{
  "model": "Mirror",
  "limits": {
    "min_width_mm": 300,
    "max_width_mm": 1500,
    "min_height_mm": 400,
    "max_height_mm": 2000
  },
  "rules": [
    {
      "trigger": "width",
      "dimensions": [
        { "name": "WIDTH@Mirror", "ratio": 1.0 },
        { "name": "LED_WIDTH@LED", "ratio": 0.9 }
      ]
    },
    {
      "trigger": "height",
      "dimensions": [
        { "name": "HEIGHT@Mirror", "ratio": 1.0 }
      ]
    }
  ]
}
```

`ratio` is multiplied by the trigger value to compute each dimension's new value. A ratio of `1.0` means the dimension equals the trigger value exactly.

---

## Docker (optional)

```bash
docker build -t lumi-ai-engine .
docker run -p 8000:8000 \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  lumi-ai-engine
```

---

## Project Structure

```
ai-engine/
├── main.py          # FastAPI app — /interpret and /health endpoints
├── interpret.py     # Orchestration — decides which case applies
├── prompts.py       # LLM system prompt templates
├── llm.py           # Claude / OpenAI HTTP clients
├── rules.py         # Rules engine — loads .rules.json, validates, expands
├── models.py        # Pydantic request/response schemas
├── rules/           # .rules.json files (one per SolidWorks model)
├── tests/           # pytest test suite (LLM mocked)
├── .env.example     # Config template
├── requirements.txt
├── Dockerfile
└── start.bat        # Windows one-click launcher
```
