"""Tests for sophia.leaked_tool_call_fix — the server-side recovery that
converts upstream `[TOOL_CALL]…[/TOOL_CALL]` markup (minimax-m2.7 emits
this when it tries to use a tool but forgets the OpenAI `tool_calls`
wire format) into a real `tool_calls` array entry on the OpenAI Chat
Completions response.

Reproduction of the live arthr bug (2026-08-23 23:43 turn):
    Upstream:    minimax-m2.7 (via sophia gateway, strategy=best_available)
    Sent:        content="I'll start by loading the relevant skills...\n
                  [TOOL_CALL]\n{tool => \"skill_view\", args => {\n  --name \"x\"\n}}\n\n
                  [/TOOL_CALL]..."
    Expected:    a real tool_calls array (so hermes runs the tool)
    Actual:      the raw markup landed in chat as text

The fix: sophia parses the markup before responding. These tests
exercise the parser directly + via the live FastAPI app to confirm
the wire format change.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sophia.leaked_tool_call_fix import (
    extract_leaked_tool_calls,
    split_streaming_chunk,
)


# ── Direct parser tests ─────────────────────────────────────────────────


def test_extracts_single_skill_view_call():
    """The exact `skill_view` block emitted by minimax-m2.7 for the
    2026-08-23 arthr session."""
    text = (
        'I will load the relevant skill.\n'
        '[TOOL_CALL]\n'
        '{tool => "skill_view", args => {\n'
        '  --name "production-readiness-review"\n'
        '}}\n'
        '[/TOOL_CALL]'
    )
    tool_calls, cleaned = extract_leaked_tool_calls(text)
    assert len(tool_calls) == 1
    tc = tool_calls[0]
    assert tc["type"] == "function"
    assert tc["function"]["name"] == "skill_view"
    args = json.loads(tc["function"]["arguments"])
    assert args == {"name": "production-readiness-review"}
    assert tc["id"].startswith("leaked_")
    assert cleaned == "I will load the relevant skill."


def test_extracts_multiple_calls_in_order():
    """3 sequential leaked blocks — the real 23:43 turn shape."""
    text = (
        'I will load the relevant skills and explore.\n'
        '[TOOL_CALL]\n'
        '{tool => "skill_view", args => {\n'
        '  --name "production-readiness-review"\n'
        '}}\n'
        '[/TOOL_CALL]\n'
        '[TOOL_CALL]\n'
        '{tool => "skill_view", args => {\n'
        '  --name "sasquatch-falcon-integration"\n'
        '}}\n'
        '[/TOOL_CALL]\n'
        '[TOOL_CALL]\n'
        '{tool => "bash", args => {\n'
        '  --cmd "ls /home/solubrew/.hermes/projects/shieldface/"\n'
        '  --cwd "/home/solubrew"\n'
        '  --explanation "List project contents"\n'
        '  --max_duration 10\n'
        '}}\n'
        '[/TOOL_CALL]'
    )
    tool_calls, cleaned = extract_leaked_tool_calls(text)
    assert [tc["function"]["name"] for tc in tool_calls] == [
        "skill_view", "skill_view", "bash"
    ]
    assert json.loads(tool_calls[0]["function"]["arguments"]) == {
        "name": "production-readiness-review"
    }
    assert json.loads(tool_calls[2]["function"]["arguments"]) == {
        "cmd": "ls /home/solubrew/.hermes/projects/shieldface/",
        "cwd": "/home/solubrew",
        "explanation": "List project contents",
        "max_duration": 10,
    }
    assert "I will load the relevant skills" in cleaned


def test_extracts_read_tool_with_array_arg():
    """The original "read" call from the user's first chat."""
    text = (
        'On it. Let me explore the project.\n'
        '[TOOL_CALL]\n'
        '{tool => "read", args => {\n'
        '  --paths ["/home/solubrew/.hermes/projects/shieldface"]\n'
        '  --filter "*"\n'
        '  --include_hidden false\n'
        '}}\n'
        '[/TOOL_CALL]'
    )
    tool_calls, cleaned = extract_leaked_tool_calls(text)
    assert len(tool_calls) == 1
    args = json.loads(tool_calls[0]["function"]["arguments"])
    assert args["paths"] == ["/home/solubrew/.hermes/projects/shieldface"]
    assert args["filter"] == "*"
    assert args["include_hidden"] is False


def test_no_leak_returns_content_unchanged():
    """Pure-text reply — nothing to extract."""
    text = "Just a normal answer."
    tool_calls, cleaned = extract_leaked_tool_calls(text)
    assert tool_calls == []
    assert cleaned == text


def test_no_tool_call_marker_returns_unchanged():
    """Content with `[TOOL]` (one word) shouldn't trigger a false parse."""
    text = "Use the [TOOL] tool to run a search."
    tool_calls, cleaned = extract_leaked_tool_calls(text)
    assert tool_calls == []
    assert cleaned == text


