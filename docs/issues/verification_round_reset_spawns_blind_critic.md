---
tags:
  - issue
  - fix-spec
  - jobs
  - orchestrator
  - verification
  - critic
---

# Verification is fail-open: lost verdicts, blind re-review, and eight paths that approve unreviewed work

**Filed:** 2026-07-27, from job `52949749` ("historische Kernwerke") on dev.
**Revised:** 2026-07-27 after a five-agent audit (three codebase, two prior-art).
The revision **corrected two misdiagnoses in the first version** — see
[Corrections](#corrections-to-the-first-version-of-this-doc).
**Status:** **FIXED AND SHIPPED** — rewritten fail-closed on `develop`
(32 commits `c9f3cf1a..928ed60b`, pushed and deployed to dev 2026-07-28).
Sweep precision (2026-08-06): one slice never landed — Slice 1 item 2, the
graph.py drain-vs-verdict overwrite guard (the drain block still
unconditionally sets `freeze_data = upgrade_freeze`; zero commits touched it
in the cited range, and test_graph.py pins the overwrite as intended). Row 1
of the fail-open table is closed in PRACTICE by Slice 2's
journal-before-observe verdict write, not by that guard. The no-progress
short-circuit (item 11) is live but measured near-zero-sensitivity; the
denylist→allowlist inversion for NON_DELIVERABLE_PATHS stays open.
Live gate run 2026-07-29/30: **partial pass** — brief delivery, the
round-recording endpoint and `content_tree` capture are all confirmed working
in production. Cross-round `content_tree` stability was **measured 2026-08-01**
and the guard has near-zero sensitivity on a verification round (the critic
writes its verdict artifacts into the target's own `output/`, so the hashed
tree moves every round regardless of worker progress) — hence the allowlist
inversion above. Still never observed: the loop **converging** (return → fix →
approve); two live-gate attempts died before round 2 on unrelated
infrastructure. Open
follow-ups, the settled-questions record and the live-gate results are in
`docs/issues/verification_fail_closed_followups.md`. This document is retained
as the incident analysis and design rationale.
**Severity:** **high** — a quality gate whose every failure mode is
"approved". Rejected work can reach `completed` with no error, no warning,
and no log line saying a verdict was lost.
**Filename note:** the file is named for the symptom this was found through
(a round counter resetting). The actual defect is broader; the name is kept
because other docs and memory link to it.

**Test coverage:** `tests/verification_delivery_test_coverage.md`.

## Symptom

Job `52949749` was returned by its critic in round 1 (9 issues, severity
high) and again in round 2 (severity high). In round 3 it was **approved** —
on a deliverable that had not changed by a single byte, by the same model at
the same temperature.

## Corrections to the first version of this doc

Recorded explicitly because both errors would have wasted implementation
effort.

1. **"There is no `{prior_findings}` placeholder in
   `verification_instructions.md`" — true but irrelevant.** The template is
   **never delivered to the critic at all.**
   `orchestrator/main.py:12400` renders `format_verification_instructions(...)`
   into a local, `:12406` uses it only as a null-check, and it is then
   discarded — `instructions` appears exactly twice in the entire
   critic-creation block and is never passed to `create_job`, which has no
   `instructions` parameter. The scholar path does this correctly
   (`main.py:11512` → `scholar_context["instructions"]`). The only code that
   ever delivered it, `OrchestratorClient.create_verification_job`, is called
   from nowhere. **Every critic since the orchestrator migration has run on
   the generic two-sentence `jobs.description`.** Adding a placeholder to the
   template changes nothing until the wiring is fixed.
2. **"The design is sound — round continuity is carried by resuming the same
   critic session" — there is no such design.**
   `docs/features/verification_phase.md:169-176` specifies spawning a **new
   critic per round**. The resume-the-same-critic mechanism appeared later
   with no recorded rationale anywhere in the design vault. This is a
   documentation void, not a settled decision — the redesign is not
   re-litigating anything.
3. Minor: the first version argued "not sampling variance" from
   `temperature = 0.0`. Temperature 0 does **not** guarantee verdict
   stability (arXiv 2603.04417). The conclusion stands, but the load-bearing
   evidence is the round-3 transcript showing zero engagement with the open
   issues — not the temperature.
4. Minor: the first version said the critic's toolset was "narrowed" by the
   `config_override` at `main.py:12438`. It is not narrowed — `deep_merge`
   merges dicts by key, so the override **adds** an `evaluation` group and
   removes nothing.

## Root cause — three stacked defects, only the third about verification

### Layer 1 — the two job-terminating decisions live only in process RAM

`_verdict_data` (`src/tools/evaluation/evaluation_tools.py:28`) and
`_final_phase_data` (`src/tools/core/job.py:28`) are bare module-level dicts
in the agent process. Both verdict tools stash intent there; `finalize_job`
consumes it later (`src/core/phase.py:796`) and **clears it before the freeze
is emitted** (`:798`). Any process boundary between rendering and finalizing
loses the decision, and critics cross process boundaries by design — every
round is a fresh dispatch.

Everything less consequential — todos, messages, phase number — is
checkpointed. The two irreversible decisions are not. See
`docs/issues/job_finalization_decisions_held_only_in_process_memory.md` for
the general case, which affects every job, not just critics.

The one durable artifact that exists is written and then never read:
`output/verification_report.json` (`evaluation_tools.py:123`, `:218`) is
written at the same path every round, so each round clobbers the last, and
**no code anywhere reads it back**.

### Layer 2 — a lost decision is *defined* as approval

The intended contract is airtight: a returned verdict builds freeze_data
`{"status": "waiting", ...}` (`src/core/phase.py:686`) and
`orchestrator/services/completion.py:950-958` reads that through verbatim. So
a critic that renders "returned" should be structurally incapable of leaving
`waiting`.

The problem is what happens when the verdict is *absent*. The gate has two
states where it needs three — `approved` / `returned` / **`unknown`** — and
`unknown` is collapsed into `approved` in two places, plus six more paths
that advance the target without any verdict at all. This is
**CWE-636, "Not Failing Securely ('Failing Open')"** in the literal sense.

### Layer 3 — rounds chain through a job status, not a record

`_trigger_verification_on_complete` decides between "next round" and "first
round" purely on the critic's status (`orchestrator/main.py:12350`):

```sql
SELECT id, status, context FROM jobs
WHERE parent_job_id = $1::uuid AND status = 'waiting'
```

The moment a critic leaves `waiting` for any reason, the next freeze takes
the `else` branch and restarts verification at round 0 with a fresh critic
that has no knowledge of the open issues. The round counter lives on the
critic (`context.verification_round`), so it resets too.

Two further defects in that one query: it has **no `verification_target`
filter, no `ORDER BY`, no `LIMIT`**, so a scholar or delegation child parked
in `waiting` is a candidate row; and `_handle_critic_verdict_on_complete`
(`main.py:12200-12217`) excludes only scholars, so a **delegation child**
completing normally — `parent_job_id` set, `freeze_data` present, no
`verdict` — hits the implicit-approval branch and advances its parent before
its siblings finish. Both are fixed by the same one-line discriminator
(`context.verification_target`), which both stale-verification sweepers
already use (`postgres.py:4433`, `:4510`).

## The fail-open inventory

Every path that advances a target without an explicit, surviving verdict.
All anchors verified in code.

| # | Path | Trigger | Target effect |
|---|---|---|---|
| 1 | **Drain overwrites the verdict** | A `version_upgrade` drain at the same `handle_transition` that produced a verdict. `src/graph.py:3607-3608` assigns the verdict freeze; `:3635` **unconditionally overwrites it** with the drain freeze — and `_verdict_data` was already cleared at `phase.py:798`. | Verdict destroyed with **no log line**. Re-dispatch lands on row 2 or 3 → approval. A `returned` silently inverts. Own doc: `drain_freeze_overwrites_critic_verdict.md` |
| 2 | **Critic calls `job_complete`** | Reachable: the `config_override` at `main.py:12438` *adds* `evaluation` and removes nothing, so `core` still carries `job_complete`/`mark_complete` (`config/experts/critic/config.yaml:43-49`). Both are strategic-phase tools, available exactly when the verdict is. | `phase.py:803-812` synthesizes an implicit approval. |
| 3 | **Completed with no verdict in freeze_data** | Any `completed` critic whose freeze blob lacks `verdict`. | `main.py:12228-12236` treats it as implicit approval. |
| 4 | **A *failed* critic still approves** | `main.py:12226` reads the verdict with **no status gate** — the scholar handler has exactly that gate at `main.py:11677`. A verdict emitted alongside an error resolves to `failed`, then approves the target anyway. | Target approved by a critic that failed. |
| 5 | **Round-limit auto-accept** | `returned` verdict AND `verification_round >= max_verification_rounds`. `main.py:12266-12281` marks the critic `failed` and accepts the target. | An explicit rejection becomes an acceptance. |
| 6 | **Round budget resets** | A dead critic is replaced by a fresh one at `verification_round: 0` (`main.py:12420`). | The cap may never bind, while each cycle still ends in auto-accept. Rows 5+6 compound. |
| 7 | **Process death between verdict and finalize** | Pod eviction / OOM / lease expiry / agent re-registration mass-pause (`postgres.py:3880-3890`). | Verdict lost (Layer 1) → re-dispatch → rows 2/3. |
| 8 | **Verdict delivery is best-effort** | Cooperative stop returns before `report_completion` (`dual_app.py:557-564`); a status-race 400 (`main.py:13943`) drops the report. No retry, no outbox. | Verdict dropped; target sits `reviewing` until the 30-min unstick watchdog. |

Related wedges (fail *closed*, but silently): approving a **critic** from the
UI leaves its target in `reviewing` permanently, because the unstick watchdog
requires every critic child to be `failed`/`cancelled` and a `completed` one
blocks it forever — see `approving_a_critic_wedges_target_in_reviewing.md`.

## Evidence from the incident

| Claim | Evidence |
|---|---|
| Deliverable unchanged across all 3 rounds | Every `write_file`/`edit_file` touching `output/kernwerke.md` in the audit DB ends at **2026-07-23 22:33:46Z**. Rounds ran 07-23 21:47, 07-25 23:33, 07-26 13:35. |
| Round 3 was registered as round **1** | New critic `2ca33236` has `context.verification_round = 0`. The prior critic `51a4ce11` sits at `verification_round = 2`, `status = completed`. |
| Both critics got the same generic brief | `jobs.description` byte-identical — now explained by the dead template, not by a missing placeholder. |
| Round 3 never saw the open issues | Its full reasoning transcript has **0 occurrences** of "Hausvater" (issue C3) and 0 of "Akzeptanzkriterium 10". |

**Not established:** which of rows 1–8 actually fired for the round-2 critic.
Its log is gone and the verification report was never committed to the job
repo. The doc's original claim ("it called `job_complete`") matches row 2
exactly but is inference.

What round 3 approved, from its own transcript: a **modern toxicology
disclaimer** (§6.3, lines 636-639) counted as bibliographic source coverage;
the exact substitution round 1 rejected (E05, a German comparative work)
accepted as the required Italian primary source; the Hausväterliteratur issue
never considered at all; and `K05` listed as `Italian: (original)` when line
44 of the deliverable says `Deutsch (Übers. aus Ital.)`.

This is not model variance. It is **criteria drift** — a documented
phenomenon (Shankar et al., UIST'24): graders form criteria *by* grading, so
a fresh judge lands on a *different* rubric, not a noisier version of the
same one. Combined with measured leniency bias, an unanchored judge's error
is biased **toward approval**.

## Prior art — what mature systems do

- **GitHub branch protection** is the exact analogue. A "request changes"
  review blocks merge until *that reviewer* clears it, and **another
  reviewer's approval does not dismiss it**. Approval is scoped to a diff and
  goes stale when the diff changes — inverted, that gives the rule we need:
  *if the diff has not changed, a prior blocking review stays in force.*
  "Require conversation resolution" gates merge on the findings ledger rather
  than on a reviewer's overall impression.
- **Cloudflare's production AI reviewer** ships the round contract: the
  reviewer receives its prior findings *with resolution status*; fixed
  findings are omitted and auto-resolve; unfixed findings **must be
  re-emitted** or the thread closes; severity is a closed taxonomy feeding a
  lookup table rather than advisory prose.
- **SARIF 2.1** solves per-finding identity across runs with
  `partialFingerprints` + `baselineState ∈ {new, unchanged, updated, absent}`.
- **Durable execution** (Temporal, Restate, DBOS) is unanimous on
  *journal-before-observe*: the decision is committed before the caller
  observes it. DBOS's strong form — step write and durability record in one
  transaction — is available to us for free, because our state is already in
  Postgres.
- **Fail-closed defaults are the norm for gates**: Kubernetes admission
  webhooks default `failurePolicy: Fail`; Envoy `ext_authz` defaults
  `failure_mode_allow: false`. Where fail-open is legitimate it is always an
  **explicit declared field**, never an implicit fallthrough.
- **Round caps terminate in escalation, never approval**: AWS CodePipeline
  manual approval times out to *failed*; GitHub Actions environment
  protection times out to a *failed job*.

This also resolves a contradiction the design vault never reconciled.
`docs/done/critic_subjobs.md` (Open Issue #2) observed that a critic seeing a
prior critic's **report** was biased by it and proposed stripping prior
reports; this doc's first version proposed injecting them. Both are right:
injecting a *verdict* anchors the judge; injecting *findings with a forced
per-ID disposition* constrains it. **Inject findings, never inject verdicts.**

## Fix plan

Sliced so the safety work can ship before the design work.

### Slice 1 — stop the bleeding (contained, no design risk)

1. **Remove `job_complete`/`mark_complete` from the critic's toolset.**
   Spell out `core` explicitly in the `config_override` at `main.py:12443`;
   `deep_merge` replaces the list, so this works. Closes row 2.
2. **Don't let the drain overwrite a verdict freeze.** At `src/graph.py:3635`,
   skip the overwrite when the existing freeze carries `verdict`/`status`, or
   move the drain check before the transition. Log loudly either way. Closes
   row 1.
3. **Add the terminal-status gate** to `_handle_critic_verdict_on_complete`,
   mirroring the scholar handler at `main.py:11677`. Closes row 4.
4. **Discriminate on `context.verification_target`** in both the verdict
   handler (`main.py:12215`) and the round lookup (`main.py:12356`). Fixes
   the wrong-sibling selection and the delegation-child misfire.
5. **Validate the verdict call at the tool boundary.**
   `return_job_with_feedback` with `severity != none` and empty `issues` is
   rejected back to the model. Record `Issues: N` from `len(issues)`, never
   from a model-supplied value. This is a *post-validation invariant* — JSON
   Schema cannot express it, which is why the incident recorded
   "Issues: 0, Severity: high" without complaint.

### Slice 2 — the durable record

6. **Append-only `verification_rounds` array on the TARGET job's context**,
   written by a new atomic helper cloned from `append_queued_reply`
   (`orchestrator/database/postgres.py:1859-1897`) — a single-statement
   `jsonb_set + ||` append that is lost-update-immune under two orchestrator
   replicas and already has a racing-writer test. Record shape:
   `{round, critic_job_id, verdict, issues[], severity, report, deliverable_hash, ts}`.
   **No migration required.**
   - Round number becomes `jsonb_array_length(...)`, so it cannot reset with
     the critic, and `increment_job_verification_round` becomes redundant.
   - Continuity stops depending on `status = 'waiting'`.
   - `_trigger_verification_on_complete` already holds the target job dict,
     so prior findings cost no extra query.
   - Guard idempotency on `critic_job_id` (a duplicate `/complete` would
     otherwise append twice) and keep read sites to two, routed through one
     coercion helper — every new JSONB key inherits the asyncpg
     string-not-dict hazard.
7. **Actually deliver the critic brief.** Put the rendered instructions in
   `context["instructions"]` the way the scholar does (`main.py:11512`), then
   add the `{prior_findings}` placeholder. Order the output schema so
   **quoted evidence precedes the verdict field** (Proof-Before-Preference —
   measured to cut verdict-flipping under misleading cues from 75-85% to
   5-22%). Note the `str.format` trap: every literal brace in the template
   must stay escaped, and a `KeyError` returns `None`, which aborts critic
   spawn entirely at `main.py:12406`.
8. **Forced per-finding disposition.** Rounds 2+ receive all open findings by
   ID; the critic must mark each `RESOLVED` (with a quote from the *new*
   deliverable) or `STILL_OPEN`. Free-form re-review is not an accepted
   output shape.

### Slice 3 — fail closed

9. **Stop synthesizing approvals.** Both implicit-approval sites
   (`phase.py:803-812`, `main.py:12228-12236`) resolve to `unknown` and
   escalate to the human gate. If an availability escape hatch is ever
   wanted, it must be a named, off-by-default flag that stamps an audit
   marker — never the fallthrough branch.
10. **Round cap escalates.** **DECIDED 2026-07-27:** at the cap the target
    goes to `pending_review` with the open findings attached, and the critic
    is not marked `failed` for what is a policy outcome. This is a
    user-visible behaviour change: work that used to auto-complete will now
    wait for a human.
11. **No-progress short-circuit.** Hash the deliverable; an identical hash
    with an open blocking finding auto-returns without spawning a judge at
    all. This alone would have prevented the incident, and it is a standard
    agent-loop stop condition.

## Decisions recorded

| Question | Decision | Date |
|---|---|---|
| Behaviour at the round cap | **Escalate to human**, never auto-accept | 2026-07-27 |
| Blind vs. informed critic (the `critic_subjobs.md` #2 conflict) | **Inject findings, never verdicts** — forced per-ID disposition | 2026-07-27 |
| Storage for the round record | **JSONB array on the target**, not a table. Zero migrations; documented 2-release upgrade path to a table exists (`job_datasources` proves it walkable) if cross-job analytics are ever needed | 2026-07-27 |
| Round budget owner | **The target job**, not the critic | 2026-07-27 |

## Test gaps to close

Currently **zero** coverage on this path:
`_trigger_verification_on_complete` has no direct test; the round-limit test
class (`tests/test_critic_loop.py:363-538`) is `@pytest.mark.skip`ped with a
note that the logic moved to the orchestrator, where it was never re-tested;
nothing exercises multi-round continuity, verdict loss, or a critic
terminating without a verdict. `TestVerificationTriggerGuards`
(`tests/test_complete_job_endpoint.py:296-354`) is tautological — it
re-asserts the predicates instead of invoking the guarded function, and would
pass if the guards were deleted.

## Triage rule until fixed

**An approval that follows a return is not automatically a quality signal.**
Compare the approving critic's job id with the returning one — a *different*
id means the approval came from a critic that never saw the findings, and the
verdict must be re-checked by hand.

## Related

- `docs/issues/drain_freeze_overwrites_critic_verdict.md` — row 1, standalone.
- `docs/issues/job_finalization_decisions_held_only_in_process_memory.md` —
  Layer 1 generalised to every job.
- `docs/issues/approving_a_critic_wedges_target_in_reviewing.md` — the
  mirror-image wedge.
- `docs/issues/feedback_resume_restricted_closure_toolset.md` — the degraded
  session that made the round-2 critic close itself.
- `docs/issues/stale_critic_waiting_status_escapes_reaper.md` — a critic
  stuck *in* `waiting`; together these show `waiting` is a fragile,
  load-bearing status.
- `docs/features/verification_phase.md` — the founding spec, which never
  contemplated multi-round continuity.
- `docs/done/critic_subjobs.md` — Open Issue #2, resolved above.
- `docs/issues/maxsessions_parallel_tools_false_workspace_death.md` — the
  incident chain this was discovered in.
