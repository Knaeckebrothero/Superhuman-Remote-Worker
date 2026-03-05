# Prompt & Framework Improvements — Not Yet Implemented

Backlog of improvements identified from agent failure analysis (`docs/issues/results.md`).
Each item references the failure pattern(s) it addresses.

See also: `docs/issues/results.md` § "Potential Mitigations (Framework-Level)" for the original proposals.

---

## What Was Already Done

For reference, these are the changes from the March 2026 optimization pass:

### Prompt changes (base + minimax)

| Pattern | Where | What |
|---------|-------|------|
| P1 | systemprompt, persona | "Run the exact verification steps from instructions, not generic substitutes" |
| P3 | systemprompt (minimax only) | "Do not say 'Let me try a simpler approach'" |
| P4 | strategic | "Check deliverable artifacts — regenerate rather than editing stale files" |
| P5 | summarization | Added "deliverable status" and "shell state" to preserve list |
| P6 | strategic | "Action bias" paragraph / over-planning decision criteria bullet |
| P7 | strategic | "Remove stale entries that no longer reflect reality" |
| P8 | tactical (minimax) | "If the same error occurs twice, document it and move on" |
| P9 | systemprompt, persona | "Confidence must reflect verified outcomes; unmet = below 0.5" |
| P10 | systemprompt | "Complete todos one at a time" |
| P11 | tactical, systemprompt (minimax) | Error reading rules, tool output disclaimer |
| P12/P13 | tactical | Shell management section (tab reuse, SSH two-step pattern) |

### Framework changes

| Pattern | Where | What |
|---------|-------|------|
| P6 | `src/graph.py` | Strategic phase budget counter (warn after 10 tool calls) |
| P8 | `src/graph.py` | Tool call loop detection (sliding window, MD5 args hash, warn at 3x) |
| P9 | `src/tools/core/job.py` | `job_complete` validation gate (deliverable existence + size check) |
| P10 | `src/tools/core/todo.py` | Reject comma-separated IDs in `todo_complete` |
| P11 | `src/tools/coding/shell_tools.py` | Shell error pattern scanning (13 patterns, prepend warning) |

### Summarization improvements (March 2026 — second pass)

Research-driven improvements based on analysis of Claude Code, OpenAI Codex, JetBrains (NeurIPS 2025 "Complexity Trap"), Anthropic cookbook, Factory.ai, MemGPT/Letta, and Manus.

| Where | What |
|-------|------|
| `src/core/context.py` — `_format_messages_for_summary` | **Observation masking**: last 10 tool results include truncated content (300 chars), older ones replaced with placeholder. Inspired by JetBrains research showing 52% cost savings with no quality loss. |
| `src/core/context.py` — `ConversationSummary` | Added `critical_facts` field for exact IDs, file paths, error messages, URLs that must survive verbatim. Inspired by Mem0/Hindsight entity extraction and Anthropic cookbook's Key References section. |
| `config/prompts/summarization_prompt.txt` | Rewritten with analysis-first pattern (Anthropic cookbook), preservation priority ordering, "handoff to future self" framing (Codex CLI pattern), explicit failed-approach preservation, user correction verbatim quotes. |
| `config/prompts/summarization_prompt_minimax.txt` | Same improvements with MiniMax XML annotation style (WHY blocks, structured sections). |

### Instruction & template improvements (March 2026 — third pass)

Research-driven improvements based on analysis of Claude Code system prompts (110+ fragments), OpenAI Codex CLI (AGENTS.md hierarchy, Skills, Plan tool), Devin (three-mode architecture), Manus (todo.md recitation, error retention, KV-cache optimization), IFScale benchmark (instruction capacity limits), Pink Elephant research (negative instruction ineffectiveness), PreFlect (prospective reflection), and LangChain context engineering patterns.

