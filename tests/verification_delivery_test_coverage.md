# Verification + delivery-failure — test coverage map (what's covered vs what isn't)

Companion to `knowledge-base/knowledge/issues/verification_fail_closed_followups.md` and
`knowledge-history/done/git_push_fails_silently_via_workspace_backend.md`. Records what is
pinned, by which mechanism, and — the point of this file — **what is not**, why
it is safe today, and how to close it.

Last updated **2026-08-07**, after the delivery-failure chain
(`abe4d1d1`, `233b2649`, `728214bf`) and the virtual-dirs kill-switch removal
(`a4929b17`).

Gap statuses were **re-verified against the suite** on that date rather than
carried forward. Of the nine gaps inherited from
`verification_fail_closed_followups.md` §3, two have since been closed and are
no longer listed:

- *"no test pins that a disposition naming an unknown id is rejected"* — closed
  by `test_verification_ledger.py::test_unknown_id_rejected`.
- *"the three-round autonomy test has no vacuity guard"* — closed;
  `tests/test_autonomy.py:636` now asserts an `archive/` path actually reached
  HEAD.

The other seven are carried below, each re-checked, plus two new ones from the
delivery chain.

**Why this file exists.** This subsystem has a specific failure signature: a
guard that ships **inert**, with a green test covering it. The `content_tree`
no-progress guard shipped inert three times, each time under a passing test,
because the test mocked the collaborator that moved the tree. So the standing
rule here is *a test that cannot fail against the unfixed code is not coverage* —
and every entry below is annotated accordingly.

---

## 1. Covered

### 1.1 The decision layer (pure, no I/O)

| Area | File | What it pins |
|---|---|---|
| Severity + blocking | `tests/test_verification_ledger.py` | unknown/missing severity fails **closed** (blocking) |
| Fold across rounds | `tests/test_verification_ledger.py` | RESOLVED closes, DISPUTED and STILL_OPEN keep open; accumulation across rounds; non-dict rounds/findings/dispositions and non-list `opened` are all skipped rather than crashing |
| Disposition validation | `tests/test_verification_ledger.py` | unknown id rejected; unknown disposition value rejected; duplicate dispositions for one id rejected |
| Verdict computation | `tests/test_verification_ledger.py` | an asserted `returned` is honoured unconditionally; otherwise blocking-open ⇒ `returned` |
| Gate decision | `tests/test_verification_flow.py::TestVerificationGateDecision` | spawn/escalate across round cap, no-progress, and the abstain case when a ledger row predates `content_tree` |

### 1.2 The wiring (the layer that kept going dead)

| Area | File | What it pins |
|---|---|---|
| `content_tree` actually flows | `TestTriggerVerificationContentTreeWiring` | escalation through the **real** trigger, not the decision function — plus a contrast case that a *changed* tree still spawns, so an always-escalate implementation fails |
| Undelivered completion | `TestUndeliveredCompletionSkipsTheCritic` | escalates instead of spawning; contrast case that a delivered completion still spawns; loop jobs escalate to `completed`, not `pending_review` |
| No duplicate critic | `TestNoDuplicateCriticSpawn` | a retried `/complete` does not spawn a second critic for the same round |
| Escalation side effects | `TestEscalateTargetWakesAndNotifies` | wake + notification fire; the status write is load-bearing and independent of both |

### 1.3 Delivery failure (agent side)

| Area | File | What it pins |
|---|---|---|
| Job-ending push failure | `tests/test_phase_delivery_failure.py` | ERROR log + `delivery_failed` / `delivery_error` on the record, for `finalize_job` (both branches) and `freeze_for_review` |
| The two non-failures | same | no remote configured and git inactive are **not** delivery failures — `push()` returns False for all three reasons and only one is a lost deliverable |
| Clean push | same | no marker written; absence is the signal, nothing writes it False |

### 1.4 Delivery failure (orchestrator side)

| Area | File | What it pins |
|---|---|---|
| Gate skips, not bounces | `tests/test_deliverable_gate.py::TestEvaluate` | `delivery_failed` ⇒ `skipped`, carrying the agent's own reason; flag-less completion still evaluated; the flag alone suffices without a reason string |
| **The ordering** | `TestRunGate::test_undelivered_completion_does_not_bounce` | `bounced is False`, `queue_resume` not awaited. This is the assertion that would have caught the original defect — the evaluate-level ones would not, because the bug was that a bounce **early-returns in the caller** and preempts the verification escalation entirely |

### 1.5 Boot guarantee (replaced the kill switch)

| Area | File | What it pins |
|---|---|---|
| Taskless boot refuses | `tests/test_graph.py::TestInitStrategicTodosNode` | raises when `task_brief.md` **and** `instructions.md` both resolve empty, whitespace included; `instructions.md` alone missing stays a normal boot |
| No route back to the write | `tests/test_workspace_phase0_seed.py::TestInstructionFilesAreNeverWritten`, `tests/test_virtual_dirs_wiring.py::test_virtual_dirs_enabled_is_inert` | the removed flag is inert; an overlay-less manager is unconstructible |

### 1.6 Mutation-checked

Not a claim of intent — actually run, by disabling the code and confirming the
test goes red:

