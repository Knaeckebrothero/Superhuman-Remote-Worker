#!/usr/bin/env python3
"""Diagnostic for Gemma 4 reasoning-channel and tool-call wire formats.

Hits the workstation router at https://ai.h4ll.app/v1 against both Gemma 4
models and runs five checks per model:

  A. Thinking prompt, NON-streaming, default chat-template settings
     - Does the model emit thinking content?
     - Does vLLM's `--reasoning-parser gemma4` lift it into `reasoning_content`?
     - Does the user-facing `content` contain leaked `<|channel>thought` /
       `<channel|>` delimiters?

  B. Thinking prompt, STREAMING, default chat-template settings
     - Same checks against the accumulated stream + per-chunk delta inspection
       (token-boundary parser bugs only show up on the streaming path).

  C. Tool-call prompt, NON-streaming, structured `tools=[]`
     - Pass an OpenAI-format tool definition.
     - Does the model emit a tool call?
     - Does vLLM's `--tool-call-parser gemma4` lift it into structured
       `tool_calls`, or does it leak as content text?
     - If it leaks, inspect the wire format used (canonical braces vs Python
       parens vs JSON) — confirms the pattern from job 3c30d72e.

  D. Thinking prompt, with `chat_template_kwargs.enable_thinking=True`
     forced via `extra_body`. Both non-stream (D) and streaming (D_stream).
     Probes whether explicit thinking-mode activation reproduces the
     `<|channel>thought ... <channel|>` leak observed in the cockpit
     screenshot. The default chat template may keep thinking off — Gemma
     only emits the thought channel when explicitly told to.

  E. Tool-call prompt, NON-streaming, **NO** structured `tools=[]`. The tool
     definition is embedded in the system prompt as text along with the
     canonical Gemma wire-format teaching block. Probes whether the absence
     of the OpenAI tool envelope causes the model to drift to Python-style
     parens or JSON-quoted-key syntax — a hypothesis for why the agent
     looped 1385× in job 3c30d72e even though scenario C works correctly.

Distinguishes three failure surfaces:
  - vLLM bug (parser receives content but fails to lift) — reasoning_content
    empty AND content has leaked delimiters
  - Cockpit bug (parser works, UI drops the field) — reasoning_content
    populated AND content clean → the leak is downstream
  - Streaming-only bug — case B leaks but case A doesn't

Usage:
    GEMMA_API_KEY=sk-... python tests/manual_test_gemma_reasoning.py

If GEMMA_API_KEY is unset, the script uses the key embedded below. Pass
--model to test a single model:

    python tests/manual_test_gemma_reasoning.py --model gemma-4-moe

Exit code is 0 if the script ran to completion (regardless of findings); the
findings are reported to stdout in a per-model breakdown plus a final summary
table. The script does not assert anything — it's a diagnostic, not a test.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any

import httpx

BASE_URL = "https://ai.h4ll.app/v1"
DEFAULT_API_KEY = "sk-PASTE-980c6c2862deebb2aa5963b1c72379c6-admin"
DEFAULT_MODELS = ["gemma-4-moe", "gemma-4-31b"]

# Patterns that should NEVER appear in `content` if vLLM's parsers are doing
# their job. Their presence in `content` is the diagnostic signal.
REASONING_DELIMITERS = [
    "<|channel>",
    "<channel|>",
    "<|channel>thought",
]
TOOL_CALL_DELIMITERS = [
    "<|tool_call>",
    "<tool_call|>",
    "<|tool_response>",
    "<tool_response|>",
]
# Wire-format variants we want to detect inside leaked tool calls.
WIRE_FORMAT_PATTERNS = {
    "canonical_braces": re.compile(r"<\|tool_call>call:\w+\{[^}]*\}<tool_call\|>"),
    "python_parens": re.compile(r"<\|tool_call>call:\w+\([^)]*\)<tool_call\|>"),
    "json_quoted_keys": re.compile(r'<\|tool_call>call:\w+\{\s*"\w+":'),
    "equals_sign": re.compile(r"<\|tool_call>call:\w+\{[^}]*\w+="),
}

THINKING_PROMPT = (
    "Think step by step, then give a one-line final answer. "
    "What is 17 multiplied by 23?"
)
TOOL_PROMPT = (
    "What is the current weather in Tokyo? Use the get_weather tool. "
    "If you need to think first, do so, then call the tool."
)

# Scenario E system prompt — teaches Gemma's canonical wire format and
# describes the weather tool inline, instead of passing it through the
# OpenAI `tools=[]` envelope. Mirrors how an agent harness might present
# tools when it doesn't structure the call (or when tool-binding is
# bypassed).  The format anchor here is intentionally close to what
# `tactical_gemma.txt` ships, so any drift the model exhibits is
# attributable to the same prompt class our agent emits.
TOOL_IN_PROMPT_SYSTEM = """You are a helpful assistant with one tool available.

