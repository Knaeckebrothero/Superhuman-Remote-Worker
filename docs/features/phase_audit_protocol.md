---
tags:
  - feature
  - prompting
  - strategic-phase
  - quality
  - verification
related:
  - "[[verification_phase]]"
  - "[[prompting]]"
  - "[[continuous_improvement_loop]]"
  - "[[stuck_agent_recovery]]"
aliases:
  - phase audit
  - strategic audit protocol
  - todo verification
---

# Phase Audit Protocol — In-Loop Deliverable Verification

> Extend the strategic-phase system prompt so that the model audits the previous tactical phase's claimed deliverables against the workspace filesystem *before* planning the next phase — instead of treating `todo_complete` status flags as ground truth. Prompt-only change; no graph, tool, or schema modifications.

## Problem

`todo_complete` is a pure bookkeeping operation (`src/tools/todo/todo.py:159-271`) — it flips a status flag and counts remaining todos. There is no check that the work described in the todo was actually performed. This is **BUG-3** in `docs/issues_job_f06e721e.md`.

**Concrete failure.** In the audio-transcription post-mortem, the tactical phase marked 5/5 todos "completed" even though:

- `output/transcriptions.md` was never created (the primary deliverable)
- Every `read_file` call on the audio inputs returned an error
- 0 of 1 deliverables were produced

The system recorded 5/5 todos complete, archived the phase as successful, and the strategic phase that followed rubber-stamped the rosy picture — because its system prompt (`config/prompts/strategic.txt`) says "review progress" without telling the model to distinguish *status-flag claims* from *filesystem reality*.

### Why not fix this in code

We considered adding a `deliverable` field to each todo and validating at `todo_complete` time. Rejected because:

1. **Guardrails age poorly.** As models improve, hard code-level checks become noise that forces the model to perform documentation busywork on tasks it has already finished.
2. **Domain variety.** Not every todo produces a file. Some update graph state, some fetch and cache data in memory, some are investigative. A universal `deliverable_path` schema would either over-constrain or be full of `null`.
3. **Wrong layer.** The failure isn't that the tactical agent *lied* — it's that the strategic agent doesn't *verify*. Fixing the verifier is the right place.

Related but distinct: `docs/features/verification_phase.md` proposes spawning a separate critic job on `job_complete`. That catches issues at *final* job submission. The phase audit catches them *between* phases, as soon as they happen, without any new agent.

## Non-goals

- No new tools, no new LangGraph nodes, no schema changes.
- No hard-coded deliverable-existence checks in Python.
- Not a replacement for `verification_phase.md` (the critic-job mechanism). The two are complementary: phase audit catches mid-job drift, critic job catches end-of-job over-claiming.
- Not a full Reflexion / self-correction loop (expensive, marginal gains — see research summary).

## Research summary

Three parallel research passes (prompt engineering, production agent frameworks, academic literature) converged on the same answer.

### What works

| Technique | Source | Core idea |
|---|---|---|
| **Forced evidence citation** | Chain-of-Verification (Dhuliawala et al., ACL 2024), citation-grounded code comprehension (2025) | Require the model to quote a literal tool-output excerpt for each claimed deliverable. Models cannot fabricate quotes they cannot produce. Empirically ~92% vs ~50% baseline on fabricated-reference detection. |
| **Adversarial framing** | CRITIC (Gou et al.), Self-Refine follow-ups | "Assume the previous phase failed; present evidence to overturn the presumption" beats "review progress". Counteracts sycophancy/confirmation bias. |
| **Structured output with mandatory negative slots** | Autorubric, LangGraph reflection pattern | A rubric with a required `missing_files` or `unverified` field is a slot the model cannot leave blank without visibly failing format. Free-form self-critique doesn't work; structured rubrics reduce local inconsistency ~23%. |
| **Primary-deliverable-first check** | Claude Code master agent loop, Anthropic "writing tools for agents" | Open by restating the job's primary artifact from the goal, before inspecting any todo status flags. Blocks anchoring on the tactical phase's framing. |
| **Negative-result search** | Reflexion (Shinn et al.), OpenHands critic | Explicitly require scanning for errors, `ENOENT`, non-zero exits, and absent writes. Models don't surface failures spontaneously but will if a slot asks for them. |