def test_malformed_block_kept_in_content():
    """If a block doesn't parse, the original text is preserved so the
    user sees *something* rather than a silently dropped call. The
    model will see the parse error on the next turn."""
    text = (
        '[TOOL_CALL]\n'
        '{tool => "broken", args => {\n'
        '  --malformed-no-value\n'
        '}}\n'
        '[/TOOL_CALL]'
    )
    tool_calls, cleaned = extract_leaked_tool_calls(text)
    # Malformed block → left in cleaned text
    assert tool_calls == []
    assert "broken" in cleaned and "malformed-no-value" in cleaned


def test_partial_malformed_with_valid_neighbors():
    """A malformed block between two valid ones — the valid ones are
    extracted, the malformed one is kept in cleaned text."""
    text = (
        '[TOOL_CALL]\n'
        '{tool => "good_a", args => {\n'
        '  --a 1\n'
        '}}\n'
        '[/TOOL_CALL]\n'
        '[TOOL_CALL]\n'
        '{tool => "broken", args => {\n'
        '  --malformed-no-value\n'
        '}}\n'
        '[/TOOL_CALL]\n'
        '[TOOL_CALL]\n'
        '{tool => "good_b", args => {\n'
        '  --b 2\n'
        '}}\n'
        '[/TOOL_CALL]'
    )
    tool_calls, cleaned = extract_leaked_tool_calls(text)
    # Two valid ones extracted
    assert len(tool_calls) == 2
    assert tool_calls[0]["function"]["name"] == "good_a"
    assert tool_calls[1]["function"]["name"] == "good_b"
    # Malformed one preserved in cleaned text
    assert "broken" in cleaned


def test_array_with_nested_string():
    """`--paths ["a", "b", "c with spaces"]` should parse the array
    correctly, including strings with embedded spaces."""
    text = (
        '[TOOL_CALL]\n'
        '{tool => "read", args => {\n'
        '  --paths ["/a", "/b c", "/d"]\n'
        '}}\n'
        '[/TOOL_CALL]'
    )
    tool_calls, _ = extract_leaked_tool_calls(text)
    args = json.loads(tool_calls[0]["function"]["arguments"])
    assert args["paths"] == ["/a", "/b c", "/d"]


def test_numeric_and_boolean_args():
    """Numbers, booleans, null — all primitive types."""
    text = (
        '[TOOL_CALL]\n'
        '{tool => "x", args => {\n'
        '  --i 42\n'
        '  --f 3.14\n'
        '  --b true\n'
        '  --n null\n'
        '}}\n'
        '[/TOOL_CALL]'
    )
    tool_calls, _ = extract_leaked_tool_calls(text)
    args = json.loads(tool_calls[0]["function"]["arguments"])
    assert args == {"i": 42, "f": 3.14, "b": True, "n": None}


# ── Streaming-helper tests ──────────────────────────────────────────────


def test_split_streaming_chunk_swaps_argument_order():
    """The streaming wrapper returns (text, tools); the core function
    returns (tools, text). The streaming callers want text first."""
    delta = (
        'Some prose.\n'
        '[TOOL_CALL]\n'
        '{tool => "x", args => {\n'
        '  --a 1\n'
        '}}\n'
        '[/TOOL_CALL]'
    )
    text, tcs = split_streaming_chunk(delta)
    assert text == "Some prose."
    assert len(tcs) == 1
    assert tcs[0]["function"]["name"] == "x"


def test_split_streaming_chunk_handles_plain_text():
    """Streaming path must handle plain text without leaking anything."""
    text, tcs = split_streaming_chunk("Just prose, no tools.")
    assert text == "Just prose, no tools."
    assert tcs == []


# ── XML / angle-bracket syntax (Variant B) ──────────────────────────────


def test_extracts_xml_minimax_tool_call_block():
    """The newer variant the same upstream model emitted during the
    second reproduction (00:14 turn): `<minimax:tool_call>` wrapper
    with one `<tool_name attr=...>` per tool call inside."""
    text = (
        'Let me load skills and explore.\n'
        '<minimax:tool_call>\n'
        '<skill_view name="sasquatch-falcon-integration">\n'
        '</skill_view>\n'
        '<skill_view name="production-readiness-review">\n'
        '</skill_view>\n'
        '</minimax:tool_call>'
    )
    tool_calls, cleaned = extract_leaked_tool_calls(text)
    assert len(tool_calls) == 2
    assert tool_calls[0]["function"]["name"] == "skill_view"
    assert json.loads(tool_calls[0]["function"]["arguments"]) == {
        "name": "sasquatch-falcon-integration"
    }
    assert json.loads(tool_calls[1]["function"]["arguments"]) == {
        "name": "production-readiness-review"
    }
    assert "Let me load skills" in cleaned


