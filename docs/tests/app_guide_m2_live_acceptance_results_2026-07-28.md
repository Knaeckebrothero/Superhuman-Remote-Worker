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

## Gate summary

| Gate | Result | Safe evidence |
|---|---|---|
| Preflight | PASS (with deviation) | ancestor gate 0; routing corpus 30 cases; capability corpus 8 cases / all 6 categories; 183 focused tests pass. Deviation: 2 tracked files dirty, unrelated to feature (see #4) |
| State A default-off | PASS | authed 503 + `rollout_disabled` + `private, no-store` + `no-cache` + `Retry-After: 60`; logged-out 401 `Not authenticated`, no rollout leak; 50 tools bound with `read_product_guide` present and `get_product_capabilities` absent; stable prompt returned folder-allowlist workflow; dynamic answer stayed explicitly unknown |
| State B endpoint dark launch | PASS (2 rows BLOCKED) | 200 / `enabled`; schema major 1; `scope.kind=user`; `session=not_applicable`; `agent_action=unknown`; complete + untruncated. Thread scope: `scope.kind=thread`, `session=unknown`, `agent_action=unknown`. Bounds: invalid topic / invalid ID syntax / limit 0 / limit 51 / malformed UUID / 21 IDs all 422; unknown ID → 200 + `completeness=partial` + `capability_not_visible`; topic∩IDs returned only the email ID; full body 17,277 B < 64 KiB, sorted. Tool remained absent in a fresh session |
| State C guide/tool canary | **FAIL** (one criterion) | Tool binding grew 50 → 51 with `get_product_capabilities` present. Stable prompt: `read_product_guide` only, **zero** capability calls. Focused dynamic: guide→guide→capability, correct ordering, and the exact `{"capability_ids":["datasources.email.send"]}` form was emitted — but an **extra broader capability call preceded it**, so "arguments are exactly …" is not satisfied (see #2). Guide topic ID `datasources-email` was never sent as capability `topic`. Answer correctly separated user vs session layers and claimed no execution |
| Fresh/resumed workspace matrix | NOT RUN | Deprioritized after the verdict was already determined by blocked/failed gates; no None/Virtual/Container fresh+resumed cells were executed. Explicitly not claimed as passing |
| Email state matrix | BLOCKED | Zero datasources exist and no mail server is deployed; no read/draft/send-tier connector fixtures and no mail sink. One row was incidentally evidenced live: an unattached session reported `session=needs_attachment` with no execution claim |
| Changed-before-action | BLOCKED | Requires a send-tier connector with unattended send plus a test sink; neither exists. **No SMTP was opened or submitted at any point in this run** |
| Partial/unknown | PASS (with caveat) | A real, naturally occurring case: `memory.recall` reported `deployment=unknown` with reason `deployment_observation_unavailable` while all other layers survived. Caveat in #3: the envelope still reported `completeness=complete` |
| Mixed deployment | BLOCKED | No staggered-deployment seam and no declared component revisions to differ (see #1). `product.mixed_build` was `null` |
| Model run 1 | FAIL (route not release) | 8 cases, passed 1, trajectory 2, grounding 2, strict order 5, critical_forbidden 0, errors 0, `release_gate_pass=false` |
| Model run 2 | FAIL (route not release) | 8 cases, passed 0, trajectory 3, grounding 1, strict order 6, critical_forbidden 0, errors 0, `release_gate_pass=false` |
| Model run 3 | FAIL (route not release) | 8 cases, passed 1, trajectory 3, grounding 2, strict order 5, critical_forbidden 0, errors 0, `release_gate_pass=false` |
| State D dependency failure | PASS | Tool remained visible (51 tools) with endpoint off; capability call completed as an `unavailable` status rather than throwing or looping; stable guidance preserved; dynamic state explicitly unknown; **no raw HTTP/provider/internal error text reached the answer** |
| State E rollback | PASS | Endpoint returned the State A 503 contract exactly; fresh session bound 50 tools with the capability tool absent; M1 guide still answered accurately; dynamic state explicitly unknown; no retry storm (2 capability-related orchestrator log lines in 5 min); ConfigMap and newly started workloads both `false`/`false` |
| Privacy scan | PARTIAL | No mailbox, folder, credential, connector, or message content appeared in any response or answer observed. The §8.5 sentinel protocol could not be executed because it requires connector/mailbox fixtures that do not exist. No mutation of connectors, mounts, workspaces, jobs, or health was performed by any probe |

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
is consistent with the held-out model scores (trajectory 2–3 of 8) and is most
plausibly attributable to the model route rather than the feature; it should be
re-run on the intended release model before being treated as a feature defect.

### 3. `completeness` stays `complete` when a layer is `unknown`

`memory.recall` returned `deployment=unknown`
(`deployment_observation_unavailable`) while the envelope reported
`completeness=complete`. Under a literal reading of §9.6 ("`completeness=partial`
or `truncated=true` must be stated"), the unknown layer is not reflected in the
envelope. Evidence suggests `completeness` intentionally describes the
*capability set* rather than per-layer resolution: a request for a non-visible
ID did return `completeness=partial` with `capability_not_visible`. Flagged for
owner adjudication; not counted as a false claim, because the affected layer
itself was correctly and visibly `unknown`.

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

Two independent reasons, either of which alone keeps M2 open under the
runbook's rules:

- The mixed-deployment cell, the live email matrix, and the changed-state
  before-action cell had no available fixtures or seams.
- The three-repeat held-out model matrix could not be run on the intended
  release route, and on the only reachable route it fails the release gate
  decisively (0–1 of 8 passing across three runs).

Separately, State C recorded a genuine FAIL against the exact-arguments
criterion (finding #2).

## Rollout recommendation

**Keep defaults off.** The deployment has been left in State E with both
controls `false`, verified in the ConfigMap and in newly started workloads.

What this run does establish, and what should carry forward: the endpoint's
admission, bounds, privacy, and read-only behavior are sound; the rollout
controls behave correctly in all four flag combinations exercised; the
dependency-failure and rollback paths degrade honestly without leaking raw
errors or letting a stale snapshot survive; and across 24 model trajectories
there were **zero critical forbidden claims**. The feature's safety envelope
looks correct. What remains unproven is the live email surface, the
mixed-deployment reporting path, and model routing quality on the intended
release model.
