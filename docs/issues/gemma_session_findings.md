---
tags:
  - debugging
  - llm
  - gemma
  - tool-calls
  - reasoning
  - prompt-architecture
  - guardrails
related:
  - "[[gemma_tool_call_parser_loop]]"
  - "[[model_issues]]"
  - "[[job_debug]]"
---

# Gemma 4 Debug Session — Consolidated Findings (2026-05-05)

This is a session-summary issue doc covering everything diagnosed, shipped,
and proposed during the 2026-05-05 Gemma 4 deep-dive. It complements the
incident-focused [`gemma_tool_call_parser_loop.md`](gemma_tool_call_parser_loop.md)
(which covers job `3c30d72e` specifically) by capturing the broader findings
about parsers, prompt architecture, backend differences, and proposed
follow-ups so that nothing gets lost between sessions.

## TL;DR

What we *thought* the problem was at the start of the session:
- Gemma drifts to Python-style parens because its training is generic and
  our worker prompts didn't have Gemma-specific examples.
- vLLM's `gemma4` parser is too strict and/or buggy.
- The reasoning channel leak in the cockpit screenshot is a vLLM
  reasoning-parser bug.

What we actually found:
- **The vLLM `gemma4` tool-call parser is correct and works fine** — both
  Gemma 4 models produce structured `tool_calls` perfectly when called
  directly through `tools=[]`.
- **The agent harness was teaching the model the wrong format itself.** The
  recovery nudge, the todo-list footer, and the `todo_complete` docstring
  all showed `todo_complete(todo_id="todo_X")` as a literal example. The
  model copied that format every turn. The system prompt said "use braces";
  every example in the conversation said "use parens"; parens won.
- **The strix backend isn't vLLM at all** — it's llama.cpp `llama-server`,
  a different engine entirely. That's why it surfaces `reasoning_content`
  while the cluster vLLM doesn't, and why side-by-side comparison was
  apples-to-oranges.
- **Hardcoded fallback prompts in code are a structural weakness.** The
  scattered guardrail strings in `src/graph.py`, `src/managers/todo.py`,
  and tool docstrings can't be tuned per model family — they fight the
  matrix-driven prompts. A `guardrails:` matrix section is the proposed
  fix.

## Findings

### Finding 1 — vLLM `gemma4` tool-call parser is not the bug

**Diagnostic:** `tests/manual_test_gemma_reasoning.py` scenario C —
direct call to `https://ai.h4ll.app/v1/chat/completions` with `tools=[]`,
clean ~140-token context, simple weather prompt.

**Result on cluster `gemma-4-moe`:** `finish_reason=tool_calls`, structured
`tool_calls[0]: name=get_weather args={"city":"Tokyo"}`, no leakage.

**Result on `gemma-4-31b`:** identical — structured tool calls, no leakage.

**Implication:** The parser works. The production parens drift in job
`3c30d72e` (30k-token context, mid-job) was triggered by something *in*
that context, not by the parser being broken.

### Finding 2 — Tool parser is not invoked when `tools=[]` is absent

**Diagnostic:** Scenario E — same prompt as C but tool definition embedded
as text in the system prompt, no `tools=[]` envelope.

**Result on both models:** model emits perfect canonical wire format
(`<|tool_call>call:get_weather{city:<|"|>Tokyo<|"|>}<tool_call|>`), but
vLLM does **not** lift it into structured `tool_calls`. `finish_reason=stop`,
content leaks raw, `tool_calls=null`.

**Implication:** vLLM's `gemma4` tool-call parser only runs when `tools=[]`
is in the request. If anything in the agent path drops the OpenAI tools
envelope, perfectly-formatted output goes nowhere. Worth verifying our
LangChain integration always sends `tools=[]` (it currently does — confirmed
by inspecting the audit doc for iter 0 of job `3c30d72e`, which shows 48
tool definitions attached).

### Finding 3 — Production failure root cause: harness teaches Python parens

**Diagnostic:** Read iter 21 of job `3c30d72e` (`doc_id 69f9ae4d814035427a50d6c2`):

```
[human]: Action required: call `todo_complete(todo_id="todo_1")` to mark your current task done. ...
[assistant]: <|tool_call>call:todo_complete(todo_id="todo_1")<tool_call|>
[human]: Action required: call `todo_complete(todo_id="todo_1")` to mark your current task done. ...
[assistant]: <|tool_call>call:todo_complete(todo_id="todo_2")<tool_call|>
```

**Implication:** This is a self-reinforcing loop. The harness's recovery
prompt at `src/graph.py:1373` showed Python parens as the example, the
model copied them, the parser correctly rejected the result, the nudge
fired again with the same parens example, and the loop ran 1385 iterations.

Three sources of parens-style examples in conversation context:

| Source | File | Visibility |
|---|---|---|
| Recovery nudge `Action required: call ...` | `src/graph.py:1373` | Every iteration the model fails to call a tool |
| Todo-list footer `Tools: Use todo_complete(...)` | `src/managers/todo.py:409-411` | Every strategic-phase prompt (Layer-2 inject) |
| `todo_complete` tool docstring | `src/tools/core/todo.py:174, 196, 207` | Every request — LangChain serializes `__doc__` into `tools[].function.description` |

The model wasn't drifting from generic training. It was copying our
nudge.

