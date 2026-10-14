"""Leaked tool-call text recovery — converts upstream `minimax-m2.7`-style
leaked tool-call markup out of an OpenAI Chat Completions response and
synthesizes a real `tool_calls` array entry.

Why this lives in sophia
========================
Some upstreams (notably `minimax-m2.7` on the LAN gateway) emit tool-call
markup as plain TEXT in the assistant's `content` field instead of using
the structured OpenAI `tool_calls` array. The leaked format is unstable
across runs — observed variants on the same `minimax-m2.7` endpoint
(arthr gateway, 2026-08-23/24):

  Variant A — block syntax (the original 23:43 turn)::

      [TOOL_CALL]
      {tool => "skill_view", args => {
        --name "production-readiness-review"
      }}
      [/TOOL_CALL]

  Variant B — XML/angle-bracket syntax (later turn, same model)::

      <minimax:tool_call>
      <skill_view name="production-readiness-review">
      </skill_view>
      <bash cmd="ls /foo" cwd="/home/user" max_duration="10">
      </bash>
      </minimax:tool_call>

Hermes' display layer wraps anything matching `[TOOL_CALL]…[/TOOL_CALL]`
(or similar) in a box — which makes it *look* like a tool call to the
user — but the underlying data is plain content, so no tool actually
executes.

Sophia is the OpenAI-compatible gateway that sits between upstream and
the client. From hermes' point of view, sophia IS the model provider; if
sophia converts the leaked markup into a real `tool_calls` array on the
way back, hermes sees a normal OpenAI response and runs the tool loop
normally. No client-side change needed.

The original hermes core has an analogous guard on the Codex Responses
transport (`agent/codex_responses_adapter.py:_TOOL_CALL_LEAK_PATTERN`,
regex `to=functions.<name>`); the chat-completions transport doesn't
have an equivalent. Doing the fix on the server side keeps the leak
contained to sophia (we own it) instead of forking hermes-agent.

Wire-format details
===================
- Non-streaming: rewrite the response's `choices[0].message` to add a
  `tool_calls` array (parsed from the leaked text) and strip the leaked
  text from `content`. If leaked text + non-leaked text both exist,
  `content` carries the non-leaked residue.
- Streaming: per-chunk, strip leaked text from `delta.content` and
  inject synthesized `delta.tool_calls` chunks that carry the
  per-tool-call id/name/arguments. The final chunk's `finish_reason`
  flips from `"stop"` to `"tool_calls"` (the OpenAI spec value for a
  model turn that ended with tool calls pending execution).
- Hermes' tool executor treats missing/malformed `arguments` as a
  crash, so we validate JSON and fall back to `{}` if parsing fails
  (matches hermes' own `_repair_tool_call_arguments` semantics).
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

# Variant A — block syntax (the original observation).
_LEAKED_BLOCK_RE = re.compile(
    r"\[TOOL_CALL\]\s*\n\s*\{tool\s*=>\s*\"([A-Za-z_][\w.]*)\"\s*,\s*args\s*=>\s*\{(.*?)\}\s*\}\s*\n\s*\[/TOOL_CALL\]",
    re.DOTALL,
)

# Variant B — XML/angle-bracket syntax. Each `<tool_name attr="value" ...>`
# tag becomes one tool_call. `<tool_name>` (no attrs) and `</tool_name>`
# are open/close markers; the attributes carry the args. Multi-tag
# blocks are common (one tool_call per top-level tag inside the
# `<minimax:tool_call>` wrapper).
#
# Match patterns:
#   <minimax:tool_call> … </minimax:tool_call>
#   <tool_name attr1="value" attr2="value">  (open)
#   </tool_name>                                (close — ignored)
_XML_BLOCK_RE = re.compile(
    r"<minimax:tool_call>(.*?)</minimax:tool_call>",
    re.DOTALL,
)
# Match a single open tag with attributes. We capture the tool name and
# the body of attrs (greedy on the attribute sequence). The closing
# `</tool_name>` is a no-op for our purposes.
#
# Tag-name constraints: must start with a letter/underscore/Unicode
# letter, then any word character (Python's `\w` is Unicode-aware by
# default in py3, so `\w` covers CJK letters) PLUS `.` and `-` (rare in
# leaked markup but cheap to allow) PLUS CJK punctuation (`,` `。` etc. —
# minimax sometimes emits tool names like `shell、干` containing CJK
# punctuation). Greedy match — single-character names are fine.
#
# Whitespace between the name and the first attr is OPTIONAL — minimax
# sometimes emits `<name/>` (self-closing, no attrs) or `<name attr=...>`
# with a single space. Requiring `\s+` here would miss self-closing tags.
_XML_TOOL_TAG_RE = re.compile(
    r"<([\w][\w\.\-、。，]*)\s*([^>]*?)\s*/?>",
    re.DOTALL,
)
# One attribute: `name="value"` or `name='value'` (double-quoted preferred;
# single-quoted as a fallback). Inner quotes inside the value aren't
# supported — leaked markup rarely has them, but if needed they can be
# added as `&quot;` which JSON serializes cleanly.
_XML_ATTR_RE = re.compile(
    r"""([A-Za-z_][\w-]*)\s*=\s*("([^"\\]|\\.)*"|'([^'\\]|\\.)*')"""
)
# Claude-style XML parameter carrier: `<parameter name="X">VALUE</parameter>`.
# The value is text content between the open and close tags.
_XML_PARAM_RE = re.compile(
    r"<parameter\s+name=\"([^\"]+)\"\s*>(.*?)</parameter>",
    re.DOTALL,
)


