# App Guide M2 verification record

> **Status (2026-07-27): incomplete.** M2a–M2d are closed. M2e's deterministic
> guide, evaluator, and changed-state action work is implemented and green, but
> the model and deployed live gates have not passed. Endpoint/tool defaults
> remain off and M2 must not be called complete.

> **Testing handoff (2026-07-28):** the source was pushed and a redeployment was
> reported. This is not yet live evidence. Independent acceptance is defined in
> [App Guide M2 live acceptance — tester-agent handoff](app_guide_m2_live_acceptance_handoff.md).

> **Live acceptance attempted (2026-07-28): verdict BLOCKED.** See [App Guide M2
> live acceptance results 2026-07-28](app_guide_m2_live_acceptance_results_2026-07-28.md).
> The rollout-control, endpoint-admission, dependency-failure, and rollback
> gates passed on a local k3d target. The live email matrix, changed-state
> before-action cell, mixed-deployment cell, and MCP-token admission rows had no
> available fixtures, and the three-repeat model matrix could not run on the
> intended release route. The status above therefore stands: M2 is not complete
> and both defaults remain off.

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

A one-case smoke attempt used the configured local OpenAI-compatible route
without printing or persisting its credential or endpoint. The provider
rejected authentication, so the result is **not** model evidence. The required
full eight-case matrix, repeated three times, remains open.

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

## Deployment and rollout state

On 2026-07-28 the source push and a redeployment were reported. The repository
experimental overlay now declares image `sha-5eb436e` and full source revision
`5eb436eb9181b3271aef223e89c8d87861d95b4c`, which contains the M2e
implementation. The current local `k3d-srw` API is not reachable, so this
record does not promote the report or overlay declaration to observed live
evidence. The tester must verify the real target through the linked handoff.

The earlier local `srw` k3d attempt had a stopped (`0/1` server) target, so no
then-current worktree image was deployed for M2e. The following remain
unverified:

One controlled start attempt was restored to the stopped state without
deploying changes. k3d reported its server container running, but the
server-load-balancer container could not reach the API upstream (`Host is
unreachable`) and `kubectl` timed out. This is infrastructure unavailability,
not live M2e evidence.

- controlled endpoint dark launch and privacy-safe fixture comparison;
- tool canary on fresh and resumed None, Virtual, and Container sessions;
- email denied, unattached, read, draft, send-off, send-on, degraded, partial,
  and detach/downgrade cells against real session binding;
- a real deployment-disabled Protected Cloud or shared-browser cell;
- a staggered mixed-component deployment;
- endpoint-off/tool-on and tool-off rollback behavior; and
- the complete three-repeat model matrix with zero critical false positives.

`PRODUCT_CAPABILITIES_ENDPOINT_ENABLED` and
`PRODUCT_CAPABILITIES_TOOL_ENABLED` therefore remain default `false` in
environment examples, Compose, and Helm. Explicit false is the rollback path.
No English/German UI copy changed because the deterministic summary is
model-facing tool context, not a Cockpit or end-user reason-code component.

## Exit decision

M2e and M2 remain **open**. Default-on rollout is authorized only after:

1. the source-derived M2 closure union, Ruff, Helm, and diff gates pass;
2. all 24 model trajectories (eight cases × three repetitions) pass with zero
   critical false-positive capability/action claims;
3. the controlled fresh/resumed live matrix passes; and
4. disabling the capability dependency leaves the M1 guide available while
   dynamic claims become explicitly unknown.
