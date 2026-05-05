---
tags:
  - debugging
  - llm
  - gemma
  - tool-calls
  - stuck-detection
  - infrastructure
related:
  - "[[job_debug]]"
  - "[[job_debug2]]"
  - "[[model_issues]]"
---

# Gemma-4-MoE Tool-Call Parser Loop + Backend Exhaustion

**Job ID:** `3c30d72e-9580-46ae-ab12-89dd10085f68`
**Date:** 2026-05-05
**Model:** `gemma-4-moe`
**Status:** Cancelled after ~72 min wall-clock (~28 min compute)
**Task:** Redesign `bad-orb.kueppelsmuehle.de` based on a provided modern mockup
**Audit entries:** 1503 · **LLM requests:** 1403 · **Tool calls executed:** 0 from iter 18 onward

## Status (2026-05-05)

| Fix | Status | Validation |
|-----|--------|-----------|
| #1 — No-tool-call streak detector (model-agnostic safety net) | **Shipped** | 11 unit tests pass; would catch this incident at iter ~22 instead of iter 1403 |
| #2a — Gemma prompt variants (systemprompt/persona/strategic/tactical/summarization) | **Shipped** | End-to-end Jinja+format pipeline validated; canonical wire-format anchor at top + bottom of systemprompt, 6 worked examples in tactical |
| #2b — Gemma instructions variant (`instructions_gemma.md`) | **Shipped** | All 5 Python-style examples in default `instructions.md` rewritten in canonical format; wired via matrix `gemma.instructions` |
| #3 — Don't dispatch worker jobs to `gemma-4-moe` (interim) | **N/A** | Superseded by #2; can dispatch once a real-job validation passes |
| #4 — Backend failover (`gemma-4-moe` → `gemma-4-moe-strix`) | **Open** | Configmap + router multi-backend support change; not blocking |
| #5 — Cockpit visibility for `parser_failure` freezes | **Open** | API + UI work; nice-to-have |
| Per-phase `enable_thinking` toggle | **Open** | LangChain template-kwarg plumbing |
| Pre-render tool defs as `<\|tool>declaration:NAME{...}<tool\|>` | **Open** | Tool-binding layer change at LangChain/vLLM |
| Pin `--chat-template tool_chat_template_gemma4.jinja` | **Open** | Workstation deployment change |

**Validation pending:** a real Gemma worker job to confirm the parser failure rate drops from ~100% to near zero. Until that succeeds, treat fixes #1, #2a, #2b as "code-complete, behaviour-unverified."

## TL;DR

Gemma emitted what *looked* like its native tool-call wire format but with **parentheses + Python-style kwargs** instead of Gemma's canonical **curly braces + `<|"|>` string delimiters**. The vLLM `gemma4` parser regex requires `\{(.*?)\}`, so the malformed output was passed through as plain content with `tool_calls=None`. The agent's stuck detector keys on `(tool_name, args_hash)` — with `tool_name=None` there is no fingerprint, so the loop ran for **1385 iterations** before sustained pressure on the single workstation backend caused the router to start returning `'no available server'`. The job was cancelled manually 16 min later.

**Verdict: fixable on our side — keep the model.** Gemma 4 supports tool calling well when steered to its canonical format; the missing piece is `_gemma` prompt variants for the worker path (the auxiliary path already has them). vLLM's parser is correct and strict; there's no leniency flag.

Three independent failures stacked:
1. **Format-emission mismatch** — Gemma emits `(args="value")` instead of `{key:<|"|>value<|"|>}`. Model-side, prompt-fixable.
2. **Stuck-detector blind spot** — no `tool_name` ⇒ no fingerprint ⇒ no detection. Graph-side bug, model-agnostic.
3. **Server pool of one** — `gemma-4-moe` resolves to a single workstation backend behind a VPN; ~50 req/min sustained eventually exhausted it. Infra-side.

## Timeline

