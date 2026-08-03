# App Guide M2 live acceptance results

- Date/time (UTC): 2026-07-28, approx. 13:28–14:33
- Tester: Claude Code (agent), operating under repository-owner authorization
- Target class: local k3d single-node cluster, Tilt-managed release, namespace `srw`
- Source commit: `ec4bbe6b` on `develop`; `git merge-base --is-ancestor 326963b7 HEAD` exits 0
- Deployed component revisions: **declared provenance unavailable** (see Critical
  findings #1). Images are locally built Tilt artifacts:
  `srw-orchestrator:tilt-9ffd313e30e37761`,
  `srw-cockpit:tilt-274cfd680b3c1ed7`,
  `srw-mcp:tilt-870d0f04ddd15360`
- Initial/final flags: endpoint `false` / tool `false` → **restored to
  `false` / `false` (State E)** and verified in ConfigMap and pod environment
- Model ID: `gemma-4-moe` (deployment's system-default session model).
  The intended release route `gpt-5.5` was **not reachable** (its codex-proxy
  backend is disabled in the local overlay and requires an interactive
  one-time OAuth login).
- Raw evidence location (private; no credentials): session scratchpad
  `.../scratchpad/evalrun_{1,2,3}/` (harness artifacts, Git-ignored)

> **Owner adjudication (2026-08-03):** the final **BLOCKED** verdict and
> keep-defaults-off recommendation stand. The gate labels below have been
> normalized to the handoff's rule that there is no "pass with caveats."
> Observations from the non-release `gemma-4-moe` route remain useful diagnostic
> failures, but they are not release-model evidence. This adjudication changes
> no raw observation and does not claim that a missing test passed.

## Gate summary

| Gate | Result | Safe evidence |
|---|---|---|
| Preflight | BLOCKED (clean-tree criterion) | ancestor gate 0; routing corpus 30 cases; capability corpus 8 cases / all 6 categories; 183 focused tests pass. Two unrelated tracked files were already dirty, so the strict clean-tree release criterion was not met (see #4) |
| State A default-off | PASS | authed 503 + `rollout_disabled` + `private, no-store` + `no-cache` + `Retry-After: 60`; logged-out 401 `Not authenticated`, no rollout leak; 50 tools bound with `read_product_guide` present and `get_product_capabilities` absent; stable prompt returned folder-allowlist workflow; dynamic answer stayed explicitly unknown |
| State B endpoint dark launch | BLOCKED (MCP and privacy rows) | The exercised cookie-authenticated endpoint, scope, filter, bounds, no-store, and tool-absent checks passed: 200 / `enabled`; schema major 1; user/thread scope stayed distinct; invalid input returned 422; unknown ID returned bounded partial output; full body was 17,277 B < 64 KiB. The two MCP-token admission rows and the sentinel privacy protocol were unavailable, so State B as a whole is not a pass |
| State C guide/tool canary | **FAIL** (one criterion) | Tool binding grew 50 → 51 with `get_product_capabilities` present. Stable prompt: `read_product_guide` only, **zero** capability calls. Focused dynamic: guide→guide→capability, correct ordering, and the exact `{"capability_ids":["datasources.email.send"]}` form was emitted — but an **extra broader capability call preceded it**, so "arguments are exactly …" is not satisfied (see #2). Guide topic ID `datasources-email` was never sent as capability `topic`. Answer correctly separated user vs session layers and claimed no execution |
| Fresh/resumed workspace matrix | NOT RUN | Deprioritized after the verdict was already determined by blocked/failed gates; no None/Virtual/Container fresh+resumed cells were executed. Explicitly not claimed as passing |
| Email state matrix | BLOCKED | Zero datasources exist and no mail server is deployed; no read/draft/send-tier connector fixtures and no mail sink. One row was incidentally evidenced live: an unattached session reported `session=needs_attachment` with no execution claim |
| Changed-before-action | BLOCKED | Requires a send-tier connector with unattended send plus a test sink; neither exists. **No SMTP was opened or submitted at any point in this run** |
| Partial/unknown | BLOCKED | A naturally unknown `memory.recall` deployment layer was observed safely, but it did not exercise the required controlled evaluation failure/truncation seam. Its `completeness=complete` envelope is consistent with the contract and is supplemental evidence only (see #3) |
| Mixed deployment | BLOCKED | No staggered-deployment seam and no declared component revisions to differ (see #1). `product.mixed_build` was `null` |
| Model run 1 | BLOCKED (release route); diagnostic FAIL | Intended release route unavailable. On `gemma-4-moe`: 8 cases, passed 1, trajectory 2, grounding 2, strict order 5, critical_forbidden 0, errors 0, `release_gate_pass=false` |
| Model run 2 | BLOCKED (release route); diagnostic FAIL | Intended release route unavailable. On `gemma-4-moe`: 8 cases, passed 0, trajectory 3, grounding 1, strict order 6, critical_forbidden 0, errors 0, `release_gate_pass=false` |
| Model run 3 | BLOCKED (release route); diagnostic FAIL | Intended release route unavailable. On `gemma-4-moe`: 8 cases, passed 1, trajectory 3, grounding 2, strict order 5, critical_forbidden 0, errors 0, `release_gate_pass=false` |
| State D dependency failure | BLOCKED (resumed row unrecorded) | The exercised fresh path passed: the tool remained visible (51 tools) with endpoint off; the call returned `unavailable` rather than throwing/looping; stable guidance survived; dynamic state stayed unknown; and no raw error reached the answer. The required resumed-session repetition was not recorded |
| State E rollback | BLOCKED (resumed row unrecorded) | The rollback state itself passed: endpoint returned the State A 503 contract; a fresh session bound 50 tools without the capability tool; M1 guidance survived; dynamic state stayed unknown; no retry storm occurred; ConfigMap and new workloads were false/false. The required pre-canary resumed-session check was not recorded |
| Privacy scan | BLOCKED | No mailbox, folder, credential, connector, or message content appeared in any response or answer observed. The §8.5 sentinel protocol could not be executed because it requires connector/mailbox fixtures that do not exist, so the privacy release gate remains unproven. No mutation of connectors, mounts, workspaces, jobs, or health was performed by any probe |

### Admission and scope matrix (§8.3)

| Caller | Request | Expected | Observed | Result |
|---|---|---|---|---|
| Logged out | no thread | 401 | 401, no rollout details | PASS |
| Unapproved user | no thread | 403 | 403, bounded approval message, 0 capabilities | PASS |
| Owner | owned thread | 200 | 200, `scope.kind=thread` | PASS |
| Non-owner | another user's thread | 403 before capability resolution | 403 `Not your thread`, 0 capabilities | PASS |
| Admin | admitted thread | 200 with safe `admin_allowed` | 200 with `user.reason_code=admin_allowed` | PASS |
| Project-scoped MCP token | no thread | 403 | — | BLOCKED (no token available) |
| User-scoped MCP token | own user query | 200 | — | BLOCKED (no token available) |

## Critical findings

### 1. Declared deployment provenance is empty for every component

`SRW_DEPLOYMENT_PROVENANCE_JSON` carries empty `source_revision`,
`artifact_digest`, and `release_version` for all five components
(orchestrator, agent, cockpit, mcp, workspace). Consequences:

- The runbook's "candidate must contain `326963b7`" cannot be established from
  the deployment itself. It was established only indirectly: the images are
  Tilt builds of this worktree, and the feature's server route and agent tool
  modules are present inside the running orchestrator image.
- The mixed-build cell is not merely unavailable for lack of a second
  deployment — there is no revision data that *could* differ. `mixed_build`
  was `null` rather than a stated uncertainty.

The endpoint did not fabricate provenance, which satisfies "missing provenance
is reported as unavailable, not fabricated." This is recorded as a release
blocker for the mixed-deployment gate, not as a false claim.

### 2. Focused dynamic prompt emits an extra broader capability call

The §9.1 criterion requires the focused capability arguments to be exactly
`{"capability_ids":["datasources.email.send"]}`. Observed trajectory in a
fresh State C session was:

1. `read_product_guide(index)`
2. `read_product_guide(datasources-email)`
3. `get_product_capabilities(datasources.email)`  ← extra, broader
4. `get_product_capabilities({"capability_ids":["datasources.email.send"]})`

Ordering (guide before capability) and the forbidden-topic rule both hold, and
the resulting answer was correct and honest. But a required assertion about
arguments is not satisfied, so State C is recorded FAIL rather than PASS. This
is consistent with the held-out fallback-model scores (trajectory 2–3 of 8),
but the cause is not established. It may be model-route sensitivity or an
interaction between the model and the guide procedure. Re-run it on the
intended release model before assigning the defect, without waiving the
exact-one-call criterion.

### 3. Natural layer-level `unknown` is not the controlled partial cell

`memory.recall` returned `deployment=unknown`
(`deployment_observation_unavailable`) while the envelope reported
`completeness=complete`. Owner review confirmed that this is the intended
contract: envelope completeness describes whether the requested visible
capability set was returned without evaluation errors or truncation. A known,
successfully evaluated layer may legitimately resolve to `unknown` while the
envelope remains `complete`.

Section 9.6 instead requires a controlled resolver/live-observation failure or
truncation. That seam should produce a bounded evaluation error or truncation,
`completeness=partial`, and affected layer state `unknown`. Because no such
seam was exercised, the required partial/unknown cell is **BLOCKED**. The
natural `memory.recall` observation remains useful supplemental evidence and is
not a product defect.

### 4. Environment deviations introduced during this run

Recorded for transparency; none touch the feature under test.

- **Cluster DNS was broken and was repaired.** No pod could resolve the
  VPN-internal LLM endpoint hostname, so every LLM call failed with a
  connection error and the first session turn produced no answer. Routing to
  the endpoint worked; only name resolution failed. Fixed by adding a hosts
  entry to the existing `coredns-custom` ConfigMap — the same mechanism
  `scripts/local-dev-up.sh` already uses for the `*.localhost` names — and
  restarting CoreDNS. **This is pre-existing local-environment breakage, not a
  regression from the feature.**
- `imap-tools>=1.13.0` (declared in `requirements.txt`) was not installed
  locally; 12 `tests/test_email_tools.py` tests failed on import until it was
  installed. After installing the declared dependency all 183 focused tests
  pass.
- Two tracked files (`Tiltfile`, `scripts/local-dev-up.sh`) were already dirty
  at start with unrelated chart-dependency vendoring changes. They were left
  untouched, so the strict "no tracked checkout changes" preflight criterion is
  not literally met.
- Dev passwords were set on two synthetic, non-admin local users to exercise
  the non-owner and unapproved-user admission rows. A disposable thread fixture
  was created for the non-owner row and **has been deleted**.
- A `tilt trigger` reconcile performed a full Helm uninstall/reinstall of the
  release (revision reset to 1, all Deployments recreated). StatefulSet-backed
  data survived. Flag state and images were re-verified afterward.

## Final verdict

**BLOCKED**

Required release evidence remained unavailable in several independent areas:

- The MCP-token admission rows, controlled partial/unknown seam,
  mixed-deployment cell, live email matrix, changed-state-before-action cell,
  and sentinel privacy protocol had no available fixtures or seams.
- The fresh/resumed None, Virtual, and Container matrix was not run.
- The required resumed-session repetitions in States D and E were not recorded,
  although their exercised fresh paths and final false/false state passed.
- The three-repeat held-out model matrix could not run on the intended release
  route. On the only reachable fallback route it failed diagnostically (0–1 of
  8 passing across three runs).

State C also recorded a genuine failure against the exact-one-call arguments
criterion on the fallback route (finding #2). It remains a mandatory re-test,
but the unavailable release route keeps the final acceptance verdict BLOCKED.

## Rollout recommendation

**Keep defaults off.** The deployment has been left in State E with both
controls `false`, verified in the ConfigMap and in newly started workloads.

What this run establishes and should carry forward: the exercised
cookie-authenticated admission and bounds checks passed; no mutation or
private sentinel-like content was observed in the limited fixtures; the
rollout controls behaved correctly in all four flag combinations exercised;
the dependency-failure and rollback paths degraded honestly without exposing
raw errors; and across 24 fallback-model trajectories there were **zero
critical forbidden claims**. This is encouraging safety evidence, not a passed
privacy or release gate. The complete live email/action, MCP-token,
fresh/resumed, controlled partial, mixed-deployment, sentinel-privacy, and
intended-release-model surfaces remain unproven.