| Where | What |
|-------|------|
| `config/templates/instructions.md` | Rewritten: added "Bias to Action" directive (Codex CLI pattern), "Batch Your Reads" strategy (Codex parallel-first pattern), "Check Your Plan Against Requirements" (PreFlect prospective reflection), "Escalate Rather Than Mask" (Devin error handling, addresses P3/P11), "Verify with evidence" (addresses P1). |
| `config/templates/instructions_minimax.md` | **New file**: MiniMax-optimized instructions with XML annotation style and WHY blocks. Same improvements as base, structured with `<action_bias>`, `<batch_reads>`, `<plan_verification>`, `<stay_grounded>`, `<write_early>`, `<error_handling>`, `<context_management>` tags. Validated by MiniMax M2.5 official documentation recommending "clarity over brevity" and "explain intent." |
| `config/instruction_matrix.yaml` | Added `minimax` entry mapping `instructions` → `instructions_minimax.md`. |
| `config/templates/workspace_template.md` | Restructured with Facts/Status separation: `## Facts` (PROTECTED — survives rewrites), `## Failed Approaches` (PROTECTED — prevents retrying failed strategies), `## Status` (freely rewritable). Added `Verified By` column to Deliverables table. Changed line limit from 50 → 60 lines (~4000 tokens). Inspired by Manus error retention ("erasing failure removes evidence"), MemGPT tiered memory, and P5/P7 failure analysis. |
| `config/templates/todo_guide.md` | Relaxed "5 todos per phase" to adaptive "3-7 todos" range (IFScale research shows fixed counts waste attention budget). Added Batch Processing phase pattern (addresses P6). Strengthened verification todos to require evidence. Reframed anti-patterns as positive "Guidance" section (Pink Elephant research). Added "Simple Task Shortcut" concept (Codex "skip planning for easy 25%"). |
| `config/templates/strategic_todos_initial.yaml` | Added PLAN VERIFICATION step (PreFlect — cross-check plan vs requirements). Added SIMPLE TASK SHORTCUT in todo 4 (skip research for trivial tasks). Reframed ANTI-PATTERNS as "INSTEAD OF" positive directives. Changed "5 todos" → "3-7 todos". Added `## Facts` and `## Failed Approaches` to workspace init (todo 3). |
| `config/templates/strategic_todos_transition.yaml` | Added PROTECTED/REWRITABLE section distinction in todo 2 — Facts, Pinned Instructions, and Failed Approaches are preserved verbatim during workspace rewrites; Status and Deliverables are freely rewritable. Added BLOCKER CHALLENGE directive (addresses P7). Added "Failed Approaches" as 6th section in retrospective (todo 1). Reframed ANTI-PATTERNS as "INSTEAD OF" directives throughout. |
| `config/templates/strategic_todos_resume.yaml` | Reframed ANTI-PATTERNS as "INSTEAD OF" positive directives. Changed "5 todos" → "3-7 todos". |
| `config/templates/phase_retrospective_template.md` | Added "Failed Approaches" section between "Blocked or Failed" and "Recommendations." Ensures failed strategies are recorded for context compaction survival. |

---

## Prompt Improvements — Not Yet Implemented

~~1. Critic-specific prompts for verification jobs (P15, P17)~~ DONE

**Status**: Implemented as a comprehensive critic expert optimization (fourth pass, March 2026).

Research-driven improvements based on LLM-as-judge research (RULERS, CheckEval, G-Eval, CyclicJudge), AI code review tool analysis (Codex CLI, Cursor BugBot, PR-Agent, Devin, SWE-bench, Aider), and LLM evaluation bias research (CALM taxonomy, SycEval, Diffray false positive analysis).

