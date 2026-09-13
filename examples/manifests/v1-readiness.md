# First-release readiness

The implemented resource API is `srw/v1alpha1`. The candidate first release
contains the core below; this is not a stable `srw/v1` compatibility declaration.
The [manifest guide](README.md) describes the contract and executable examples.

| Area | Implemented core | Current boundary |
| --- | --- | --- |
| Resources | Five authored kinds, JSON/YAML, references and inline definitions, validation, resolution, preview, export, versioned apply and immutable execution snapshots | Generic private configuration uses JSON semantics. The shipped SRW adapter alone interprets its legacy configuration language. |
| Experts | Installation-managed SRW harness and ordinary image hosting with optional integration hooks | Generic hosting requires an enabled, verified installation. The local K3s startup network-policy check failed; the separate Cilium profile passed. |
| Workspaces | Independent Job/Session selection, compatible custom VM images and resource sizes, initialization, retained Job disks, scoped preparation and caching | Preparation requires same-cluster KubeVirt/CDI and compatible VM images. Cache artifacts are local CDI disks. Existing Sessions retain their own lifecycle; cross-execution Session `instanceRef` selection is separate work. |
| Connectors | Named or inline external-resource configuration, authorization and credential references; explicit environment/file delivery for the native image host | Connectors are broader than MCP. A runtime must support the requested delivery protocol; an arbitrary protocol name does not install an implementation. |
| Projects | Resource collections, defaults, existing Officer configuration and atomic activation of a resolved revision | Automatic team commissioning, package reconciliation and a Project-wide concurrency ceiling remain unsupported. |
| Tags and labels | Common metadata on all five kinds, including version-checked metadata edits on admitted Jobs | Metadata does not grant permissions, select resources or trigger execution. A Job metadata edit preserves its admitted specification, dependencies and execution identity. |
| Clients | Canonical API, thin resource CLI and MCP operations, including workspace-cache management | The broader operational CLI and distributable team packages remain extensions. |

## Current candidate

The preparation candidate through `dd060d9c3` includes initialization, retained
disks and cache increments, the six Helm changes from `develop` at `1b40ea300`, and the endpoint
inventory correction. It also corrects integration defects found while testing
the real MCP-to-harness path:

- Orchestrator preparation admission consumes the same Helm capability settings
  as the controller.
- Starting the harness preserves initialized files and existing working trees.
  An existing delivery repository must match the requested remote.
- An absent IDE profile no longer blocks readiness on Python 3.11/3.12.
- VM heartbeats store connection telemetry on the VM. They update only an
  existing live VM IDE on the current generation. Migration 0246 repairs only
  the exact authenticated legacy telemetry placeholder; actual runtime cleanup
  still requires its independent retirement evidence.
- Failed or cancelled preparation can retire before VM allocation. The controller
  records source issuance before returning a disk and provides signed cancellation
  evidence for work that never received one. Cleanup also verifies runtime absence
  and the current owner generation. Previously issued or uncertain allocations
  retain the existing VM retirement requirements.
- Workspace cleanup reuses its held physical mutation lock during capture.
  Reconciliation, finalizer release and direct deletion no longer wait on a
  second connection trying to acquire that same lock. Public entry points
  retain their existing serialization and identity checks.
- Updating Job tags, labels or annotations keeps the captured execution and
  cannot silently resolve newer dependencies or replay completed work.

These changes are committed locally on `feat/workspace-preparation-cache`,
including the repeatable acceptance harness at `a91e56b19`. They have not been
pushed or rolled out to main dev. Concurrent changes in
another checkout require their own integration review.

The 2026-09-13 `develop` push at `a116c4682` contains the completion-workflow
refactor. It does not yet contain this manifest candidate. Its
[CI/CD run](https://github.com/Knaeckebrothero/Superhuman-Remote-Worker/actions/runs/34764758725)
passed, including 30,755 backend tests with 180 skips, image builds and chart
publication. Policy and migration workflows passed too. Both fresh-cluster
[application E2E profiles](https://github.com/Knaeckebrothero/Superhuman-Remote-Worker/actions/runs/34764758792)
passed on their first browser attempt with exact resource cleanup and cluster
teardown. Main dev has installed chart `0.0.998`, app version `sha-a116c46`.
All 15 deployments have their current replicas ready, including all six workers
on the published agent image. Cockpit, API and MCP health probes return HTTP 200.
The [publication evidence](verification/develop-publication-2026-09-13.json)
records source/image identities and the temporary readiness delay during image
pulls. No CI fix or manual deployment patch was needed.

This separate publication does not establish deployment of workspace preparation.
Integrating the two branches and verifying the combined revision remain required.

## Acceptance status — 2026-09-13

The coherent candidate is deployed on local `k3d-srw`. The service-level
[preparation gate](workspace-preparation-k3d-evidence.json) passed with real VMs,
retained disks, cache policies, cancellation and cleanup. Its separate Cilium
network gate passed; that result does not certify the ordinary K3s profile.

The complete [MCP/harness preparation gate](verification/k3d-prepared-srw-mcp-2026-09-13.json)
passed through `ee8d0eb67`: cold preparation, a fresh cache hit,
retained allocation and handoff, failed build, running-builder cancellation and
owned resource cleanup. The gate uses a deterministic model fixture with real MCP
admission, the installed SRW harness and SSH tools. All four successful Jobs
required actual guest shell output before completion. See
[repeatable local acceptance](workspace-preparation.md#repeatable-local-acceptance).

The complete [ordinary Job/Session smoke](verification/k3d-srw-adapter-2026-09-13.json)
passed through `dd060d9c3`, including sandbox/virtual Jobs, the existing Job API,
Session configuration updates and End/Resume, and one unchanged Session Expert
on sandbox, virtual and no workspace. All seven workloads, fixture registrations
and temporary credentials retired successfully. Two Session deletions needed
explicit 503 continuations with exact identity readback; no transport-ambiguous
mutation was replayed. The earlier failed invocation was recovered separately.

The final Python 3.12 regression through `dd060d9c3` passed **30,951 tests** with
179 skips in 37:38, using `PYTHONSAFEPATH=1` and four bounded workers. All 2,728
recorded inputs remained unchanged. The 433 focused provisioning/PostgreSQL
checks also pass. The [acceptance summary](verification/v1-stabilization-2026-09-13.json)
records exact revisions, deployment checks and verification limits.

All 3,137 Cockpit tests, translation checks and the production build pass. Both
Helm lint profiles pass. The 276 focused retirement/provisioning/PostgreSQL checks,
265 controller/auth checks and 24 final retirement/helper checks pass. Ruff lint
and formatting pass across all source and tests. The full application migration
chain replays from an empty database. A readable local database backup was taken
before migration 0246.

## Release decisions still required

Integrate the final candidate with the then-current `develop`, run CI and deploy
matching chart, orchestrator, controller and builder artifacts. Enable preparation
on main dev deliberately and repeat the actual developer workflow there. Verify
startup network enforcement before enabling online preparation.

Keep `srw/v1alpha1` until supported backend behavior, migration/rollback handling
and version compatibility guarantees have been reviewed. Changing a version
string does not establish those guarantees. Automatic team reconciliation, a
complete operational CLI, OCI/S3 distribution and additional adapters remain on
the broader roadmap; this candidate does not claim those features are complete.
