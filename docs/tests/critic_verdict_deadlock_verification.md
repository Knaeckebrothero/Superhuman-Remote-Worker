# Critic Verdict Deadlock — Verification Runbook

Verifies the fix for the 2026-06-03 incident where a **critic verification subjob
looped forever** instead of rendering a verdict. Job `8a3fc7d1-3a66-4337-b51a-50c926431f19`
(verifying scholar job `4486f28a`, model `gpt-5.5`) ran **631 turns / ~248 min /
0 % progress** before it was cancelled.

Background: memory `project_critic_verdict_deadlock.md` (root-cause write-up).

**Root cause (one line):** the critic's verdict tools `approve_job` /
`return_job_with_feedback` are **strategic-phase-only**, but the critic's strategic
prompt told the agent *"Do NOT render verdicts during strategic phases"* — so there
was **no phase in which the agent could both call the tool *and* believe it was
allowed to**.

The mechanics of the deadlock:

- `src/tools/evaluation/evaluation_tools.py:61,69` register `approve_job` and
  `return_job_with_feedback` with `"phases": ["strategic"]`. These are merged into
  `TOOL_REGISTRY` (`src/tools/registry.py:76`) and bound **per phase** by
  `filter_tools_by_phase` (`registry.py:120-140`) at `src/agent.py:1883/1886`, so
  the tools are **absent from the tactical LLM**.
- `config/experts/critic/strategic.txt:4` (and `strategic_minimax.txt:4`) said
  *"Do NOT render verdicts during strategic phases…"*. The agent obeyed, staged a
  **tactical** todo *"Invoke the managed `approve_job` tool"*, entered tactical
  where the tool isn't bound, couldn't call it, hit the tactical stuck-cap →
  `todo_rewind` → back to strategic → re-plan → **loop forever**. `approve_job` was
  never emitted in any of the 633 LLM requests.

The verdict tools use a **deferred-verdict** pattern (calling them sets
`_final_phase_data`, which triggers `finalize_job` on strategic-phase-complete), so
**strategic is the correct phase for the verdict** — the prompt, not the code, was
wrong. `gpt-5.5`'s literal obedience reliably trips the contradiction; looser models
may have just called the tool in strategic and finished.

**What was fixed** (committed `21b5ce8c`, branch `develop`, 2026-06-03):

1. **Verdict-timing instruction** — `strategic.txt:4` + `strategic_minimax.txt:4`
   rewritten: the verdict IS rendered in a strategic phase (after ≥1 tactical
   verification phase); the FIRST strategic phase is planning-only; the tools are
   **strategic-phase-only** and the agent must **never stage `approve_job` as a
   tactical todo** or defer it to tactical.
2. **Decision-criteria line** — the `→ proceed to verdict` bullet
   (`strategic.txt:26` / `strategic_minimax.txt:61`) now says *render the verdict
   now, in this strategic phase, by calling `approve_job`/`return_job_with_feedback`
   directly*.

No code changed — the code-side invariant (verdict tools strategic-only) was already
correct and already tested.

**Coverage map** — what each layer proves:

| Layer | Proves | Needs |
|---|---|---|
| §0 Automated tests | code-side invariant holds + prompt no longer forbids strategic verdicts | local pytest |
| §1 Static check | the fix is present in the repo / deployed image | repo or pod |
| §2 Live end-to-end | a real critic renders a verdict in strategic instead of looping | dev cluster |
| §3 Regression signature | how to recognize the deadlock if it recurs | — |

Target time: **~5 min** for §0–§1 (no cluster); §2 is opportunistic (piggy-back on
the next real review-autonomy job).

---

## 0. Automated tests (quick gate — no cluster)

**Code-side invariant (already in the suite — these pin that the verdict tools stay
strategic-only and every tool has a valid phase list):**

```bash
python -m pytest tests/test_critic_loop.py tests/test_tool_registry.py \
  -q -k "strategic_only or has_phases or verdict"
```

**Pass criteria:** all green (19 passed / 4 skipped at the time of writing — the 4
added by the prompt-side guard below also match this `-k` filter).
Key assertions:
- `tests/test_critic_loop.py::…::test_approve_job_is_strategic_only`
  → `EVALUATION_TOOLS_METADATA["approve_job"]["phases"] == ["strategic"]`
- `…::test_return_job_is_strategic_only` (same for `return_job_with_feedback`)
- `tests/test_tool_registry.py::…::test_every_tool_has_phases`

**Prompt-side guard (the part that actually regressed).** The code tests above would
have stayed green *throughout the entire incident* because the bug was in the prompt,
not the metadata. This guard — wired in as
`tests/test_critic_loop.py::TestCriticStrategicPromptVerdictTiming` — keeps the
contradiction from silently coming back:

```python
# In tests/test_critic_loop.py — project_root is the module-level constant
# (Path(__file__).parent.parent) already defined near the top of the file.
CRITIC_PROMPT_DIR = project_root / "config" / "experts" / "critic"


class TestCriticStrategicPromptVerdictTiming:
    @pytest.mark.parametrize("fname", ["strategic.txt", "strategic_minimax.txt"])
    def test_prompt_does_not_forbid_strategic_verdict(self, fname):
        text = (CRITIC_PROMPT_DIR / fname).read_text(encoding="utf-8")
        # The exact sentence that caused the 8a3fc7d1 deadlock.
        assert "Do NOT render verdicts during strategic phases" not in text, (
            f"{fname} forbids strategic verdicts, but approve_job/"
            "return_job_with_feedback are strategic-only — this deadlocks the critic."
        )

    @pytest.mark.parametrize("fname", ["strategic.txt", "strategic_minimax.txt"])
    def test_prompt_directs_verdict_into_strategic(self, fname):
        text = (CRITIC_PROMPT_DIR / fname).read_text(encoding="utf-8")
        assert "approve_job" in text
        # The fix tells the agent these tools are strategic-only and must not be
        # deferred to a tactical todo.
        assert "strategic-phase-only" in text, (
            f"{fname} lost the directive that the verdict tools are strategic-only."
        )
```