Changes:
- **`persona.txt`** — Slimmed from 143 lines to ~25 lines. Kept identity, principles, identity anchors. Moved review modes, report format, severity system, and diagnostics to instructions.md. Added forced-flaw identification principle ("List at least 3 areas you scrutinized before approving").
- **`instructions.md`** — Complete rewrite with research-backed evaluation flow: (1) criteria-first pattern (G-Eval — extract criteria BEFORE reading deliverables), (2) evidence-before-verdict (cite evidence per criterion before scoring), (3) forced-flaw identification (SycEval anti-sycophancy — list 3+ weaknesses before any verdict), (4) checklist-based evaluation (CheckEval — binary YES/NO per criterion, +0.45 inter-rater agreement), (5) confidence ratings on findings (Codex/Devin pattern — reduces hallucinated issues). Five review modes with domain-specific checklists. Anti-patterns reframed as "INSTEAD OF" positive directives.
- **`strategic.txt`** — New critic-specific strategic prompt. Prevents premature verdicts (P15): "Do NOT render verdicts during strategic phases." Enforces criteria-first pattern. Review scoping protocol.
- **`tactical.txt`** — New critic-specific tactical prompt. Enforces independent verification (P17): "SSH to targets, check service status, curl endpoints." Evidence gathering protocol with tool output interpretation rules.
- **`prompt_matrix.yaml`** — Updated to register strategic/tactical overrides.
- **Base `strategic.txt`** — Fixed stale "50 lines" → "60 lines (~4000 tokens)"

---

### 2. ~~Summarization prompt: aggressive tool output stripping (P5)~~ DONE

**Status**: Superseded by observation masking in `_format_messages_for_summary`. Tool results are now masked at the framework level (old results get placeholders, recent 10 get truncated content). The summarization prompt was also rewritten with explicit preservation priorities and a `critical_facts` field for domain knowledge.

---

### ~~3. workspace.md fact/status separation (P5, P7)~~ DONE

**Status**: Implemented in workspace_template.md and strategic_todos_transition.yaml. The workspace template now has PROTECTED sections (`## Pinned Instructions`, `## Facts`, `## Failed Approaches`) that survive workspace rewrites, and REWRITABLE sections (`## Deliverables`, `## Status`, `## Key Decisions`). The transition template's REFLECT todo explicitly distinguishes protected vs rewritable sections and includes a BLOCKER CHALLENGE directive.

---

### ~~4. Blocker TTL / auto-challenge (P7)~~ DONE

**Status**: Implemented as BLOCKER CHALLENGE directive in strategic_todos_transition.yaml todo 2. The instruction says: "If Status lists blockers from previous phases, re-verify them now. Re-read the source, re-run the command, or re-check the assumption. If the blocker is still valid, keep it. If not, remove it."

---

### ~~5. Instruction requirement tracking at job_complete (P1, P3)~~ DONE

**Status**: Implemented via the "Check Your Plan Against Requirements" principle in instructions.md and the PLAN VERIFICATION step in strategic_todos_initial.yaml. The PreFlect-inspired prospective reflection pattern has the agent cross-check its plan against instructions.md requirements upfront, rather than only checking at job_complete. The instructions also add "Escalate Rather Than Mask" for honest failure reporting and "Verify with evidence" for verification todos.

---

### 6. Model-family-specific prompt variants (partially done)

**Status**: MiniMax variant implemented for the instruction matrix (`instructions_minimax.md` with XML/WHY blocks). Other model families (deepseek, gemini, claude) remain as potential future work but are lower priority since MiniMax M2.5 is used 99% of the time.

**Remaining variants** (add as needed):
- **`deepseek`**: Minimal system prompt, user-prompt-centric (R1 performs best with empty system prompt)
- **`gemini`**: Verbosity constraints, XML-or-Markdown (never both)
- **`claude`**: Autonomy emphasis, reduce over-caution

---

## Framework Improvements — Not Yet Implemented

### 7. Stale artifact detection and warning (P4)

**Problem**: Agents inherit output files from prior jobs/phases and list them as deliverables without regenerating them. The current prompt-level fix ("check timestamps") depends on the model actually doing it.

**Proposed implementation**:

In `src/core/phase.py` or workspace injection, at the start of each strategic phase:
1. Scan `output/` directory for files
2. Compare file mtimes against the current phase start time
3. If files predate the current phase, inject a transient warning message:

```python
# In workspace_injection.py or phase.py (strategic phase init)
stale_files = []
for f in workspace.list_files("output/"):
    mtime = os.path.getmtime(workspace.resolve_path(f))
    if mtime < phase_start_timestamp:
        stale_files.append(f)

if stale_files:
    warning = (
        "⚠ Stale artifacts detected — these files were last modified before "
        "the current phase and may not reflect current state:\n"
        + "\n".join(f"  - {f}" for f in stale_files)
        + "\nVerify each file is current or regenerate it."
    )
    # Inject as transient message
```