### Finding 4 — vLLM reasoning parser: not exercised on cluster

**Diagnostic:** Scenarios A, B, D (default thinking + forced
`chat_template_kwargs.enable_thinking=True`).

**Result on cluster `gemma-4-moe` and `gemma-4-31b`:** `reasoning_content`
field absent on every scenario. `content` clean (no `<|channel>thought`
delimiter leak).

**Result on `gemma-4-moe-strix`:** `reasoning_content` populated on every
scenario, including with `tools=[]` (model's chain-of-thought is correctly
separated from the structured tool call).

**Implication:** The cluster vLLM has `--reasoning-parser gemma4` enabled
in its entrypoint
(`Scripts-and-Notebooks/llm_containers/gemma4-moe-vllm/entrypoint.sh:142,185`),
so the flag is set — but the parser never matches anything on this
deployment. The `chat_template_kwargs.enable_thinking=True` knob we tried
had no effect. Hypotheses:

1. The chat template loaded with the model doesn't gate any block on
   `enable_thinking` — the kwarg goes nowhere.
2. The router strips `chat_template_kwargs` from extra request fields
   before forwarding to vLLM.
3. Gemma 4 needs a different thinking-mode trigger (e.g. a `<|think|>`
   system-prompt prefix, a special "developer" role, or an instruction-tuned
   thinking-prompt phrase).

The cockpit-screenshot leak (`<|channel>thought ... <channel|>` text in
the user-facing transcript) must come from a code path different from
what we tested. Likely candidates: persistent-graph (interactive) sessions,
a specific prompt that triggers spontaneous thinking, or a model variant
that emits the channel without the kwarg.

**Not blocking the loop fix** — flagged for follow-up.

### Finding 5 — Strix vs cluster: different inference engines entirely

**Diagnostic:** Inspected
`HomeLab/deployments_unmanaged/gemma4-moe-strix/deployment.yaml:24-31`.

| Backend | Engine | Image | Runtime | Model format |
|---|---|---|---|---|
| `gemma-4-moe` (cluster) | vLLM 0.19.x | `vllm/vllm-openai:v0.19.1` (or our custom container) | NVIDIA CUDA on workstation, VPN-tunnelled | HF safetensors |
| `gemma-4-moe-strix` | llama.cpp `llama-server` | `ghcr.io/knaeckebrothero/gemma4-moe-strix-llamacpp:sha-aed9348` | AMD ROCm 7.1 on Strix Halo APU (gfx1151) | gguf (`gemma-4-26B-A4B-it-UD-Q5_K_XL.gguf`) |

**Implications for fix #4** (multi-backend failover for `gemma-4-moe`):

- A multi-backend route would need to **normalise** responses across the
  two engines. `reasoning_content` populates on strix but not on cluster
  vLLM; tool-call parsing semantics differ; chat template behaviour
  differs. Users routed to different backends would see inconsistent
  thinking visibility unless we wrap and unify.
- Failover isn't a free lunch. The strix backend is gguf-quantised
  (Q5_K_XL) — quality is closer to but not identical to BF16 cluster vLLM.
- The strix backend handles thinking-mode without our help. If we ever
  want consistent reasoning visibility, we may want to prefer strix or
  fix the cluster vLLM chat template.

### Finding 6 — Hardcoded fallback prompts can't be tuned per model family

**Discovery context:** While patching the parens-teaching prompts in
finding 3, it became clear that these strings live scattered through
the code rather than in the matrix-driven prompt system. They can't be
overridden per model family because they're string literals.

**Identified hardcoded prompts:**

- `src/graph.py:1373` — `injected_reminder` recovery nudge (HumanMessage)
- `src/managers/todo.py:409-411` — `format_for_layer2()` todo-list tools footer
- `src/tools/core/todo.py:148` — `next_phase_todos` transition hint string
- `src/tools/core/todo.py:91-92` — `next_phase_todos` docstring postlude
- `src/tools/core/todo.py:174, 196, 207` — `todo_complete` docstring + runtime error
- `src/tools/core/todo.py:323` — `todo_rewind` docstring referencing `todo_complete`

These fight the matrix system: the system prompt and instructions are
matrix-driven and family-aware (we ship `_gemma`, `_minimax`, `_gpt_oss`,
`_gpt_5` variants), but every guardrail string the harness injects at
runtime is hardcoded. The mismatch is the structural weakness that let
finding 3 happen.

There are likely more hardcoded prompts elsewhere (other tool
docstrings, error messages, recovery messages); we did a partial sweep
of `src/tools/git/git_tools.py` and `src/tools/shell/shell_tools.py`
and confirmed `Examples:` blocks there also use parens-style. Those
didn't trigger the production loop because the loop centred on
`todo_complete`, but the same drift could happen on any tool the
harness nudges the model to call.

## Fixes Shipped This Session

### Shipped 1 — Format-neutral runtime nudges (model-agnostic)

Patched the three sources from finding 3 to format-neutral prose. After
this fix, no conversation context source teaches Python parens syntax.

