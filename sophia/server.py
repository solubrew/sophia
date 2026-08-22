"""FastAPI app exposing OpenAI-compatible endpoints over SophiaPool.

Auth: Sophia-internal requestor keys (see :mod:`sophia.keys`). Every
/v1/chat/completions call resolves the ``Authorization: Bearer`` header
to a requestor record; one row lands in ``sophia_usage`` per call; if
the requestor's key has ``store_payload=True``, the full prompt +
response is written to the capture store (see :mod:`sophia.captures`).
"""

from __future__ import annotations

import json
import time
from typing import AsyncIterator, Optional

from fastapi import FastAPI, Header, HTTPException, Query, Request, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from kahndor.logma import Logma

from sophia.captures import CaptureStore
from sophia.config import load_config
from sophia.keys import KeyStore
from sophia.pool import SophiaPool, new_completion_id, now_ts
from sophia.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    Choice,
    Message,
    ModelList,
    Usage,
)
from sophia.sophia import SophiaDB
from sophia.usage import UsageLog, UsageRow, default_window_summaries

logma = Logma(__name__)


def _cost_for_model(provider_meta: dict, prompt_tokens: int, completion_tokens: int) -> tuple[float, float, float]:
    """Pull per-1k costs from the model meta (costs_per_1k_input /
    costs_per_1k_output are ladyol keys). Returns (in, out, total) in USD.
    Returns (0, 0, 0) when costs aren't recorded — common for local models."""
    cin = float(provider_meta.get("costs_per_1k_input", 0) or 0)
    cout = float(provider_meta.get("costs_per_1k_output", 0) or 0)
    return (
        round(prompt_tokens / 1000 * cin, 6),
        round(completion_tokens / 1000 * cout, 6),
        round(prompt_tokens / 1000 * cin + completion_tokens / 1000 * cout, 6),
    )


