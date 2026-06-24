"""
Leaked tool-call recovery.

Some serving layers (notably vLLM's gemma4 tool parser) fail to lift a model's
tool call into a structured ``tool_calls`` array when the model emits a slightly
off-spec wire format — e.g. Gemma's ``<|tool_call>call:NAME(args)<tool_call|>``
with Python-style parens instead of the canonical braces. The raw markup then
leaks into the message ``content`` while ``tool_calls`` stays empty, so the agent
makes no tool call and loops (job 2dacba6f ran 24k iterations this way).

This module recovers such leaks:

* ``parse_leaked_tool_calls`` — strict path: rebuild real structured tool calls
  from the markup, but only when the content is *wholly* one or more well-formed
  blocks whose tool names are known and whose args parse cleanly. Any ambiguity
  → bail entirely (never partially recover), so we never execute a misparsed
  write/command tool.
* ``has_leaked_tool_call_markup`` — loose detector: True when content is
  dominated by leaked markup even if the args are unparseable. Used by the
  no-tool-call circuit breaker to fail fast when recovery is impossible.
* ``strip_tool_call_markup`` — remove recovered markup from content so the leak
  is not re-archived.

Canonical Gemma grammar (config/templates/instructions_gemma.md):
``<|tool_call>call:NAME{arg:<|"|>string val<|"|>,num:42,flag:true}<tool_call|>``
— curly braces, string values wrapped in ``<|"|>``, bare numbers/bools, closing
tag ``<tool_call|>``. The observed leak variants additionally use Python parens
(``call:NAME(k=v)`` / ``call:NAME()``).

All functions are pure and side-effect free.
"""

import ast
import re
import uuid
from typing import Any, Optional

# A single leaked tool-call block. The two argument delimiters — braces (canonical
# Gemma) and parens (the Python-style leak) — are separate alternatives so a
# mismatched ``{...)`` cannot cross-match. DOTALL so string args may span newlines.
_BLOCK_RE = re.compile(
    r"<\|tool_call>\s*call:(?P<name>\w+)\s*"
    r"(?:\{(?P<braces>.*?)\}|\((?P<parens>.*?)\))\s*"
    r"<tool_call\|>",
    re.DOTALL,
)

# Looser block used only for *detection* — matches a full block OR a truncated
# one (opening sentinel to end-of-string), so persistent truncated leaks still
# trip the circuit breaker.
_LOOSE_BLOCK_RE = re.compile(r"<\|tool_call>.*?(?:<tool_call\|>|$)", re.DOTALL)

# Gemma wraps string values in ``<|"|> ... <|"|>`` (never normal quotes).
_GEMMA_STR_RE = re.compile(r'<\|"\|>(.*?)<\|"\|>', re.DOTALL)

# Opening sentinel — cheap presence check before any regex work.
_OPEN_SENTINEL = "<|tool_call>"

# Fraction of non-whitespace characters that must sit inside leaked blocks for
# ``has_leaked_tool_call_markup`` to consider the content "dominated" by markup.
_MARKUP_DOMINANCE = 0.6

# Sentinel distinguishing "could not parse this value" from a legitimate ``None``.
_BAIL = object()


def parse_leaked_tool_calls(
    content: str, allowed_names: Optional["set[str]"] = None
) -> "list[dict]":
    """Recover structured tool calls from leaked markup in ``content``.

    Returns a list of ``{"name", "args", "id", "type": "tool_call"}`` dicts ready
    to assign to ``AIMessage.tool_calls``, or ``[]`` when recovery is not safe.

    Recovery happens only when ALL hold (otherwise bail entirely):

    1. ``content.strip()`` is wholly covered by one or more consecutive
       ``<|tool_call>...<tool_call|>`` blocks (whitespace-only in between/around).
       Markup embedded in prose → bail.
    2. Every tool name is in ``allowed_names`` (the phase-bound tool set). When
       ``allowed_names is None`` it falls back to the global ``TOOL_REGISTRY``.
    3. Every block's arguments parse cleanly (no arrays/objects, no positional
       args, no ambiguous tokens).

    Each recovered call gets a unique ``id`` (``rcv_<hex>``) — required so context
    compaction's tool-call/result pairing does not strip it as an orphan.
    """
    if not content or _OPEN_SENTINEL not in content:
        return []

    stripped = content.strip()
    matches = list(_BLOCK_RE.finditer(stripped))
    if not matches:
        return []

    # Whole-string gate: only whitespace may sit between/around the blocks.
    cursor = 0
    for m in matches:
        if stripped[cursor : m.start()].strip():
            return []
        cursor = m.end()
    if stripped[cursor:].strip():
        return []

    calls: "list[dict]" = []
    for m in matches:
        name = m.group("name")
        if not _is_known_tool(name, allowed_names):
            return []
        if m.group("braces") is not None:
            args = _parse_gemma_args(m.group("braces"))
        else:
            args = _parse_python_args(m.group("parens"))
        if args is None:
            return []
        calls.append(
            {
                "name": name,
                "args": args,
                "id": "rcv_" + uuid.uuid4().hex[:12],
                "type": "tool_call",
            }
        )
    return calls


