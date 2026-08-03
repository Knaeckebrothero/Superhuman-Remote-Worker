# App Guide M2 verification record

> **Status (owner-reconciled 2026-08-03): release incomplete.** M2a–M2d are
> closed. M2e's implementation and offline verification are complete, but its
> release acceptance has not passed. Endpoint/tool defaults remain off and M2
> must not be called complete.
>
> **Live acceptance attempted 2026-07-28: verdict BLOCKED.** See the
> [sanitized results](app_guide_m2_live_acceptance_results_2026-07-28.md) and
> [re-run handoff](app_guide_m2_live_acceptance_handoff.md). The exercised flag
> transitions, cookie-authenticated endpoint/bounds subset, and the fresh
> dependency-failure/rollback paths produced positive evidence; their required
> resumed repetitions were not recorded. State B remained blocked on MCP-token
> and sentinel rows; State C failed the
> exact-one-call criterion on the fallback model; fresh/resumed coverage was
> not run; and the email/action, controlled-partial, mixed-deployment,
> sentinel-privacy, and intended-release-model gates remained blocked.

This record separates offline contract evidence, model behavior, and deployed
runtime evidence. A green deterministic fixture is not a substitute for a
real authenticated endpoint, persistent session, or operation.

## Source and scope

The M2e work started from repository revision `03a8399b` (completed M2d).
The scoped M2e changes:

- integrate `get_product_capabilities` into the managed App Guide only for
  current deployment/user/session/tool/workspace/attachment claims;
- preserve guide-only stable workflows and a guide-only rollback path;
- add a separate held-out eight-case M2 trajectory corpus and production-model
  capability fixtures;
- reject guide-topic IDs passed into the capability tool before a fetch;
- prevent already-bound email tools from acting through a connection removed
  by live detach/rebind; and
- update the implementation and live-settings documentation.

Unrelated dirty-worktree files and nested projects are outside this record.

## Deterministic behavior now proven

| Boundary | Evidence |
|---|---|
| Stable versus dynamic routing | The skill and content contracts require `read_product_guide` first, zero capability calls for stable how-to answers, and an exact-ID lookup for current-state answers. |
| Capability near miss | A definition-only support-versus-current-state question reads the focused guide but must make zero live capability calls and no current-session claim. |
| Identifier safety | `CapabilityToolRequest` rejects the guide topic `datasources-email`; no server fetch occurs. Focused queries copy `datasources.email` / `datasources.email.send` from guide frontmatter. |
| Layer honesty | The skill preserves build, deployment, user, session, and `agent_action`; partial, truncated, unknown, degraded, attachment, upgrade, and mixed-build values do not collapse into unsupported/disabled/denied. |
| Snapshot boundary | `can_execute` remains advisory. The held-out action scorer requires guide round `<` capability round `<` operation round and rejects authority/snapshot arguments on the operation. |
| Real changed state | A real production capability output first reports email send `can_execute`; live detach then invalidates the already-bound production `email_send` closure, which refuses before `open_smtp`. Tier and unattended-send are also rechecked after binding and immediately before SMTP submission. |
| Rollback | With the capability tool absent, the managed guide still explains stable email attachment and must report current attachment/readiness as unknown. |
| Privacy | Capability fixtures use only production public models. Tool traces retain safe logical arguments, status, size, and hashes—not raw results, mailbox identity, folders, credentials, endpoints, or message content. Prompts and answers contain only versioned synthetic data such as `person@example.test`. |

The changed-state claim is deliberately narrow. Live detach/rebind and changes
to the current bound connection are enforced. An out-of-band
`email_autonomous_send` grant revocation is re-read during orchestrator
datasource resolution/rebind; `email_send` does not query the orchestrator on
every SMTP call. M2e does not claim instant operation-time revocation beyond
the tested boundary.

## Held-out model matrix

`eval/app_guide/capability_cases.yaml` is separate from the balanced 30-case
M1 routing corpus:

| Case | Required trajectory |
|---|---|
| Stable email folder setup | guide only |
| Capability terminology near miss | guide only; no live lookup or current-state claim |
| Current send ready | guide → exact send capability |
| Current send denied | guide → exact send capability; user layer remains distinct |
| Partial/unknown | guide → exact send capability; partial and unknown preserved |
| Mixed build | guide → exact send capability; component revision uncertainty named |
| Changed before action | guide → exact send capability → `email_send`; refusal reported |
| Capability rollback | guide remains; no capability call; current state unknown |

All fixtures are constructed and serialized through
`ProductCapabilitiesToolOutput`; the action result is a synthetic model
fixture backed by the separate real-operation regression.

Offline validation passes for both corpora:

```text
routing:    30 cases (17 positive / 13 negative)
capability: 8 cases across stable, near-miss, dynamic, failure, action, and rollback
```

An early one-case smoke attempt used the configured local OpenAI-compatible
route without printing or persisting its credential or endpoint. The provider
rejected authentication, so that attempt supplied no model evidence. During
the later live run, the complete eight-case corpus was repeated three times on
the only reachable route, `gemma-4-moe`:

```text
run 1: 1/8 passed; trajectory 2; grounding 2; strict order 5
run 2: 0/8 passed; trajectory 3; grounding 1; strict order 6
run 3: 1/8 passed; trajectory 3; grounding 2; strict order 5
all runs: 0 critical forbidden claims; 0 provider errors
```