def _parse_value(raw: str) -> Any:
    """Convert one leaked `--key value` payload into a Python value.

    Used by the block-syntax parser (Variant A). The XML parser has its
    own value extraction (`_xml_attr_value`) since XML attribute values
    are already quoted strings (with HTML-entity encoding that needs
    decoding)."""
    s = raw.strip()
    if s.startswith('"') and s.endswith('"'):
        return json.loads(s)  # handles escapes + unicode
    if s.startswith('[') and s.endswith(']'):
        inner = s[1:-1].strip()
        if not inner:
            return []
        # Split on commas that are NOT inside brackets/quotes.
        items: List[str] = []
        buf: List[str] = []
        in_str = False
        depth = 0
        i = 0
        while i < len(inner):
            ch = inner[i]
            if ch == '"' and (i == 0 or inner[i - 1] != "\\"):
                in_str = not in_str
            elif not in_str:
                if ch == "[":
                    depth += 1
                elif ch == "]":
                    depth -= 1
                elif ch == "," and depth == 0:
                    items.append("".join(buf).strip())
                    buf = []
                    i += 1
                    continue
            buf.append(ch)
            i += 1
        if buf:
            items.append("".join(buf).strip())
        return [_parse_value(it) for it in items]
    if s == "true":
        return True
    if s == "false":
        return False
    if s == "null":
        return None
    try:
        if "." in s:
            return float(s)
        return int(s)
    except ValueError:
        return s  # bare identifier — opaque string


def _xml_attr_value(raw: str) -> Any:
    """Convert one XML attribute payload into a Python value.

    XML attributes in the leaked markup look like `key="value"` where
    `value` is a JSON-shaped literal: a string, number, bool, null, or
    array. We `json.loads` the quoted value when it parses cleanly;
    fall back to the raw string when it doesn't (covers any
    leaked-text variation the model emits that isn't strict JSON)."""
    if raw.startswith('"') and raw.endswith('"'):
        inner = raw[1:-1]
        try:
            return json.loads('"' + inner.replace('\\"', '"') + '"')
        except json.JSONDecodeError:
            # Maybe it's a JSON literal embedded in the string body.
            # Try parsing the inner value as JSON; if that fails too,
            # return the unquoted string.
            try:
                return json.loads(inner)
            except json.JSONDecodeError:
                return inner
    if raw.startswith("'") and raw.endswith("'"):
        return raw[1:-1]
    return raw


