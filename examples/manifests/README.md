# SRW resource manifests (v1alpha1)

Read [resources.yaml](resources.yaml), [referenced-job.yaml](referenced-job.yaml),
then [project.yaml](project.yaml) for the basic composition.
[inline-job.yaml](inline-job.yaml) describes an ordinary image with no SRW hooks;
[retained-job.yaml](retained-job.yaml) selects an existing workspace instance. Images, credentials,
scopes and instance IDs in these examples are illustrative.

[native-process-job.yaml](native-process-job.yaml) and
[native-workspace-job.yaml](native-workspace-job.yaml) are executable examples for
an enabled native Kubernetes host. The latter requires the updated SRW workspace image
and an available StorageClass; its harness runs in a separate container over SSH.

SRW stores, authorizes and resolves versioned resources through one configuration
API. Manifest Jobs and existing UI/MCP requests use immutable execution
configurations. Bundled and stored Experts and Project defaults migrate to this
model; existing editor fields are projections of those resources. The `srw` CLI
and MCP use the same API. Generic image hosting requires the operator capability
described below.

The [packaged JSON Schema](../../src/shared/manifests/schema.json) defines five
authored kinds with `apiVersion`, `kind`, `metadata`, and `spec`. Observed status
and server identity are not authored fields.

| Kind | Defines |
|---|---|
| `Expert` | Harness image, launch options and private configuration |
| `WorkspaceTemplate` | Independent environment recipe, resources and retention |
| `Connector` | External resource configuration and credential references |
| `Project` | Resource collection, selection defaults and team configuration |
| `Job` | Assignment combining expert, workspace and connectors |

Use `ref` to select a named definition or `inline` to supply the same kind's spec
directly. Omitted Job selections inherit project defaults.
Explicit `workspace: null` and `connectors: {}` suppress those defaults. No deep merge is performed on
private settings. The protocol remains `v1alpha1` while migration and execution
semantics are exercised.

For the shipped harness, `runtime: {adapter: srw/v1}` selects the installed SRW
worker pool. Omit `image` so the definition follows installation upgrades; an
explicit image must match that installation at admission. Generic runtimes require
their own image and launch it directly. See [installed SRW Expert](installed-srw-expert.yaml)
and the [configuration guide](../../config/README.md) for existing-definition migration.
New execution snapshots record the selected concrete SRW image; reapplying a Job
after a rollout preserves its existing execution.
The [2026-09-10 k3d verification](verification/k3d-installed-harness-2026-09-10.json)
records migration of all 20 installed Experts, preserved execution history, and
a successful real Job/Session run with this binding.

## Execution-owned workspace selection

[srw-workspace-selection.yaml](srw-workspace-selection.yaml) uses the same
installed SRW Expert with two referenced WorkspaceTemplates and with an explicit
`workspace: null`. An Expert's `workspacePreference` is advisory: the creation UI
can follow it, but the runtime never replaces a Job/Session selection with it.
Project defaults take precedence over this recommendation. The UI previews tool
availability for the chosen tier; the reference harness keeps its existing
backend filtering and authorized upgrade requests.

The existing `POST /api/jobs` and `POST /api/persistent/threads` requests accept
the same binding as a top-level `workspace` field:

```json
{"workspace": {"template": {"ref": {"name": "build-env"}}}}
```

Omission inherits the active Project default, then account/role defaults. Explicit
null selects no workspace. `config_override.workspace.backend` remains an
explicit compatibility input; supplying both forms is rejected. Referenced
workspace revisions are frozen in execution snapshots, including across Session
configuration edits and End/Resume. Unattended Project loops, automations and
Officer dispatch also resolve the Project workspace before connector selection.

The current SRW provisioner accepts backend-only templates with `Delete`
retention. It rejects template resources, custom workspace images, initialization,
preparation, retained instances and `instanceRef` instead of discarding them.
These fields are part of the manifest schema and are supported only where the
selected provisioner implements them. This change does not add a general
auto-upgrade policy or preparation cache.

## Local use

Install with `pip install -e '.[manifests]'` or use the configured orchestrator
environment. From the checkout:

```bash
python -m shared.manifests validate examples/manifests/*.yaml
python -m shared.manifests preview examples/manifests/*.yaml
python -m shared.manifests export examples/manifests/*.yaml --output-format json
```

