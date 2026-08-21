"""FastAPI app exposing OpenAI-compatible endpoints over SophiaPool."""
from __future__ import annotations

import json
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from kahndor.logma import Logma

from sophia.config import load_config
from sophia.pool import SophiaPool, new_completion_id, now_ts
from sophia.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    Choice,
    Message,
    ModelList,
    Usage,
)

logma = Logma(__name__)


def create_app(config=None) -> FastAPI:
    cfg = config or load_config()
    pool = SophiaPool(cfg)

    app = FastAPI(title="Sophia", version="0.1.0")
    app.state.pool = pool
    app.state.config = cfg

    @app.get("/healthz")
    async def healthz():
        models = await pool.list_models()
        providers = sorted({m.owned_by for m in models})
        return {"status": "ok", "providers": len(providers), "models": len(models)}

    @app.get("/v1/models")
    async def list_models():
        models = await pool.list_models()
        return ModelList(data=models).model_dump()

    @app.post("/v1/chat/completions")
    async def chat_completions(req: ChatCompletionRequest):
        if not req.messages:
            raise HTTPException(status_code=400, detail="messages must not be empty")

        system_parts = [m.content for m in req.messages if m.role == "system"]
        user_parts = [m.content for m in req.messages if m.role != "system"]
        sprompt = "\n".join(system_parts).strip()
        prompt = "\n".join(user_parts).strip()
        if not prompt:
            raise HTTPException(status_code=400, detail="no user message in messages")

        strategy = req.strategy or "random"
        temperature = req.temperature if req.temperature is not None else 0.7
        # Operator rule: "auto" means let the strategy pick; any concrete name forces that model.
        model = None if req.model == "auto" else req.model

        if req.stream:
            async def event_source() -> AsyncIterator[bytes]:
                try:
                    async for chunk in pool.stream(
                        prompt=prompt,
                        sprompt=sprompt,
                        temperature=temperature,
                        strategy=strategy,
                        model=model,
                    ):
                        payload = {
                            "id": new_completion_id(),
                            "object": "chat.completion.chunk",
                            "created": now_ts(),
                            "model": req.model,
                            "choices": [{"index": 0, "delta": {"content": chunk}, "finish_reason": None}],
                        }
                        yield f"data: {json.dumps(payload)}\n\n".encode("utf-8")
                    yield b"data: [DONE]\n\n"
                except Exception as e:
                    logma.exception(f"[sophia] streaming error: {e}")
                    err = {"error": {"message": str(e), "type": "server_error"}}
                    yield f"data: {json.dumps(err)}\n\n".encode("utf-8")
            return StreamingResponse(event_source(), media_type="text/event-stream")

        # Non-streaming
        try:
            content = await pool.chat(
                prompt=prompt,
                sprompt=sprompt,
                temperature=temperature,
                strategy=strategy,
                model=model,
            )
        except Exception as e:
            logma.exception(f"[sophia] chat error: {e}")
            raise HTTPException(status_code=502, detail=str(e)) from e

        resp = ChatCompletionResponse(
            id=new_completion_id(),
            created=now_ts(),
            model=req.model,
            choices=[Choice(index=0, message=Message(role="assistant", content=content or ""))],
            usage=Usage(),
        )
        return JSONResponse(content=resp.model_dump())

    return app


app = create_app()  # uvicorn entrypoint: uvicorn sophia.server:app
