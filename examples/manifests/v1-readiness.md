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

## Integrated implementation

Integration merge `c0c443e03` combines the workspace-preparation candidate through
`bdd5968df` with published `develop` at `a116c4682`, including its completion-workflow
refactor. The merge preserves the published completion services and their import
boundaries. It was built and tested in a separate worktree while development
continued in the primary checkout; unpublished work there is outside this revision.

The implementation includes initialization, retained disks, prepared-workspace
caching and the earlier Helm integration. Testing the real MCP-to-harness path
also produced these corrections:

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

The [integration record](verification/develop-integration-2026-09-13.json) identifies
the combined source and its new local acceptance results. The earlier
[publication record](verification/develop-publication-2026-09-13.json) describes
the separate `a116c4682` rollout at chart `0.0.998`; it is historical evidence for
that parent revision.

## Acceptance status — 2026-09-13

The combined revision `c0c443e03` is deployed on local `k3d-srw` through Tilt CI.
Deployed source checks match the merged checkout, including the completion
services. A readable application database backup was captured before Helm apply.

The new [ordinary Job/Session smoke](verification/k3d-integrated-srw-adapter-2026-09-13.json)
passed: sandbox/virtual Jobs, existing Job API workspace selection, frozen Session
configuration, next-turn changes, End/Resume, and the same Session Expert on
sandbox, virtual and no workspace. All seven workloads and owned fixture
registrations retired successfully. Two Session deletions completed through
explicit 503 continuations with exact identity readback.

The new [MCP/harness preparation gate](verification/k3d-integrated-prepared-srw-2026-09-13.json)
passed all six cases: cold preparation, a cache hit on an independent writable
disk, retained allocation, handoff to a new Job/VM on the same disk, failed build
and running-builder cancellation. All four successful Jobs required actual guest
shell output before completion. The gate uses a deterministic model fixture with
real MCP admission, the installed SRW harness and SSH tools. Owned workloads,
retained disks, prepared artifacts and temporary credentials were cleaned up.
The scoped base import remains under the installed cache TTL. See
[repeatable local acceptance](workspace-preparation.md#repeatable-local-acceptance).

The combined Python 3.12.14 regression passed **30,974 tests**, with 179 skips
and 175 warnings, in 36:28. It used `PYTHONSAFEPATH=1`, four bounded workers and
no fail-fast flag. All 3,573 tracked inputs remained unchanged throughout the run.
Ruff lint and formatting cover 2,001 files; all 23 import contracts, the 512-entry
endpoint inventory, 105 runtime-coordinate classifications and both Helm lint
profiles pass. Dependency and canonical-import checks pass in the isolated
environment.

The Cockpit tree is unchanged from the previously accepted candidate, whose
3,137 tests, translations and production build passed. That frontend result is
reused for this identical tree. The earlier
[stabilization record](verification/v1-stabilization-2026-09-13.json) also records
migration replay and focused controller/retirement checks. The service-level
[preparation gate](workspace-preparation-k3d-evidence.json) includes additional
cache-policy checks and a separate Cilium network test. Its network result does
not certify the ordinary K3s profile or online guest package installation.

After the integrated gates, the shared golden DataVolume and PVC retained their
original identities, and the unrelated scratch database remained running. The
primary checkout's Tilt watcher remains paused; local k3d retains the integrated
deployment.

## Release decisions still required

Develop publication uses CI-built component images and a versioned Helm chart;
[GitHub Actions](https://github.com/Knaeckebrothero/Superhuman-Remote-Worker/actions?query=branch%3Adevelop)
records those publication checks. Preparation remains disabled by default.
Enable it deliberately on main dev and repeat the actual developer workflow
there with matching orchestrator, controller and builder artifacts. Verify
startup network enforcement before enabling online preparation.

Keep `srw/v1alpha1` until supported backend behavior, migration/rollback handling
and version compatibility guarantees have been reviewed. Changing a version
string does not establish those guarantees. Automatic team reconciliation, a
complete operational CLI, OCI/S3 distribution and additional adapters remain on
the broader roadmap; this candidate does not claim those features are complete.
