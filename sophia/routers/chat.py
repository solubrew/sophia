"""Chat router — POST /v1/chat/completions.

The heavyweight route: streaming + non-streaming paths, structured
and leaked-tool-call recovery, quota/oversize error shapes, usage
recording. Lives on its own router because it carries the most
state and the most OpenAI-shape fidelity requirements.

Splits out of the original ``create_app`` monolith (card sophia@next,
2026-09-11, web_blueprint_structure fix). The route body is preserved
verbatim — the refactor is structural, not behavioural. Helpers
``_record`` / ``_token_counts`` moved here because they're chat-only.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..captures import CaptureStore
from ..leaked_tool_call_fix import extract_leaked_tool_calls
from ..pool import PoolQuotaExhausted, RequestTooLarge, SophiaPool, new_completion_id, now_ts
from ..schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    Choice,
    Message,
    Usage,
)
from ..usage import UsageLog, UsageRow
from ..util import cost_for_model, quota_exhausted_response, request_too_large_response, retry_after_for
from ._helpers import build_router

log = logging.getLogger(__name__)


def register(app, cap_dep, router=None) -> None:
    r = router or build_router()

    def _record(
        req: ChatCompletionRequest,
        requestor,
        completion_id: str,
        model,
        strategy: str,
        *,
        usage_log: UsageLog,
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
        #
        # NB: ladyol's ``Provider`` is a dataclass with a ``.name``
        # string attribute — historically the pool stashed the raw
        # Provider object here, not the string. Coerce to str so the
        # UsageRow tuple stays sqlite-bindable.
        raw_provider_name = pm.get("provider_name") or ""
        if not isinstance(raw_provider_name, str):
            raw_provider_name = getattr(raw_provider_name, "name", str(raw_provider_name))
        provider_name = raw_provider_name or ""
        provider_account = pm.get("account_alias") or provider_name or ""
        # Same coercion for account_alias in case it's also a Provider
        if not isinstance(provider_account, str):
            provider_account = getattr(provider_account, "name", str(provider_account))
        usage_log.record(UsageRow(
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
        real counts in provider_meta[\"usage\"] when the upstream sends them."""
        u = provider_meta.get("usage") or {}
        pt = int(u.get("prompt_tokens") or 0)
        ct = int(u.get("completion_tokens") or 0)
        if pt or ct:
            return pt, ct
        # Fallback: ~1 token per 0.75 words. The accounting row is
        # self-correcting on the next request — providers that send
        # real usage will overwrite our estimate.
        return int(len(prompt.split()) * 1.3), int(len(completion.split()) * 1.3)

    @r.post("/v1/chat/completions")
    async def chat_completions(
        request: Request,
        req: ChatCompletionRequest,
        rec=Depends(cap_dep("chat")),
    ):
        pool: SophiaPool = request.app.state.pool
        db = request.app.state.db
        captures: CaptureStore = request.app.state.captures
        usage_log: UsageLog = request.app.state.usage

        requestor_name = rec.name
        requestor = rec
        assert requestor is not None  # _require_cap just succeeded

        if not req.messages:
            raise HTTPException(status_code=400, detail="messages must not be empty")

        # T-NEW-119 (2026-08-27): forward the FULL OpenAI-style
        # conversation history instead of flattening to a 2-message
        # ``(system, user)`` pair. Each role is preserved verbatim.
        legacy_user_parts = [m.content for m in req.messages if m.role != "system" and m.content]
        sprompt = "\n".join(
            m.content for m in req.messages if m.role == "system" and m.content
        ).strip()
        prompt = "\n".join(legacy_user_parts).strip()
        if not prompt and not any(m.role != "system" for m in req.messages):
            raise HTTPException(status_code=400, detail="no user message in messages")

        strategy = req.strategy or "random"
        temperature = req.temperature if req.temperature is not None else 0.7
        # Forward max_tokens from the caller's request. Operators can
        # override the default via SOPHIA_DEFAULT_MAX_TOKENS if their
        # model needs more than 4096.
        max_tokens = req.max_tokens if req.max_tokens is not None else int(
            os.environ.get("SOPHIA_DEFAULT_MAX_TOKENS", "4096")
        )
        model = (
            None
            if req.model in ("auto", "sophia-auto", "")
            else req.model
        )
        completion_id = new_completion_id()
        t0 = time.monotonic()

        forward_messages: List[Dict[str, Any]] = []
        for m in req.messages:
            msg_dict: Dict[str, Any] = {"role": m.role}
            if m.content is not None:
                msg_dict["content"] = m.content
            if m.tool_calls:
                msg_dict["tool_calls"] = list(m.tool_calls)
            if m.name:
                msg_dict["name"] = m.name
            if m.tool_call_id:
                msg_dict["tool_call_id"] = m.tool_call_id
            forward_messages.append(msg_dict)

        if req.stream:
            async def event_source():
                chunks: list[str] = []
                leaked_tool_calls_emitted: list[dict] = []
                structured_tool_calls_emitted: list[dict] = []
                try:
                    async for chunk in pool.stream(
                        messages=forward_messages,
                        sprompt=sprompt,
                        temperature=temperature,
                        strategy=strategy, model=model,
                        max_tokens=max_tokens,
                        tools=req.tools,
                    ):
                        if isinstance(chunk, str):
                            chunks.append(chunk)
                            if not chunk:
                                continue
                            chunk_tcs, chunk_cleaned = extract_leaked_tool_calls(chunk)
                            if chunk_tcs:
                                leaked_tool_calls_emitted.extend(chunk_tcs)
                                tc_payload = {
                                    "id": completion_id,
                                    "object": "chat.completion.chunk",
                                    "created": now_ts(),
                                    "model": req.model,
                                    "choices": [{
                                        "index": 0,
                                        "delta": {"tool_calls": chunk_tcs},
                                        "finish_reason": None,
                                    }],
                                }
                                yield f"data: {json.dumps(tc_payload)}\n\n".encode("utf-8")
                            if chunk_cleaned:
                                payload = {
                                    "id": completion_id,
                                    "object": "chat.completion.chunk",
                                    "created": now_ts(),
                                    "model": req.model,
                                    "choices": [{"index": 0, "delta": {"content": chunk_cleaned}, "finish_reason": None}],
                                }
                                yield f"data: {json.dumps(payload)}\n\n".encode("utf-8")
                        elif isinstance(chunk, dict):
                            payload = {
                                "id": completion_id,
                                "object": "chat.completion.chunk",
                                "created": now_ts(),
                                "model": req.model,
                                "choices": [dict(chunk)],
                            }
                            for tc in (chunk.get("delta", {}).get("tool_calls") or []):
                                if isinstance(tc, dict):
                                    structured_tool_calls_emitted.append(tc)
                            yield f"data: {json.dumps(payload)}\n\n".encode("utf-8")
                    has_tool_calls = bool(
                        structured_tool_calls_emitted or leaked_tool_calls_emitted
                    )
                    final_finish_reason = "tool_calls" if has_tool_calls else "stop"
                    stop_payload = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": now_ts(),
                        "model": req.model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": final_finish_reason}],
                    }
                    yield f"data: {json.dumps(stop_payload)}\n\n".encode("utf-8")
                    yield b"data: [DONE]\n\n"
                    elapsed_ms = int((time.monotonic() - t0) * 1000)
                    full = "".join(chunks)
                    su = getattr(pool, "last_stream_usage", {}) or {}
                    pt = int(su.get("tokens_in") or su.get("prompt_tokens") or len(prompt.split()))
                    ct = int(su.get("tokens_out") or su.get("completion_tokens") or len(full.split()))
                    _record(req, requestor, completion_id, model, strategy,
                            usage_log=usage_log,
                            prompt_tokens=pt, completion_tokens=ct,
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
                    log.exception("[sophia] streaming error: %s", e)
                    elapsed_ms = int((time.monotonic() - t0) * 1000)
                    _record(req, requestor, completion_id, model, strategy,
                            usage_log=usage_log,
                            prompt_tokens=0, completion_tokens=0, elapsed_ms=elapsed_ms,
                            status="error", error_kind=type(e).__name__)
                    if isinstance(e, PoolQuotaExhausted):
                        retry_after = retry_after_for(e)
                        err = {
                            "error": {
                                "message": str(e),
                                "type": "rate_limit_error",
                                "code": "pool_quota_exhausted",
                                "paused_accounts": e.paused_accounts,
                                "earliest_unpause_utc": (
                                    e.earliest_unpause_utc.isoformat()
                                    if e.earliest_unpause_utc is not None
                                    else None
                                ),
                                "retry_after_seconds": retry_after,
                            }
                        }
                        yield f"retry: {retry_after * 1000}\n".encode("utf-8")
                    elif isinstance(e, RequestTooLarge):
                        err = {
                            "error": {
                                "message": str(e),
                                "type": "request_too_large",
                                "code": "request_too_large",
                                "estimated_tokens": e.estimated_tokens,
                                "smallest_fittable_window": e.smallest_fittable_window,
                                "largest_fittable_window": e.largest_fittable_window,
                                "eligible_count": e.eligible_count,
                                "compact_required": e.eligible_count == 0,
                            }
                        }
                    else:
                        err = {"error": {"message": str(e), "type": "server_error"}}
                    yield f"data: {json.dumps(err)}\n\n".encode("utf-8")
            return StreamingResponse(event_source(), media_type="text/event-stream")

        # Non-streaming
        try:
            content, provider_meta = await pool.chat_with_meta(
                messages=forward_messages,
                sprompt=sprompt,
                temperature=temperature,
                strategy=strategy, model=model,
                max_tokens=max_tokens,
                tools=req.tools,
            )
        except Exception as e:
            log.exception("[sophia] chat error: %s", e)
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            _record(req, requestor, completion_id, model, strategy,
                    usage_log=usage_log,
                    prompt_tokens=0, completion_tokens=0, elapsed_ms=elapsed_ms,
                    status="error", error_kind=type(e).__name__)
            if isinstance(e, PoolQuotaExhausted):
                return quota_exhausted_response(e)
            if isinstance(e, RequestTooLarge):
                return request_too_large_response(e)
            raise HTTPException(status_code=502, detail=str(e)) from e

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        pt, ct = _token_counts(prompt, content or "", provider_meta)
        in_usd, out_usd, total_usd = cost_for_model(provider_meta, pt, ct)
        _record(req, requestor, completion_id, model, strategy,
                usage_log=usage_log,
                prompt_tokens=pt, completion_tokens=ct, elapsed_ms=elapsed_ms,
                status="ok", error_kind=None,
                provider_meta=provider_meta, in_usd=in_usd, out_usd=out_usd,
                total_usd=total_usd)

        structured_tool_calls = (provider_meta or {}).get("tool_calls") or []
        leaked_tool_calls, cleaned_content = extract_leaked_tool_calls(content or "")
        tool_calls = structured_tool_calls or leaked_tool_calls
        non_streaming_finish_reason = "tool_calls" if tool_calls else "stop"
        resp = ChatCompletionResponse(
            id=completion_id, created=now_ts(),
            model=(provider_meta or {}).get("model") or req.model,
            choices=[Choice(
                index=0,
                finish_reason=non_streaming_finish_reason,
                message=Message(
                    role="assistant",
                    content=cleaned_content or None,
                    tool_calls=tool_calls or None,
                ),
            )],
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
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE sophia_usage SET captured_path = ? WHERE completion_id = ?",
                    (str(captured_path) if captured_path else None, completion_id),
                )
        return JSONResponse(content=resp.model_dump())

    if router is None:
        app.include_router(r)
