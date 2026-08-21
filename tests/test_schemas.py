# tests/test_schemas.py
from sophia.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    Choice,
    Message,
    ModelList,
)


def test_minimal_request_parses():
    req = ChatCompletionRequest(model="auto", messages=[Message(role="user", content="hi")])
    assert req.model == "auto"
    assert req.stream is False
    assert req.temperature is None


def test_request_accepts_concrete_model_name():
    # Operator rule: model can be "auto" (let strategy pick) or a specific name (force that model).
    req = ChatCompletionRequest(
        model="claude-3-sonnet", messages=[Message(role="user", content="hi")]
    )
    assert req.model == "claude-3-sonnet"


def test_request_accepts_strategy_body_field():
    # Sophia extension: an optional `strategy` body field.
    # OpenAI clients don't send it; it stays None by default.
    req_with = ChatCompletionRequest(
        model="auto", messages=[Message(role="user", content="hi")], strategy="cheapest"
    )
    assert req_with.strategy == "cheapest"

    req_default = ChatCompletionRequest(
        model="auto", messages=[Message(role="user", content="hi")]
    )
    assert req_default.strategy is None


def test_response_shape_matches_openai():
    resp = ChatCompletionResponse(
        id="chatcmpl-x",
        model="minimax-m3",
        choices=[Choice(index=0, message=Message(role="assistant", content="hello"), finish_reason="stop")],
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    )
    d = resp.model_dump()
    assert d["object"] == "chat.completion"
    assert d["choices"][0]["message"]["role"] == "assistant"


def test_model_list_shape():
    ml = ModelList(data=[])
    assert ml.object == "list"