### What doesn't work (and why we're not doing it)

- **"Check your work" / "make sure todos were really done"** — too vague; models produce plausible "yes, I checked" with no grounding. Biggest documented failure mode.
- **Intrinsic self-correction in shared context** — Huang et al. (ICLR 2024) "LLMs Cannot Self-Correct Reasoning Yet" and Stechly/Kambhampati (2023-24) show models *degrade* performance when asked to critique their own output in the same context. This is why we need adversarial framing + mechanical evidence requirements, not reflection.
- **Same role for generator and critic** — Self-Refine only works with a distinct critic persona. Mitigated here by the adversarial framing reframing the strategic agent as "auditor of the tactical phase."
- **Interrogative framing without a required output shape** — invites yes/no hallucinations.
- **Iterating self-critique past round 2** — reward hacking kicks in.

### Realistic ceiling

Cemri et al. (NeurIPS 2025) MAST study of 1,600 multi-agent traces: **task-verification failures account for 21.3% of all failures even in systems that already have verification prompts**. A prompt-only change to the strategic phase is expected to reduce premature-completion failures by **~20-40%**, not eliminate them. The real ceiling is a separate verifier LLM call (Self-Taught Evaluators, Meta FAIR 2024), which we can add later as Phase 2 if the prompt-only change is insufficient. This is not a magic fix — it is the best-backed cheap intervention.

## Critical prerequisite: context compaction must preserve evidence

**This must be verified before the prompt change ships, or the audit is theatrical.**

The strategic phase can only audit what it can see. The agent compacts context at phase boundaries (`compact_on_archive: true` in `config/defaults.yaml`, logic in `src/core/archiver.py`). If compaction drops raw tool errors and recent `read_file`/`write_file` outputs into a narrative summary like *"attempted to process audio files"*, the strategic phase has no grounded evidence to cite, and the audit protocol collapses to narrative self-critique — which we know doesn't work.

### Prerequisite work

1. **Read `src/core/archiver.py`** and trace what gets preserved vs. summarized at phase boundaries. Focus on:
   - Tool errors (non-zero exit codes, exceptions, "not found" results)
   - The last N `write_file` / `edit_file` / `read_file` results per phase
   - Any paths mentioned in the phase's todos
2. **If evidence is lost,** change the compactor to preserve verbatim:
   - All tool outputs with `error`, `Exception`, `ENOENT`, stack traces
   - The last file write/read per workspace path mentioned in the completed todos
3. **Only then** ship the prompt change. Otherwise the model is being asked to audit a phantom.

This prerequisite alone may be higher-leverage than the prompt change. The BUG-3 failure mode (audio job) fits the hypothesis that tactical errors got summarized away before the strategic phase could see them.

## Design: the prompt addition

Target file: `config/prompts/strategic.txt` (currently 35 lines). Add a "Phase Audit Protocol" block as step 0, before the existing "Review protocol" numbered list. The block runs *first* so the audit happens before planning, not as an afterthought.

Also update model-family variants in `config/prompts/strategic_gpt_oss.txt`, `strategic_gpt_5.txt`, `strategic_minimax.txt`, `strategic_codex_spark.txt` with the same content (the matrix resolver picks the right file per model family — see `docs/features/prompting.md`).

### Prompt draft

