import faulthandler
import os
import sys
import threading
faulthandler.enable()  # dumps Python traceback to stderr on SIGSEGV/SIGFPE

from contextlib import asynccontextmanager
from fastapi import FastAPI
from pydantic import BaseModel

print(f"[DEBUG] server.py importing model.llm  (pid={os.getpid()}, tid={threading.get_ident()})", flush=True)
from model.llm import engine
print(f"[DEBUG] model.llm imported OK", flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"[DEBUG] lifespan startup  pid={os.getpid()}", flush=True)
    yield
    print(f"[DEBUG] lifespan shutdown pid={os.getpid()}", flush=True)


app = FastAPI(title="Qwen 14B Local API (ExLlamaV2)", lifespan=lifespan)


class GenerateRequest(BaseModel):
    prompt: str
    max_new_tokens: int = 1024


@app.get("/health")
def health():
    return {
        "status": "ok",
        "model": "qwen-14b",
    }


@app.post("/generate")
def generate(req: GenerateRequest):
    output = engine.generate(
        prompt=req.prompt,
        max_new_tokens=req.max_new_tokens
    )

    return {
        "response": output
    }