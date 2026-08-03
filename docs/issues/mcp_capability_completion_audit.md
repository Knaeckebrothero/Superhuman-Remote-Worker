# MCP Capability and Reliability Audit

**Status:** Source remediation implemented; disposable live acceptance pending
**Date:** 2026-08-03
**Scope:** The orchestrator-facing MCP server in `orchestrator/mcp/`, its REST
client, deployment packaging, tool contracts, and validation coverage.

## Executive summary

The MCP server is broad—it currently registers 104 tools. At the start of this
audit it was not a reliable or authoritative representation of the
orchestrator API. The source/image remediation below closes the identified P0
transport risks and establishes an authoritative contract, while the remaining
workflow and disposable live-acceptance gaps still prevent a completion claim.

The Scholar incident did **not** show that MCP job creation bypasses the regular
job API. Both `create_job` and `create_project_job` ultimately use the normal
orchestrator create-job path. The failure was caused by the surrounding MCP
contract: its success response instructed the caller to perform a redundant
manual assignment, and the deployed tool schema was stale relative to source.

The wider audit found two urgent cross-cutting risks that are now remediated in
source and the production image:

1. request authentication is stored in mutable headers on a shared asynchronous
   client, so concurrent requests and health probes can overwrite one another's
   identity; and
2. the client applies timeout retries uniformly, including to non-idempotent
   mutations, so a mutation can succeed server-side and then be repeated or
   reported as failed.

The broad live mutation exercise remains gated on a disposable environment and
the acceptance controls recorded below.

## Remediation update — 2026-08-03

The source and production-image remediation is implemented. No external
deployment or shared-infrastructure mutation test was performed.

Completed:

- Request identity is now task-local and copied into each outgoing HTTP request
  by an `httpx.Auth` flow. The shared client's default headers contain no user,
  scope, or internal-key state. Concurrent delayed calls from two differently
  scoped users and a simultaneous unauthenticated health probe are covered.
- Safe GET retries are separate from mutations. Every mutation is sent exactly
  once. A read/write/response-stream failure after send raises an explicit
  `MutationOutcomeUnknown`, and MCP action formatting no longer labels that
  condition as a confirmed failure. Connect failures remain distinguishable.
- `orchestrator/mcp/capabilities.py` is the authoritative 104-tool manifest.
  Registration fails without a contract entry and publishes read-only,
  destructive, idempotent, and open-world annotations plus REST operation,
  authorization, side-effect, retry, schema-source, and coverage metadata.
- Missing REST workflows now have explicit `required`,
  `intentionally_excluded`, or `superseded` decisions. The human contract is in
  `docs/mcp_capability_contract.md`.
- Schema revision 3 exposes a canonical SHA-256 `tools/list` digest, count,
  baked-artifact match status, source/release/image provenance, Python version,
  and FastMCP version through `/health`. A configured stale/missing schema
  artifact makes readiness degraded.
- The production Dockerfile now uses Python 3.12 and exact MCP dependency
  versions, bakes `/app/tool-schema.json`, and ships a disposable real-protocol
  smoke test. Two fresh stdio clients must produce the baked raw `tools/list`;
  the test then invokes read, mutation, denied, and degraded-service paths.
- Both Python test workflows now install `orchestrator/mcp/requirements.txt`
  alongside the root, orchestrator, and development requirements. This keeps
  raw-schema tests on the same pinned FastMCP/MCP runtime as the shipped image.
- The production-like Fleet values resolve MCP by immutable image digest.
  Helm can require that digest, stamps image/source identity into the pod
  template, and injects bounded provenance. Develop CI records the pushed build
  digest in `values-experimental.yaml`, so an MCP artifact change changes the
  pod template and rolls the deployment.
- Priority schema drift is reconciled: `paused` is a job-list status; job and
  project-job creation expose database expert selection, kickoff message,
  context, priority, and deliverables; project create/update expose default
  config overrides; connector tools expose all 12 canonical types plus config,
  publication, and read-only fields with corrected ownership/publication text.
  Job creation still uses normal automatic dispatch; `assign_job` remains an
  administrator recovery/override action.