For resources without explicit metadata scope, provide both `--scope-kind Account`
and `--scope-name personal` (or the intended Project/Catalog scope). Validation
permits omitted scope; preview/export must resolve it. A Project itself requires
Account scope. References resolve within the definition's scope; there is no global
fallback lookup. Supply the referenced resources in the same preview bundle.

JSON accepts one resource object or an array of resource objects. YAML uses separate
documents with `---`. The parser rejects duplicate keys, merge keys, recursive
aliases, custom non-JSON values and ambiguous YAML scalar spellings. Quote strings
such as `yes`, dates and numeric-looking identifiers. Text is limited to 1 MiB per
input; bundles contain at most 100 resources and have bounded nesting/expansion.

## HTTP operations

Use the same approved-user authentication as other public orchestrator operations
(for example, an existing PAT/OIDC Bearer credential). Stored operations enforce
account/project/catalogue authority on the server. Apply can admit and dispatch Jobs.

| Method | Path | Body/result |
|---|---|---|
| GET | `/api/manifests/schema` | Packaged authored-resource JSON Schema |
| POST | `/api/manifests/validate` | `{source, format}` → structure/alias validation |
| POST | `/api/manifests/preview` | `{source, format, default_scope?, resolution?}` → resolved resources and provenance |
| POST | `/api/manifests/export` | `{source, format, default_scope?, output_format}` → `{format, source}` |
| POST | `/api/manifests/apply` | `{source, format, default_scope?, expected_versions?, plan_revision?, idempotency_key?}` → stored/admitted resources |
| GET | `/api/resources` | `scope_kind`, `scope_name`, optional `kind` → authorized resource list |
| GET | `/api/resources/{uid}` | Authored resource, UID, `resourceVersion`, revision and available observed status |
| DELETE | `/api/resources/{uid}` | Required `expected_version` query parameter; ownership and active dependencies checked |
| POST | `/api/resources/{uid}/outcome` | `{attempt, outcome: "Succeeded" | "Failed"}` for generic Manual/Reported Jobs |
| PUT | `/api/resource-secrets/{name}` | `{scope?, values, expected_version?}`; encrypted storage, version-only response |
| GET | `/api/workspace-instances/{uid}` | Workspace generation, initialization, retention and ownership status |
| DELETE | `/api/workspace-instances/{uid}` | Required `expected_generation`; deletes only an exclusively released, fenced instance |

`source` is the JSON/YAML text. `format` and `output_format` accept `yaml` or `json`.
`default_scope` is `{kind: "Account", name: "personal"}`, for example. Defaults to
YAML when format is omitted. Field names in the HTTP request envelope use the
existing Python API convention; fields inside resources follow the manifest schema.

Bundle preview returns `documents` (authored, with explicit scope), `resolved` (effective
configuration), `dependencies` (bundle content revisions), and `defaults` (project
default provenance). `admissionReady` is always false for bundle preview; `pendingChecks`
lists live checks not performed. Scope names are declarations, not proof of access.
Secret values, images, backend capabilities and retained instances are never fetched.
Bundle preview cannot supply an apply precondition. Use `resolution: "stored"` for
authorized live-reference resolution and a `planRevision` that may be supplied as
`plan_revision` to apply. It detects changed inputs/dependencies; apply still performs
execution admission. HTTP preview defaults to bundle resolution; the CLI and MCP
default to stored resolution.

Local `sha256:` revisions identify resolved definition content in this bundle; they
are not stored resource versions. A requested revision must match that content.
Export preserves named references and project ownership choices, rather than turning
external definitions into owned inline resources. It can export a valid declaration
whose external references are not present locally. Use preview to check references.

Private `runtime.config` and connector `config` use ordinary JSON semantics, including
literal nulls. They are preserved, not passed through SRW's legacy tool/model loader.
Keep credentials in secret references: opaque text cannot be reliably inspected for
embedded secrets. Validation, previews, resource reads and exports do not return
materialized credential values. Execution delivery resolves authorized secret
references separately from authored configuration.

## Native CLI and MCP

Install the command with `pip install -e '.[cli]'`. It also runs as
`python -m orchestrator.operator_cli.manifest_resources` in the configured environment.
Set `SRW_API_URL` to the public API and provide an approved user's PAT/OIDC bearer
credential through `SRW_TOKEN`. `--token-stdin` reads a token from one stdin line;
it cannot be combined with `-f -`. TLS uses the system trust store, with `--ca-file`
for an explicit PEM trust bundle. CLI credentials never inherit internal MCP authority.