**Pass criteria:** 4 passed (2 files × 2 assertions). Run just this:

```bash
python -m pytest tests/test_critic_loop.py -q -k "VerdictTiming"
```

> This guard intentionally couples to the *metadata*: if anyone ever flips the
> verdict tools to be tactical-bindable (`evaluation_tools.py`), revisit the prompt
> and this test together — the two must agree.

---

## 1. Static check: the fix is present

Against the checked-out branch (or `kubectl exec` into a live agent pod and grep the
baked `/app/config/experts/critic/…` files to confirm the **deployed image** carries
it):

```bash
# The forbidding sentence must be GONE from both prompt variants:
grep -c "Do NOT render verdicts during strategic phases" \
  config/experts/critic/strategic.txt config/experts/critic/strategic_minimax.txt
# expect: 0 and 0

# The corrected directive must be PRESENT in both:
grep -l "Verdict timing — read carefully" config/experts/critic/strategic*.txt
# expect: both files listed

# The code-side metadata must still be strategic-only:
grep -n "\"phases\": \[\"strategic\"\]" -A0 -B2 src/tools/evaluation/evaluation_tools.py
# expect: under both approve_job and return_job_with_feedback
```

**Pass criteria:** `0` / `0` for the forbidding sentence; both files listed for the
directive; `phases: ["strategic"]` still on both verdict tools.

---

## 2. Live end-to-end: a critic renders a verdict instead of looping (dev cluster)

> Prereq: the `develop` image carrying `21b5ce8c` is rolled out to the dev cluster
> (it predates current HEAD, so it is — confirm with §1 against a live pod if unsure).
> Investigate via the `orchestrator-cluster` MCP tools (they target the homelab/dev
> cluster) or the curl fallback `curl http://localhost:8085/api/...`.

The cleanest trigger is a **real review-autonomy job**: submit a small scholar or
developer job whose autonomy spawns a critic verification subjob on completion (the
same path that produced `8a3fc7d1`). Then watch the **critic subjob**:

1. `get_job_progress(<critic_job_id>)` — progress advances; elapsed stays small.
2. `list_llm_requests(<critic_job_id>, limit=100)` — scan the `-> tool` column:
   - **PASS:** an **`approve_job`** (or `return_job_with_feedback`) call appears,
     in a **strategic** iteration that follows at least one tactical phase, within
     the first ~tens of requests.
   - **FAIL:** `approve_job` never appears; instead `next_phase_todos` → … →
     `todo_rewind` cycles repeat (see §3).
3. `get_todos(<critic_job_id>)` — there is **no** archived todo list named
   `failed_*` whose items read *"Invoke the managed `approve_job` tool"*.
4. `get_job(<critic_job_id>)` reaches a terminal verdict state, and the **target
   job leaves `reviewing`** (`get_job(<target_job_id>)` → `completed` if approved,
   or back to `processing`/`paused` carrying feedback if returned).

**Pass criteria:** a simple verification completes in **single-digit-to-low-tens of
phases** with a verdict tool actually emitted — not hundreds of turns at 0 %.

---

## 3. Regression signature — how to recognize the deadlock if it recurs

If a future critic ever loops again, these are the fingerprints from `8a3fc7d1`
(use them as the negative checklist):

- **Turn/elapsed explosion at 0 % progress** — e.g. `get_job_progress` shows
  hundreds of minutes elapsed, `0.0%`, status still `processing`; `get_job` audit
  count in the hundreds/thousands.
- **`todo_rewind` cycling** — in `list_llm_requests`, repeating
  `… next_phase_todos … todo_rewind …` pairs (tactical phase staged, entered, gets
  stuck, rewound to strategic).
- **Phase counter climbing into the teens+** — `git_tags` in the workspace show
  `…-phase-10/12/14-…` for what should be a handful-of-phase verification.
- **`failed_*` todo archives naming the verdict tool** — an archived todo list with
  *"Invoke the managed `approve_job` tool … Not Completed"*.
- **`approve_job` never in any tool-call list** — the agent plans it but never
  emits it.

The decisive question: *is the verdict tool ever emitted, and is the phase in which
the agent tries to emit it the same phase in which `filter_tools_by_phase` binds it?*
If a future change makes them disagree (prompt says one phase, metadata says the
other), this deadlock returns.

---

## Known gaps — NOT covered by this fix

- **No hard graph-level backstop.** The fix is a prompt correction; there is no code
  that *forces* a stuck critic to finalize. If a different prompt/model combination
  re-introduces a "plan a tool I can never call" loop, the tactical stuck-cap will
  rewind rather than terminate, and it can still spin until a budget cap (if any)
  trips. A belt-and-suspenders follow-up would be to surface strategic-only verdict
  tools in the strategic-phase transition message, or to detect "staged a todo
  naming a tool not bound in the target phase" and refuse the stage. Out of scope
  here; track separately if it recurs.
- **Other experts.** Only the critic carried the contradictory directive (verified:
  the forbidding sentence appears in no other expert prompt). `curator` /
  `designer-interactive` reference the verdict tools but never instruct the agent to
  defer the verdict, so they were not deadlock-prone.

---

## Rollback

The change is prose-only in two prompt files. To revert, restore the
`strategic.txt` / `strategic_minimax.txt` line 4 and the `→ proceed to verdict`
bullet to their pre-`21b5ce8c` text. (Reverting reintroduces the deadlock for
literal models — don't, unless the verdict tools are simultaneously made
tactical-bindable.)