Current evidence:

- `tests/test_mcp_client_safety.py`: 6 passed (identity isolation, health-probe
  isolation, delayed/ambiguous mutation behavior, connect-failure distinction).
- `tests/test_mcp_client_contracts.py`: 6 passed (job forwarding and email, MCP,
  kubeconfig, KB, publication/config connector bodies).
- `tests/test_mcp_capabilities.py`: 6 passed (104-name equality, metadata and
  annotations, canonical digest, health provenance/artifact match, priority
  schemas, and missing-workflow decisions).
- `tests/test_mcp.py` plus `tests/test_mcp_dispatch_contract.py`: 64 passed.
- Supporting connector API/setup, scope, naming, and registry suites: 76 passed;
  the directly relevant remediation selection totals 158 passing tests.
- A clean CI-equivalent Python 3.12.13 environment resolved the four workflow
  requirement sets, including `langchain-mcp-adapters==0.1.14`, FastMCP 3.4.4,
  and MCP SDK 1.29.0. `uv pip check` found all 254 installed packages
  compatible; the MCP wildcard suite passed 171 tests, and the complete Python
  gate passed 12,644 tests with 28 explicitly environment-gated skips.
- Both documented Helm lint commands passed; the Fleet render uses
  `repository@sha256:...` and includes MCP source/image provenance.
- `docker/Dockerfile.mcp` built successfully. Its in-image smoke result was
  `status=ok`, `tool_count=104`, `fresh_connections=2`, and
  `mutation_calls=1` using Python 3.12, FastMCP 3.4.4, HTTPX 0.28.1, PyJWT
  2.13.0, Tenacity 9.1.4, and MCP SDK 1.29.0.

Remaining before MCP can be called complete:

- Implement the workflows classified `required`: job accept/reject, job export,
  persistent input/interrupt and approvals, connector eligibility, and
  connector indexing status/recovery.
- Finish lower-priority schema reconciliation beyond the job/project/connector
  fields addressed here, including an explicit decision for remaining upload
  and infrastructure-specific request fields.
- Fix the table-query non-page-aligned offset defect and richer job-list
  predicates noted below.
- Run the full live acceptance matrix in a disposable namespace with real OAuth
  callers, independent state verification, optional-service degradation, and
  reversible fixtures. The image smoke uses stdio plus a stub; production HTTP
  auth/ingress and client cache rediscovery have not been exercised live.
- Expand representative protocol invocation into behavior coverage for every
  supported tool. The manifest/schema test covers every registration, but not
  every endpoint's success/error semantics.
- Deploy the changed digest-pinned chart through the authorized release process
  and capture live `/health` plus raw `tools/list`. This session did not deploy
  externally.

## What the MCP does—and does not do

The MCP server is an HTTP client of the orchestrator. It does not connect to job
workspaces with SSH.

The job execution chain is:

```text
MCP tool
  -> orchestrator REST job API
  -> dispatcher provisions or restores an isolated workspace
  -> orchestrator supplies workspace connection data to a worker
  -> worker agent uses SSH/SFTP to operate in that workspace
```

Consequently, the observed `No authentication methods available` exception was
a worker-to-workspace resume failure, not MCP-to-orchestrator communication.

## Confirmed findings

### P0 — Shared mutable authentication headers

The MCP server reuses a global `AsyncCockpitClient`. Per-request identity and
scope headers are installed by mutating that client's default headers.

This creates several hazards:

- concurrent tool calls can replace or clear another request's identity;
- a retry can run under headers installed by a different request;
- periodic health checks can mutate the same client while user calls run; and
- failures can be intermittent and appear to be authorization or tenant-scope
  problems rather than client-state corruption.

Required correction:

- make authentication/request headers immutable and local to each request;
- do not mutate a process-global client's default headers;
- ensure health probes use a separate unauthenticated client or isolated
  request headers; and
- add a concurrent two-user test with different scopes and delayed responses.