**Complexity**: Low — ~20 lines in phase init or workspace injection.

**Impact**: Medium — makes P4 programmatically detectable rather than relying on the model.

---

### 8. Observation masking for old tool results (P5)

**Problem**: Old tool results (file reads, shell outputs, search results) consume context tokens without providing value. The current `clear_old_tool_results` in `context.py` keeps the last 10, but doesn't do progressive masking.

**Current state**: `context_mgr.clear_old_tool_results(messages)` replaces old tool results with `[Output cleared to save context]`.

**Proposed enhancement**:

Instead of binary keep/clear, implement a tiered approach:
1. Last 5 tool results: keep in full
2. Results 6-15: replace body with one-line summary (e.g., "read_file(plan.md) → 45 lines, topics: deployment, config")
3. Results 16+: current behavior (full clear)

The one-line summary can be generated cheaply by truncating to first 100 chars + metadata. JetBrains research showed this "observation masking" approach cuts costs 50%+ with no accuracy loss.

**Where**: `src/core/context.py` → `clear_old_tool_results()` method.

**Complexity**: Medium — needs to parse ToolMessage content and generate meaningful summaries without an LLM call.

**Impact**: Medium — reduces context pressure, helps P5 by keeping more room for actual domain knowledge.

---

### 9. todo_complete requiring completion notes (P1, P10)

**Problem**: When the agent completes a todo, it provides no evidence of what it actually verified. This makes it easy to mark todos done without doing the work.

**Proposed change**:

Add an optional but encouraged `notes` parameter to `todo_complete`:

```python
def todo_complete(todo_id: str = "", notes: str = "") -> str:
```

If a todo's content contains keywords like "verify", "test", "check", "confirm", or "validate", and `notes` is empty, return a soft warning:
```
"Warning: This todo appears to be a verification task. Consider adding notes
describing what you verified and what the outcome was. Call again with
notes='...' or proceed without notes."
```

The notes are already stored on the Todo object (via `todo.notes`). This just adds a prompt to fill them in for verification tasks.

**Complexity**: Low — small change to `todo_complete` in `src/tools/core/todo.py`.

**Impact**: Medium — encourages the agent to document verification outcomes, making P1 (fabricated verification) more visible.

---

### 10. Phase-gated evaluation tools for critics (P14, P15)

**Problem**: Critics can call `approve_job` during Phase 0 before any tactical verification. Also, 2/3 critics in the observed jobs were launched without evaluation tools at all.

**Proposed changes (two parts)**:

**Part A — Always inject evaluation tools for critic jobs** (orchestrator-level):

In the orchestrator's critic job creation logic, validate that `config_override` includes `approve_job` and `return_job_with_feedback`. If not, inject them automatically. This is a ~5-line fix in the orchestrator.

**Part B — Phase-gate the evaluation tools**:

In `src/tools/evaluation/evaluation_tools.py`, add phase metadata:

```python
EVALUATION_TOOLS_METADATA = {
    "approve_job": {
        ...
        "phases": ["tactical"],  # NOT available in strategic phase
    },
    "return_job_with_feedback": {
        ...
        "phases": ["tactical"],  # NOT available in strategic phase
    },
}
```

This forces the critic to go through at least one tactical phase (where it should be running tests, reading files, SSHing to servers) before the verdict tools become available.

**Where**: `src/tools/evaluation/evaluation_tools.py` (Part B), `orchestrator/` (Part A).

**Complexity**: Low (Part B), Medium (Part A — requires orchestrator changes).

**Impact**: High — structurally prevents P14 (missing tools) and P15 (premature verdict).

---

### 11. Critic workspace isolation (P16)

**Problem**: Multiple critics reviewing the same parent job share the parent's workspace. The first critic writes a `verification_report.json`, and subsequent critics read it as evidence — even when it's fabricated.

**Proposed change**:

