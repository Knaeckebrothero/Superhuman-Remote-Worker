# Prepared VM workspaces

The SRW adapter can prepare a reusable VM disk before allocating a workspace.
Jobs and Sessions select the same `WorkspaceTemplate` through a reference or
inline configuration. Applying a template stores its recipe; preparation starts
when an execution needs it. See [the prepared development template](srw-prepared-development-vm.yaml).

`environment.prepare` runs ordered argv commands as root **inside a disposable
libguestfs appliance**. Use it for system packages and shared toolchains.
`initialize` runs as `agent-host` inside each allocated VM. Use it for work
directories and per-workspace setup. SRW waits for initialization before
dispatching work into the workspace. Shell expansion requires an explicit `sh -c`
command.

For example, on the Ubuntu development image with online preparation enabled:

```yaml
environment:
  image: YOUR_PINNED_SRW_VM_IMAGE
  cache: Reuse
  prepare:
    - command:
        - sh
        - -c
        - apt-get update && apt-get install -y --no-install-recommends g++ cmake ninja-build
```

The next Job using the same recipe clones the completed toolchain. Repository
checkout and execution credentials still belong to the Job and its Connectors.

A prepared artifact is immutable. Each fresh workspace receives its own writable
clone, machine identity and SSH credentials. Account and Project caches are
separate. Initialization and ordinary working files never flow back into the
cache. `retention: Retain` and `instanceRef` preserve one working disk across Jobs;
they do not turn that disk into a template. Reusing a retained instance works
with new preparation disabled and does not rerun successful initialization.

## Policies

| Setting | Behavior |
| --- | --- |
| `pullPolicy: IfNotPresent` | Reuse the scope's resolved base digest while its cached disk exists; otherwise resolve/import it. |
| `pullPolicy: Always` | Resolve the base reference for every new execution allocation. An unchanged digest can still reuse a prepared artifact. |
| `pullPolicy: Never` | Use a ready matching artifact or an imported base in this scope. Otherwise fail with `ImageNotCached` without pulling the base image. A new recipe may still prepare a clone of a cached base. |
| `cache: Reuse` | Share one completed preparation for matching immutable inputs. Concurrent callers share the build. |
| `cache: Rebuild` | Build once per Job allocation or Session runtime generation. Polls and transport retries keep that allocation; another execution gets a new artifact. |

The key includes the scope, resolved base and builder digests, builder protocol,
ordered commands, architecture, preparation disk size and operator network policy
revision. CPU and RAM affect the allocated workspace, so they do not invalidate
its prepared contents. Changing a template affects new executions; existing
executions retain their captured recipe. Updating a package repository without
changing those inputs needs `Rebuild` or explicit cache eviction.

For compatibility, prebuilt templates with no `prepare` and the default
`IfNotPresent`/`Reuse` keep the existing golden-disk path. Add `prepare: []` to opt
into digest resolution and these cache policies without extra commands.

## Operator setup

This backend requires same-cluster KubeVirt/CDI, persistent VM rootdisks, lifecycle
HMAC authentication and an amd64 SRW-compatible VM image. Build/publish the
`docker/Dockerfile.vm-preparer` image, or use the image published by this release's
CI. Deploy the chart, orchestrator and controller from the same revision before
enabling preparation. The default is disabled.

```yaml
vm:
  mode: same-cluster
  lifecycleAuthSecretName: srw-vm-lifecycle-auth # Existing Secret with a VM_LIFECYCLE_HMAC_SECRET key.
vmController:
  persistentRootdisk:
    enabled: true
  preparation:
    enabled: true
    diskSize: 30Gi
    maxConcurrent: 2
    maxCacheEntries: 128
    timeoutSeconds: 3600
    importTimeoutSeconds: 2700
    cacheTtlSeconds: 604800
    network:
      enabled: false
```