### P0 — Unsafe retries of non-idempotent mutations

The REST client uses the same timeout retry policy for reads and mutations.
A timed-out `POST`, `PATCH`, or `DELETE` can have committed on the server even
though the client did not receive the response. Retrying it can duplicate an
action, return a misleading second-call error, or surface failure after success.

An existing issue already records a representative case: a delete committed
after about 30 seconds, the retry received a different authorization result,
and the MCP reported failure despite the resource having been deleted.

Required correction:

- retry safe/idempotent reads separately from mutations;
- do not automatically retry non-idempotent mutations without an idempotency
  key and server-side deduplication contract;
- distinguish connect failures from ambiguous post-send timeouts; and
- test that delayed mutations occur no more than once and report their final
  state truthfully.

### P1 — Tool risk metadata is incomplete

At least 32 tools have mutation-shaped behavior, but only a minority document
themselves consistently as mutations. Inspected registrations do not provide a
complete set of MCP annotations such as `readOnlyHint`, `destructiveHint`, and
`idempotentHint`.

Required correction:

- maintain one authoritative tool manifest containing operation, REST mapping,
  authorization, side effects, idempotency, and destructive classification;
- derive or validate tool descriptions and annotations from that manifest; and
- treat annotations as client guidance, not as an authorization boundary.

### P1 — Deployed schema freshness is not guaranteed

The deployed MCP observed during the Scholar exercise did not expose the
source-defined `required_deliverables` parameter and described workspace-file
retrieval using obsolete local-workspace semantics.

Likely causes include an old pod/image, mutable image tags, or client-side tool
schema caching. The source now reports schema revision/provenance, but that does
not force clients to rediscover tools.

Required correction:

- deploy MCP images by immutable tag or digest;
- include image revision and canonical tool-schema digest in `/health`;
- make the pod template change when the MCP artifact changes;
- compare raw `tools/list` output with the expected CI artifact; and
- explicitly reconnect clients after a schema revision, then verify the schema
  again rather than trusting a previously cached tool list.

### P1 — Connector contracts drift from the REST API

The REST API supports more datasource types and configuration fields than the
MCP tools advertise. The MCP description also implies that an omitted `job_id`
makes a connector globally available, while publication/global visibility is a
separate permission-controlled behavior.

The MCP cannot currently express all supported connector settings, including
several email, knowledge-base, MCP, and credential-file configurations.

Required correction:

- align connector enums and fields with the REST request models;
- correct ownership/publication descriptions;
- document which sensitive fields are deliberately not exposed; and
- add contract tests generated from representative connector types.

### P1 — Authorization and discoverability are conflated

Several administrator-only tools are visible without clearly identifying their
required role. Examples include agent fleet administration, system information,
database table inspection, and expert/skill reload operations. A normal user
discovers them and only learns about the restriction from a formatted `403`.

Required correction:

- declare the required role/scope in every privileged tool's contract;
- decide whether tools should be filtered at discovery time by capability;
- retain server-side authorization regardless of discovery filtering; and
- test both permitted and denied callers.

### P1 — Job and project schemas are incomplete

Examples found during static mapping:

- `list_jobs` does not expose every supported lifecycle status, including
  `paused`;
- `create_project_job` omits context supported by its client/API path;
- job creation omits preferred database-backed expert selection and some kickoff
  fields; and
- project create/update omits `default_config_override` supported by the API.

Each omission needs an explicit decision: expose it, replace it with a safer
workflow, or record it as intentionally UI/REST-only.

### P2 — REST workflows without an MCP decision

The REST API contains workflow operations with no equivalent MCP capability or
documented exclusion. Areas include:

- direct thread-message retrieval;
- job accept/reject, export, upgrade, IDE, and snapshot operations;
- persistent-session input, interrupt, approval, configuration, tool-group, and
  cloud-diff operations;
- datasource eligibility, indexing status, and reindexing; and
- user expert and skill management.

Completeness should not mean mechanically wrapping every endpoint. Each workflow
should be classified as required, intentionally excluded, or superseded by a
higher-level MCP tool, with the reason and authorization model recorded.