def test_extracts_xml_with_multiple_attrs_per_tag():
    """A single tag with multiple attrs becomes one tool_call with
    multiple kwargs (the real bash call from the second reproduction)."""
    text = (
        '<minimax:tool_call>\n'
        '<shell、干 pwd="x" cmd="ls /home/solubrew/.hermes/projects/shieldface/">\n'
        '</shell、干>\n'
        '</minimax:tool_call>'
    )
    tool_calls, _ = extract_leaked_tool_calls(text)
    assert len(tool_calls) == 1
    assert tool_calls[0]["function"]["name"] == "shell、干"
    args = json.loads(tool_calls[0]["function"]["arguments"])
    assert args["cmd"] == "ls /home/solubrew/.hermes/projects/shieldface/"
    assert args["pwd"] == "x"


def test_xml_self_closing_tag_emits_sentinel():
    """A tag without attributes becomes a tool_call with `{"_empty_args": True}`
    so the model at least gets a structured retry surface."""
    text = '<minimax:tool_call><some_tool/></minimax:tool_call>'
    tool_calls, _ = extract_leaked_tool_calls(text)
    assert len(tool_calls) == 1
    args = json.loads(tool_calls[0]["function"]["arguments"])
    assert args == {"_empty_args": True}


def test_extracts_claude_style_invoke_with_parameter_tags():
    """Variant C — Claude XML: <invoke name="bash"><parameter
    name="command">x</parameter></invoke>. The tool name comes from
    the `name=` attr on `<invoke>`; the args come from sibling
    `<parameter>` elements inside the same `<invoke>` block."""
    text = (
        '<minimax:tool_call>\n'
        '<invoke name="bash">\n'
        '<parameter name="description">Explore project structure</parameter>\n'
        '<parameter name="command">ls /home/solubrew/.hermes/projects/shieldface/</parameter>\n'
        '</invoke>\n'
        '</minimax:tool_call>'
    )
    tool_calls, _ = extract_leaked_tool_calls(text)
    assert len(tool_calls) == 1
    assert tool_calls[0]["function"]["name"] == "bash"
    args = json.loads(tool_calls[0]["function"]["arguments"])
    assert args["command"] == "ls /home/solubrew/.hermes/projects/shieldface/"
    assert args["description"] == "Explore project structure"


def test_extracts_mixed_v2_and_v3_in_one_response():
    """The same response containing both Variant B (angle-bracket) and
    Variant C (Claude XML) tags should recover all of them."""
    text = (
        '<minimax:tool_call>\n'
        '<skill_view name="production-readiness-review">\n'
        '</skill_view>\n'
        '<invoke name="bash">\n'
        '<parameter name="command">ls /foo</parameter>\n'
        '</invoke>\n'
        '</minimax:tool_call>'
    )
    tool_calls, _ = extract_leaked_tool_calls(text)
    assert len(tool_calls) == 2
    assert tool_calls[0]["function"]["name"] == "skill_view"
    assert json.loads(tool_calls[0]["function"]["arguments"]) == {
        "name": "production-readiness-review"
    }
    assert tool_calls[1]["function"]["name"] == "bash"
    assert json.loads(tool_calls[1]["function"]["arguments"]) == {
        "command": "ls /foo"
    }


def test_xml_wrapper_tags_are_skipped():
    """The `<minimax:tool_call>` wrapper tag itself shouldn't become a
    tool_call — only the inner per-tool tags should."""
    text = (
        '<minimax:tool_call>\n'
        '<inner_tool foo="bar">\n'
        '</inner_tool>\n'
        '</minimax:tool_call>'
    )
    tool_calls, _ = extract_leaked_tool_calls(text)
    # Only one tool_call — from `<inner_tool foo="bar">`, not from
    # `<minimax:tool_call>` wrapper.
    assert len(tool_calls) == 1
    assert tool_calls[0]["function"]["name"] == "inner_tool"


def test_mixed_variants_in_one_response():
    """A response that contains both Variants A and B should recover
    both blocks (covers the case where the upstream model switches
    formats mid-message)."""
    text = (
        '[TOOL_CALL]\n'
        '{tool => "skill_view", args => {\n'
        '  --name "block-syntax"\n'
        '}}\n'
        '[/TOOL_CALL]\n'
        '<minimax:tool_call>\n'
        '<skill_view name="xml-syntax">\n'
        '</skill_view>\n'
        '</minimax:tool_call>'
    )
    tool_calls, _ = extract_leaked_tool_calls(text)
    assert len(tool_calls) == 2
    assert json.loads(tool_calls[0]["function"]["arguments"]) == {"name": "block-syntax"}
    assert json.loads(tool_calls[1]["function"]["arguments"]) == {"name": "xml-syntax"}


# ── End-to-end live verification ─────────────────────────────────────────
# The unit tests above cover the parser + wire-format transformation.
# End-to-end verification (sophia running, hermes → sophia → upstream,
# response is a real tool_calls array) is done by rebuilding the sophia
# image and running the original failing query through the arthr
# gateway; see scripts/build-sophia.sh and the live-state probe at
# /home/solubrew/.hermes/profiles/arthr/state.db (turn at 23:43
# pre-fix vs the post-fix run should differ in tool_calls column).