## Tool Call Format (load-bearing)

Every tool call MUST use this exact wire format:

```
<|tool_call>call:TOOL_NAME{arg:<|"|>string val<|"|>,num:42,flag:true}<tool_call|>
```

- Curly braces — never parentheses.
- String values wrapped in `<|"|>...<|"|>` — never `"..."` or `'...'`.
- Numbers, booleans, null appear bare: `count:42`, `enabled:true`, `value:null`.
- Closing tag `<tool_call|>` (pipe on right).
- One call per turn.

## Tool: get_weather

Get the current weather for a city.

- `city` (string, required): City name, e.g. `Tokyo` or `London`.
- `units` (string, optional, one of `celsius` / `fahrenheit`): Temperature units.

Example: `<|tool_call>call:get_weather{city:<|"|>Berlin<|"|>,units:<|"|>celsius<|"|>}<tool_call|>`

When the user asks for weather, emit exactly one tool call in the format above.
"""
WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "City name, e.g. 'Tokyo' or 'London'",
                },
                "units": {
                    "type": "string",
                    "enum": ["celsius", "fahrenheit"],
                    "description": "Temperature units",
                },
            },
            "required": ["city"],
        },
    },
}


@dataclass
class Finding:
    """One diagnostic observation per (model, scenario)."""

    model: str
    scenario: str  # 'A_think_nonstream' | 'B_think_stream' | 'C_tool_nonstream'
    http_ok: bool
    error: str | None = None
    content: str = ""
    reasoning_content: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    # Derived flags
    leaked_reasoning_delims: list[str] = field(default_factory=list)
    leaked_tool_delims: list[str] = field(default_factory=list)
    wire_format_hits: dict[str, int] = field(default_factory=dict)

    def derive_flags(self) -> None:
        for d in REASONING_DELIMITERS:
            if d in self.content:
                self.leaked_reasoning_delims.append(d)
        for d in TOOL_CALL_DELIMITERS:
            if d in self.content:
                self.leaked_tool_delims.append(d)
        for name, pat in WIRE_FORMAT_PATTERNS.items():
            hits = len(pat.findall(self.content))
            if hits:
                self.wire_format_hits[name] = hits


def _post(
    client: httpx.Client,
    api_key: str,
    payload: dict[str, Any],
    stream: bool = False,
) -> httpx.Response:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if stream:
        return client.stream(
            "POST",
            f"{BASE_URL}/chat/completions",
            headers=headers,
            json=payload,
            timeout=120.0,
        )
    return client.post(
        f"{BASE_URL}/chat/completions",
        headers=headers,
        json=payload,
        timeout=120.0,
    )


def list_models(client: httpx.Client, api_key: str) -> list[str]:
    r = client.get(
        f"{BASE_URL}/models",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30.0,
    )
    r.raise_for_status()
    return [m["id"] for m in r.json().get("data", [])]


def run_thinking_nonstream(
    client: httpx.Client, api_key: str, model: str
) -> Finding:
    f = Finding(model=model, scenario="A_think_nonstream", http_ok=False)
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": THINKING_PROMPT}],
        "temperature": 0.3,
        "max_tokens": 600,
        "stream": False,
    }
    try:
        r = _post(client, api_key, payload, stream=False)
        r.raise_for_status()
        f.http_ok = True
        body = r.json()
        choice = body["choices"][0]
        msg = choice.get("message", {})
        f.content = msg.get("content") or ""
        # vLLM's reasoning parser surfaces this on `reasoning_content`
        f.reasoning_content = msg.get("reasoning_content")
        f.finish_reason = choice.get("finish_reason")
        usage = body.get("usage", {})
        f.prompt_tokens = usage.get("prompt_tokens")
        f.completion_tokens = usage.get("completion_tokens")
    except httpx.HTTPStatusError as e:
        f.error = f"HTTP {e.response.status_code}: {e.response.text[:300]}"
    except Exception as e:
        f.error = f"{type(e).__name__}: {e}"
    f.derive_flags()
    return f


def run_thinking_stream(
    client: httpx.Client, api_key: str, model: str
) -> Finding:
    f = Finding(model=model, scenario="B_think_stream", http_ok=False)
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": THINKING_PROMPT}],
        "temperature": 0.3,
        "max_tokens": 600,
        "stream": True,
    }
    accumulated_content = []
    accumulated_reasoning = []
    delta_count = 0
    try:
        with _post(client, api_key, payload, stream=True) as r:
            r.raise_for_status()
            f.http_ok = True
            for line in r.iter_lines():
                if not line or not line.startswith("data: "):
                    continue
                data_str = line[len("data: "):]
                if data_str.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                choice = (chunk.get("choices") or [{}])[0]
                delta = choice.get("delta") or {}
                if "content" in delta and delta["content"]:
                    accumulated_content.append(delta["content"])
                if "reasoning_content" in delta and delta["reasoning_content"]:
                    accumulated_reasoning.append(delta["reasoning_content"])
                if choice.get("finish_reason"):
                    f.finish_reason = choice["finish_reason"]
                delta_count += 1
        f.content = "".join(accumulated_content)
        rc = "".join(accumulated_reasoning)
        f.reasoning_content = rc if rc else None
    except httpx.HTTPStatusError as e:
        f.error = f"HTTP {e.response.status_code}: {e.response.text[:300]}"
    except Exception as e:
        f.error = f"{type(e).__name__}: {e}"
    f.derive_flags()
    return f


def run_tool_call_nonstream(
    client: httpx.Client, api_key: str, model: str
) -> Finding:
    f = Finding(model=model, scenario="C_tool_nonstream", http_ok=False)
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": TOOL_PROMPT}],
        "tools": [WEATHER_TOOL],
        "tool_choice": "auto",
        "temperature": 0.3,
        "max_tokens": 400,
        "stream": False,
    }
    try:
        r = _post(client, api_key, payload, stream=False)
        r.raise_for_status()
        f.http_ok = True
        body = r.json()
        choice = body["choices"][0]
        msg = choice.get("message", {})
        f.content = msg.get("content") or ""
        f.reasoning_content = msg.get("reasoning_content")
        f.tool_calls = msg.get("tool_calls") or []
        f.finish_reason = choice.get("finish_reason")
        usage = body.get("usage", {})
        f.prompt_tokens = usage.get("prompt_tokens")
        f.completion_tokens = usage.get("completion_tokens")
    except httpx.HTTPStatusError as e:
        f.error = f"HTTP {e.response.status_code}: {e.response.text[:300]}"
    except Exception as e:
        f.error = f"{type(e).__name__}: {e}"
    f.derive_flags()
    return f


def run_thinking_force_nonstream(
    client: httpx.Client, api_key: str, model: str
) -> Finding:
    """Scenario D — thinking mode forced ON via chat_template_kwargs.

    vLLM accepts `chat_template_kwargs` as a top-level field on the chat
    completions request and forwards it to the chat template renderer.
    Gemma's chat template gates the `<|channel>thought` block on
    `enable_thinking`, so this should be the regime where the reasoning
    parser is exercised.
    """
    f = Finding(model=model, scenario="D_think_force_nonstream", http_ok=False)
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": THINKING_PROMPT}],
        "temperature": 0.3,
        "max_tokens": 800,
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": True},
    }
    try:
        r = _post(client, api_key, payload, stream=False)
        r.raise_for_status()
        f.http_ok = True
        body = r.json()
        choice = body["choices"][0]
        msg = choice.get("message", {})
        f.content = msg.get("content") or ""
        f.reasoning_content = msg.get("reasoning_content")
        f.finish_reason = choice.get("finish_reason")
        usage = body.get("usage", {})
        f.prompt_tokens = usage.get("prompt_tokens")
        f.completion_tokens = usage.get("completion_tokens")
    except httpx.HTTPStatusError as e:
        f.error = f"HTTP {e.response.status_code}: {e.response.text[:300]}"
    except Exception as e:
        f.error = f"{type(e).__name__}: {e}"
    f.derive_flags()
    return f


def run_thinking_force_stream(
    client: httpx.Client, api_key: str, model: str
) -> Finding:
    """Scenario D_stream — same as D but streaming.

    Streaming exercises a different code path in vLLM's reasoning parser
    (incremental delimiter matching across chunk boundaries). If A and D
    both come back clean but D_stream leaks, that points at the
    streaming-state-machine bug specifically.
    """
    f = Finding(model=model, scenario="D_think_force_stream", http_ok=False)
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": THINKING_PROMPT}],
        "temperature": 0.3,
        "max_tokens": 800,
        "stream": True,
        "chat_template_kwargs": {"enable_thinking": True},
    }
    accumulated_content: list[str] = []
    accumulated_reasoning: list[str] = []
    try:
        with _post(client, api_key, payload, stream=True) as r:
            r.raise_for_status()
            f.http_ok = True
            for line in r.iter_lines():
                if not line or not line.startswith("data: "):
                    continue
                data_str = line[len("data: "):]
                if data_str.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                choice = (chunk.get("choices") or [{}])[0]
                delta = choice.get("delta") or {}
                if "content" in delta and delta["content"]:
                    accumulated_content.append(delta["content"])
                if "reasoning_content" in delta and delta["reasoning_content"]:
                    accumulated_reasoning.append(delta["reasoning_content"])
                if choice.get("finish_reason"):
                    f.finish_reason = choice["finish_reason"]
        f.content = "".join(accumulated_content)
        rc = "".join(accumulated_reasoning)
        f.reasoning_content = rc if rc else None
    except httpx.HTTPStatusError as e:
        f.error = f"HTTP {e.response.status_code}: {e.response.text[:300]}"
    except Exception as e:
        f.error = f"{type(e).__name__}: {e}"
    f.derive_flags()
    return f


def run_tool_in_prompt_nonstream(
    client: httpx.Client, api_key: str, model: str
) -> Finding:
    """Scenario E — tool described in system prompt, NO `tools=[]` envelope.

    The `gemma4` tool-call parser regex-matches the canonical wire format
    in the assistant's *content* stream. When tools are passed via
    `tools=[]`, vLLM splices a tool-definition block into the rendered
    chat template, and the model has clear cues. When they're not, the
    model has to invent the call from the system-prompt teaching alone.
    Drift to parens, JSON, or `key=value` form means our system prompts
    aren't load-bearing enough on their own — the structured tools
    envelope is what's keeping us out of the parens trap.
    """
    f = Finding(model=model, scenario="E_tool_in_prompt", http_ok=False)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": TOOL_IN_PROMPT_SYSTEM},
            {"role": "user", "content": "What is the current weather in Tokyo?"},
        ],
        "temperature": 0.3,
        "max_tokens": 400,
        "stream": False,
    }
    try:
        r = _post(client, api_key, payload, stream=False)
        r.raise_for_status()
        f.http_ok = True
        body = r.json()
        choice = body["choices"][0]
        msg = choice.get("message", {})
        f.content = msg.get("content") or ""
        f.reasoning_content = msg.get("reasoning_content")
        f.tool_calls = msg.get("tool_calls") or []
        f.finish_reason = choice.get("finish_reason")
        usage = body.get("usage", {})
        f.prompt_tokens = usage.get("prompt_tokens")
        f.completion_tokens = usage.get("completion_tokens")
    except httpx.HTTPStatusError as e:
        f.error = f"HTTP {e.response.status_code}: {e.response.text[:300]}"
    except Exception as e:
        f.error = f"{type(e).__name__}: {e}"
    f.derive_flags()
    return f


def _truncate(s: str | None, n: int = 200) -> str:
    if not s:
        return "<empty>"
    s = s.replace("\n", " ⏎ ")
    return s if len(s) <= n else s[:n] + "…"


def report_finding(f: Finding) -> None:
    print(f"\n  [{f.scenario}]")
    if not f.http_ok:
        print(f"    ERROR: {f.error}")
        return
    print(
        f"    finish={f.finish_reason} "
        f"prompt_tokens={f.prompt_tokens} completion_tokens={f.completion_tokens}"
    )
    print(f"    content        : {_truncate(f.content)}")
    if f.reasoning_content is not None:
        print(f"    reasoning_content: {_truncate(f.reasoning_content)}")
    else:
        print("    reasoning_content: <field absent on response>")
    if f.tool_calls:
        for i, tc in enumerate(f.tool_calls):
            fn = tc.get("function") or {}
            print(
                f"    tool_calls[{i}]   : name={fn.get('name')} "
                f"args={_truncate(fn.get('arguments'), 120)}"
            )
    elif f.scenario.startswith("C_"):
        print("    tool_calls       : <empty>")

    if f.leaked_reasoning_delims:
        print(
            f"    *** REASONING DELIMS LEAKED in content: "
            f"{f.leaked_reasoning_delims}"
        )
    if f.leaked_tool_delims:
        print(
            f"    *** TOOL-CALL DELIMS LEAKED in content: "
            f"{f.leaked_tool_delims}"
        )
    if f.wire_format_hits:
        print(f"    wire-format detected: {f.wire_format_hits}")


def diagnose_model(
    client: httpx.Client, api_key: str, model: str
) -> list[Finding]:
    print(f"\n{'=' * 72}")
    print(f"  MODEL: {model}")
    print(f"{'=' * 72}")
    findings = [
        run_thinking_nonstream(client, api_key, model),
        run_thinking_stream(client, api_key, model),
        run_tool_call_nonstream(client, api_key, model),
        run_thinking_force_nonstream(client, api_key, model),
        run_thinking_force_stream(client, api_key, model),
        run_tool_in_prompt_nonstream(client, api_key, model),
    ]
    for f in findings:
        report_finding(f)
    return findings


def summary_table(all_findings: list[Finding]) -> None:
    print(f"\n{'=' * 72}")
    print("  SUMMARY")
    print(f"{'=' * 72}\n")
    by_model: dict[str, dict[str, Finding]] = {}
    for f in all_findings:
        by_model.setdefault(f.model, {})[f.scenario] = f

    def _rc_line(label: str, f: Finding) -> str:
        rc_status = "populated" if f.reasoning_content else "empty/absent"
        leak = "LEAKED" if f.leaked_reasoning_delims else "clean"
        return f"    {label} : reasoning_content={rc_status}, content={leak}"

    def _tool_line(label: str, f: Finding) -> str:
        tc_status = (
            f"{len(f.tool_calls)} structured"
            if f.tool_calls
            else "0 structured"
        )
        leak_marker = "LEAKED" if f.leaked_tool_delims else "clean"
        wire = list(f.wire_format_hits.keys()) or ["(none detected)"]
        return (
            f"    {label} : tool_calls={tc_status}, "
            f"content={leak_marker}, wire={wire}"
        )

    for model, scenarios in by_model.items():
        print(f"  {model}")
        a = scenarios.get("A_think_nonstream")
        b = scenarios.get("B_think_stream")
        c = scenarios.get("C_tool_nonstream")
        d = scenarios.get("D_think_force_nonstream")
        ds = scenarios.get("D_think_force_stream")
        e = scenarios.get("E_tool_in_prompt")

        if a and a.http_ok:
            print(_rc_line("A non-stream thinking (default)        ", a))
        if b and b.http_ok:
            print(_rc_line("B streaming  thinking (default)        ", b))
        if c and c.http_ok:
            print(_tool_line("C non-stream tool   (tools=[])         ", c))
        if d and d.http_ok:
            print(_rc_line("D non-stream thinking (enable_thinking) ", d))
        if ds and ds.http_ok:
            print(_rc_line("D streaming  thinking (enable_thinking) ", ds))
        if e and e.http_ok:
            print(_tool_line("E non-stream tool   (in-prompt, no tools=[])", e))

    # Verdicts
    print("\n  VERDICT (per check)")
    print(
        "    Reasoning parser: 'populated + clean' → working. "
        "'empty + LEAKED' → vLLM bug (#38855 candidate). "
        "'populated + LEAKED' → partial. "
        "'empty + clean' → model not emitting thoughts (try a stronger prompt "
        "or D — explicit enable_thinking)."
    )
    print(
        "    Tool parser:      'structured + clean' → working. "
        "'0 structured + LEAKED canonical_braces' → unexpected (regex matches "
        "but parser refuses). '0 structured + LEAKED python_parens' → confirms "
        "job 3c30d72e pattern (model emits wrong format)."
    )
    print(
        "    Cross-check:      A clean + D LEAKED → bug only when thinking "
        "explicitly enabled (matches cockpit screenshot). "
        "C structured + E parens/JSON → drift only without `tools=[]` envelope; "
        "implicates how LangChain or our agent presents tools. "
        "C structured + E structured → model survives even without the "
        "envelope; the bug is something else (multi-turn, large context, etc)."
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--model",
        action="append",
        help="Model id (repeatable). Default: gemma-4-moe and gemma-4-31b.",
    )
    ap.add_argument(
        "--list-only",
        action="store_true",
        help="Only list available models from /v1/models and exit.",
    )
    args = ap.parse_args()

    api_key = os.environ.get("GEMMA_API_KEY") or DEFAULT_API_KEY
    if not api_key or api_key.startswith("sk-PASTE-PLACEHOLDER"):
        print("ERROR: set GEMMA_API_KEY env var or edit DEFAULT_API_KEY")
        return 2

    with httpx.Client() as client:
        try:
            available = list_models(client, api_key)
        except httpx.HTTPStatusError as e:
            print(
                f"ERROR: failed to list models — HTTP "
                f"{e.response.status_code}: {e.response.text[:300]}"
            )
            return 1
        except Exception as e:
            print(f"ERROR: failed to list models — {type(e).__name__}: {e}")
            return 1

        print(f"Endpoint: {BASE_URL}")
        print(f"Available models ({len(available)}):")
        for m in sorted(available):
            print(f"  - {m}")

        if args.list_only:
            return 0

        targets = args.model or DEFAULT_MODELS
        missing = [m for m in targets if m not in available]
        if missing:
            print(
                f"\nWARN: requested models not in /v1/models: {missing}. "
                f"Will still try them — the router may resolve aliases."
            )

        all_findings: list[Finding] = []
        for model in targets:
            all_findings.extend(diagnose_model(client, api_key, model))

        summary_table(all_findings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