```bash
export SRW_API_URL=https://api.localhost
srw validate -f examples/manifests/inline-job.yaml
srw preview -f examples/manifests/inline-job.yaml > plan.json
srw apply -f examples/manifests/inline-job.yaml --plan plan.json --idempotency-key zero-example-1
srw get --kind Job --scope-kind Account --scope-name me
srw get RESOURCE_UUID
srw export RESOURCE_UUID -o yaml
srw delete RESOURCE_UUID --expected-version 1
```

Repeat `-f` to combine YAML/JSON files. Supply the same files and scope options to
preview and apply when using `--plan`. Updating an existing resource also requires
its observed version, for example
`--expected-version 'Expert/Account/ACCOUNT_UUID/custom=3'`, or
`--expected-versions versions.json` containing that JSON map. Use canonical scopes
from the stored resource response. The CLI never retries a mutation automatically;
after an unknown outcome, inspect current resources and reuse an idempotency key
only with the identical apply request. Resource deletion can be blocked by active
work or project ownership.

The MCP exposes `manifest_validate`, `manifest_preview`, `manifest_apply`,
`manifest_list`, `manifest_get`, `manifest_export`, and `manifest_delete` through
the same HTTP client methods. Its existing authenticated invocation scope and
capability annotations apply. Apply is marked as a mutation that can start external
work; delete is marked destructive. No secret-write CLI/MCP operation is introduced.

## Legacy migration seam

New and existing editor/API requests now converge on canonical resources and
execution snapshots. Startup imports stored Experts and Projects transactionally,
keeps their UUIDs/grants/default pointers, then clears obsolete Expert payloads
and Project default fields. The old editor fields are projections of the canonical
resource, not a second configuration store. Bundled Expert files are authored
manifests; their model/prompt/tool language remains private to `runtime.adapter:
srw/v1`. Generic images never use that adapter.

The SRW private `config_name` selects its configuration base; `asset_name` selects
installed prompt, matrix and skill assets independently. For example, Developer
uses `config_name: worker_base` with `asset_name: developer`. Its authored leaf
comes from the canonical manifest, so removing a setting cannot restore a value
from the bundled file. Named roster references also resolve through the canonical
catalogue when the server prepares an execution.

Job settings freeze at insertion. Sessions record immutable configuration
generations; an already admitted recipient retains its generation. Active Project
definitions freeze complete dependency content, so editing a source Expert does
not change an operating team's configuration until the Project is updated.
The source recipe retained by the SRW editor is server-owned provenance.

Historical execution import is explicit and repeatable:

```bash
python -m orchestrator.operator_cli.manifest_execution_migration --limit 100
python -m orchestrator.operator_cli.manifest_execution_migration --limit 100 --apply
```

Dry run is the default. Only saved resolved payloads are copied; the command never
recreates a paused execution from today's defaults. Rows without a frozen payload
are reported as `requires-resolution` and retain their historical read path.
This does not resume, dispatch, or change the outcome of historical work.
For a historical session without a saved snapshot, its next authorized attachment
records a new canonical generation before delivery. This records the new
attachment's settings, not an assertion about settings used in earlier turns.

The control-plane rollout uses `Recreate` through
`orchestrator.manifestContractCutoverEnabled`. Take the normal database backup
before upgrading. Rolling back to an older orchestrator that reads the cleared
legacy payloads requires restoring that backup. Compatible SRW worker images may
coexist during rollout; `srw/v1` selects the installation-managed adapter, whose
existing recipient attestation remains in force.

The following comparison helper remains a test/migration diagnostic:

`orchestrator.services.manifest_legacy.preview_legacy_job` translates a bounded
pre-dispatch case using the existing `JobCreate` and `resolve_config` contracts.
The caller supplies the existing policy filter and already-selected workspace and
connector definitions. It supports `description`, `config_name`, and
`config_override`; other explicitly supplied job fields fail until mapped.

The resulting Expert contains the credential-redacted serialized SRW configuration
as its private payload, including prompts and instructions. Job completion is
explicitly `Reported`. This helper neither authorizes resource access nor enables
config-file consumption by the existing harness. It is an internal migration/test
seam, not another public job creation route.

## Execution policy and current host capabilities

Generic admission is **disabled by default** until the operator verifies isolation
from the first container instruction and sets
`manifestHosting.networkIsolationVerified: true`. Helm creates a dedicated
`<release>-native` namespace with a persistent default-deny baseline and scoped
runtime RBAC. Creating a NetworkPolicy alone is insufficient evidence.

