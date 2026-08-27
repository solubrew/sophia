# sophia/schemas.py
"""OpenAI Chat Completions request/response shapes.

We mirror the public OpenAI Chat Completions API. The only addition is an
optional ``strategy`` field on the request — that's the knob for picking a
provider-pool selection strategy. OpenAI-compatible clients ignore it.
"""
from __future__ import annotations

from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class Message(BaseModel):
    model_config = ConfigDict(extra="allow")
    role: str
    content: Optional[str] = None
    name: Optional[str] = None
    # T-NEW-119 (2026-08-27): ``tool_call_id`` is on the ``tool`` role
    # message per the OpenAI Chat Completions spec — the upstream
    # uses it to correlate tool results back to the originating
    # assistant tool_call. Without this field the schema silently
    # dropped tool messages (the role made it through as
    # ``"tool"`` but the correlation id vanished).
    tool_call_id: Optional[str] = None
    # OpenAI tool-calls array. Synthesized on the server side when the
    # upstream model emitted leaked tool-call markup as text — see
    # sophia.leaked_tool_call_fix for the wire format. Also present on
    # assistant turns where the model produced structured tool calls.
    tool_calls: Optional[List[Any]] = None


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    # "sophia-auto" (or empty) = let Sophia's backend pick the model
    # via the configured rotation strategy; anything else is treated
    # as a literal model id handed to the pool.
    model: str = "sophia-auto"
    messages: List[Message]
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    n: Optional[int] = 1
    stream: bool = False
    stop: Optional[Any] = None
    max_tokens: Optional[int] = None
    presence_penalty: Optional[float] = None
    frequency_penalty: Optional[float] = None
    user: Optional[str] = None
    # OpenAI tool definitions.  Forwarded to the upstream so the model
    # sees which tools it can call (and what their argument schemas
    # are).  Prior to this field being added (2026-08-24), sophia
    # silently dropped the caller's `tools` field, so upstream models
    # saw text-only requests and hallucinated tool calls in non-spec
    # formats.  See CHANGES.md af7e8b2.
    tools: Optional[List[Any]] = None
    tool_choice: Optional[Any] = None
    # Sophia extension — not part of OpenAI, ignored by stock clients.
    strategy: Optional[str] = None


class Choice(BaseModel):
    index: int
    message: Message
    finish_reason: str = "stop"


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int = 0
    model: str
    choices: List[Choice]
    usage: Usage = Field(default_factory=Usage)


class ModelCard(BaseModel):
    id: str
    object: str = "model"
    created: int = 0
    owned_by: str = "sophia"


class ModelList(BaseModel):
    object: str = "list"
    data: List[ModelCard]
