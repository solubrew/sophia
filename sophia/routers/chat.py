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

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..captures import CaptureStore
from ..leaked_tool_call_fix import extract_leaked_tool_calls
from ..pool import (
    AllProvidersUnreachable,
    PoolQuotaExhausted,
    RequestTooLarge,
    SophiaPool,
    _RECOVERY_BUDGET_SECONDS,
    new_completion_id,
    now_ts,
)
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

    # ── recovery helpers (NEW 2026-09-12, operator directive) ───
    #
    # Three error classes get DIFFERENT handling from this router:
    #
    #   PoolQuotaExhausted — at least one 429 in the rotation; every
    #     account paused by quota. Surface as a REAL 429 with
    #     Retry-After (operator-facing signal "all providers exhausted").
    #
    #   AllProvidersUnreachable — no 429s, but every account cycled
    #     failed transiently (empty stream / 5xx / network). Surface
    #     as a REAL 502 (operator-facing signal "providers can't be
    #     reached"). Per operator: both errors must reach hermes so
    #     the operator can review them in the audit log.
    #
    #   RequestTooLarge — context exceeded the SMALLEST fittable
    #     window. Per operator: auto-trim from the oldest messages
    #     until the conversation fits the smallest available window,
    #     then retry. We never bubble 413 to the user unless even the
    #     bare minimum (current message + system prompt) won't fit.
    #
    # Transient failures (empty stream / 5xx / network on SOME
    # accounts but not ALL) are absorbed by the pool's internal
    # rotation + recovery budget — hermes never sees them.

    def _estimated_input_tokens(messages: List[Dict[str, Any]]) -> int:
        """Cheap word-count token estimate for compaction math.

        Mirrors sophia.pool._estimate_input_tokens (kept local so the
        router can decide "trim from the oldest" without round-tripping
        to the pool). Word count × 1.3 ≈ tokens for English text;
        good enough to decide whether a trim round is needed.
        """
        text = " ".join(str(m.get("content") or "") for m in messages)
        return int(len(text.split()) * 1.3) + 8  # +8 for role markers

    def _trim_oldest(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Drop the oldest non-system message; keep system + current.

        Operator directive 2026-09-12: trim from the OLDEST end.
        Preserves the system prompt (oldest usually) and the current
        user message (newest). Mid-conversation is what gets trimmed.
        If only system + current remain, returns them as-is and the
        caller surfaces a clear error.
        """
        if len(messages) <= 2:
            return list(messages)
        return list(messages[:1]) + list(messages[2:])

    async def _auto_compact_and_retry(
        pool: SophiaPool,
        messages: List[Dict[str, Any]],
        sprompt: str,
        temperature: float,
        strategy: str,
        model: Optional[str],
        max_tokens: int,
        tools: Optional[list],
    ):
        """Loop: trim oldest messages until the pool can serve the
        request, OR we've trimmed down to just system + current and
        it still doesn't fit (then raise RequestTooLarge — the
        single-message case is too big for every available model).

        Returns ``(content, provider_meta)`` on success; raises
        ``RequestTooLarge`` when nothing fits. We never synthesize a
        200 — the operator wants real compaction to succeed OR a
        real error.

        BUGFIX (2026-10-11): this was a plain ``def`` that did
        ``return pool.chat_with_meta(...)`` without ``await`` — the
        coroutine object was created but never executed inside the
        try/except, so the trim loop was dead code and every
        RequestTooLarge bubbled straight out as a 413 even when
        compaction could have served the request. Now ``async def``
        + ``await`` so the trim loop actually runs.
        """
        attempt_messages = list(messages)
        # Bound the compaction loop: don't trim below 2 messages
        # (system + current). Beyond that, no model can help —
        # BUT we still try once at len=2 in case the underlying
        # upstream is more permissive than the pool's
        # RequestTooLarge exception. If it still fails, the outer
        # try/except in the chat router surfaces the 413.
        while True:
            try:
                return await pool.chat_with_meta(
                    messages=attempt_messages,
                    sprompt=sprompt,
                    temperature=temperature,
                    strategy=strategy,
                    model=model,
                    max_tokens=max_tokens,
                    tools=tools,
                )
            except RequestTooLarge as e:
                if len(attempt_messages) <= 2:
                    # Tried at the minimum (system + current) and
                    # still too big. Surface to the router's outer
                    # catch — 413 with the audit detail.
                    raise
                attempt_messages = _trim_oldest(attempt_messages)
                log.info(
                    "[sophia] RequestTooLarge → auto-compacted to %d messages, retrying",
                    len(attempt_messages),
                )

    async def _with_recovery_budget(coro_factory):
        """Wrap an async operation in a recovery-budget retry loop.

        Operator directive 2026-09-12: transient failures (empty
        stream / 5xx / network / broken-model) must NEVER bubble to
        hermes unless the recovery budget elapses. The pool's chat()
        and stream() raise ``AllProvidersUnreachable`` when they cycle
        through every account once; this wrapper retries with
        exponential backoff (1s → 2s → 4s → ... → 30s cap) until the
        wall-clock budget expires or one of:
          * ``PoolQuotaExhausted`` → bubble (real quota wall)
          * ``RequestTooLarge`` → bubble (auto-compact doesn't apply
            because the caller is already in the auto-compact loop
            upstream; this only fires for streaming paths where
            compaction isn't possible because the stream already opened)
        """
        import asyncio as _asyncio

        budget = _RECOVERY_BUDGET_SECONDS
        deadline = time.monotonic() + budget
        attempt = 0
        backoff = 1.0
        while True:
            attempt += 1
            try:
                return await coro_factory()
            except AllProvidersUnreachable:
                elapsed = time.monotonic() - (deadline - budget)
                if elapsed >= budget or time.monotonic() >= deadline:
                    # Bubble — surface the structured audit detail.
                    raise
                log.info(
                    "[sophia] AllProvidersUnreachable (attempt %d, %.1fs "
                    "into budget) — sleeping %.1fs before retry",
                    attempt, elapsed, backoff,
                )
                await _asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

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
                # NEW 2026-09-12: streaming auto-compact + recovery
                # budget. The outer while-loop handles auto-compact
                # (trim oldest on RequestTooLarge BEFORE any chunk
                # yielded); the inner while-loop handles the recovery
                # budget (retry the whole stream on AllProvidersUnreachable
                # BEFORE any chunk yielded, until the budget expires).
                # Both layers must coexist: auto-compact retries a
                # smaller request on the SAME attempt; recovery
                # retries the SAME request after a backoff.
                attempt_messages = list(forward_messages)
                stream_started = False  # becomes True once we yield anything
                recovery_started_at = time.monotonic()
                recovery_backoff = 1.0
                recovery_attempt = 0
                while True:
                    chunks: list[str] = []
                    leaked_tool_calls_emitted: list[dict] = []
                    structured_tool_calls_emitted: list[dict] = []
                    try:
                        async for chunk in pool.stream(
                            messages=attempt_messages,
                            sprompt=sprompt,
                            temperature=temperature,
                            strategy=strategy, model=model,
                            max_tokens=max_tokens,
                            tools=req.tools,
                        ):
                            # Once we yield ANY chunk, we cannot
                            # unwind and retry (the client has seen
                            # partial output). Set the flag here so
                            # the AllProvidersUnreachable catch block
                            # knows to surface the error rather than
                            # retry the whole stream.
                            stream_started = True
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
                        return  # successful stream — done
                    except RequestTooLarge as e:
                        # Compact-and-retry — only valid BEFORE we
                        # yielded anything. Since the pool raises
                        # RequestTooLarge from the pre-flight check
                        # (before any chunks), `chunks` is still [].
                        # If the stream had already produced output,
                        # surface the error rather than corrupting
                        # the SSE stream mid-flight.
                        if chunks:
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
                            yield f"data: {json.dumps(err)}\n\n".encode("utf-8")
                            yield b"data: [DONE]\n\n"
                            return
                        if len(attempt_messages) <= 2:
                            # Nothing left to trim — surface the
                            # error to the stream consumer.
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
                            yield f"data: {json.dumps(err)}\n\n".encode("utf-8")
                            yield b"data: [DONE]\n\n"
                            elapsed_ms = int((time.monotonic() - t0) * 1000)
                            _record(req, requestor, completion_id, model, strategy,
                                    usage_log=usage_log,
                                    prompt_tokens=0, completion_tokens=0,
                                    elapsed_ms=elapsed_ms, status="error",
                                    error_kind="RequestTooLarge")
                            return
                        attempt_messages = _trim_oldest(attempt_messages)
                        log.info(
                            "[sophia] stream RequestTooLarge → auto-compacted to "
                            "%d messages, retrying",
                            len(attempt_messages),
                        )
                        continue
                    except PoolQuotaExhausted as e:
                        log.warning("[sophia] stream pool quota exhausted: %s", e)
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
                        yield f"data: {json.dumps(err)}\n\n".encode("utf-8")
                        yield b"data: [DONE]\n\n"
                        elapsed_ms = int((time.monotonic() - t0) * 1000)
                        _record(req, requestor, completion_id, model, strategy,
                                usage_log=usage_log,
                                prompt_tokens=0, completion_tokens=0,
                                elapsed_ms=elapsed_ms, status="error",
                                error_kind="PoolQuotaExhausted")
                        return
                    except AllProvidersUnreachable as e:
                        # Operator directive 2026-09-12: transient
                        # failures must NOT bubble to hermes unless
                        # the recovery budget elapses. If we haven't
                        # yielded any chunk yet, retry the WHOLE
                        # stream after a backoff; if the budget is
                        # exhausted OR we've already started streaming,
                        # surface the structured audit detail.
                        if stream_started or chunks:
                            log.error(
                                "[sophia] stream all_providers_unreachable "
                                "(partial=%s, attempts=%d, last: %s: %s) "
                                "— surfacing",
                                bool(chunks), e.attempts,
                                e.last_error_kind, e.last_error_msg,
                            )
                            err = {
                                "error": {
                                    "message": str(e),
                                    "type": "server_error",
                                    "code": "all_providers_unreachable",
                                    "attempts": e.attempts,
                                    "elapsed_seconds": e.elapsed_seconds,
                                    "last_error_kind": e.last_error_kind,
                                    "last_error_message": e.last_error_msg[:500],
                                    "excluded_accounts": e.excluded_accounts,
                                }
                            }
                            yield f"data: {json.dumps(err)}\n\n".encode("utf-8")
                            yield b"data: [DONE]\n\n"
                            elapsed_ms = int((time.monotonic() - t0) * 1000)
                            _record(req, requestor, completion_id, model, strategy,
                                    usage_log=usage_log,
                                    prompt_tokens=0, completion_tokens=0,
                                    elapsed_ms=elapsed_ms, status="error",
                                    error_kind="AllProvidersUnreachable")
                            return
                        # No chunks yielded yet — retry the whole
                        # stream after a backoff, but only if the
                        # recovery budget hasn't elapsed.
                        recovery_elapsed = time.monotonic() - recovery_started_at
                        if recovery_elapsed >= _RECOVERY_BUDGET_SECONDS:
                            log.error(
                                "[sophia] stream recovery budget exhausted "
                                "(%.1fs >= %ds, attempts=%d) — surfacing",
                                recovery_elapsed, _RECOVERY_BUDGET_SECONDS,
                                recovery_attempt + 1,
                            )
                            err = {
                                "error": {
                                    "message": str(e),
                                    "type": "server_error",
                                    "code": "all_providers_unreachable",
                                    "attempts": e.attempts,
                                    "elapsed_seconds": recovery_elapsed,
                                    "last_error_kind": e.last_error_kind,
                                    "last_error_message": e.last_error_msg[:500],
                                    "excluded_accounts": e.excluded_accounts,
                                }
                            }
                            yield f"data: {json.dumps(err)}\n\n".encode("utf-8")
                            yield b"data: [DONE]\n\n"
                            elapsed_ms = int((time.monotonic() - t0) * 1000)
                            _record(req, requestor, completion_id, model, strategy,
                                    usage_log=usage_log,
                                    prompt_tokens=0, completion_tokens=0,
                                    elapsed_ms=elapsed_ms, status="error",
                                    error_kind="AllProvidersUnreachable")
                            return
                        recovery_attempt += 1
                        log.info(
                            "[sophia] stream AllProvidersUnreachable "
                            "(attempt %d, %.1fs into budget) — sleeping "
                            "%.1fs before retry",
                            recovery_attempt, recovery_elapsed,
                            recovery_backoff,
                        )
                        await asyncio.sleep(recovery_backoff)
                        recovery_backoff = min(recovery_backoff * 2, 30.0)
                        continue  # retry the while-True auto-compact loop
                    except Exception as e:
                        log.exception("[sophia] streaming error: %s", e)
                        elapsed_ms = int((time.monotonic() - t0) * 1000)
                        _record(req, requestor, completion_id, model, strategy,
                                usage_log=usage_log,
                                prompt_tokens=0, completion_tokens=0, elapsed_ms=elapsed_ms,
                                status="error", error_kind=type(e).__name__)
                        err = {"error": {"message": str(e), "type": "server_error"}}
                        yield f"data: {json.dumps(err)}\n\n".encode("utf-8")
                        yield b"data: [DONE]\n\n"
                        return
            return StreamingResponse(event_source(), media_type="text/event-stream")

        # Non-streaming (NEW 2026-09-12): auto-compact on
        # RequestTooLarge before bubbling — trim oldest messages
        # until the pool can serve the conversation. We never
        # synthesize a 200; either the compaction succeeds and we
        # return real content, or RequestTooLarge survives the loop
        # and we surface it as the operator's "request too large"
        # 413.
        try:
            content, provider_meta = await _with_recovery_budget(
                lambda: _auto_compact_and_retry(
                    pool, forward_messages, sprompt, temperature,
                    strategy, model, max_tokens, req.tools,
                )
            )
        except PoolQuotaExhausted as e:
            log.warning("[sophia] chat pool quota exhausted: %s", e)
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            _record(req, requestor, completion_id, model, strategy,
                    usage_log=usage_log,
                    prompt_tokens=0, completion_tokens=0, elapsed_ms=elapsed_ms,
                    status="error", error_kind="PoolQuotaExhausted")
            return quota_exhausted_response(e)
        except AllProvidersUnreachable as e:
            log.error(
                "[sophia] chat all_providers_unreachable after %d attempts "
                "(last: %s: %s)",
                e.attempts, e.last_error_kind, e.last_error_msg,
            )
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            _record(req, requestor, completion_id, model, strategy,
                    usage_log=usage_log,
                    prompt_tokens=0, completion_tokens=0, elapsed_ms=elapsed_ms,
                    status="error", error_kind="AllProvidersUnreachable")
            # Real error to hermes — operator directive 2026-09-12:
            # "providers can't be reached" must surface. The body
            # carries the audit metadata so it lands in the log
            # collection.
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "all_providers_unreachable",
                    "attempts": e.attempts,
                    "elapsed_seconds": e.elapsed_seconds,
                    "last_error_kind": e.last_error_kind,
                    "last_error_message": e.last_error_msg[:500],
                    "excluded_accounts": e.excluded_accounts,
                },
            ) from e
        except RequestTooLarge as e:
            log.warning("[sophia] chat request too large: %s", e)
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            _record(req, requestor, completion_id, model, strategy,
                    usage_log=usage_log,
                    prompt_tokens=0, completion_tokens=0, elapsed_ms=elapsed_ms,
                    status="error", error_kind="RequestTooLarge")
            return request_too_large_response(e)
        except Exception as e:
            log.exception("[sophia] chat error: %s", e)
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            _record(req, requestor, completion_id, model, strategy,
                    usage_log=usage_log,
                    prompt_tokens=0, completion_tokens=0, elapsed_ms=elapsed_ms,
                    status="error", error_kind=type(e).__name__)
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