These are diagnostic failures. They do not satisfy the release gate because
the intended release route was unavailable. The required three-repeat matrix
on that route remains open.

## Focused verification

The following results pass on the M2e worktree:

```text
183 passed
  tests/test_app_guide_capability_eval_harness.py
  tests/test_app_guide_eval_harness.py
  tests/test_app_guide_content.py
  tests/test_product_capabilities_tool.py
  tests/test_live_datasource_update.py
  tests/test_email_tools.py
```

Repository-wide Ruff lint (including `eval/app_guide`), scoped Ruff format,
both Helm lint profiles, and `git diff --check` pass. The repository-wide
format check reports only the unrelated, pre-existing
`tests/test_atomic_job_context.py`. The generic skill-creator validator rejects
SRW's existing project-specific `display_name`, `icon`, `color`, and `tags`
frontmatter extensions; repository bundled-skill loading and content tests are
the applicable parser authority and pass in the closure union.

The current post-review planned M2 closure union also passes:

```text
1280 passed, 12 warnings
```

Before the final near-miss and mixed-build acceptance hardening, the larger
source-derived closure union passed 1778 tests with the same 12 warnings. The
post-review delta is covered by both the 183-test focused run and the current
1280-test planned union. The warnings are existing FastAPI/Starlette
deprecations, AsyncMock resource-warning cases, and duplicate OpenAPI
operation-ID warnings; no M2e assertion failed.

## Deployment and rollout evidence

The 2026-07-28 acceptance run targeted a local single-node k3d/Tilt deployment
built from source commit `ec4bbe6b`, which contains minimum implementation
commit `326963b7`. The locally built images did not declare component source
revisions or artifact digests, so candidate identity was established only from
the worktree/build path and the presence of the feature modules. This is
useful local-canary evidence, not immutable deployed provenance and not the
mixed-build test.

The run exercised the four distinct flag combinations and restored State E:

| Evidence area | Reconciled outcome |
|---|---|
| State A default-off | PASS |
| Cookie-authenticated endpoint, thread ownership, filters, bounds, and no-store behavior | PASS for exercised rows |
| State B as a complete gate | BLOCKED: both MCP-token rows and the sentinel privacy protocol lacked fixtures |
| State C tool canary | FAIL on fallback route: one extra broad capability call preceded the required exact-ID call |
| Fresh/resumed None, Virtual, and Container sessions | NOT RUN |
| Live email tiers and changed-before-action sink test | BLOCKED: no connectors, server, or sink |
| Controlled partial/unknown and mixed declared provenance | BLOCKED: no approved seams |
| Three-repeat intended-release-model matrix | BLOCKED: route unavailable; fallback diagnostics failed 0–1/8 |
| State D dependency failure and State E rollback | BLOCKED as complete gates: exercised fresh paths and final false/false state passed, but the required resumed repetitions were not recorded |
| Privacy | BLOCKED: no leak was observed, but the required sentinel fixture was unavailable |

The naturally occurring `memory.recall deployment=unknown` observation is not
the controlled partial cell. Envelope completeness describes whether the
requested visible set was returned without evaluation errors or truncation;
it may remain `complete` when an individual layer legitimately resolves to
`unknown`.

Before this run, a push and redeployment of experimental revision
`5eb436eb9181b3271aef223e89c8d87861d95b4c` (`sha-5eb436e`) was reported. The
acceptance run did not target that deployment, so the report remains historical
context rather than App Guide live evidence. An even earlier controlled local
start attempt also supplied no evidence: the k3d load balancer could not reach
the API upstream (`Host is unreachable`), and the stopped state was restored
without deploying changes.

`deployment/values-experimental.yaml` is CI-updated and may now contain newer,
independently advanced component revisions. A future tester must inspect the
actual reconciled workloads and capability response rather than copying any
historical tag from this record. That independently versioned overlay may be a
candidate seam for the mixed-build test only when an operator deliberately
controls and verifies the stagger.

`PRODUCT_CAPABILITIES_ENDPOINT_ENABLED` and
`PRODUCT_CAPABILITIES_TOOL_ENABLED` remain default `false` in environment
examples, Compose, and Helm. Explicit false is the rollback path. No
English/German UI copy changed because the deterministic summary is
model-facing tool context, not a Cockpit or end-user reason-code component.

## Exit decision

M2e and M2 remain **open**. Default-on rollout is authorized only after:

1. a clean candidate reruns the source-derived M2 closure union, Ruff, Helm,
   and diff gates;
2. project- and user-scoped MCP admission plus the complete sentinel privacy
   protocol pass;
3. State C emits exactly one focused capability-ID call and all 24 intended-
   release-model trajectories pass with zero critical false-positive
   capability/action claims;
4. fresh and resumed None, Virtual, and Container sessions pass;
5. the complete email tier/degraded/detach matrix and the real
   changed-before-action sink test pass without SMTP submission;
6. controlled partial/unknown, real deployment-disabled, and staggered mixed-
   provenance cells pass; and
7. dependency failure and full rollback, including resumed behavior, leave the
   M1 guide available while dynamic claims become explicitly unknown.

The first run's positive subset remains useful regression context but cannot
be combined with a later candidate to manufacture one passing release run.
This 2026-08-03 reconciliation updates documentation only; it did not rerun the
offline suite or a deployment.