| File | Change |
|---|---|
| `src/graph.py:1373` | `Action required: complete the current todo \`{first.id}\` now by invoking the \`todo_complete\` tool. ... Use the tool-call format defined in your system prompt — do not type the call as plain text.` |
| `src/managers/todo.py:409-411` | `Tools: \`todo_complete\` (mark a task finished — pass the todo id). ... Invoke them via the tool-call format defined in your system prompt.` |
| `src/tools/core/todo.py:91-92` | `complete your current strategic todo by invoking the \`todo_complete\` tool` |
| `src/tools/core/todo.py:148` | `Invoke the \`todo_complete\` tool to transition to tactical phase.` |
| `src/tools/core/todo.py:174, 195-196` | `Invoke with no arguments...` / `Invoke with todo_id set to a specific id...` |
| `src/tools/core/todo.py:207` | `Invoke \`todo_complete\` with todo_id set to \`{ids[0]}\` first, then invoke it again for each subsequent task.` |
| `src/tools/core/todo.py:323` | `use the \`todo_complete\` tool for that` |

**Validated:** 52 tests in `tests/test_graph_helpers.py` pass after
the changes. AST parse OK on all three files.

**Not validated:** behaviour on a real Gemma worker job. Open until a
real-job validation run completes.

### Shipped 2 — Diagnostic script extended (`tests/manual_test_gemma_reasoning.py`)

Six scenarios per model now:
- A: thinking, non-stream, default chat template
- B: thinking, streaming, default chat template
- C: tool-call, non-stream, structured `tools=[]`
- D: thinking, non-stream, `chat_template_kwargs.enable_thinking=True`
- D_stream: thinking, streaming, `enable_thinking=True`
- E: tool described in system prompt, no `tools=[]` envelope

Summary table reports `reasoning_content` populated/absent and content
leak status per scenario, with cross-check verdicts that interpret the
A vs D and C vs E combinations.

### Shipped 3 — Issue doc updates

`docs/issues/gemma_tool_call_parser_loop.md` updated with:
- New status row #2c
- New "Root cause 1b" section with iter-21 audit evidence
- New Fix 2c section
- Strix vs cluster backend stack difference paragraph
- TL;DR amended to flag the recovery-nudge mechanism as the actual root
  cause (not generic training drift)

## Proposed Follow-ups

### Proposal A — Guardrails matrix section (high-leverage, structural fix)

**Problem:** Hardcoded fallback prompts can't be tuned per model family
(finding 6). The format-neutral fix in shipped #1 works for any model
but is less load-bearing than concrete examples in the canonical format
would be.

**Design sketch:**

New top-level section in `config/model_config_matrix.yaml` parallel to
`prompts:` and `instructions:`:

```yaml
default:
  guardrails:
    todo_complete_nudge: guardrails/todo_complete_nudge.txt
    todo_list_tools_footer: guardrails/todo_list_tools_footer.txt
    next_phase_transition_hint: guardrails/next_phase_transition_hint.txt
    no_tool_call_recovery: guardrails/no_tool_call_recovery.txt
gemma:
  guardrails:
    todo_complete_nudge: guardrails/todo_complete_nudge_gemma.txt
    ...
gpt-oss:
  guardrails:
    todo_complete_nudge: guardrails/todo_complete_nudge_gpt_oss.txt
    ...
```

New directory `config/guardrails/` (parallel to `config/prompts/` and
`config/templates/`). Each file is a Jinja+`str.format()` template with
named placeholders (`{todo_id}`, `{pending_count}`, `{todo_lines}`).

`src/core/loader.py` extended with `resolve_guardrail(name, family,
**placeholders) -> str`, mirroring the existing prompt/instruction
resolution path. Family resolution falls through to default.

Call sites updated:
- `src/graph.py:1373` — replace inline string with
  `loader.resolve_guardrail("todo_complete_nudge", family, todo_id=...,
  pending_count=..., todo_lines=...)`
- `src/managers/todo.py:409-411` — replace inline string with
  `loader.resolve_guardrail("todo_list_tools_footer", family)`
- `src/tools/core/todo.py:148` — replace return string with
  `loader.resolve_guardrail("next_phase_transition_hint", family)`

**Defer to a second pass:**
- Tool docstring family-awareness. Docstrings are evaluated at
  module-import time so LangChain reads `__doc__` once when binding
  tools. Two options: (1) override `description` post-bind (LangChain
  supports overriding `description` on the bound `StructuredTool`), or
  (2) keep docstrings format-neutral and let only the runtime nudges go
  family-aware. Option (2) is simpler; the runtime nudge is higher
  leverage (fires every iteration vs once per request).
- `git_tools.py` / `shell_tools.py` parens examples — same mechanism
  would handle these once the docstring family-awareness path is built.

**First-pass scope:** matrix + loader infra + three guardrails (default
+ Gemma) + tests. ~6 small new files, ~30 lines in `loader.py`, three
call-site edits.

### Proposal B — Validate shipped fixes on a real Gemma worker job