| Time (UTC) | Event |
|------------|-------|
| 08:17:45 | Job created (`status=created`) |
| 08:45:29 | First LLM request (iter 0) — ~28 min after creation, presumably agent dispatch/cooldown |
| 08:45:29–08:46:01 | iters 0–17: strategic phase ran cleanly. `read_file`, `kb_write`, `write_file`, `next_phase_todos` (staged 6 todos, transitioned to tactical phase 1) |
| **08:46:01** | **iter 18: first leaked-text response.** `gemma-4-moe` returns `<|tool_call>call:todo_complete(todo_id="todo_1")<tool_call|>` as content. `finish_reason=stop`, `completion_tokens=21`, `tool_calls=None`. doc_id `69f9ae49814035427a50d6b9` |
| 08:46:01–09:13:40 | iters 18–1402: stuck loop. ~1385 iterations at ~50 req/min, every one with `tool_calls=None` and the same leaked-text content. Tool calls executed: 0 |
| ~09:13:40+ | Audit entries 1425+: router returns `{'type': 'llm_error', 'message': 'no available server', 'recoverable': True, 'attempts': 4}`. Continues for ~80 attempts |
| 09:29:20 | Job cancelled (`status=cancelled`) |

`job_complete` was never called (verified — `search_audit("job_complete")` returns no matches).

## Evidence

### Leaked-text tool calls

Sample audit entries (page 48, ~iter 1411–1423):

```
[1411] LLM (gemma-4-moe): <|tool_call>call:todo_complete(todo_id="todo_1")<tool_call|>
[1412] LLM (gemma-4-moe): <|tool_call>call:todo_complete(todo_id="todo_1")<tool_call|>
[1413] LLM (gemma-4-moe): <|tool_call>call:todo_complete(todo_id="todo_1")<tool_call|>
... (1385× total)
```

Full LLM request `69f9ae49814035427a50d6b9` (iter 18):

```
Model: gemma-4-moe
Iteration: 18
Tokens: 30117 prompt, 21 completion
Finish reason: stop
Tool Definitions (48): read_file, ..., todo_complete, ..., job_complete, ...
Response: <|tool_call>call:todo_complete(todo_id="todo_1")<tool_call|>
```

The structured `tool_calls` array on the response is `None`. The model thinks it called the tool; the agent sees plain text.

### Backend exhaustion

Audit entries 1425–1503 (last ~80 entries):

```
[1424] LLM (gemma-4-moe)
[1425] ERROR: {'type': 'llm_error', 'message': 'no available server', 'recoverable': True, 'attempts': 4}
[1426] LLM (gemma-4-moe)
[1427] ERROR: {'type': 'llm_error', 'message': 'no available server', 'recoverable': True, 'attempts': 4}
... (interleaved, every other entry)
```

`recoverable: True` with `attempts: 4` — the router exhausted its retry budget and gave up each time, but the agent kept retrying at the next iter.

## Root cause analysis

### 1. Format-emission mismatch (Gemma-side, prompt-fixable)

The vLLM container is `vllm/vllm-openai:v0.19.1` running with `--enable-auto-tool-choice --tool-call-parser gemma4 --reasoning-parser gemma4` (see `Scripts-and-Notebooks/llm_containers/gemma4-moe-vllm/entrypoint.sh:139-142,181-185`).

**Canonical Gemma 4 tool-call format** (per Google's docs and the model's chat template):

```
<|tool_call>call:get_current_weather{location:<|"|>Tokyo, JP<|"|>,units:42}<tool_call|>
```

- `{...}` — curly braces around args
- `key:<|"|>string_val<|"|>` — custom string delimiters, not regular quotes
- `key:42` — bare numerics, no quotes

**vLLM `gemma4` tool parser regex** (Tier 1, primary):

```regex
<\|tool_call\>call:(\w+)\{(.*?)\}(?:<tool_call\|>|<turn\|>)
```

The parser **requires** `\{(.*?)\}`. There is no strict-mode / leniency flag — `--tool-call-parser gemma4` is the only knob.