The current local k3d profile (`k3s v1.31.5+k3s1`) failed the startup gate: 5 of 12
fresh pods connected to a forbidden test destination, and those connections
survived after enforcement caught up. Its native admission must remain disabled;
the SRW reference adapter remains available. This matches the documented
[kube-router startup limitation](https://github.com/cloudnativelabs/kube-router/blob/master/docs/troubleshoot.md#networkpolicy-not-enforced-on-freshly-launched-pods).
The strict gate and functional workspace tests are separate; passing SSH/PVC
checks does not waive the network requirement. A suitable CNI installation/profile
is an operator prerequisite, not an option an authored image can override.

Generic image integration is optional. ProcessExit observes the actual container
exit; Manual waits for an authorized external decision even after process exit;
Reported requires a report before its finite deadline. An outcome report identifies
the current attempt and never skips process fencing. Images receive no implicit
API token or control-plane network access. An external caller can report through
the ordinary approved-user API; a Reported Job without a report visibly times out.

Job status includes its stable execution identity, latest attempt and workspace UID.
Failed attempts reuse the same workspace instance and PVC after both harness and
workspace processes have terminated. Successful initialization runs once per
instance. Each attachment receives fresh SSH keys and pinned host identity; no key
material is saved in an authored resource or execution snapshot. Retries/handoffs
use observed pullable image digests. If the runtime cannot supply such a digest,
automatic image replacement is refused. A pod disappearing without terminal
evidence keeps ownership fenced for investigation.

Retain releases a fenced workspace for a later Job's `instanceRef`. Delete removes
it at final completion. Manual review preserves its data and exclusive ownership
until the decision. Explicit workspace deletion checks the observed generation.
Project deletion requires owner authority and refuses live dependencies, resumable
sessions or retained workspace instances.

The initial native host supports sandbox images, user-level initialization, and
explicit `srw.env/v1` / `srw.files/v1` connector delivery. A connector value is a
literal string or `{credential: "declared-name"}`; credentials are declared via
SecretRefs. File destinations live under `/run/srw/bindings/`. Env/file credentials
must carry their own external access restrictions; this host rejects an
unenforceable `ReadOnly`/`ReadWrite` declaration. Runtime requests are capped at
8 CPU / 16 GiB per container and 100 GiB per workspace. Native concurrency is
separately bounded by `MANIFEST_MAX_CONCURRENT_JOBS` (default 10).

Generic harness provider access is an installation policy:
`manifestHosting.harnessEgress` contains Kubernetes NetworkPolicy egress rules and
defaults to `[]`. An attached workspace adds its SSH route; it does not route model
SDK calls automatically. Operators can authorize public CIDRs or private model
gateways, together with DNS when hostnames are used. Credentials still require
explicit env/file bindings. For example, a private gateway and cluster DNS:

```yaml
manifestHosting:
  harnessEgress:
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: model-serving
          podSelector:
            matchLabels:
              app: model-gateway
      ports:
        - protocol: TCP
          port: 8080
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: kube-system
          podSelector:
            matchLabels:
              k8s-app: kube-dns
      ports:
        - protocol: UDP
          port: 53
        - protocol: TCP
          port: 53
```

Helm serializes this setting into `MANIFEST_HARNESS_EGRESS` and rolls the
orchestrator when it changes. These rules apply to every generic harness on that
installation; manifests cannot enlarge them through private settings or connector
content. New attempts, including retries, read the current installed policy.
Policies of active attempts remain immutable; cancel those attempts to revoke
access immediately. The workspace's own network policy is unchanged.

Unknown fields, invalid CIDRs, selector expressions, ports, or malformed JSON make
generic admission return `503 HostingConfigurationInvalid` before workload effects.
Queued attempts also refuse to launch. The reference SRW adapter and runtime
cleanup remain available. Egress rules retain [Kubernetes
semantics](https://kubernetes.io/docs/reference/kubernetes-api/networking/network-policy-v1/#networkpolicyegressrule):
omitted/empty destinations or ports are unrestricted for that dimension, and an
explicit `{}` rule allows all egress. CIDR exclusions must be strictly contained
subnets. Configuring egress does not enable generic hosting or waive the separate
startup-isolation verification requirement.

Workspace preparation/cache builds, authored network profiles and native VM/virtual
hosting currently fail admission explicitly. The reference SRW adapter keeps its
existing workspace provisioner (simple backend selection, Reported completion,
one attempt). Custom initialized/retained recipes use generic hosting. Existing
Officer kit/policy updates publish an atomic Project revision; automatic team
commissioning and new generic team controllers remain future capabilities.

## Live k3d check

With the current checkout already deployed through Helm/Tilt and the local mkcert
CA trusted, run:

```bash
.venv/bin/python scripts/manifests-k3d-gate.py
```

The gate checks that the manifest implementation in the ready `k3d-srw` pod matches
the checkout, then exercises the real HTTPS ingress and Keycloak login. It covers
all four endpoints, missing/invalid authentication, all current examples, project
defaults, private configuration, JSON/YAML round trips and validation errors.
It prints only test results and deployment identity. It uses the documented local
`test` account; `SRW_K3D_TEST_USER` and `SRW_K3D_TEST_PASSWORD` select another
already-provisioned test identity. It does not create or execute resource manifests.

## Generic image execution contract

The Kubernetes runtime admits and observes ordinary images through the resource
API and persists execution/attempt state in PostgreSQL. The Cilium service gate
below exercises that complete path with real pods and retained storage. Runtime
configuration never passes through the SRW reference harness loader.

The Kubernetes backend follows these launch rules:

| `command` | `args` | Process launch |
|---|---|---|
| Omitted | Omitted | Image ENTRYPOINT and CMD |
| Omitted | Nonempty array | Image ENTRYPOINT with supplied arguments |
| Array | Omitted or array | Supplied command, followed only by supplied arguments |
| Omitted | Empty array | Rejected at execution admission; clearing CMD requires an explicit command |

These rules follow [containerd's Kubernetes argument handling](https://github.com/containerd/containerd/blob/main/internal/cri/opts/spec_opts.go).
The portable schema permits the last shape, but this backend cannot honor it
without first resolving image entrypoint metadata. SRW adds no shell wrapper.

When supplied, opaque config, task and binding descriptors are mounted read-only
at `/run/srw/config.json`, `/run/srw/task.json` and `/run/srw/bindings.json`, with
`SRW_CONFIG_FILE`, `SRW_TASK_FILE` and `SRW_BINDINGS_FILE` respectively. Images may
ignore all three. Materialized credential/file delivery uses an immutable Secret
belonging to one attempt; the current payload limit is 512 KiB. Only requested and
authorized bindings are delivered. A workspace descriptor names an independent SSH
environment; its storage is never mounted into the harness container.

Generic pods inherit no SRW ConfigMap/Secret environment or service-account token.
They have no restart loop, drop Linux capabilities, disable privilege escalation,
and apply CPU, memory and ephemeral-storage limits. Their NetworkPolicy allows
only explicitly granted egress. Kubernetes policies are additive, so deployment
verification must establish that other policies do not grant unintended access
and that the installed CNI enforces the rules.

Pod running state, optional readiness, exit code and resolved image ID are separate
observations. Cancellation deletes only the observed pod UID. A finalizer preserves
terminal evidence until the orchestrator records the outcome and requests cleanup;
`Failed` without terminated-container evidence does not prove processes stopped.
Retries also require independent workspace process fencing. The whole-Job deadline
includes queueing and previous attempts; it is never restarted when a pod launches.

The sandbox workspace runtime uses one PVC per workspace UID, with a fresh SSH pod
and fresh client/host keys for each attachment generation. A retry or later Job can
reuse that exact PVC after the previous pod is fenced. Missing retained storage is
an error; it is never replaced by an empty volume. `Retain` and `instanceRef` require
the caller's resource authorization and exclusive attachment record.

Custom sandbox images implement the workspace SSH protocol (deriving from the SRW
workspace image supplies it):

- Persist the `agent-host` user's home at `/home/agent-host` (UID 1000), with the work
  directory at `/home/agent-host/workspace`.
- Run SSH on port 30022 and install the attachment's public key from
  `/tmp/ssh-pubkey/ssh-publickey`. No shared worker key or gateway CA is supplied.
- Install the attachment's pinned Ed25519 host key from `/tmp/ssh-hostkey` into
  root-owned runtime storage. The updated SRW workspace entrypoint implements this.
- Support `/bin/sh`, `install` and `su` when `initialize` commands are used. Those
  commands execute as `agent-host` in the persistent work directory.

The orchestrator marks initialization complete after every initializer exits zero,
then omits them on later attachments. Partial failures may repeat earlier steps, so
initializers must tolerate repetition. This follows [Kubernetes init-container
sequencing](https://kubernetes.io/docs/concepts/workloads/pods/init-containers/).
Image preparation/cache builds and custom VM providers remain execution capability
gates. System packages belong in the workspace image; initialization can populate
repositories, files and user-space tool installations in the retained home.

The harness receives a workspace descriptor, its scoped private key and pinned
`known_hosts`. SDKs can consume these directly. An optional
`/run/srw-workspace/ssh` helper supports OpenSSH images with root or nonroot users by
copying the read-only key into a temporary file with mode 0600, enforcing strict host
verification, and deleting the copy afterward. Each image decides whether to use
this helper. Component tests and the native runtime gate below have different
coverage from production API and admission tests.

### Disposable native runtime k3d gate

With this checkout installed in the active Python environment, an existing local
`k3d-srw` cluster and `srw-registry` on `localhost:5005`:

```bash
docker build -f docker/Dockerfile.workspace -t srw-manifest-workspace:gate .
python scripts/manifests-native-k3d-gate.py
```

The script verifies the local cluster and registry, checks the workspace image's
entrypoint against this checkout, and publishes test images by the registry's
verified content digest. It creates a random `srw-native-gate-*` namespace and
removes its pods, PVCs and credential Secrets afterward. Cached test images remain
in the local registry. It does not deploy or write to the installed SRW namespace.

The checks use unmodified BusyBox/Python images and an upstream Git image with an
SSH client. They cover image defaults, opaque JSON, absence of ambient credentials,
CNI isolation, actual SSH, initialization as the workspace user, retained data
across failed attempts and later executions, fresh SSH keys, pinned host identity,
and cancellation/storage deletion with exact pod/PVC UIDs. A retained file written
by an orphan workspace process must stop changing before reuse. The output records
the image digests actually observed by Kubernetes; it never prints key material.

A temporary SQLite identity ledger supplies durable sequencing for this adapter
gate. It does not exercise production HTTP authentication, grants, resource apply,
Postgres admission or the production execution reconciler; their integration gates
remain separate. Cleanup retains the process fence if a node cannot provide
terminal evidence and reports the disposable namespace requiring recovery.

Cold-start isolation can be checked independently with:

```bash
python scripts/manifests-native-k3d-gate.py --network-startup-samples 12
```

**Current local isolation gate: failed.** On 2026-09-09, k3s
`v1.31.5+k3s1` allowed forbidden traffic from 5 of 12 newly created probes despite a
preexisting namespace-wide deny policy. A prior witness established that the
baseline was already effective, and positive controls reached the same listener
before and after. All five established connections still exchanged data after
eight seconds while new connections were denied. The disposable namespace was
removed. [Recorded probe evidence](verification/k3d-native-netpol-2026-09-09.json)
contains only test identities and measurements.

Creating a policy before a pod, a successful steady-state probe, and a fixed sleep
do not establish isolation from the first packet. This installation needs a
validated CNI capability that blocks unauthorized traffic before workload code
starts. Kubernetes describes the required [pod lifecycle behavior and lack of a
policy-applied acknowledgment](https://kubernetes.io/docs/concepts/services-networking/network-policies/#pod-lifecycle).
Kube-router documents the [startup race and a newer node-level default-deny
option](https://github.com/cloudnativelabs/kube-router/blob/master/docs/troubleshoot.md#networkpolicy-not-enforced-on-freshly-launched-pods).
That option is absent from the kube-router version embedded in this local k3s
release; it cannot be enabled by an SRW namespace policy. Functional workspace
checks do not override this failed security acceptance result.

The independent function checks passed on that installation:

```bash
python scripts/manifests-native-k3d-gate.py --functional-only
```

Their [recorded result](verification/k3d-native-functional-2026-09-09.json) is
explicitly marked `passed-functional-only` and `coldStartIsolationAccepted: false`.

For a separate test cluster with a supported isolation profile:

```bash
python scripts/manifests-cilium-k3d-gate.py
```

This wrapper creates a uniquely named k3d cluster with a separate kubeconfig and
localhost API port, installs pinned Cilium 1.18.13 with
`policyEnforcementMode: always`, and runs the isolation, adapter, and production
service gates. It disables Flannel and the
embedded network-policy controller in that new cluster. The cluster, temporary
credentials, and extra registry network attachment are removed afterward; the
default kubeconfig is checked for changes. It does not install SRW or change the
existing cluster's CNI. Cilium's [always enforcement
mode](https://github.com/cilium/cilium/blob/v1.18.13/Documentation/security/policy/intro.rst)
applies policy to endpoints before any resource-specific rule selects them.

On 2026-09-09 this profile passed all nine native runtime checks and denied all
12 cold-start probes, with no surviving forbidden connections. The
[Cilium evidence](verification/k3d-native-cilium-2026-09-09.json) records the pinned
chart digest, observed images, installation values, individual probe measurements,
and cleanup checks. Coverage is one IPv4 node and the Kubernetes adapter boundary;
production authentication, admission, Postgres state, and reconciliation remain
separate integration gates. These measurements do not certify every deployment
of a CNI provider or enable the capability on another installation.

To run the startup isolation probes and production service integration without
repeating the standalone adapter suite:

```bash
python scripts/manifests-cilium-k3d-gate.py --service-only
```

The service gate creates a disposable PostgreSQL container with the complete
application schema and an approved test Account. It calls the production manifest
admission and execution/workspace services against real Kubernetes adapters. It
checks foreign Account rejection, an image's default command, immutable reapply,
opaque configuration and explicit env/file connector delivery, then retained
workspace initialization, retry and cross-job reuse. It reconstructs the services
while each execution is running, and verifies PostgreSQL attempt identities and
workspace generations before releasing the PVC and removing its namespace and
database container. It does not install the SRW stack or exercise HTTP middleware
on this disposable cluster.

The [2026-09-09 service evidence](verification/k3d-native-service-2026-09-09.json)
records four admitted Jobs, five process attempts and three workspace generations
on the same PVC, together with 12 passing startup isolation probes. The gate
found and verified a fix for file connectors being rejected by the runtime after
admission selected their required `/run/srw/bindings/` mount directory. Other
platform delivery paths and ambiguous or overlapping file mounts remain rejected.
All test Pods, the retained PVC, namespace, PostgreSQL container, temporary cluster
and kubeconfig were removed; registry network attachments and the default
kubeconfig were restored or left unchanged.

The subsequent [provider egress gate](verification/k3d-native-provider-egress-2026-09-09.json)
passed seven Jobs and eight attempts against schema 0236. A real generic harness
made authenticated provider-style HTTP requests through an operator-approved route
using its env connector. An unselected live provider and Kubernetes API stayed
blocked before its first provider call; positive controls before and after checked
the denied provider's availability. The startup isolation and retained-workspace
checks also passed, and both provider fixtures plus all other test resources were
removed. The report preserves the live source hashes and separately identifies a
later validation-only change rejecting trailing newlines in labels and port names,
covered by 71 focused tests. It does not claim external model inference or enable
native hosting on the existing k3d installation.

## Deployed HTTP cutover gate

After deploying this checkout coherently to the existing local SRW installation:

```bash
python scripts/manifests-cutover-k3d-gate.py > /tmp/srw-manifest-cutover-gate.json
```

This script checks the explicit `k3d-srw` context, a local Kubernetes API, one
ready orchestrator, and matching deployed source hashes before authenticating or
writing. It uses verified TLS through `https://api.localhost` and the documented
local Keycloak identity. `SRW_K3D_TEST_USER` and `SRW_K3D_TEST_PASSWORD` can select
another local test identity; credentials stay in memory and are not recorded.

The gate creates uniquely named Account Experts and a disposable Project without
a team controller. It checks stored preview, list/get/export, idempotency, version
conflicts, the existing SRW expert editor's canonical read/write projection,
atomic Project activation failure, child removal, active defaults, and retirement.
It uses the Project list and canonical resource get; the legacy Project detail
GET can schedule cloud repair and is only checked after deletion for its 404.

On the current unverified cluster, generic hosting must remain disabled. A valid
generic Job bundle must return `HostingCapabilityUnavailable` with HTTP 503.
Read-only, aggregate PostgreSQL checks verify that the rejected bundle leaves no
resource, operation, Job, execution, attempt or workspace binding records behind;
native Kubernetes object identities must also remain unchanged. This gate does
not admit jobs, exercise sessions, or prove process reconciliation. Those rollout
checks and the independent Cilium adapter gate cover different boundaries.

Cleanup discovers uncertain creates by exact owned names and annotations, reads
current versions, and retires only those resources through the API. Historical
revisions and operation receipts remain as normal audit data. Mutations are not
automatically replayed after transport errors. Any failed cleanup is reported in
the JSON evidence with its unique gate identity. HTTP bodies, Secret contents,
tokens, and database connection strings are never included in that evidence.

The script passed against local Helm release 335 on 2026-09-09, with 62 deployed
source files matching the checkout and complete cleanup. The authenticated
configuration gate also passed all nine examples. See the
[recorded cutover evidence](verification/k3d-manifest-cutover-2026-09-09.json).
Nine local tests additionally cover the script, including a Project flow through
the real manifest/project routes and full PostgreSQL schema.

## Deterministic SRW adapter smoke

After the coherent orchestrator **and stateless agent** rollout:

```bash
python scripts/manifests-srw-k3d-smoke.py > /tmp/srw-manifest-srw-smoke.json
```

The script verifies the explicit local context and deployed source hashes, builds
the existing deterministic E2E provider with a unique model ID, and publishes its
verified digest to the local registry. It creates a disposable namespace, a
temporary Keycloak administrator and a run-owned OAuth client through the
documented local bootstrap setup, and one endpoint/model registration. The
client includes a subject mapper and role scopes; its access token is verified
by the normal administrator API. The ordinary local test identity owns the
work. Existing accounts, endpoints and configured/effective model defaults are
not edited. Bootstrap credentials can be provided through
`SRW_K3D_BOOTSTRAP_USER` and `SRW_K3D_BOOTSTRAP_PASSWORD`; tokens and fixture keys
stay in memory and are excluded from evidence.

The smoke admits a native `srw/v1` Job with an independent sandbox workspace and
checks reported completion and reapply without replay. Its authored private
settings disable auxiliary work, memory and instruction gates, and select only
the four in-workspace/core tools needed by the deterministic phase driver. It
then creates a stateless Session, opens the real owner SSE stream, and checks
three replies across a source Expert edit, a next-turn temperature PATCH, and
End/Resume. Read-only PostgreSQL evidence compares snapshot identity, generation,
model and hashes of captured prompts/instructions/skills. This covers stateless
reattachment; it does not force another pool pod or exercise the pinned Session
WebSocket configuration protocol.

Cleanup fences and removes owned work through the normal lifecycle APIs, retires
its manifests, deletes only its endpoint/model and application administrator,
then disables, logs out and removes the exact Keycloak identity and OAuth client.
Access is revoked in the final cleanup even when workload cleanup fails. The public API
protects default Projects from deletion. For this test-owned account only, a
captured Project UUID, canonical resource UID, creation time and exclusive owner
receipt permit the existing database deletion helper after user removal. The
helper refuses outside membership, work, repositories and resources. Normal
cloud/Gitea profile records may outlive application user deletion; this residue
is reported, and unrelated profiles are never removed. A failed workload cleanup
retains its fixture resources and reports the exact run identities.

A final SSE reply can precede claim drain. Cleanup therefore retries an explicit
HTTP 503 for at most two minutes, checking the exact workload UUID, owner and
title before each attempt and confirming absence afterward. Transport ambiguity
and other HTTP errors stop the gate. Evidence records cleanup stages and status
codes without response bodies.

The complete live smoke passed on `k3d-srw`, Helm revision 335, with schema
0237 and all 62 orchestrator source paths verified. The Session's permanent
cleanup returned 503 once, then 200 after exact identity readback, followed by
GET 404. Job cleanup also confirmed GET 404. Provider/model defaults stayed
unchanged, and all fixture workloads, definitions, registrations, namespace,
application/default Project, Keycloak identity and OAuth client were removed.
A separately authorized inspection removed the exact empty Gitea profile and
found no Nextcloud account matching the fixture email. Canonical Job/Session
snapshots and both Session revisions remain after workload deletion.
See the [accepted SRW smoke evidence](verification/k3d-srw-adapter-2026-09-09.json)
and its [unmodified invocation](verification/k3d-srw-adapter-invocation-2026-09-09.json).

Earlier failed runs exposed historical workspace cleanup and replay gaps.
Migration 0236 records namespace and resource names with new UID captures;
permanent deletion requires a settled terminal receipt for each preserved
runtime. Migration 0237 permits current projection replay across only fully
settled historical terminal receipts under the same owner generation and queue
token, with exact process-zero proof. The successful live run retained all three
receipts: original preserve, current terminal reclaim, and historical terminal
reclaim, each with captured location evidence. Older captures without location
evidence and changed namespaces remain refused; independently verified operator
recovery is required. The earlier test fixtures were recovered and removed, and
their failed artifacts remain separate from the accepted invocation. Cached
fixture image layers remain in the local registry.