def _parse_xml_block(xml_body: str) -> List[Dict[str, Any]]:
    """Parse one `<minimax:tool_call>…</minimax:tool_call>` block into a
    list of OpenAI-shape tool_call dicts.

    Strategy: structural, not pattern-specific. The upstream model
    emits DIFFERENT markup shapes for the same failure across turns
    (verified 2026-08-23/24 — `minimax-m2.7` produced three distinct
    leaked-text formats in three consecutive probes):

      Variant A — block syntax: `[TOOL_CALL] {tool => "x", args => {...}} [/TOOL_CALL]`
      Variant B — angle-bracket: `<minimax:tool_call><skill_view name="x"/></skill_view></minimax:tool_call>`
      Variant C — Claude-style XML: `<minimax:tool_call><invoke name="bash"><parameter name="command">x</parameter></invoke></minimax:tool_call>`

    The unifying invariant across all three: the TAG NAME is the
    tool name, and the tag's attributes (Variant B) or its
    `<parameter>` children (Variant C) carry the arguments. For
    Variant B, the `name=` attr is just a regular argument that
    happens to share a key with the tool's first arg — there's no
    consistent way to tell them apart at parse time, so we pass the
    whole attribute dict through and let hermes/the tool surface a
    parse error if `name` is unexpected.

    Special case: self-closing tags with no attributes (`<name/>`) get
    an `_empty_args: True` sentinel so hermes doesn't crash on a
    bare `{}` arguments dict.
    """
    tool_calls: List[Dict[str, Any]] = []
    # Tags we never want to emit as tool_calls. These are wrappers or
    # structural markers, not actual tool invocations.
    wrapper_tags = {
        "minimax",       # outer wrapper
        "tool_call",     # alt outer wrapper
        "parameter",     # Claude XML arg carrier (paired with <invoke>)
        "antml",         # antml namespace elements that sometimes leak
    }
    # Tags where the tool name is an attribute (not the tag name
    # itself). Used for Claude-style Variant C: <invoke name="bash">.
    # Note "invoke" is NOT in wrapper_tags because we DO want to emit
    # a tool_call for it — with the name from the `name=` attribute.
    name_attr_tags = {"invoke"}

    for m in _XML_TOOL_TAG_RE.finditer(xml_body):
        tag_name = m.group(1)
        if tag_name in wrapper_tags:
            continue
        attrs_str = m.group(2).strip()
        tool_name: str
        kwargs: Dict[str, Any] = {}
        if tag_name in name_attr_tags and "name=" in attrs_str:
            # Claude Variant C: tool name comes from `name=` attr.
            tool_name = _extract_name_value(attrs_str)
        else:
            # Variant B and most others: tag name IS the tool name.
            tool_name = tag_name
            # Collect all attributes as kwargs (including `name=`,
            # which is a regular arg in Variant B's `<skill_view name="x">`).
            for am in _XML_ATTR_RE.finditer(attrs_str):
                kwargs[am.group(1)] = _xml_attr_value(am.group(2))
        # For `<invoke>`, args come from sibling `<parameter>` children,
        # not from `<invoke>`'s own attrs (Variant C).
        if tag_name == "invoke":
            invoke_end = xml_body.find("</invoke>", m.end())
            if invoke_end > 0:
                inner = xml_body[m.end():invoke_end]
                kwargs = _extract_parameter_args(inner)
        # Self-closing tags with no attrs get a sentinel so hermes
        # doesn't crash on a bare `{}` arguments dict.
        if not kwargs and not attrs_str:
            kwargs = {"_empty_args": True}
        tool_calls.append(_make_tool_call(tool_name, kwargs))
    return tool_calls


def _extract_name_value(attrs_str: str) -> str:
    """Return the value of the first `name="..."` attribute in a
    string, or the empty string if none is found. Used by Variant C
    where the tool name lives in the `name=` attribute (e.g.
    `<invoke name="bash">`)."""
    m = re.search(r'name\s*=\s*"([^"]+)"', attrs_str)
    return m.group(1) if m else ""


def _extract_parameter_args(inner_xml: str) -> Dict[str, Any]:
    """Extract `<parameter name="k">value</parameter>` pairs from the
    inner XML of a Claude-style `<invoke>` block. Each parameter tag
    contributes one kwarg to the tool_call."""
    kwargs: Dict[str, Any] = {}
    # Match: <parameter name="X">VALUE</parameter>
    # The value can contain text (which becomes a string) or be empty
    # (which is rare; usually means the model used attr syntax).
    for pm in _XML_PARAM_RE.finditer(inner_xml):
        key = pm.group(1)
        raw_value = pm.group(2)
        kwargs[key] = raw_value.strip() if raw_value else ""
    return kwargs


def _parse_xml_attrs(attrs_str: str) -> Dict[str, Any]:
    """Parse all attributes in an attr string into a dict. Used when
    the tag has multiple attrs (Variant B without name=)."""
    kwargs: Dict[str, Any] = {}
    for am in _XML_ATTR_RE.finditer(attrs_str):
        kwargs[am.group(1)] = _xml_attr_value(am.group(2))
    return kwargs