**What our model actually emitted:**

```
<|tool_call>call:todo_complete(todo_id="todo_1")<tool_call|>
```

- `(...)` — parentheses, not braces ❌
- `todo_id="todo_1"` — Python-style kwarg with standard quotes, not `<|"|>` delimiters ❌

The regex does not match, the parser yields no tool call, the entire response is returned as content with `tool_calls=None`. The model thinks it called the tool (it produced what looks structurally like a Gemma tool call); the parser correctly refused to lift a non-canonical format.

**Why does the model emit the wrong format?** Best hypothesis: the default agent prompts teach a generic JSON/Python tool-calling format. With no Gemma-specific examples in the worker prompt path (`config/model_config_matrix.yaml:214-247` only ships `_gemma` aux variants for memory/curation), the model wraps with the right outer delimiters from its training but defaults to Python-call-syntax inside. The behaviour matches the `mlx-lm` issue #1096 ("Gemma 4 native tool calls are not parsed, so the OpenAI-compatible tool_calls field stays empty") and the symptom side of vLLM #39043 ("tool calls leak to chat") even though the cause there is undiagnosed.

This is **fixable with prompt scaffolding**, not a model capability gap.

### 2. Stuck-detector blind spot (graph-side)

`src/graph.py` stuck detection fingerprints `(tool_name, args_hash)`. When `tool_name is None`, no fingerprint exists, so the detector never trips — even though 1385 consecutive identical responses is the textbook definition of stuck.

This is **model-agnostic**: any model on any backend that emits malformed tool calls would tar-pit the agent the same way. Gemma exposed it; it isn't a Gemma-specific bug.

### 3. Server pool of one (infra-side)

`gemma-4-moe` is served by a single backend in `HomeLab/deployments_managed/model-orchestrator/10-configmap.yaml:85-93`:

```yaml
- name: "Gemma-4-MoE"
  type: "chat"
  backend: "http://127.0.0.1:18090"
  endpoint: "/v1/chat/completions"
  backend_model_name: "gemma-4-moe"
  backend_api_key_env: "WORKSTATION_BACKEND_API_KEY"
  models:
    - "gemma-4-moe"
```

The `127.0.0.1:18090` backend is a VPN-tunnelled forward to the workstation at `10.18.2.105:8090` via the `vpn-workstation` sidecar (`20-deployment.yaml:110-149`). The router itself is pinned to `replicas: 1` because its rate limiter is in-memory.

`gemma-4-moe-strix` exists in the same configmap as a separate model (in-cluster Strix Halo, `10.43.210.54:80`) but it has a **different model alias**, so it isn't a transparent failover target — the agent has to be configured to use it explicitly.

The "server pool" for `gemma-4-moe` is therefore a pool of **one** backend, with no failover. ~1400 requests in ~28 min (~50 req/min sustained, no real-work pauses since every tool call was a no-op) eventually saturated it. Without router/backend logs we can't pinpoint which: VPN tunnel reset, llama-server / vLLM OOM or restart, the router's health probe started failing, or an in-memory rate-limit threshold fired. All four converge on the same observable: `no available server`.

## Fixes

### Fix 1 — Stuck guard for "no tool emitted" — **SHIPPED**

Implemented as `_check_no_tool_call_streak()` in `src/graph.py:315-365` with closure state at `src/graph.py:535-541` and the circuit-breaker block at `src/graph.py:1083-1170`. Tracks consecutive iterations where `tool_calls_count == 0 AND content` is non-empty AND content hash matches the previous iteration. Threshold = 3 (uses strict `>`, so freezes at the 4th identical no-tool-call response). Returns `error.type = "parser_failure"` with `recoverable=False`. Includes a 500-char content sample in the error and a 200-char sample in audit warnings.