```text
Phase Audit Protocol (run this FIRST, before any planning):

You are auditing the previous tactical phase. Your default assumption is that
the tactical phase failed to produce at least one required deliverable. You
must present specific file evidence to overturn this presumption.

1. Restate the job's primary deliverable from the original goal. Not from
   todos, not from the tactical phase's summary — from the goal itself.
   Example: "Primary deliverable: output/transcriptions.md must exist and
   contain the transcribed text of each input audio file."

2. For each todo the tactical phase marked `completed`, emit one audit row:

   - todo_id: <id>
   - claimed_deliverable: <one-line description of what the todo said it
     would produce>
   - evidence_tool_call: <name of the tool call whose output proves the work
     happened, e.g. write_file, edit_file, shell, graph_write>
   - evidence_excerpt: <verbatim excerpt from that tool's output, <=200
     chars, copied not paraphrased>
   - verdict: VERIFIED | UNVERIFIED | CONTRADICTED

   If you cannot locate a tool call whose output contains the claimed
   deliverable, the verdict is UNVERIFIED. Do not invent evidence. Do not
   paraphrase. If you did not see it in a tool output, it is UNVERIFIED.

3. Scan the previous phase's tool outputs for failures. Enumerate three
   lists; empty lists must be stated as `NONE FOUND` explicitly:
   - tool_errors: any error, exception, non-zero exit, ENOENT, stack trace
   - absent_writes: any file path mentioned in a todo that never appeared
     in a successful write_file, edit_file, or shell write
   - contradictions: any case where a todo's claimed outcome conflicts
     with a later tool output

4. Emit an audit verdict:

   AUDIT_VERDICT: CLEAN       — every deliverable VERIFIED, primary
                                deliverable exists, no unexplained errors
   AUDIT_VERDICT: REWORK      — one or more deliverables UNVERIFIED or
                                CONTRADICTED, or the primary deliverable
                                is absent
   AUDIT_VERDICT: INCONCLUSIVE — evidence is insufficient to decide (e.g.
                                tool outputs were summarized away)

5. If the verdict is REWORK or INCONCLUSIVE:
   - Do NOT archive the phase as complete.
   - Reopen the affected todos (set status back to pending with a note
     referencing the audit finding), or revise plan.md to add corrective
     todos for the next tactical phase.
   - The next phase's first priority is the corrective work, not new
     scope.

6. If the verdict is CLEAN, proceed to the existing review protocol below.
```

### Where it fits in `strategic.txt`

Insert between the opening `<strategic_phase>` tag and the current "Review protocol" section. The existing steps 1-7 become the "what to do after a CLEAN verdict" branch — structurally unchanged, but now downstream of the audit.

### Estimated length impact

- Current `strategic.txt`: 35 lines
- After addition: ~75 lines
- Trade-off explicitly acknowledged: we are spending attention budget on audit rigor. The Cemri et al. / MAST data says this is the highest-value place to spend it.

## Why this design

- **Evidence-bound + structured = the intersection of what open-weights models can do reliably.** Open-weights models (gpt-oss, kimi-k2, minimax) are weaker than Claude at spontaneous skepticism, but equally good at filling out a schema. The mandatory `evidence_excerpt` slot forces the check by making the format fail-loud if it's not done.
- **Adversarial framing without a separate agent.** Re-framing the strategic phase as "auditor whose default assumption is failure" is the closest we can get to the Self-Refine distinct-critic-role pattern without adding a node.
- **Deterministic verdict token.** `AUDIT_VERDICT: {CLEAN,REWORK,INCONCLUSIVE}` is parseable. If Phase 2 ever adds a code-level branch in `handle_transition` (e.g., route REWORK phases differently), the signal is already there.
- **No code changes.** Only `config/prompts/strategic*.txt` files. Reversible, A/B-testable, no graph changes.

## Open questions