def _make_tool_call(tool_name: str, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Build one OpenAI-shape tool_call dict. Empty kwargs get a
    sentinel so the model has a retry surface (hermes treats missing
    args as a crash)."""
    if not kwargs:
        kwargs = {"_empty_args": True}
    return {
        "id": "leaked_" + uuid.uuid4().hex[:12],
        "type": "function",
        "function": {
            "name": tool_name,
            "arguments": json.dumps(kwargs, ensure_ascii=False),
        },
    }


def _parse_block(tool_name: str, args_block: str) -> Optional[Dict[str, Any]]:
    """Parse one leaked block into a tool_call dict, or None if the block
    is malformed (caller falls back to surfacing the original text).

    A block is malformed if:
      * no `--key value` pairs matched at all (regex didn't match), OR
      * the matched kwargs are suspiciously empty/single-keyword-only
        (e.g. `--malformed` with no value) — those are usually typos
        that would translate to an empty `arguments: "{}"` and get
        executed by hermes as a no-op call, which is worse than
        surfacing the original text.
    """
    kwargs: Dict[str, Any] = {}
    for m in _KWARG_RE.finditer(args_block):
        try:
            kwargs[m.group(1)] = _parse_value(m.group(2))
        except Exception:
            return None
    if not kwargs:
        # No --key value pairs found at all (regex didn't match anything).
        # The block was probably malformed — surface as text instead of
        # executing a no-op call.
        return None
    arguments = json.dumps(kwargs, ensure_ascii=False)
    return {
        "id": "leaked_" + uuid.uuid4().hex[:12],
        "type": "function",
        "function": {
            "name": tool_name,
            "arguments": arguments,
        },
    }


# `--key value` form inside the args block. Supports:
#   - quoted strings: --name "value"        -> Python str
#   - bracketed arrays: --paths ["a","b"]   -> Python list (recursive)
#   - true / false / null                   -> Python bool / None
#   - numbers: 42 / 3.14                    -> int / float
#   - bare tokens: *                        -> str (treated as opaque)
_KWARG_RE = re.compile(
    r"--([A-Za-z_][\w-]*)\s+"
    r"(\"(?:[^\"\\]|\\.)*\"|"
    r"\[(?:[^\[\]]+)\]|"
    r"true|false|null|"
    r"-?\d+(?:\.\d+)?|"
    r"[A-Za-z_][\w./-]*)"
)


def extract_leaked_tool_calls(content: str) -> Tuple[List[Dict[str, Any]], str]:
    """Pull leaked tool-call markup out of `content` and return
    `(tool_calls, cleaned_content)`. Handles both the block syntax
    (Variant A) and the XML/angle-bracket syntax (Variant B).

    If a block is malformed, that block is left in the cleaned content
    (so the user sees *something*) but still counts as a tool_call
    attempt that hermes will see as malformed args — the rest of the
    loop will surface the parse error to the model on the next turn
    rather than silently dropping it.
    """
    if not content:
        return [], content
    tool_calls: List[Dict[str, Any]] = []
    kept_spans: List[Tuple[int, int]] = []

    # Variant A — block syntax. Parse first because the regex is more
    # specific (won't accidentally match inside XML).
    for m in _LEAKED_BLOCK_RE.finditer(content):
        tc = _parse_block(m.group(1), m.group(2))
        if tc is None:
            continue
        tool_calls.append(tc)
        kept_spans.append((m.start(), m.end()))

    # Variant B — XML syntax. Same kept_spans merge logic as below.
    for m in _XML_BLOCK_RE.finditer(content):
        xml_tool_calls = _parse_xml_block(m.group(1))
        if not xml_tool_calls:
            continue
        tool_calls.extend(xml_tool_calls)
        kept_spans.append((m.start(), m.end()))

    if not kept_spans:
        return tool_calls, content

    # Merge overlapping spans and rebuild the content with them removed.
    kept_spans.sort()
    merged: List[Tuple[int, int]] = []
    for s, e in kept_spans:
        if not merged or s > merged[-1][1]:
            merged.append((s, e))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
    parts: List[str] = []
    cursor = 0
    for s, e in merged:
        if cursor < s:
            parts.append(content[cursor:s])
        cursor = max(cursor, e)
    if cursor < len(content):
        parts.append(content[cursor:])
    cleaned = "\n".join(p.strip() for p in parts if p and p.strip()).strip()
    return tool_calls, cleaned


def split_streaming_chunk(content: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Streaming-friendly variant: extract leaked tool_calls from one
    incremental `delta.content` chunk. Returns `(cleaned_text,
    tool_calls_for_this_chunk)`. Tool calls synthesized from a single
    chunk each get their own id; hermes aggregates them downstream.

    Note: the return order is swapped vs `extract_leaked_tool_calls` —
    streaming callers want `(text, tools)` since they emit text first
    and tool_calls second. Both helpers share the same parser; this is
    just a convenience wrapper.
    """
    tool_calls, cleaned = extract_leaked_tool_calls(content)
    return cleaned, tool_calls