**Why:** Everything in shipped #1, #2, #3 plus the prompt fixes from the
prior session (#1 streak detector, #2a/#2b prompt variants) are
"code-complete, behaviour-unverified". The whole stack only matters if
it actually drops the parens-emission rate to near-zero on a real
job.

**How:** Pick a moderate-complexity task (e.g. a research write-up that
exercises read_file → kb_write → next_phase_todos → todo_complete loops
without web browsing requirements), dispatch to `gemma-4-moe`, monitor
for parens-style emissions in the audit. Pass criterion: zero
`<|tool_call>call:fn(...)<tool_call|>` patterns across at least one
strategic→tactical→strategic phase cycle.

### Proposal C — Investigate reasoning-channel leak path

**Why:** Finding 4 is unfinished. The cockpit screenshot showed
`<|channel>thought ... <channel|>` leaking into user-facing content,
but our diagnostic can't reproduce that on direct API calls. The
production path doing it must be different.

**Suggested probes:**
1. Check whether the persistent-session graph (`src/persistent_graph.py`)
   sends a different request shape than the worker graph.
2. Try `<|think|>` as a system-prompt prefix in scenario D.
3. Try a more nuanced thinking-prompt like "Think carefully before
   answering. ..." or a developer-role system message.
4. Inspect the deployed chat template via vLLM's
   `/v1/chat/completions/chat_template` introspection endpoint if the
   router exposes it.
5. Confirm the screenshot is from cluster vLLM and not strix
   llama-server (strix DOES surface `<|channel>thought` content via
   `reasoning_content` natively).

**Not blocking the loop fix.** Track separately.

### Proposal D — Sweep tool docstring parens examples (low priority)

**Why:** `git_tools.py`, `shell_tools.py`, and likely others still
have `Examples: shell_execute(command="...", name="...")` style blocks.
The same drift mechanism could apply if the harness ever nudges the
model to call those tools.

**Defer until:** proposal A's guardrail mechanism is built (it gives us
a cleaner path to family-aware tool descriptions without rewriting
every docstring).

### Proposal E — Backend failover for `gemma-4-moe` (open from prior incident)

Carried over from `gemma_tool_call_parser_loop.md` fix #4. Now scoped
with finding 5 context: failover requires response normalisation across
the two engines, not just routing. Defer until the loop fix is
validated; don't add complexity if Gemma worker dispatch isn't going to
be a hot path.

## What We're NOT Doing

- **Switching off Gemma 4 for worker jobs.** The model is fine when
  given the right prompts. Finding 1 confirms parser+model work in
  isolation; the bug was on our side.
- **Patching vLLM.** The parser is correct and strict. There is no
  upstream fix to wait for.
