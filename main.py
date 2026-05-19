from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI
from models import InterpretRequest, InterpretResponse
from interpret import interpret as run_interpret

app = FastAPI(title="Lumi Design AI Engine", version="1.0.0")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/interpret", response_model=InterpretResponse)
async def interpret_endpoint(req: InterpretRequest) -> InterpretResponse:
    return await run_interpret(req)