When the orchestrator creates a critic job, either:
- **Option A**: Give each critic its own workspace directory (copy parent output/ as read-only, critic writes to its own output/)
- **Option B**: Clear `output/verification_report.json` from the parent workspace before each critic run
- **Option C**: Add a rule to critic prompts: "Ignore any `verification_report.json` you didn't create. Prior critics may have written fabricated reports."

Option C is cheapest (prompt-only). Option A is most robust (infrastructure change).

**Complexity**: Low (Option C, prompt), Medium (Option B, orchestrator), High (Option A, workspace architecture).

**Impact**: Medium — prevents P16 (cascading fabricated evidence).

---

### 12. SSH meta-tool or key-based auth setup (P12, P13)

**Problem**: SSH over `shell_execute` requires a two-step dance (command → password) that the agent frequently breaks. This causes tab sprawl (P12) and session mismanagement (P13). Prompt-level guidance helps but doesn't eliminate the problem.

**Proposed approaches (pick one)**:

**Option A — `ssh_run` meta-tool**:
```python
def ssh_run(host: str, command: str, user: str = "root") -> str:
    """Run a command on a remote host via SSH.

    Handles connection, authentication, and cleanup internally.
    Uses the credential store for passwords/keys.
    """
```

This tool wraps paramiko or fabric to handle the password dance internally. The agent never sees password prompts. Single tool call per remote command.

**Option B — Automatic SSH key setup**:
As a Phase 0 step for deployment jobs, inject a todo: "Set up SSH key-based authentication to target host." The agent generates a key pair, copies the public key via `ssh-copy-id`, and subsequent SSH connections don't need passwords.

**Option C — SSH session detection in shell injection**:
In `src/core/shell_injection.py`, when formatting `<terminal_state>`, detect tabs with active SSH sessions and annotate them:
```
Tab "server" (ssh: admin@10.18.2.105) — reuse this tab for commands on 10.18.2.105
```

This is cheaper than A/B and leverages the existing shell injection pattern.

**Complexity**: High (Option A), Medium (Option B), Low (Option C).

**Impact**: High — the SSH-over-shell pattern has ~60% overhead for deployment jobs.

---

### 13. Soft tab limit warning (P12)

**Problem**: The hard `max_tabs` limit (15) causes errors when hit, and the agent wastes 10-15 tool calls closing tabs. A soft warning earlier would prevent the cascade.

**Proposed change**:

In `shell_tools.py` → `shell_execute`, after `sm.ensure_tab(name)`:
```python
tab_count = len(sm._tabs)
if tab_count >= 8:  # soft warning at ~half of max_tabs
    warning = f"⚠ You have {tab_count} open tabs. Close unused tabs to avoid hitting the limit."
    # Prepend to output
```

**Complexity**: Trivial — 5 lines in `shell_tools.py`.

**Impact**: Low-Medium — prevents the cascade of tab management overhead.

---

### 14. job_complete instruction checklist validation (P1, P3)

**Problem**: The current `job_complete` validation gate checks that deliverable files exist and are non-empty, but doesn't verify that the agent actually ran the tests/commands specified in the instructions.

**Proposed enhancement (framework-level)**:

When `job_complete` is called, read `instructions.md` (if it exists in the workspace) and extract lines that look like commands or test names (lines starting with `$`, backtick-wrapped commands, numbered test items). Cross-reference against the audit trail (MongoDB tool call log) to check whether those commands were actually executed.

This is the "instruction enforcement" mitigation from results.md. It requires:
1. Parsing instructions for verification commands
2. Querying the audit trail for matching tool calls
3. Warning if specified commands were never executed

**Complexity**: High — requires instruction parsing heuristics + audit trail querying.

**Impact**: Very High — would directly prevent P1 (fabricated verification) at the framework level.

---

### 15. Configurable strategic phase frequency (P6)

**Problem**: The strategic→tactical alternation runs after every tactical phase regardless of task type. For batch/simple tasks this creates massive overhead (4 strategic phases vs 3 tactical phases in one observed job).

**Proposed change**:

Add a config key:
```yaml
phases:
  strategic_frequency: 1        # Run strategic every N tactical phases (default 1)
  strategic_frequency_batch: 3  # For batch tasks (when todo count > 15)
```

