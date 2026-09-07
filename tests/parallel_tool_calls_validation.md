# Parallel tool calls (gpt-5.x / codex families) — validation plan & coverage

Companion to the `config/model_config_matrix.yaml` flip of 2026-07-15 that set
`parallel_tool_calls: true` for the four Responses-API/codex-proxy families:
`gpt-5`, `gpt-5.6`, `codex`, `codex-spark`. Background analysis of the two
langchain bugs is in the `reference_parallel_tool_calls_langchain_bugs` memory.

**Claim under test.** `langchain#34660` (the still-open bug that historically
gated these families) only concerns the final `response.completed` Responses
chunk lacking `tool_calls`. The graph never reads that chunk for tool calls — it
reconstructs them from the *incremental* `response.output_item.added` /
`function_call` + args-delta chunks (`langchain_openai/chat_models/base.py`
~4730), which are correct. The bug that *did* corrupt parallel calls
(`langchain#34807`, name/arg concatenation in `merge_dicts`) is already fixed
by the `langchain-openai>=1.1.12` bump. So the flip should be behaviour-neutral
except that N tool calls now dispatch in one turn instead of one-at-a-time.

Current bind paths (source layout updated 2026-09-05):
`supports_parallel_tool_calls()` in `src/shared/runtime/core/loader.py` gates
the kwarg; workers bind it in `src/agent/agent.py`, persistent sessions in
`src/agent/api/persistent_session.py`. Historical test observations below are
unchanged by this path correction.

---

## 1. Covered (automated, in CI)

| Area | File / selector | What it asserts |
|---|---|---|
| Matrix value — gpt-5.6 | `tests/test_settings_matrix.py::TestGpt56Family::test_gpt56_registered_in_matrix` | `matrix["gpt-5.6"]["parallel_tool_calls"] is True` (updated in the flip) |
| Base default stays off | `tests/test_settings_matrix.py` (`data["llm"]["parallel_tool_calls"] is False`) | conservative floor unchanged |
| glm stays off | `tests/test_settings_matrix.py` (`matrix["glm"]... is False`) | non-langchain gate unchanged |
| Gate kwarg suppression | `tests/test_loader_routing.py::TestSupportsParallelToolCalls` | kwarg passed for openai/openrouter/codex/groq/anthropic; suppressed for google + o-series |

**Coverage gap (automated).** Nothing asserts the matrix value for `gpt-5`,
`codex`, `codex-spark` (only gpt-5.6). Nothing exercises the *runtime* behaviour
— that two tool calls in one turn actually both execute with intact args. The
unit layer can only prove the flag is set and forwarded, not that the transport
aggregates parallel calls. That is what §2 and §3 exist to close.

---

## 2. Tests we still need to do (the real gate — live, on k3d)

Each row is a live turn on the running cluster that fires **≥2 independent tool
calls in a single model response** and confirms both execute with correct,
un-concatenated arguments. Recipe skeleton per row below the table.

| ID | Family | Path | Status | Priority |
|---|---|---|---|---|
| L1 | gpt-5.6 | persistent session (astream) | user-observed "seems to work"; formalize | P0 |
| L2 | gpt-5.6 | worker job (ainvoke) | **not done** | P0 |
| L3 | gpt-5 | persistent + worker | **not done** (shares transport w/ 5.6) | P1 |
| L4 | codex | persistent + worker | **not done** | P1 |
| L5 | codex-spark | worker | **not done** | P2 |

Why worker *and* persistent (L1 vs L2): the two graphs invoke differently —
persistent streams (`astream`), worker uses `ainvoke`. They hit the Responses
API through different code, so "works in a session" does not by itself prove
"works in a loop job." L2 is the one that matters for the prod loop and is the
highest-value missing test.

**Recipe (persistent, Lx-session).**
1. Cockpit → New Session on the target model (e.g. gpt-5.6-sol), or drive via
   Playwright per `reference_cockpit_playwright_zoneless`.
2. Prompt that forces parallel, independent calls, e.g. *"Read both `plan.md`
   and `todos.yaml` and summarize each"* (two distinct `read_file` args).
3. Assert in the agent log:
   `kubectl --context=k3d-srw -n srw logs -l srw/managed-by=agent-provisioner -f`
   — **two** tool-call entries, distinct `name`/args, no concatenated name
   (`read_fileread_file`) and no merged/empty args.
4. Confirm both results come back and the turn completes without a recursion /
   "invalid tool call" error.

**Recipe (worker, Lx-job).** Create a small job on the target model whose first
tactical todo naturally needs two reads; watch the same log; assert two calls
dispatched in one turn (not two sequential turns). A loop-style job is ideal
since the prod concern is the loop path.

**Args-integrity is the pass criterion, not just count.** The `#34807`-class
failure mode is silent corruption (`read_file(a)` + `read_file(b)` →
`read_file(ab)` or one dropped), so eyeball the actual argument JSON, not just
"two calls happened."

---

## 3. Recommended automated additions (close the §1 gap without a live model)

- **A1 — matrix assertions for the other three families.** Extend
  `tests/test_settings_matrix.py` with
  `matrix[f]["parallel_tool_calls"] is True` for `gpt-5`, `codex`,
  `codex-spark`. Cheap regression guard so a future matrix edit can't silently
  revert three of the four.
- **A2 — regression guard for the OFF families.** Assert `deepseek`,
  `gpt-oss`, `gemma` remain `False` (glm already asserted). These are
  model/provider gates (DeepSeek unreliable, `vllm#39392` `<pad>` tokens, vLLM
  provider support), *not* langchain — a blanket "enable everything" edit
  must not sweep them along.
- **A3 — mechanism unit test (highest value).** Feed a recorded sequence of
  Responses-API stream chunks containing two parallel `function_call` items
  (distinct `output_index`) through the chunk→`AIMessageChunk` aggregation and
  assert the merged message yields two `tool_calls` with intact args. This pins
  the core claim ("incremental chunks aggregate correctly; the completed chunk
  is irrelevant") in CI, independent of any live model. Mirror the fixture
  style in `langchain`'s own `test_responses_stream_completed_chunk_*` tests.

---

## 4. Explicitly not covered / accepted risk

- **Only gpt-5.6 was empirically observed.** `gpt-5`, `codex`, `codex-spark`
  were flipped by shared-transport reasoning, not individual observation. L3–L5
  exist precisely to retire that assumption before heavy prod-loop reliance.
- **`#34660` remains open upstream.** We are relying on its narrowness (completed
  chunk only), not on a fix. If a future langchain-openai bump changes the
  streaming aggregation, re-run §2. The flip carries no version pin because no
  version fixes it — see the memory note.
- **Non-langchain OFF families** (`deepseek`, `glm`, `gpt-oss`, `gemma`) are out
  of scope here; revisit each against its own upstream issue, not this one.