### P1 — Protocol and image-level test coverage is too shallow

Most MCP tests call Python wrappers with a mocked FastMCP layer. They do not
prove that the shipped server initializes, registers, serializes, and invokes
tools correctly over the actual protocol. The dispatch contract test is useful
but static/AST-based and cannot catch runtime registration or dependency drift.

There is also an environment mismatch: CI exercises root Python dependencies,
while the MCP production image uses its own dependency set and Python runtime.
FastMCP dependency bounds are not sufficiently authoritative.

Required correction:

- build the production MCP image in CI;
- start it against a stub or isolated orchestrator;
- perform MCP initialization and raw `tools/list` over the shipped transport;
- assert name, schema, annotation, count, and digest;
- invoke representative read, reversible mutation, denied, and degraded-service
  calls; and
- pin/test the same Python and dependency versions shipped in production.

### P2 — Known individual capability defects

- table-query offsets are converted to pages, so non-page-aligned offsets can
  return the wrong slice; and
- job-list filtering is coarser than the API and lacks some fleet/predicate
  search behavior.

### P2 — Documentation and packaging have diverged

- completed design notes describe a substantially smaller tool inventory than
  the server now registers;
- some feature proposals still describe tools that already exist;
- several MCP Dockerfiles have diverged, while only one is the actual production
  build path;
- local development provenance may be blank; and
- a document named as live MCP connector validation actually tests agents
  consuming external MCP servers, not this orchestrator-facing MCP server.

Required correction:

- designate one production Dockerfile and remove or explicitly mark alternatives;
- generate tool inventory documentation from the authoritative manifest;
- keep operational validation for this MCP separate from external-connector
  validation; and
- make build provenance mandatory in production and meaningful in local tests.

## Recommended implementation order

1. Isolate per-request authentication and split safe read retries from mutation
   behavior.
2. Establish the authoritative tool/risk manifest and expose a canonical schema
   digest plus immutable deployment provenance.
3. Reconcile schemas and descriptions, beginning with job/project and connector
   contracts.
4. Classify missing REST workflows as required, deliberately excluded, or
   replaced by a higher-level tool.
5. Add image-level protocol smoke tests and concurrency/idempotency regression
   tests.
6. Run the destructive live capability matrix only in a disposable environment.

## Live acceptance matrix

After the P0 issues are corrected:

1. Deploy exact immutable MCP and orchestrator revisions into an isolated
   namespace/tenant with a dedicated least-privilege user and disposable
   project.
2. Capture `/health` and raw `tools/list`; compare tool names, input schemas,
   annotations, count, revision, and digest with the CI artifact.
3. Reconnect a real MCP client and repeat the comparison to test cache/schema
   rediscovery behavior.
4. Exercise every read tool against seeded fixtures, including denied scope,
   missing resource, pagination, large output, and degraded optional-service
   cases.
5. Exercise reversible mutations on uniquely prefixed disposable resources and
   confirm before/after state independently through REST, persistence, or Gitea.
6. Gate destructive tools behind an explicit checkpoint and use only disposable
   resources and synthetic agents/approvals.
7. Run concurrent calls as two differently scoped users with injected delays and
   ambiguous timeouts. Prove there is no identity bleed and no duplicate
   mutation.
8. Finish with a Scholar job declaring `required_deliverables`; verify automatic
   dispatch, committed artifact retrieval from the correct project branch, and
   absence of orphaned workspaces or repositories.

## Definition of MCP completion

The MCP is complete when every supported user workflow has an intentional,
tested contract—not when every REST endpoint has been exposed mechanically.

For every tool or deliberately omitted REST workflow, the capability matrix must
record:

- intended caller and required authorization;
- request and response schema;
- REST endpoint or higher-level orchestration mapping;
- side effects, destructiveness, and idempotency;
- retry and timeout behavior;
- tenant/project ownership rules;
- degraded-service behavior;
- protocol-level and live-test evidence; and
- an explicit reason for any exclusion.