| Guard | Disabled how | Result |
|---|---|---|
| `delivery_failed` recording | `if record is not None:` → `if False:` | 3 positive tests fail, 4 negative pass |
| Verification skip | `if freeze_data.get("delivery_failed"):` → `if False:` | 2 positive tests fail, 1 contrast passes |
| Gate skip | `if isinstance(freeze, dict) and …:` → `if False:` | 2 skip tests fail, 31 pass |

---

## 2. Outstanding — ranked

Nothing here is a known defect. Each is a missing pin.

### 2.1 Worth doing first

1. **The agent→orchestrator wire for `delivery_failed` is unpinned end to end.**
   `phase.py` writing the key is tested; the orchestrator reading it is tested;
   **nothing tests that the key survives the transport between them.** If the
   completion payload ever drops unknown freeze keys, both sides stay green and
   the whole chain silently reverts to the old behaviour. This is exactly the
   shape of the three inert-guard incidents. Close it with one test that posts a
   real completion body carrying the flag through the `/complete` handler and
   asserts no critic is spawned.
2. **The route wrapper for `POST /api/jobs/{id}/verification/rounds` is
   untested** — only the impl function and the client side are. Confirmed still
   open 2026-08-07 (`tests/test_orchestrator_client.py` covers the client
   posting to the URL, not the server route). A wrapper-level regression —
   auth, body parsing, status mapping — would be invisible.
3. **`_verification_rounds`' string-coercion branch is untested.** Confirmed
   still open: the two tests matching that name
   (`test_unstick_reviewing_parents_ledger.py`) exercise the **SQL** watchdog's
   JSON handling, not the Python helper every ledger read goes through. This is
   the asyncpg JSONB-as-string path ([[asyncpg_jsonb_returns_string]]); failure
   is loud and fail-closed (empty list ⇒ escalate), which is why it is not
   ranked higher.

### 2.2 Lower value

4. **Malformed `job_id` on `append_verification_round`** — untested guard
   branch, inherited from the helper it was cloned from. Confirmed still open.
5. **An `opened` entry with a falsy id** is not pinned as dropped from the fold.
   Adjacent shapes *are* covered (non-dict findings, non-list `opened`), so this
   is a narrow hole in an otherwise defensive set.
6. **`freeze_data` as a JSON string** is not covered by the head-commit-authority
   tests — they cover dict and `None`, while `_parse_freeze_data` handles three
   forms.
7. **Only one of the four job-ending sites asserts the ERROR log.**
   `_finalize_with_verdict` and `freeze_for_review` are pinned for the record
   marker but not the log level. Cheap to widen.

### 2.4 Test hygiene, not coverage

8. **`tests/test_autonomy.py::test_a_real_content_change_moves_the_value` still
   uses `MagicMock()` for the todo manager** (line ~733), against that file's
   own "no mocks on the workspace side" rule. Confirmed still present
   2026-08-07. Harmless in itself — the test asserts *inequality*, so a mocked
   collaborator cannot make it pass falsely — but mocking exactly this
   collaborator is what hid the inert `content_tree` guard for three
   iterations. Its sibling three-round test now uses a real `TodoManager`; this
   one should match.
9. **`tests/test_complete_job_endpoint.py::test_critic_returned_gets_waiting`
   passes but exercises a path no production caller reaches** — a returned
   verdict freezes `completed` now, not `waiting`. Confirmed still present
   2026-08-07. A green test for dead behaviour is worse than no test: it
   implies a contract that no longer holds. Either delete it or re-point it at
   the live path.

### 2.3 Deliberately not covered

- **`_complete_phase_with_git`** does not record delivery failure, **by design**:
  a mid-job phase push that fails is recoverable by the next boundary or the
  job-ending push. Only endings are terminal. Do not "fix" this without
  changing that reasoning first.
- **The critic's own workspace sharing.** Nothing runs two agents against one
  filesystem, which is why
  `knowledge-history/done/critic_brief_lands_in_shared_workspace_and_misleads_target.md`
  surfaced only on a real two-round run. A unit test cannot reach it; this is
  live-gate territory.

---

## 3. Not reachable by tests — the live gate

**The verification loop converging has never been observed**: round 1 returns
with a blocking finding, round 2 fixes exactly that, round 2's critic approves.
Two attempts died before round 2 on unrelated infrastructure. No unit test can
substitute — the failures both times were in the seams between real components.

Before a third attempt, see the "what a third run needs" table in
`knowledge-base/knowledge/issues/verification_fail_closed_followups.md`. Both infrastructure
blockers are now closed; **the fixture itself is still defective** and must be
rewritten first — the agent read "omit X now, add it when returned" as a script
to run internally rather than as two rounds, so round 1 had no gap to find.

## 4. Related

- `knowledge-base/knowledge/issues/verification_fail_closed_followups.md` — open follow-ups, the
  settled-questions record, and the live-gate results
- `knowledge-base/knowledge/issues/verification_round_reset_spawns_blind_critic.md` — the incident
  analysis and design rationale
- `knowledge-history/done/git_push_fails_silently_via_workspace_backend.md` — the delivery
  chain and why each layer handles it the way it does
- `tests/virtual_directories_test_coverage.md` — the overlay that serves
  `instructions.md`, whose kill switch §1.5 replaced
