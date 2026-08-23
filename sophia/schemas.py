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
    content: str
    name: Optional[str] = None


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