1. **Should INCONCLUSIVE trigger something special?** Current draft treats it the same as REWORK (don't archive). Alternative: bail out and surface to human via `pending_review`. Depends on how often compaction leaves the model genuinely blind.
2. **Per-model-family tuning.** The draft is tuned for open-weights models. Claude variants may need less scaffolding; `strategic_gpt_5.txt` may need more or fewer rubric slots. Decision: ship the same text across all variants first, tune per-model only if evals show divergence.
3. **Eval harness.** How do we measure whether this works? Candidate: replay the `f06e721e` audio-transcription job trace with the new prompt and check that the strategic phase catches the missing `transcriptions.md`. Longer-term: a "sandbagging" eval suite where tactical phases deliberately mark todos done without producing deliverables, scored on whether the strategic phase catches each one.
4. **Context compaction fix ordering.** If `archiver.py` turns out to drop evidence, do we fix the compactor first and ship both together, or ship them as separate changes? Recommend: separate PRs, compactor first (the prerequisite), prompt second.

## Phase plan

1. **Prerequisite audit** — read `src/core/archiver.py`; trace what survives phase-boundary compaction; document findings. If gaps exist, fix them.
2. **Draft the prompt addition** — edit `config/prompts/strategic.txt` + all `strategic_*.txt` variants. Match existing XML-tag style.
3. **Manual replay eval** — rerun the `f06e721e` trace (or a fresh synthetic sandbagging job) against the new prompt and confirm the strategic phase now catches missing deliverables.
4. **Ship behind a config toggle** — `strategic.phase_audit_enabled: true` in `config/defaults.yaml` so we can A/B test against current behavior if regressions appear.
5. **Monitor for 2-3 real jobs** — look for false positives (audit flags something the model actually did) and prompt over-rejection of otherwise-fine phases.
6. **If insufficient → Phase 2**: separate verifier LLM call (Self-Taught Evaluator pattern), which the `AUDIT_VERDICT` token is already designed to feed into.

## References

### Papers

- Huang et al., ICLR 2024 — *LLMs Cannot Self-Correct Reasoning Yet* — https://arxiv.org/abs/2310.01798
- Stechly, Valmeekam, Kambhampati, 2023/2024 — *GPT-4 Doesn't Know It's Wrong* / *Self-Verification Limitations* — https://arxiv.org/abs/2310.12397, https://arxiv.org/abs/2402.08115
- Shinn et al., NeurIPS 2023 — *Reflexion: Language Agents with Verbal Reinforcement Learning* — https://arxiv.org/abs/2303.11366
- Madaan et al., NeurIPS 2023 — *Self-Refine: Iterative Refinement with Self-Feedback* — https://arxiv.org/abs/2303.17651
- Dhuliawala et al., ACL Findings 2024 — *Chain-of-Verification Reduces Hallucination* — https://arxiv.org/abs/2309.11495
- Lightman et al., ICLR 2024 — *Let's Verify Step by Step* — https://arxiv.org/abs/2305.20050
- Wang et al., 2024 — *Self-Taught Evaluators* (Meta FAIR) — https://arxiv.org/abs/2408.02666
- Cemri et al., NeurIPS 2025 — *Why Do Multi-Agent LLM Systems Fail?* (MAST) — https://arxiv.org/abs/2503.13657

### Framework patterns

- LangChain blog — *Reflection Agents* — https://blog.langchain.com/reflection-agents/
- CrewAI docs — *Hierarchical Process* — https://docs.crewai.com/en/learn/hierarchical-process
- Anthropic — *Building Agents with the Claude Agent SDK* — https://www.anthropic.com/engineering/building-agents-with-the-claude-agent-sdk
- Anthropic — *Writing Effective Tools for AI Agents* — https://www.anthropic.com/engineering/writing-tools-for-agents
- OpenAI Cookbook — *Orchestrating Agents: Routines and Handoffs* — https://cookbook.openai.com/examples/orchestrating_agents

### Prompt engineering guides

- Lilian Weng — *LLM Powered Autonomous Agents* — https://lilianweng.github.io/posts/2023-06-23-agent/
- Eugene Yan — *LLM Patterns* / *LLM-as-Judge* — https://eugeneyan.com/writing/llm-patterns/, https://eugeneyan.com/writing/llm-evaluators/
- Learn Prompting — *Chain of Verification* — https://learnprompting.org/docs/advanced/self_criticism/chain_of_verification
- PromptingGuide — *Reflexion* — https://www.promptingguide.ai/techniques/reflexion

### Internal references

- `docs/issues_job_f06e721e.md` — BUG-3 original description and failure trace
- `docs/features/verification_phase.md` — related but distinct: post-`job_complete` critic job mechanism
- `docs/features/prompting.md` — prompt/instruction matrix architecture the audit block plugs into
- `config/prompts/strategic.txt` — current strategic-phase system prompt (35 lines)
- `src/core/archiver.py` — phase-boundary compaction; the prerequisite audit target
- `src/tools/todo/todo.py:159-271` — `todo_complete` tool (the thing whose claims this protocol verifies)
