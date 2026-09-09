# SRW resource manifests (v1alpha1)

Read [resources.yaml](resources.yaml), [referenced-job.yaml](referenced-job.yaml),
then [project.yaml](project.yaml) for the basic composition.
[inline-job.yaml](inline-job.yaml) describes an ordinary image with no SRW hooks;
[retained-job.yaml](retained-job.yaml) selects an existing workspace instance. Images, credentials,
scopes and instance IDs in these examples are illustrative.

The first implementation supports validation, bundle resolution, preview and
portable export. Execution, stored manifest lookup, credential delivery and project
activation follow in later slices. Existing job/session creation remains available
through its current API.

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
(for example, an existing PAT/OIDC Bearer credential). These routes grant no resource
access and do not start assignments:

| Method | Path | Body/result |
|---|---|---|
| GET | `/api/manifests/schema` | Packaged authored-resource JSON Schema |
| POST | `/api/manifests/validate` | `{source, format}` → structure/alias validation |
| POST | `/api/manifests/preview` | `{source, format, default_scope?}` → resolved bundle and provenance |
| POST | `/api/manifests/export` | `{source, format, default_scope?, output_format}` → `{format, source}` |

`source` is the JSON/YAML text. `format` and `output_format` accept `yaml` or `json`.
`default_scope` is `{kind: "Account", name: "personal"}`, for example. Defaults to
YAML when format is omitted. Field names in the HTTP request envelope use the
existing Python API convention; fields inside resources follow the manifest schema.

Preview returns `documents` (authored, with explicit scope), `resolved` (effective
configuration), `dependencies` (bundle content revisions), and `defaults` (project
default provenance). `admissionReady` is always false in this slice; `pendingChecks`
lists live checks not performed. Scope names are declarations, not proof of access.
Secret values, images, backend capabilities and retained instances are never fetched.
Do not use a preview as an apply/admission token.

Local `sha256:` revisions identify resolved definition content in this bundle; they
are not stored resource versions. A requested revision must match that content.
Export preserves named references and project ownership choices, rather than turning
external definitions into owned inline resources. It can export a valid declaration
whose external references are not present locally. Use preview to check references.

Private `runtime.config` and connector `config` use ordinary JSON semantics, including
literal nulls. They are preserved, not passed through SRW's legacy tool/model loader.
Keep credentials in secret references: opaque text cannot be reliably inspected for
embedded secrets. Operations echo only supplied authored configuration and never look
up or inject secret values.

## Legacy migration seam

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

## Live k3d check

With the current checkout already deployed through Helm/Tilt and the local mkcert
CA trusted, run:

```bash
.venv/bin/python scripts/manifests-k3d-gate.py
```

The gate checks that the manifest implementation in the ready `k3d-srw` pod matches
the checkout, then exercises the real HTTPS ingress and Keycloak login. It covers
all four endpoints, missing/invalid authentication, all seven examples, project
defaults, private configuration, JSON/YAML round trips and validation errors.
It prints only test results and deployment identity. It uses the documented local
`test` account; `SRW_K3D_TEST_USER` and `SRW_K3D_TEST_PASSWORD` select another
already-provisioned test identity. It does not create or execute resource manifests.