def create_app(config=None) -> FastAPI:
    cfg = config or load_config()
    pool = SophiaPool(cfg)

    # Single shared DB + per-subsystem wrappers. All live in sophia.db.
    db = SophiaDB()
    keys = KeyStore(db)
    usage = UsageLog(db)
    captures = CaptureStore()

    app = FastAPI(title="Sophia", version="0.1.0")
    app.state.pool = pool
    app.state.config = cfg
    app.state.db = db
    app.state.keys = keys
    app.state.usage = usage
    app.state.captures = captures

    def _auth(authorization: Optional[str]) -> Optional[str]:
        """Return the requestor name on success, or None. Caller decides
        whether None is fatal. Splitting this out lets us return 401
        uniformly without coupling to the route."""
        if not authorization:
            return None
        if not authorization.lower().startswith("bearer "):
            return None
        raw = authorization[7:].strip()
        rec = keys.lookup(raw)
        return rec.name if rec else None

    def _resolve_key(authorization: Optional[str]):
        """Return the full ApiKey record (for role checks), or None."""
        if not authorization or not authorization.lower().startswith("bearer "):
            return None
        return keys.lookup(authorization[7:].strip())

    async def _require_cap(authorization: Optional[str], cap: str):
        """Coroutine that resolves the Bearer header and enforces a
        capability. Used as a FastAPI dependency. 401 on missing key,
        403 on missing capability."""
        rec = _resolve_key(authorization)
        if rec is None:
            raise HTTPException(status_code=401, detail="missing or invalid API key")
        if not rec.can(cap):
            raise HTTPException(
                status_code=403,
                detail=f"role {rec.role!r} does not permit {cap!r}",
            )
        return rec

    def _cap_dep(cap: str):
        """The inner coroutine FastAPI will call. Use with
        ``Depends(_cap_dep("chat"))``."""
        async def _dep(authorization: Optional[str] = Header(default=None)):
            return await _require_cap(authorization, cap)
        return _dep

    @app.get("/healthz")
    async def healthz():
        """Process-alive probe. Does NOT touch ladyol or the DB —
        a transient import hiccup in either must not trigger container
        restarts. Returns a richer payload if providers/models are
        reachable, but always returns 200 if the FastAPI loop is
        responsive."""
        try:
            models = await pool.list_models()
            providers = sorted({m.owned_by for m in models})
            return {"status": "ok", "providers": len(providers), "models": len(models)}
        except Exception as exc:  # noqa: BLE001 — healthz must always 200
            logma.warning(f"/healthz: pool unreachable ({exc.__class__.__name__}); returning minimal ok")
            return {"status": "ok", "providers": 0, "models": 0, "note": "pool not initialized"}

    @app.get("/v1/models")
    async def list_models(rec=Depends(_cap_dep("chat"))):
        models = await pool.list_models()
        return ModelList(data=models).model_dump()

    @app.post("/v1/chat/completions")
    async def chat_completions(
        req: ChatCompletionRequest,
        rec=Depends(_cap_dep("chat")),
    ):
        requestor_name = rec.name
        requestor = rec
        assert requestor is not None  # _require_cap just succeeded

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
        model = None if req.model == "auto" else req.model
        completion_id = new_completion_id()
        t0 = time.monotonic()

        if req.stream:
            async def event_source():
                chunks: list[str] = []
                try:
                    async for chunk in pool.stream(
                        prompt=prompt, sprompt=sprompt, temperature=temperature,
                        strategy=strategy, model=model,
                    ):
                        chunks.append(chunk)
                        payload = {
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "created": now_ts(),
                            "model": req.model,
                            "choices": [{"index": 0, "delta": {"content": chunk}, "finish_reason": None}],
                        }
                        yield f"data: {json.dumps(payload)}\n\n".encode("utf-8")
                    yield b"data: [DONE]\n\n"
                    elapsed_ms = int((time.monotonic() - t0) * 1000)
                    full = "".join(chunks)
                    _record(req, requestor, completion_id, model, strategy,
                            prompt_tokens=len(prompt.split()),  # rough proxy
                            completion_tokens=len(full.split()),
                            elapsed_ms=elapsed_ms, status="ok",
                            error_kind=None)
                    if requestor.store_payload:
                        captured = {"messages": [m.model_dump() for m in req.messages],
                                    "temperature": temperature, "stream": True}
                        captured_resp = {"id": completion_id, "model": req.model,
                                         "choices": [{"index": 0, "text": full}]}
                        captures.write(
                            completion_id=completion_id, requestor=requestor_name,
                            requestor_id=requestor.id, model=req.model or "auto",
                            strategy=strategy, provider_name="(stream)",
                            request_payload=captured, response_payload=captured_resp,
                        )
                except Exception as e:
                    logma.exception(f"[sophia] streaming error: {e}")
                    elapsed_ms = int((time.monotonic() - t0) * 1000)
                    _record(req, requestor, completion_id, model, strategy,
                            prompt_tokens=0, completion_tokens=0, elapsed_ms=elapsed_ms,
                            status="error", error_kind=type(e).__name__)
                    err = {"error": {"message": str(e), "type": "server_error"}}
                    yield f"data: {json.dumps(err)}\n\n".encode("utf-8")
            return StreamingResponse(event_source(), media_type="text/event-stream")

        # Non-streaming
        try:
            content, provider_meta = await pool.chat_with_meta(
                prompt=prompt, sprompt=sprompt, temperature=temperature,
                strategy=strategy, model=model,
            )
        except Exception as e:
            logma.exception(f"[sophia] chat error: {e}")
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            _record(req, requestor, completion_id, model, strategy,
                    prompt_tokens=0, completion_tokens=0, elapsed_ms=elapsed_ms,
                    status="error", error_kind=type(e).__name__)
            raise HTTPException(status_code=502, detail=str(e)) from e

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        pt, ct = _token_counts(prompt, content or "", provider_meta)
        in_usd, out_usd, total_usd = _cost_for_model(provider_meta, pt, ct)
        _record(req, requestor, completion_id, model, strategy,
                prompt_tokens=pt, completion_tokens=ct, elapsed_ms=elapsed_ms,
                status="ok", error_kind=None,
                provider_meta=provider_meta, in_usd=in_usd, out_usd=out_usd,
                total_usd=total_usd)

        resp = ChatCompletionResponse(
            id=completion_id, created=now_ts(), model=req.model,
            choices=[Choice(index=0, message=Message(role="assistant", content=content or ""))],
            usage=Usage(prompt_tokens=pt, completion_tokens=ct, total_tokens=pt + ct),
        )
        captured_path = None
        if requestor.store_payload:
            captured_path = captures.write(
                completion_id=completion_id, requestor=requestor_name,
                requestor_id=requestor.id, model=resp.model,
                strategy=strategy, provider_name=provider_meta.get("provider_name", ""),
                request_payload={"messages": [m.model_dump() for m in req.messages],
                                 "temperature": temperature},
                response_payload=resp.model_dump(),
            )
            # Stamp the path on the most-recent usage row
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE sophia_usage SET captured_path = ? WHERE completion_id = ?",
                    (str(captured_path) if captured_path else None, completion_id),
                )
        return JSONResponse(content=resp.model_dump())

    # ── usage reporting (admin + collection only) ────────────────────────

    @app.get("/v1/usage/summary")
    async def usage_summary(
        window: Optional[str] = Query(default=None, description="today|7d|all (default all)"),
        rec=Depends(_cap_dep("usage_read")),
    ):
        log = UsageLog(db)
        if window == "today":
            from datetime import datetime, timezone, timedelta
            since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            return {"window": "today", "by_requestor": log.per_requestor(since=since),
                    "by_provider": log.per_provider(since=since),
                    "by_model": log.per_model(since=since)}
        if window == "7d":
            from datetime import datetime, timezone, timedelta
            since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            return {"window": "7d", "by_requestor": log.per_requestor(since=since),
                    "by_provider": log.per_provider(since=since),
                    "by_model": log.per_model(since=since)}
        return {"window": "all", "by_requestor": log.per_requestor(),
                "by_provider": log.per_provider(), "by_model": log.per_model()}

    @app.get("/v1/usage/by_requestor")
    async def usage_by_requestor(
        name: str = Query(...),
        limit: int = Query(default=100, le=500),
        rec=Depends(_cap_dep("usage_read")),
    ):
        return {"requestor": name, "rows": UsageLog(db).for_requestor(name, limit)}

    @app.get("/v1/usage/errors")
    async def usage_errors(
        limit: int = Query(default=50, le=200),
        rec=Depends(_cap_dep("usage_read")),
    ):
        return {"errors": UsageLog(db).recent_errors(limit)}

    @app.get("/v1/captures")
    async def captures_list(
        requestor: Optional[str] = Query(default=None),
        rec=Depends(_cap_dep("usage_read")),
    ):
        """List captures metadata (paths + sizes), not the bodies. Body
        fetch is left to direct file access (path returned in the row)."""
        return {"stats": captures.stats(), "items": captures.list(requestor=requestor)}

    # ── helpers ───────────────────────────────────────────────────────────

    def _record(
        req: ChatCompletionRequest,
        requestor,
        completion_id: str,
        model,
        strategy: str,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        elapsed_ms: int,
        status: str,
        error_kind: Optional[str],
        provider_meta: Optional[dict] = None,
        in_usd: float = 0.0,
        out_usd: float = 0.0,
        total_usd: float = 0.0,
    ):
        """Append one row to sophia_usage. We always know the requestor
        and the model/strategy on the wire — those are recorded even
        when the upstream didn't surface its identity. Provider name +
        token counts + USD cost are best-effort: when the pool returns
        metadata we record it, otherwise we leave the columns zero and
        the operator sees the gap in /v1/usage/summary. Better a missing
        cost number than a made-up one."""
        pm = provider_meta or {}
        # ``provider_name`` from the meta is the ladyol account key
        # (e.g. ``anthropic_alice``); ``provider_account`` is the
        # alias the operator wrote in providers.yaml — same thing
        # today, but the split keeps the door open for one provider
        # having N aliases later (multi-account rotation).
        provider_name = pm.get("provider_name") or ""
        provider_account = pm.get("account_alias") or provider_name or ""
        usage.record(UsageRow(
            requestor=requestor.name,
            provider_account=provider_account,
            provider_name=provider_name,
            model=model or req.model or "auto",
            strategy=strategy,
            completion_id=completion_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            cost_input_usd=in_usd, cost_output_usd=out_usd, cost_total_usd=total_usd,
            status=status, error_kind=error_kind,
            elapsed_ms=elapsed_ms,
        ))

    def _token_counts(prompt: str, completion: str, provider_meta: dict) -> tuple[int, int]:
        """Best-effort token counts. The provider's API returns real
        usage when available; we only proxy via word-counts for the
        captured payloads. The model pool's ``chat_with_meta`` returns
        real counts in provider_meta["usage"] when the upstream sends them."""
        usage = provider_meta.get("usage") or {}
        pt = int(usage.get("prompt_tokens") or 0)
        ct = int(usage.get("completion_tokens") or 0)
        if pt or ct:
            return pt, ct
        # Fallback: ~1 token per 0.75 words. The accounting row is
        # self-correcting on the next request — providers that send
        # real usage will overwrite our estimate.
        return int(len(prompt.split()) * 1.3), int(len(completion.split()) * 1.3)

    return app


# Module-level app for live-server tests + simple ``uvicorn sophia.server:app``.
# Live tests can override the env BEFORE importing this module if they
# need a different SOPHIA_DB.
try:
    app = create_app()
except Exception:  # pragma: no cover — keep import tolerant for partial envs
    app = None