`diskSize` is the preparation/cache disk capacity, independent of an execution's
larger writable disk. It must fit the base image and installed software.
Preparation does not boot cloud-init; the base guest filesystem must have enough
free space for its install commands. Increasing PVC capacity alone does not promise offline
partition/filesystem growth for arbitrary custom images. The
workspace's `resources.storage` must be at least this size; omission defaults to
this size. Imports, cloning and preparation have bounded waits. Their overall
wait budget is three import windows plus one build window and five minutes for
handoff; polling never resets it or spends ordinary boot retries.

Builds receive no service-account token, host socket, host devices, workspace SSH
key or Connector credentials. Networking is off by default. Package downloads
require `network.enabled: true` and `network.enforcementVerified: true` after the
operator verifies CNI enforcement from the first application packet, including
new Pod startup. Merely having a NetworkPolicy object is insufficient; the local
k3d K3s profile allowed startup traffic before enforcing the policy.
`scripts/workspace-preparation-network-gate.py` exercises the boundary in its own
disposable namespace. Run it with an explicit kubeconfig, context and reachable
vm-preparer image. The generated policy permits cluster DNS and
public HTTP/HTTPS, excluding private and link-local ranges. Explicit
`network.additionalEgress` rules can authorize internal package mirrors. This
setting is installation-owned and applies only to preparation Pods.

Public OCI base images are resolved only through `registryHosts` and the separate
`tokenHosts` allowlist. Private base-registry authentication and preparation-time
Connector secrets are not implemented. `global.imagePullSecrets` applies to the
trusted builder image. `insecureRegistryHosts` is an explicit development-only
opt-in for registries such as a local k3d registry.

For a private builder image, pin its digest and allow its registry host; the
controller does not resolve private tags with image-pull credentials. For Tilt or
a local k3d registry, allow the injected builder reference's host and configure
`insecureRegistryHosts` when it serves HTTP. The registry address must resolve
and be reachable from the controller and CDI Pods. A node-only registry mirror
or a Docker-network hostname alone does not provide that Pod DNS route.

## Cache operations and failures

Using the existing manifest CLI authentication (`SRW_TOKEN`):

```bash
python -m orchestrator.operator_cli.manifest_resources cache list
python -m orchestrator.operator_cli.manifest_resources cache list --scope-kind Project --scope-name PROJECT_UUID
python -m orchestrator.operator_cli.manifest_resources cache delete ARTIFACT_UUID
```

The API equivalents are `GET /api/workspace-cache` and
`DELETE /api/workspace-cache/{artifact_id}`, with `scope_kind` and `scope_name`
query parameters. Reads use scope visibility; deletion requires scope write
permission. Deletion addresses an immutable artifact UUID and returns a conflict
while the artifact is building, referenced by a pending allocation, or still used
by CDI. Eviction does not delete already cloned workspaces.

The TTL collects unused prepared disks and base imports. Zero disables TTL
collection. `maxCacheEntries` bounds the number of base and prepared entries;
cache hits remain available at capacity, but new entries fail with
`CacheCapacityExceeded` until space is available. These shared cache PVCs count
as platform storage overhead; allocated workspace disks retain their normal
execution/instance attribution.

Build output is available through the preparation Pod logs while it exists.
A cluster log collector can retain that output after Pod retirement; raw logs
are not copied into manifests or API responses.

Failed commands never publish an artifact or start a VM. Cancellation waits for
the builder's terminal state before removing its disk. Controller restarts recover
durable build records and never substitute a new writer for a missing one.
An unaccounted-for or replaced builder is quarantined as `Lost`; its disk cannot
be reused automatically. Inspect its exact Pod/PVC identities and establish that
the writer has stopped before operator recovery. A timeout alone is insufficient.

Sessions persist preparation separately from VM identity. Ending a Session while
it is building closes admission and cancels its allocation; resuming uses the new
runtime generation. Physical VM retirement keeps the existing VM/PVC identity
checks. Disabling preparation rejects new builds and retains controller cleanup
permissions and NetworkPolicy so existing allocations can be cancelled and collected.

The implemented cache is a CDI PVC in the controller's namespace. OCI/S3 artifact
export, cross-cluster distribution, generic-harness VM providers and sandbox image
builders are separate capabilities. This feature does not enable those backends.