def has_leaked_tool_call_markup(
    content: str, dominance: float = _MARKUP_DOMINANCE
) -> bool:
    """Return True when ``content`` is dominated by leaked tool-call markup.

    Looser than :func:`parse_leaked_tool_calls`: it does not require the args to
    be parseable, only that leaked blocks account for at least ``dominance`` of
    the non-whitespace characters. This lets the no-tool-call circuit breaker
    fail fast on persistent unrecoverable leaks even when the payload varies.
    """
    if not content or _OPEN_SENTINEL not in content:
        return False
    matches = list(_LOOSE_BLOCK_RE.finditer(content))
    if not matches:
        return False
    total_nonws = len(re.sub(r"\s+", "", content))
    if total_nonws == 0:
        return False
    inside_nonws = sum(
        len(re.sub(r"\s+", "", content[m.start() : m.end()])) for m in matches
    )
    return inside_nonws / total_nonws >= dominance


def strip_tool_call_markup(content: str) -> str:
    """Remove well-formed leaked tool-call blocks from ``content``.

    Intended for use after a successful :func:`parse_leaked_tool_calls`, where the
    content was wholly markup and the result is therefore ``""``. Returns the
    surrounding text (trimmed) in the general case.
    """
    if not content:
        return ""
    return _BLOCK_RE.sub("", content).strip()


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _is_known_tool(name: str, allowed_names: Optional["set[str]"]) -> bool:
    if allowed_names is not None:
        return name in allowed_names
    try:
        from src.tools.registry import TOOL_REGISTRY
    except Exception:
        return False
    return name in TOOL_REGISTRY


def _parse_gemma_args(body: str) -> Optional[dict]:
    """Parse canonical Gemma args ``{key:<|"|>str<|"|>,n:42,b:true}`` → dict.

    Returns ``None`` (bail) on arrays/objects, malformed pairs, or ambiguous
    values. Empty body → ``{}``.
    """
    body = body.strip()
    if not body:
        return {}

    # Stash <|"|>-wrapped strings first so their commas/colons don't break the
    # top-level split.
    strings: "list[str]" = []

    def _stash(match: "re.Match") -> str:
        strings.append(match.group(1))
        return f"\x00{len(strings) - 1}\x00"

    masked = _GEMMA_STR_RE.sub(_stash, body)

    # Arrays / nested objects are unsupported in v1 — bail rather than guess.
    if any(ch in masked for ch in "[]{}"):
        return None

    args: dict = {}
    for part in masked.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            return None
        key, _, val = part.partition(":")
        key, val = key.strip(), val.strip()
        if not re.fullmatch(r"\w+", key) or not val:
            return None
        coerced = _coerce_gemma_value(val, strings)
        if coerced is _BAIL:
            return None
        args[key] = coerced
    return args


def _coerce_gemma_value(val: str, strings: "list[str]") -> Any:
    """Coerce a single Gemma arg value, or return ``_BAIL`` if ambiguous."""
    placeholder = re.fullmatch(r"\x00(\d+)\x00", val)
    if placeholder:
        return strings[int(placeholder.group(1))]
    if "\x00" in val:
        # A string placeholder fused with other characters — ambiguous.
        return _BAIL
    low = val.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "none"):
        return None
    if re.fullmatch(r"-?\d+", val):
        return int(val)
    if re.fullmatch(r"-?\d*\.\d+", val):
        return float(val)
    # A bare (unquoted) string — accept only a "simple" token; canonical Gemma
    # wraps anything richer in <|"|>, so a bare multi-word value is malformed.
    if re.fullmatch(r"[\w./\-]+", val):
        return val
    return _BAIL


def _parse_python_args(body: str) -> Optional[dict]:
    """Parse Python-style args ``(k=v, k2="s")`` → dict via the ``ast`` module.

    Returns ``None`` (bail) on positional args, ``**`` splats, arrays/objects, or
    any value ``ast.literal_eval`` rejects (except JS-style bare ``true``/``false``/
    ``null``, which Gemma emits). Empty body → ``{}``.
    """
    body = body.strip()
    if not body:
        return {}
    try:
        tree = ast.parse(f"_f({body})", mode="eval")
    except SyntaxError:
        return None
    call = tree.body
    if not isinstance(call, ast.Call) or call.args:
        return None

    args: dict = {}
    for kw in call.keywords:
        if kw.arg is None:  # ** splat
            return None
        try:
            value = ast.literal_eval(kw.value)
        except (ValueError, SyntaxError, TypeError):
            if isinstance(kw.value, ast.Name) and kw.value.id in (
                "true",
                "false",
                "null",
            ):
                value = {"true": True, "false": False, "null": None}[kw.value.id]
            else:
                return None
        if isinstance(value, (list, dict, tuple, set)):
            return None
        args[kw.arg] = value
    return args