In `handle_transition` (or `check_goal`), if the previous tactical phase completed without errors and there are staged todos ready, skip the strategic phase and go directly to the next tactical phase. Only enter strategic when:
- The last phase had errors or blockers
- N tactical phases have passed without strategic review
- The agent explicitly requested strategic review (via `todo_rewind`)

**Complexity**: Medium — changes to phase transition logic in `src/core/phase.py` and `src/graph.py`.

**Impact**: Medium — eliminates P6 planning loops for straightforward batch work.

---

## Research References

These findings informed the improvements above:

- [Tools Fail: Detecting Silent Errors in Faulty Tools](https://arxiv.org/html/2406.19228v1) — disclaimers boost error detection 30%, checklists reach 60-70% F1
- [OpenClaw Tool-Loop Detection](https://docs.openclaw.ai/tools/loop-detection) — genericRepeat / knownPollNoProgress / pingPong with 3-tier thresholds
- [The Agent That Says No: Verification Gate](https://vadim.blog/verification-gate-research-to-practice) — 5-check framework, counterfactual analysis, trajectory-level assessment
- [Effective Context Engineering (Anthropic)](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) — compaction, structured note-taking, multi-agent context isolation
- [Efficient Context Management (JetBrains)](https://blog.jetbrains.com/research/2025/12/efficient-context-management/) — observation masking cuts costs 50%+ with no accuracy loss
- [AgentSpec: Runtime Enforcement](https://arxiv.org/abs/2503.18666) — DSL for trigger/check/enforce rules, 90%+ unsafe execution prevention
- [Chain-of-Verification (CoVe)](https://arxiv.org/abs/2309.11495) — self-verification reduces hallucination, independence in verification is key
- [Tackling Partial Completion in LLM Agents](https://medium.com/@georgekar91/tackling-the-partial-completion-problem-in-llm-agents-9a7ec8949c84) — structured checklists as forcing functions
- [AI Agents Infinite Loops](https://www.fixbrokenaiapps.com/blog/ai-agents-infinite-loops) — sliding window detection, semantic similarity, hard caps
- [IFScale: How Many Instructions Can LLMs Follow?](https://arxiv.org/html/2507.11538v1) — practical ceiling ~150-200 instructions, threshold/linear/exponential decay patterns
- [The Pink Elephant Problem](https://eval.16x.engineer/blog/the-pink-elephant-negative-instructions-llms-effectiveness-analysis) — "DO NOT" instructions unreliable across all models, reframe as positives
- [PreFlect: Prospective Reflection](https://arxiv.org/html/2602.07187v1) — evaluating plans before execution beats retrospective reflection, +17% accuracy
- [Impact of AGENTS.md on Efficiency](https://arxiv.org/html/2601.20404v1) — 29% faster, 17% fewer tokens, but files hurt when overconstraining
- [When AGENTS.md Backfires](https://notchrisgroves.com/when-agents-md-backfires/) — instruction files reduce success 5/8 scenarios when duplicating accessible info
- [Codex Prompting Guide](https://developers.openai.com/cookbook/examples/gpt-5/codex_prompting_guide/) — bias to action, batch reads, plan hygiene, skip planning for easy 25%
- [Manus Context Engineering](https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus) — todo.md recitation combats lost-in-middle, error retention preserves evidence
- [MiniMax M2.5 Best Practices](https://platform.minimax.io/docs/coding-plan/best-practices) — clarity over brevity, explain intent, sequential focus, XML-native
- [Context Engineering for Agents (LangChain)](https://blog.langchain.com/context-engineering-for-agents/) — write/select/compress/isolate context strategies
- [Design Patterns for LLM Memory](https://serokell.io/blog/design-patterns-for-long-term-memory-in-llm-powered-architectures) — MemGPT tiered memory, entity memory, knowledge graphs
- [Agent Drift: Behavioral Degradation](https://arxiv.org/html/2601.04170) — detectable after 73 interactions, -42% success at 600 turns
- [Prompt Formatting Impact](https://arxiv.org/html/2411.10541v1) — XML preferred by Claude, Markdown by GPT-4, up to 40% variation by format