Validated by 11 unit tests in `tests/test_graph_helpers.py::TestCheckNoToolCallStreak` covering: tool-call resets, empty resets, first-call init, varied content reset-to-1, identical increment, threshold boundary, custom threshold, recovery-then-relapse, varied responses never fail, unicode hash stability.

**Behaviour against this incident:** would catch the leaked-text pattern at iter 21 (4th consecutive identical content with `tool_calls=None`) instead of iter 1403. Saves ~1380 wasted requests, prevents the cascade into backend exhaustion. The hash-match condition prevents false positives — a model writing varied natural-language reflections (e.g. between strategic-phase tool calls) won't trip it because the hash always differs.

### Fix 2a — Gemma prompt variants — **SHIPPED**

Created in `config/prompts/`:
- `systemprompt_gemma.txt` (73 lines) — format anchor at top + bottom recency anchor
- `persona_gemma.txt` (30 lines) — Markdown-style port, no XML wrappers
- `strategic_gemma.txt` (75 lines) — light format reminder, output discipline
- `tactical_gemma.txt` (78 lines) — heavy format anchor with 6 worked tool examples
- `summarization_prompt_gemma.txt` (73 lines) — explicit "JSON only, no `<|"|>` delimiters" rule

Wired in at `config/model_config_matrix.yaml` under `gemma.prompts`. End-to-end Jinja+format pipeline validated.

Format anchor block taught to the model:

```
<|tool_call>call:TOOL_NAME{arg:<|"|>string val<|"|>,n:42,b:true}<tool_call|>
```

Plus positive-contrast guidance ("curly braces — never parentheses"; "string values wrapped in `<|"|>...<|"|>` — never `"..."`"; "closing tag is `<tool_call|>` — pipe on the right, not `<|tool_call|>`"). Repeated at top and bottom of `systemprompt_gemma.txt` for U-shaped attention recency. Tactical phase carries 6 concrete worked examples covering `read_file`, `write_file`, `edit_file`, `run_command`, `kb_write`, `todo_complete`.

**Implementation note:** literal Gemma tool-call examples are wrapped in `{% raw %}...{% endraw %}` Jinja blocks with doubled braces (`{{...}}`) inside, so they survive the harness's two-stage Jinja → `str.format()` pipeline (`src/core/loader.py:2451-2470`).

### Fix 2b — Gemma instructions variant — **SHIPPED**

Created `config/templates/instructions_gemma.md` (203 lines, +1018 chars vs default). Rewrote all 5 Python-style examples in the default `instructions.md` (`read_file(path=...)`, `cite_web(url=..., claim=...)`, `kb_write(type='learning', tag='failed-approach')`, `delegate_work(tasks=[...], context=...)`, etc.) in canonical Gemma wire format. Adds a top-of-document "Tool Call Format" primer.

Wired in at `config/model_config_matrix.yaml` under `gemma.instructions.instructions`. Loaded into the agent's context every turn via `agent.py:1867-1868`. Without this fix, the prompt-side anchor saying "use braces" was contradicted by Pythonic examples in instructions.md every turn — Gemma got mixed signals. Now both pull in the same direction.

### Fix 3 — Short-term: don't dispatch worker jobs to `gemma-4-moe` — superseded

Superseded by 2a + 2b. Once a real Gemma worker job validates the format-emission rate, the model is cleared for general worker dispatch.

### Fix 4 — Longer-term: failover for `gemma-4-moe` — open

In `HomeLab/deployments_managed/model-orchestrator/10-configmap.yaml`, consider unifying `gemma-4-moe` and `gemma-4-moe-strix` under a single alias with the router selecting between backends, or expose a `gemma-4-moe-any` route that fails over from workstation → strix. This requires the router to support multi-backend routes (verify capability).

Even with that, the in-memory rate limiter pinning the router to 1 replica is the deeper bottleneck. If sustained load on Gemma is going to be common, moving the rate limiter to Redis would unlock horizontal scaling.

### Fix 5 — Operator visibility — open

Add a Cockpit alert / job-page banner when `freeze_data.type == "parser_failure"` so the operator sees *why* a job stopped before opening the audit. Today the audit has to be paginated by hand to find the leaked-text pattern. Now that fix #1 emits `parser_failure` as a structured error type, the API and UI changes are localised.

### Other follow-ups (deliberately deferred)

- **Per-phase `enable_thinking` toggle** — strategic on, tactical off. Compounds with #2a but needs LangChain template-kwarg plumbing, not just prompt files. Worth doing once #1+#2 are validated.
- **Pre-render tool definitions** as `<|tool>declaration:NAME{...}<tool|>` rather than raw JSON schema. Would further anchor the model's surface form. Tool-binding layer change at LangChain/vLLM serving level.
- **Pin `--chat-template tool_chat_template_gemma4.jinja`** on the workstation deployment to dodge known issues in HF discussion #20 (missing `<|turn>model\n` after assistant tool_calls, malformed `<|channel>thought` formatting). One-line change in `Scripts-and-Notebooks/llm_containers/gemma4-moe-vllm/entrypoint.sh`.

## Upstream vLLM bug landscape (separate from this incident)

For tracking: vLLM's `gemma4` tool parser has known issues, but the ones below are **distinct** from what we hit here. Worth tracking, not waiting on.

| Issue | Symptom | Affects us? |
|-------|---------|-------------|
| [vLLM #38837](https://github.com/vllm-project/vllm/issues/38837) | `Gemma4ToolParser.__init__()` signature mismatch — 400 on tool calls | **Fixed** in PR #38847, shipped in 0.19.x |
| [vLLM #38855](https://github.com/vllm-project/vllm/issues/38855) | Reasoning parser strips `<|channel>` tokens | Possibly relevant if we enable thinking mode; currently we don't surface reasoning_content separately |
| [vLLM #38910](https://github.com/vllm-project/vllm/issues/38910) | Streaming tool parser duplicates HTML-tag prefixes | Streaming-only; we use non-streaming via the orchestrator |
| [vLLM #39043](https://github.com/vllm-project/vllm/issues/39043) | "Tool calls leak to chat" with Gemma 4 + Claude Code | **Symptom matches ours** but root cause undiagnosed in the issue. Our analysis suggests it's the same prompt-side issue we're fixing |
| [vLLM #39072](https://github.com/vllm-project/vllm/issues/39072) | Model omits required tool params | Model-side, similar class of issue. Better prompts help here too |
| [vLLM #39392](https://github.com/vllm-project/vllm/issues/39392) | `<pad>` tokens under concurrent tool-call requests | **Mitigated** by `parallel_tool_calls: false` in our config matrix. Did not affect this job |
| [vLLM #39468](https://github.com/vllm-project/vllm/issues/39468) | Returned tool args contain extra `<|"|>` delimiters | PR #39484 still open as of search date. Different symptom — we don't see strings, we see no tool calls at all. Track for when fix #2 starts producing tool calls |
| [mlx-lm #1096](https://github.com/ml-explore/mlx-lm/issues/1096) | Same wire-format issue in MLX (different framework) | Confirms this is a Gemma-emission pattern, not a vLLM-specific bug |

The takeaway from the upstream bug list: the parser is correct, the model is capable, but the integration ergonomics are rough. Several frameworks have hit the same "model emits the right delimiters but the wrong inner syntax" pattern — none have shipped a generic fix because the right answer is prompt-side.

## Open questions

- Did the workstation backend actually crash / OOM, or did the router circuit-break on health checks? Needs router and llama-server logs from the 09:13–09:29 window.
- Is the 28 min gap between job creation (08:17:45) and iter 0 (08:45:29) just dispatch cooldown, or is there a separate scheduling issue? Out of scope for this doc but worth a glance at orchestrator logs for that window.
- Should we pin a fixed-revision Gemma chat template to dodge the known issues in HF discussion #20 (missing `<|turn>model\n` after assistant tool_calls, malformed `<|channel>thought` formatting)?