- **Adding a leniency mode to our parsing layer.** Working around
  malformed tool calls would mask the same class of bug for other
  models. The streak detector (#1 from the prior session) is the right
  safety net.
- **Sweeping every Python-style docstring example codebase-wide right
  now.** Targeted fixes on the known loop-trigger (`todo_complete`)
  plus the guardrails infrastructure (proposal A) is a higher-leverage
  path than a one-off sweep.

## Files Touched This Session

```
M  src/graph.py                                      (recovery nudge)
M  src/managers/todo.py                              (todo-list footer)
M  src/tools/core/todo.py                            (5 docstring/error sites)
M  tests/manual_test_gemma_reasoning.py              (added scenarios D, D_stream, E)
M  docs/issues/gemma_tool_call_parser_loop.md        (status table, root cause 1b, fix 2c, strix backend note)
A  docs/issues/gemma_session_findings.md             (this doc)
```

## 2026-05-06 Validation Run

Re-ran the diagnostic script against `gemma-4-moe` after the 2026-05-05
patches. Three results to log.

### Scenario F — implemented, **clean**

Production-shape replay of job `3c30d72e` mid-job conditions (proposal B
above) is now in `tests/manual_test_gemma_reasoning.py` as
`run_prod_shape_tool_nonstream`:

- Two system messages totalling ~2.4k tokens — rendered
  `systemprompt_gemma` + tactical phase directive + task instructions.
- 38 OpenAI-format tool definitions mirroring the worker registry
  (`read_file`, `write_file`, `kb_search`, `kb_write`, `cite_*`,
  `web_search`, `search_papers`, git tools, shell tools, todo
  management, …).
- Multi-turn history with three prior assistant `tool_calls`
  (kb_search → read_file → write_file), each followed by a substantive
  tool result (KB search excerpt, plan.md content, write confirmation).
- Final `user` turn = verbatim format-neutral recovery nudge from
  `config/guardrails/gemma.yaml` `todo_action` plus the suffix wired
  in `src/graph.py:1380`.
- Total: `prompt_tokens=5028`, `completion_tokens=47`,
  `finish_reason=tool_calls`.

**Result:** structured `tool_calls[0]: name=todo_complete args={"result":
"Searched KB for ..."}`. Content empty, no leaked delimiters,
`wire=(none detected)`. The model emitted exactly the canonical brace
form, vLLM's `gemma4` parser lifted it into structured tool_calls, and
the harness would proceed cleanly.

**Implication:** The 2026-05-05 prompt fixes hold up at production
scale. The hypothesis "prompt scale alone triggers parens drift even
with `tools=[]`" is **disconfirmed at ~5k prompt tokens**. **Proposal
B status: DONE / PASSED.**

Caveat: F is still smaller than the real failing job (~5k vs ~30k
prompt tokens at iter 18-21). If a future failure recurs at higher
scale, F can be extended (longer history, larger tool_results) before
concluding scale is irrelevant. For now the smaller repro is sufficient
evidence that the patches do their job under realistic-but-moderate
load.

### Scenarios D and D_stream — re-run, still empty

Both D variants (`chat_template_kwargs.enable_thinking=True`,
non-stream and stream) returned `reasoning_content=empty/absent` on
this run, identical to the 2026-05-05 result. The kwarg is confirmed
not to activate the reasoning channel on this deployment. One subtle
data point worth recording: D's `completion_tokens=490` was nearly
double A's `completion_tokens=259` on the same prompt, and the two
runs picked different mathematical approaches (A: distributive
property; D: difference of squares). The kwarg is having *some*
prompt-level effect, just not the structural one we want — vLLM's
parser is not lifting any of the longer output into
`reasoning_content`.

**Implication:** Eliminates "we just need to send the right kwarg" as
a quick fix. Among the three hypotheses listed in finding 4,
hypothesis 3 ("Gemma 4 needs a different thinking-mode trigger") is
now the working one. The thinking-mode investigation needs in-script
probes to locate the actual activation knob before any agent-side
change makes sense — wiring `enable_thinking=True` into
`config/settings_matrix.yaml` would just send a flag we already know
does nothing.

Probes ahead, to be added to the diagnostic script:

1. **Deployment introspection.** `GET /v1/models/{id}` and
   `POST /v1/tokenize` with `add_generation_prompt=True` — read what
   the chat template actually renders, including any thinking-mode
   tokens the template inserts automatically.
2. **Scenario G — `<|think|>` literal token as system-prompt prefix.**
   Some chat templates gate thinking on a literal token in the
   prompt rather than a kwarg.
3. **Scenario H — `developer`-role system message** instructing the
   thought-channel format explicitly. OpenAI introduced `developer`
   role in late 2024; some chat templates branch on it.
4. **Scenario I — thinking prompt combined with `tools=[]`
   envelope** (`tool_choice="none"` so the model isn't forced to
   call a tool). Tests whether tools-mode forces thinking off, which
   would make production thinking impossible regardless of activation
   knob — every worker request ships with `tools=[]`.

**Proposal C status: NARROWED.** No longer "investigate the leak path"
 — now "find the activation knob, then audit `src/persistent_graph.py:722`
and `src/core/archiver.py:599` to confirm the captured field flows
through both worker and persistent paths."

### Scenario E — re-run, reproduced

`gemma-4-moe` again emitted
`<|tool_call>call:get_weather{city:<|"|>Tokyo<|"|>}<tool_call|>`
perfectly when the tool was described in the system prompt without
a `tools=[]` envelope. vLLM did not lift it: `finish_reason=stop`,
`tool_calls=null`, `wire_format_hits=['canonical_braces']`.
Reproduces 2026-05-05 result; no behavioural drift.

**Implication:** vLLM's `gemma4` tool-call parser remains gated on
`tools=[]` being in the request. No change required — flagged here
only as a reproducibility check.

### Activation hunt — all knobs failed

Added scenarios G, H, I and `probe_deployment()` to
`tests/manual_test_gemma_reasoning.py` and re-ran against `gemma-4-moe`.
All four activation paths returned `reasoning_content=empty/absent`:

- **G** — `<|think|>` literal token as a system-prompt prefix.
- **H** — `developer`-role system message instructing thought-channel
  format (request was accepted, no HTTP error — the role parses, just
  doesn't trigger thinking).
- **I** — thinking prompt + `tools=[]` envelope with `tool_choice="none"`.
- **D / D_stream re-run** — `chat_template_kwargs.enable_thinking=True`,
  same as the 2026-05-05 run.

Deployment introspection probes:

- `GET /v1/models/{id}` — returns a minimal model card with no
  `chat_template` field. The router doesn't expose the template that
  way.
- `POST /v1/tokenize` — HTTP 404. Endpoint not enabled on this
  deployment, so we can't read the rendered prompt to see what tokens
  the chat template inserts automatically.

**Most informative single data point:** D's `completion_tokens=790` vs
A's `completion_tokens=270` on the same prompt. The kwarg IS reaching
vLLM and IS changing the rendered prompt — D's output is nearly 3×
longer and consistently picks the difference-of-squares approach
instead of A's distributive-property approach. So the chat template
responds to `enable_thinking=True` *somehow*; it just doesn't add the
structural channel-opener tokens that
`--reasoning-parser gemma4` scans for.

### Deployed cluster ground truth

Inspected `Scripts-and-Notebooks/llm_containers/gemma4-moe-vllm/`:

| Knob | Value |
|---|---|
| Default `MODEL` | `google/gemma-4-26B-A4B-it` (`entrypoint.sh:109`) |
| Base image | `vllm/vllm-openai:v0.19.1` (`Dockerfile:27`) |
| `TOOL_CALL_PARSER` | `gemma4` (`entrypoint.sh:141`) |
| `REASONING_PARSER` | `gemma4` (`entrypoint.sh:142`, README claim: "Extracts thinking content") |
| Auto tool choice | enabled — `--enable-auto-tool-choice --tool-call-parser gemma4` |

The container README says: *"Gemma 4 tool parser is young — pin vLLM ≥
0.19.0 for fixes (PR #38847, #39468)"*. So the operator team
believed the reasoning parser would lift thinking content. The flags
are wired correctly.

### What "the model isn't thinking" actually means

The model **is** producing chain-of-thought prose in every scenario —
it breaks down the multiplication, picks methods, shows work. The
reasoning is real. What's missing is the structural marker layer.
vLLM's `--reasoning-parser gemma4` looks for
`<|channel>thought ... <channel|>` delimiters in the output stream
and lifts the wrapped content into `reasoning_content`. Our model
never emits those delimiters, so the parser has nothing to lift,
regardless of which activation we try.

Two cleanly separable causes (the website-search Step 4 below should
pin which one applies):

1. **The deployed model variant doesn't emit channel tokens at all.**
   Even if base Gemma 4 was trained with thinking-channel emission,
   `google/gemma-4-26B-A4B-it` (the instruction-tuned MoE) may have
   shed that capability during fine-tuning, or the README hint about
   "lower capability ceiling than the 31B dense" may include thinking.
   No chat-template knob can recover what the model can't emit.
2. **The chat template has no thinking branch — only a "weak" one.**
   D's `completion_tokens=790` proves the template responds to
   `enable_thinking=True`, but probably with a natural-language nudge
   ("think step by step") rather than channel-opener tokens. A
   different model with the same template might emit channels; ours
   doesn't.

These are testable cheaply:

1. **Pull `tokenizer_config.json` from HuggingFace** for
   `google/gemma-4-26B-A4B-it`. Read the `chat_template` Jinja. If any
   branch emits `<|channel>thought`, cause #2 is dead — model variant
   is the issue. If no branch ever emits it, cause #2 is the issue
   (and a chat-template override would unblock thinking).
2. **Read vLLM's `gemma4` reasoning parser source code** to confirm
   what tokens it actually scans for. We assumed `<|channel>thought`
   based on the leaked-content delimiter pattern from the cockpit
   screenshot; vLLM may scan for a different opener.
3. **Web-search for "google/gemma-4-26B-A4B-it" + thinking / channel /
   reasoning** to see whether anyone else has gotten `reasoning_content`
   populated on this model under vLLM, and if so, what config they
   used.

### Cockpit screenshot reframed

Confirmed by user: the screenshot leak came from **cluster
`gemma-4-moe`** (not Strix) in a **persistent (interactive) session**
(not a worker job). That's significant because:

- Our diagnostic uses raw httpx + `/v1/chat/completions` non-streaming
  (worker shape). The persistent path uses LangChain's `astream` via
  `ChatOpenAI` → `ReasoningChatOpenAI` (`src/llm/reasoning_chat.py`),
  consumes per-chunk deltas through `src/persistent_graph.py:722`,
  separately accumulates `delta.content` and `delta.reasoning_content`,
  and surfaces them to the cockpit via different callback events.
- If the screenshot showed `<|channel>thought ... <channel|>` text in
  the user-facing transcript despite cluster-side parser introspection
  always finding it idle, the leak likely lives in the persistent
  streaming path or in cockpit's rendering — not in cluster parser
  configuration.

Updated working hypothesis for the screenshot, in priority order:

1. **Streaming-only delimiter-boundary bug.** vLLM's reasoning-parser
   state machine (the streaming variant, separate from non-streaming)
   may match `<|channel>thought` on some delta-chunk boundary
   conditions but not others. If a chunk arrives mid-delimiter, the
   parser may surface the partial open tag in `delta.content` before
   later "correcting" it. Streaming consumers that never receive the
   correction render the leak.
2. **Cockpit reasoning rendering.** The cockpit may display
   `reasoning_content` by re-wrapping it in delimiters for the user
   ("here's the thinking") and then incorrectly overlay it onto the
   chat history. Worth auditing how `simple/` and `pages/` render
   reasoning events.
3. **A persistent-session-only flag.** `src/persistent_graph.py` or
   `src/api/persistent_session.py` may set a different `extra_body`
   that activates thinking — something we don't send from the
   diagnostic. Worth grepping the persistent path for
   `extra_body`, `chat_template_kwargs`, `enable_thinking`.

Concrete next probes for the screenshot investigation (separable from
the cluster activation question, and actually higher-leverage now):

1. **Re-run the thinking diagnostic via LangChain `ChatOpenAI` +
   astream** rather than raw httpx — same target model, same prompt,
   same activation kwargs, but production's exact code path. If
   `reasoning_content` populates this way but not the raw httpx way,
   the answer is "the persistent streaming path receives signals the
   non-streaming path doesn't" and we have our screenshot source.
2. **Grep `src/persistent_graph.py`, `src/api/persistent_session.py`,
   `src/llm/reasoning_chat.py` for `extra_body`,
   `chat_template_kwargs`, `enable_thinking`, `<|think|>`** — whatever
   activation knob the persistent path uses (if any). Quick.
3. **Find the actual screenshot job/session.** The cockpit chat
   history should preserve enough metadata to identify which
   conversation it came from. Extracting the actual prompts from that
   session would let us replay it locally.

Until #1 or #2 produces signal, the working explanation for the
screenshot is **persistent-path streaming code (LangChain wrapper,
cockpit rendering, or a session-only kwarg) leaks delimiters into
user-visible content** — NOT the cluster activation gap that today's
hunt failed to close.

### Files Touched 2026-05-06

```
M  tests/manual_test_gemma_reasoning.py              (added scenario F: run_prod_shape_tool_nonstream + ~38 tool defs + multi-turn history + recovery-nudge user turn; later added scenarios G/H/I + probe_deployment for the activation hunt)
M  docs/issues/gemma_session_findings.md             (this section)
```

## References

- `tests/manual_test_gemma_reasoning.py` — diagnostic script (10 scenarios as of 2026-05-06: A/B baseline thinking, C/E/F tool-call shape, D/D_stream `enable_thinking` kwarg, G/H/I thinking-activation hunt, plus per-model `/v1/models` and `/v1/tokenize` introspection probe)
- `docs/issues/gemma_tool_call_parser_loop.md` — incident-focused issue doc
- `docs/model_issues.md` — earlier failed-job notes
- `Scripts-and-Notebooks/llm_containers/gemma4-moe-vllm/entrypoint.sh` — cluster vLLM launch flags
- `HomeLab/deployments_unmanaged/gemma4-moe-strix/deployment.yaml` — strix llama.cpp deployment
- MongoDB doc IDs (cluster orchestrator audit DB):
  - `69f9ae29814035427a50d671` — job 3c30d72e iter 0 (clean strategic phase request)
  - `69f9ae49814035427a50d6b9` — iter 18 (first parens drift; 30k prompt tokens)
  - `69f9ae4d814035427a50d6c2` — iter 21 (smoking gun: recovery nudge teaches parens, model copies them back)

## 2026-05-07 — Activation gap resolved: vLLM #38855

After the 2026-05-06 hunt failed to find an activation knob, web research
on `google/gemma-4-26B-A4B-it` + vLLM thinking pinned the real cause.
**Neither the model variant nor the chat template is the problem** — both
are correctly configured for thought-channel emission. The activation
hunt was searching the wrong layer.

### Cause: vLLM tokenizer detokenization strips channel tokens before parsing

Per the HuggingFace model card for `google/gemma-4-26B-A4B-it`:

- The model **does** emit `<|channel>thought\n[reasoning]<channel|>[answer]`
  natively when thinking is activated.
- Activation is `<|think|>` token (id `98`) at the start of the system
  prompt — which is exactly what
  `chat_template_kwargs.enable_thinking=True` injects via the model's
  bundled chat template.
- vLLM's `--reasoning-parser gemma4` (a purpose-built parser, not generic)
  is wired to lift those exact delimiters into `reasoning_content`.

So why was scenario D returning empty `reasoning_content` despite
`completion_tokens=790` (model thinking 3× longer than baseline)?

**vLLM Issue #38855:** the OpenAI-compatible chat completion endpoint
runs detokenization with `skip_special_tokens=True` by default, **which
strips `<|channel>` / `<channel|>` from the text stream BEFORE the
gemma4 reasoning parser scans for them**. The parser sees text without
delimiters, has nothing to lift, returns empty `reasoning_content`. The
model's reasoning prose ends up in `content` (without the delimiters,
since they were already stripped — which is exactly why scenario A's
`content` looked clean and conversational rather than channel-formatted).

This explains every previously-puzzling observation:

| Observation | Now explained |
|---|---|
| D `completion_tokens=790` vs A `=270` | Model IS thinking longer with the kwarg; the kwarg works |
| D `reasoning_content` always empty | Delimiters stripped before parser sees them |
| D `content` reads as long natural-language reasoning | Delimiters stripped, prose remains |
| Scenario E (no `tools=[]`) shows `wire_format_hits=['canonical_braces']` for tool calls | Tool-call parser's `<|tool_call>` markers ARE preserved in some code paths — confirms not all special tokens are stripped uniformly. The reasoning markers specifically suffer #38855. |
| All of G/H/I empty | Same root cause; activation isn't the problem |

### The fix: two-knob combo from the vLLM Gemma 4 recipe page

```python
extra_body={
    "skip_special_tokens": False,
    "chat_template_kwargs": {"enable_thinking": True},
}
```

Captured in `tests/manual_test_gemma_reasoning.py` as:

- **Scenario J** — non-streaming with both flags. Confirms the workaround
  on the cluster.
- **Scenario K** — same payload, streaming. Tests whether the streaming
  reasoning parser (a separate incremental state machine) is fixed by
  the same flag.

Pending validation run. Expected outcomes:

- J populated + content clean → confirms #38855; fix is one config-matrix entry.
- K populated → both paths fixed; cockpit leak is downstream rendering.
- K empty/LEAKED → streaming parser still bugged; persistent path needs separate mitigation.

### Persistent-path audit: no divergent request envelope

Grepped `src/persistent_graph.py`, `src/api/persistent_session.py`,
`src/api/persistent_app.py`, `src/llm/reasoning_chat.py`,
`src/core/loader.py`, `orchestrator/`, and `config/` for
`extra_body`, `skip_special_tokens`, `chat_template_kwargs`,
`enable_thinking`, `<|think|>`, `<|channel>`. **Zero hits anywhere
except `src/core/loader.py`, where `extra_body` carries only
`top_k`** (lines 1932-1937, 2225-2227, 2344-2346 for the three
provider branches).

This kills hypothesis 3 from the 2026-05-06 cockpit-screenshot section
("a persistent-session-only flag activates thinking"). Worker and
persistent paths share `loader.py` and ship **identical request
envelopes** to vLLM. The screenshot leak cannot be attributed to a
divergent request shape — it must come from one of:

1. **vLLM streaming-parser variant of #38855** — the streaming
   incremental state machine may handle delimiter boundaries differently
   from the non-streaming path, leaking partial channel markers into
   `delta.content` chunks. Scenario K resolves this.
2. **Cockpit rendering** — reasoning content is rendered with
   delimiter wrapping somewhere in `cockpit/src/app/simple/` or
   `cockpit/src/app/pages/` chat views, then incorrectly overlaid on
   the chat history. Audit downstream of `PersistentChatService`
   reasoning event handlers.
3. **LangChain wrapper passthrough** — `src/llm/reasoning_chat.py`
   accumulates `reasoning_content` from delta chunks into
   `additional_kwargs`. If a delta contains `<|channel>thought` text
   in the `content` field (because the streaming parser leaked it),
   the wrapper has no logic to strip it — it just forwards content to
   the consumer.

### Open question: why does the screenshot show delimiters at all?

If both worker and persistent paths send identical envelopes WITHOUT
`skip_special_tokens=False`, vLLM should be stripping delimiters on
both — yet the cockpit screenshot showed raw `<|channel>thought ...
<channel|>` text in user-visible content. Two possibilities:

- **Streaming detokenization races.** vLLM's streaming detokenizer may
  surface a partial delimiter (e.g. `<|channel>` arrives in chunk N,
  `thought` in chunk N+1) before `skip_special_tokens=True` is applied
  retroactively. The user briefly sees the partial token; the
  finalization "corrects" it but the streaming consumer already
  rendered.
- **The screenshot session ran on a different deployment version.**
  vLLM #38855 is open against current main; the cluster is on
  `vllm/vllm-openai:v0.19.1`. If 0.19.1 stripped tokens unreliably and
  the bug was tightened in a later patch (or vice versa), behaviour
  would have been different at screenshot time.

Both are answerable from the current diagnostic + a re-screenshot of
a fresh persistent session against the same model. Defer until J/K
results are in.

### Action items (in order)

1. **Run scenarios J/K** against `gemma-4-moe`. Confirm or rule out the
   non-streaming workaround and the streaming variant.
2. **If J passes**: add a `gemma` family entry under
   `config/settings_matrix.yaml` (or wherever provider-specific
   `extra_body` lives — see `src/core/loader.py:1935`) that injects
   `{"skip_special_tokens": False, "chat_template_kwargs":
   {"enable_thinking": True}}` for every Gemma request. The capture
   path in `src/llm/reasoning_chat.py` (lines 192-214 store
   `reasoning_content` into `additional_kwargs`) and the persistent
   consumer at `src/persistent_graph.py:722` are already wired
   correctly — they just never receive the field today.
3. **If K passes**: persistent path is fully fixed by (2). Close
   proposal C as resolved.
4. **If K fails**: streaming-only mitigation needed. Options: keep
   thinking off on persistent path (override the matrix entry), wait
   for vLLM patch, or strip delimiter sequences in
   `src/llm/reasoning_chat.py` post-receive.
5. **Audit cockpit reasoning rendering** regardless of K outcome —
   the screenshot leak preceded any of these fixes, so the rendering
   path needs to handle delimiter-bearing content gracefully (it should
   never leak markers to the user even if vLLM ships them).

Note also **vLLM Issue #39130**: setting `enable_thinking=False`
silently disables xgrammar (structured output enforcement). Worth
remembering if we ever opt to disable thinking per-job for cost
reasons — we'd lose schema enforcement on tool calls as a side effect.

### Files Touched 2026-05-07

```
M  tests/manual_test_gemma_reasoning.py              (added scenarios J + K: vLLM #38855 workaround non-stream + stream)
M  docs/issues/gemma_session_findings.md             (this section)
```

### Web sources

- vLLM gemma4_reasoning_parser API docs — https://docs.vllm.ai/en/latest/api/vllm/reasoning/gemma4_reasoning_parser/
- vLLM Gemma 4 Usage Guide (recipes) — https://docs.vllm.ai/projects/recipes/en/latest/Google/Gemma4.html
- vLLM Issue #38855 — gemma4 reasoning parser fails: `<|channel>` tokens stripped before parsing — https://github.com/vllm-project/vllm/issues/38855
- vLLM Issue #39130 — `--reasoning-parser gemma4` silently disables xgrammar when `enable_thinking=false` — https://github.com/vllm-project/vllm/issues/39130
- HuggingFace `google/gemma-4-26B-A4B-it` model card — https://huggingface.co/google/gemma-4-26B-A4B-it
- HuggingFace `gemma-4-31B-it` discussion #28 — missing reasoning field on vLLM — https://huggingface.co/google/gemma-4-31B-it/discussions/28
- Google AI for Developers — Thinking mode in Gemma — https://ai.google.dev/gemma/docs/capabilities/thinking
- vLLM blog — Announcing Gemma 4 on vLLM — https://vllm-project.github.io/2026/04/02/gemma4.html
- vLLM Recipes — `google/gemma-4-26B-A4B-it` — https://recipes.vllm.ai/Google/gemma-4-26B-A4B-it
- vLLM-project recipes repo — Gemma4.md — https://github.com/vllm-project/recipes/blob/main/Google/Gemma4.md
