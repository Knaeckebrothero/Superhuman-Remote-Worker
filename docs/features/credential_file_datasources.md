---
tags:
  - data-management
  - credential-management
  - tool-development
  - cloud-infrastructure
---

# Credential File Datasources

Design document for adding credential-file-backed datasource types — initially `kubeconfig`, `ssh_key`, and `generic_file` — so users can attach credentials that need to be materialized as **files on disk** in the agent's workspace pod/VM (rather than injected as environment variables).

> **Status (2026-05-17):** Phase 1 (backend) and Phase 2 (cockpit UI) are **shipped to dev** at `sha-f61113a`. Encryption-at-rest for the `credentials` JSONB column is **shipped** (PR `544254d` + lifespan fix `b1bb254`). Phase 3 (docs cross-references) is partially done — this section plus `repo_datasource.md`'s superseded markers. End-to-end verification against the live cluster: validator + DB round-trip confirmed; agent-side materialization on a real job has not yet been driven manually. See [Implementation roadmap](#implementation-roadmap) for the per-phase status.

## Motivation

The existing datasource model covers three credential surfaces:

- **`generic`** — injects `credentials.env_vars` as process environment variables. Works well for tools that read connection details from env (`PGHOST`, `MONGOSH_URI`, `GH_TOKEN`).
- **`repository`** — git-specific: an SSH key or HTTPS token consumed by the git clone path only.
- **Managed connectors** (`postgresql`, `neo4j`, `mongodb`, `webdav`) — connection objects stay in the orchestrator/agent process and the agent gets typed tools.

None of these fit the common case of **a credential that is itself a file**:

- A **Kubernetes kubeconfig** (Rancher, k3d, a managed cluster) — `kubectl` expects `$KUBECONFIG` to point at a *path*, not contain YAML.
- A **free-standing SSH key** (not tied to a specific repo) — for the agent to run `ssh somehost`, `rsync`, `scp`.
- **Cloud-CLI credential files** — `application_default_credentials.json` (gcloud), `~/.aws/credentials`, `~/.azure/credentials`.
- **Certificate bundles, CA roots, signing keys, license files.**

Stuffing a multi-line YAML kubeconfig into an env var is technically possible but operationally broken — the consuming CLI tool wants a path. It also makes `/proc/self/environ` leakage worse (see [[hardened_container]] — env-var redaction is currently future work).

This document specifies a uniform mechanism for materializing credential files into the agent's filesystem at job start, with type-specific UI affordances so common cases (kubeconfig, SSH key) are one-form uploads.

## Trust model

**Posture 1 — agent-readable.** The credential file lives in the agent's process filesystem with `chmod 0600`. The agent runs as the same Unix user that owns the file (`srw:srw` per [[hardened_container]]) and has unrestricted shell tools, so a sufficiently motivated agent can `cat ~/.kube/config` and read the credential's contents.

This is intentional, and it matches how the existing `repository` datasource handles SSH keys today. The credential is **issued to the agent for use**, not **hidden from the agent**. The product responsibility is on the user to:

- Use credentials scoped appropriately for the job's trust level (read-only kubeconfig, project-scoped Rancher token, deploy key for one repo).
- Not attach a cluster-admin kubeconfig to a job that doesn't need it.
- Treat job logs and snapshots accordingly.

The UI explains this posture at credential-creation time. Stronger postures (wrapper CLIs that hide the file behind a UID barrier, ssh-agent socket forwarding, orchestrator-mediated typed tools) are out of scope for the initial design and listed under [Future work](#future-work).

## Design

### New datasource types

| Type | UI form | Materializes to | Mode | Env var set |
|------|---------|-----------------|------|-------------|
| `kubeconfig` | Paste/upload kubeconfig YAML | `~/.kube/configs/<ds-slug>.yaml` (per ds) + merged `~/.kube/config` (per job) | `0600` | `KUBECONFIG=~/.kube/config` |
| `ssh_key` | Paste/upload private key + optional comment, **or** "Generate ed25519 for me" button (reuses `POST /api/datasources/ssh-keys/generate` at `orchestrator/main.py:8754`) | `~/.ssh/<ds-slug>` + `~/.ssh/<ds-slug>.pub` | `0600` / `0644` | — (ssh discovers keys via `~/.ssh/`) |
| `generic_file` | Filename, target path, contents, optional env var | user-chosen | user-chosen (default `0600`) | optional |

The existing types (`generic`, `repository`, managed connectors) are unchanged. The three new types are additive.

Future types that fit the same pattern (`gcloud_credentials`, `aws_credentials`, `azure_credentials`, `cert_bundle`) can be added incrementally as type-specific forms over the same backend shape.

### Backend storage shape

All new types share a single internal representation in the `credentials` JSONB column:

```json
{
  "files": [
    {
      "name": "config",
      "contents": "apiVersion: v1\nkind: Config\n...",
      "target_path": "~/.kube/config",
      "mode": "0600",
      "env_var": "KUBECONFIG"
    }
  ]
}
```

Field semantics:

- **`name`** — a label used for logging and the cleanup manifest. Need not match the on-disk filename.
- **`contents`** — UTF-8 file contents. Stored as JSON string. Hard cap **64 KB per file, max 5 files per datasource** — enforced in `DatasourceCreate` and `DatasourceUpdate` validators in `orchestrator/main.py`.
- **`target_path`** — absolute path or `~`-rooted path. Tilde expands against the agent's home (`/home/srw` per [[hardened_container]]). Must resolve under a writable mount; rejected at validation time if it doesn't.
- **`mode`** — octal string (`"0600"`, `"0644"`). Default `0600` if omitted.
- **`env_var`** — optional. If set, the resolved absolute target path is injected into the agent's process environment under this name (e.g. `KUBECONFIG=/home/srw/.kube/config`). Only the path is exposed via env, never the contents.

Type-specific defaults (target paths, modes, env vars from the table above) are applied **server-side** at datasource creation. The user picks the type and provides contents; the backend fills in the materialization targets. `generic_file` is the escape hatch where the user provides everything.

### Materialization lifecycle

Materialization happens in `src/core/datasource_setup.py`, alongside the existing `process_datasources()` flow that handles `generic`/`repository`/managed types. Concretely:

1. **On job start** — add a `process_credential_files(ds_configs, workspace_dir)` step:
   - For each datasource with a non-empty `credentials.files` list:
     - Resolve `target_path` (expand `~` against the agent's home directory).
     - `mkdir -p` the parent directory if it doesn't exist. Record created dirs in a session-scoped manifest for cleanup.
     - Refuse to overwrite an existing file unless it was materialized by us (i.e. present in a prior cleanup manifest). Prevents accidentally clobbering `~/.ssh/known_hosts` etc.
     - Write contents, `chmod` to the requested mode.
     - If `env_var` is set, inject `os.environ[env_var] = absolute_path`.
   - Append a one-line entry to `workspace.md` per datasource (e.g. *"Kubernetes access: `kubectl get nodes` (kubeconfig at `~/.kube/config`)"*). Do **not** echo file contents.
2. **On job end / failure / cancellation** — mirror `_close_datasource_connections` (`src/agent.py`):
   - `unlink` each file written.
   - Remove parent directories *we created* (not pre-existing ones like `~/.ssh/`).
   - Best-effort: log warnings on cleanup failure, never fail the job over it.

The manifest of materialized files is held in `_datasource_files` on the agent runtime, parallel to `_datasource_clients` for MongoDB cleanup today.

### Kubeconfig merging

A single job can attach multiple `kubeconfig` datasources — one per cluster. At materialization time:

1. Each datasource's YAML is written to `~/.kube/configs/<ds-slug>.yaml` (`0600`).
2. Before writing, each kubeconfig's `clusters`/`users`/`contexts` entries are **prefixed with the datasource slug** to prevent name collisions (e.g. a context named `default` from datasource `prod-eu` becomes `prod-eu-default`). Multi-context uploads are accepted — all contexts in the uploaded YAML get the prefix.
3. A merged kubeconfig is produced at `~/.kube/config` via:

   ```bash
   KUBECONFIG=~/.kube/configs/prod-eu.yaml:~/.kube/configs/staging.yaml \
     kubectl config view --flatten --merge > ~/.kube/config
   ```

   This uses kubectl's own merger — no custom YAML merging in our code. If `kubectl` isn't present in the agent image, fall back to a small `pyyaml`-based merger; in practice the agent image will have `kubectl` since that's the point of the feature.
4. `$KUBECONFIG` is set to `~/.kube/config` (single path, not the colon list) so non-kubectl tools that read the env var also see the merged view.
5. The agent discovers available contexts via `workspace.md` (one line per attached cluster: *"Cluster prod-eu: `kubectl --context prod-eu-default get nodes`"*) and via `kubectl config get-contexts`.

Per-datasource files remain on disk for debugging. Cleanup removes both the per-datasource files and the merged file; `~/.kube/configs/` is removed if we created it and is now empty.

### Path collision and safety rules

A handful of paths require care because they may pre-exist on the agent image or be touched by other tooling:

- **`~/.ssh/`** — may already exist with `known_hosts`. Don't recreate the directory if present; don't chmod the directory; don't delete the directory on cleanup. Only own the specific files we wrote.
- **`~/.kube/`** — typically does not pre-exist. Safe to create and remove.
- **`~/.aws/`, `~/.config/gcloud/`** (future) — same caution as `~/.ssh/`.

The validator on `target_path` rejects:

- Paths outside the writable mounts (must be under `/home/srw`, `/tmp`, `/run`, or `/workspace` per [[hardened_container]]).
- Paths that traverse symlinks (resolve and re-check after).
- The specific blocklist: `/etc`, `/proc`, `/sys`, `/dev`, `/var`, `/usr`, the literal `/home/srw/.bashrc`, `~/.bash_profile`, `~/.profile`, and any agent-managed file like `~/workspace.md`.

### Resolved datasource payload

The orchestrator's `_build_datasources_payload()` (`orchestrator/main.py`) already strips internal fields (`id`, `created_at`, etc.) before sending to the agent. No change to the payload shape — `credentials.files[]` flows through as part of the existing `credentials` dict.

### Tool registry

Credential file datasources do **not** register tools. They expose CLIs (`kubectl`, `ssh`, `gcloud`) which the agent uses via the existing `run_command` / `shell_execute` shell tools. No `kubernetes` tool category, no `cloud` category — that's the realm of [Future work](#future-work) if and when we add typed managed connectors.

The `workspace.md` summary line per datasource is how the agent discovers what's available. Same pattern as the existing `generic` type's `cli_hint`.

## UI design (cockpit)

A type-aware datasource creation form. The user picks the type first; the form rerenders.

**Kubeconfig form:**

- Name (required)
- Description (free text, agent-visible)
- Kubeconfig contents — paste box with a "📎 Upload file" button that reads the file into the textarea client-side. No multipart upload to the backend.
- "Test connection" button (optional, Phase 2) — orchestrator spins up a one-shot pod with the kubeconfig and runs `kubectl version --short` against the cluster.
- Trust-model notice: *"This kubeconfig will be readable by the agent process. Use a scoped, least-privilege kubeconfig for jobs that don't need cluster-admin."*

**SSH key form:**

- Name, Description.
- Private key — paste box / upload, **or** "Generate ed25519 keypair for me" button (calls existing `POST /api/datasources/ssh-keys/generate`). On generation, the public key is shown with a "Copy to clipboard" affordance for the user to paste into their `authorized_keys` on the target host.
- Comment field (optional, embedded in the generated public key).
- Trust-model notice: same general posture-1 warning.

**Generic file form:**

- Name, Description.
- Repeatable file entries: filename, target path, contents, mode, optional env var.
- Path-validation feedback inline.

All three forms share the same submit handler that POSTs to `/api/datasources` with the appropriate `type` and a backend-derived `credentials.files[]`. The list page (`cockpit/src/app/views/datasources/datasource-list.component.ts`) gets new badges for the three types; row actions (Test/Edit/Delete) work uniformly.

## Implementation roadmap

### Phase 1 — Backend ✅ Done (commit `544254d`, lifespan fix `b1bb254`)

- ✅ Added `kubeconfig`, `ssh_key`, `generic_file` to `valid_types` in `create_datasource` (`orchestrator/main.py:8847`), to the `DatasourceCreate.type` description, and to the schema comment in `orchestrator/database/schema.sql:906`. No CHECK constraint — enforcement stays at the API layer, matching the existing convention.
- ✅ New validator module at `orchestrator/security/credential_files.py`:
  - `normalize_credential_files(ds_type, ds_name, credentials)` — single entry point.
  - Caps: ≤5 files per datasource, ≤64 KB per file.
  - Path safety: writable-roots allowlist (`/home/srw`, `/tmp`, `/run`, `/workspace`), system-root blocklist (`/etc/`, `/proc/`, `/sys/`, `/dev/`, `/var/`, `/usr/`), reserved-file blocklist (`~/workspace.md`, `~/.bashrc`, `~/.bash_profile`, `~/.profile`).
  - Mode regex `^0[0-7]{3}$`; env-var regex `^[A-Za-z_][A-Za-z0-9_]*$`.
  - Per-type defaults applied server-side: `kubeconfig` → `~/.kube/configs/<slug>.yaml` 0600; `ssh_key` → `~/.ssh/<slug>` 0600 (+ optional `.pub` 0644); `generic_file` requires user-supplied `target_path`.
  - Wired into both `create_datasource` and `update_datasource` endpoints.
- ✅ Agent-side materialization at `src/core/datasource_setup.py` — `process_credential_files()` + `cleanup_credential_files()`:
  - mkdir-tracking so cleanup only removes dirs we created (pre-existing `~/.ssh` with `known_hosts` survives).
  - Per-datasource kubeconfig YAML is name-prefixed (`<slug>-<context>`, `<slug>-<cluster>`, `<slug>-<user>`) via PyYAML before write to prevent collisions on merge.
  - Multi-kubeconfig merge via `kubectl config view --flatten --merge`, with a graceful fall-back to colon-separated `KUBECONFIG` when kubectl isn't on PATH.
  - Refuses to overwrite existing files at the target path.
- ✅ Wired into the agent: `_datasource_files_manifest` field on `Agent`, populated after `process_datasources()`, drained by `_close_datasource_connections()` at job teardown (`src/agent.py`).
- ✅ Tests: 85 new tests (64 validator + 21 materialization, including a fake-kubectl shim to pin the merge contract). 101/101 credential-related tests pass (16 PR #1 encryption + 64 validator + 21 materialization).
- ✅ Live-cluster verification (2026-05-17): validator accepts all positive cases with correct defaults, rejects all 10 negative cases (oversize, too-many-files, blocked-root, traversal, blocked workspace.md, bad mode, bad env_var, kubeconfig 2-files, ssh_key 3-files, missing creds); DB round-trip writes `v1:<nonce>:<ct>` ciphertext via `jsonb_typeof=string` and decrypt-on-read restores the original `files[]` shape.
- ⚠️ **Not yet verified live**: agent-side materialization on a real dispatched job (writing into `/home/srw/...`, `kubectl config get-contexts` working, env var injection). Unit tests cover it; the kubectl-merge path is fake-kubectl-tested.

### Phase 2 — Cockpit UI ✅ Done (commit `f61113a`)

- ✅ `DatasourceType` extended in `cockpit/src/app/core/models/api.model.ts` with the three new variants; new `CredentialFileEntry` interface for the `files[]` payload shape.
- ✅ New optgroup **Credential files** in the type dropdown of `datasource-list.component.ts`, with options Kubeconfig / SSH Key / Generic file.
- ✅ Three type-specific form sections:
  - **Kubeconfig**: paste-or-upload textarea, posture-1 trust notice, hint about runtime prefixing.
  - **SSH key**: paste-or-upload textarea, reuses the existing `/api/datasources/ssh-keys/generate` endpoint and public-key dialog from the `repository` type, posture-1 trust notice.
  - **Generic file**: repeatable file editor (≤5 entries) with name, target_path, contents, mode, optional env_var; per-entry upload button; posture-1 trust notice.
- ✅ Type badge + icon: shared `warning` tone for credential-file types so they group visually in the table; icons `rocket_launch` (kubeconfig), `key` (ssh_key), `description` (generic_file).
- ✅ Connection-URL field and Test-connection button suppressed for credential-file types via `hasConnectionUrl()` / `isCredentialFileType()` helpers.
- ✅ `buildCredentials()` now produces the matching `{ files: [...] }` payload for each type; `canSave()`, `resetFormData()`, `openEditForm()` updated to handle the new state.
- ✅ Client-side 64 KB upload cap mirrors the backend validator (friendly error before the request is sent).
- ✅ i18n: 31 new keys in `en.json` + `de-DE.json` (parity check passes — 1388 keys both sides).
- ✅ Build clean; 273/273 cockpit vitest tests pass.
- ✅ Live-cluster verification (2026-05-17): both pods on `sha-f61113a`, deployed `en.json` contains all new keys, compiled `main-QLOC6TQ5.js` references all new template + class identifiers, validator accepts the exact payload shapes `buildCredentials()` produces.
- ⚠️ **Not yet verified live**: human-driven browser click-through (dropdown opening, trust-notice rendering, end-to-end create flow).

### Phase 3 — Docs and discoverability 🟡 In progress

- ✅ This roadmap section updated with shipped status and commit refs.
- ✅ `repo_datasource.md` "Future: General Credentials Store" + Phase 3 roadmap entry marked superseded.
- ✅ `docs/datasources.md` Supported Types table extended; Credentials section updated to reflect encryption-at-rest.
- ⬜ Short "Working with credentials" section in `CLAUDE.md` so the agent knows the materialization paths and env vars to expect — deferred until we've seen the first real agent run consume a credential file.

### Encryption at rest ✅ Done (PR #1, commit `d8044f5` + lifespan fix `b1bb254`)

The `datasources.credentials` JSONB column is encrypted at rest with AES-256-GCM via `orchestrator/security/crypto.py` (the same machinery used for `user_api_keys.api_key` and `llm_endpoints.api_key`). On disk: `jsonb_typeof = 'string'` with the canonical `v1:<nonce-b64>:<ct-b64>` prefix. The migration:

- Encrypt on write in `create_datasource` / `update_datasource` / `upsert_default_datasource` (three write sites in `orchestrator/database/postgres.py`).
- Decrypt on read across seven read sites (`list_datasources`, `get_datasource`, `resolve_datasources_for_job`, `resolve_datasources_for_thread`, `list_project_datasources`, plus `RETURNING` clauses in create/upsert).
- Legacy plaintext rows still readable with a warning, then migrated by `PostgresDB.backfill_encrypt_datasource_credentials()`.
- Backfill runs from the FastAPI lifespan handler (`orchestrator/main.py`), **not** from `init.py` — `init.py` is not invoked by the cluster deploy pipeline (caused the 2026-05-12 MongoDB-index outage; documented at `main.py:3072-3074`).
- Redaction layer at `orchestrator/security/access.py:509` (`redact_datasource`) is unchanged — credentials are stripped before any REST response.

## Future work

- **Posture 2 — wrapper CLI.** Inject a `kubectl` / `rancher` wrapper into the agent's `PATH` that reads the kubeconfig from a path owned by a different UID (e.g. `/run/secrets/kubeconfig` `0400 root:srw`, readable via setuid binary or tiny localhost helper). The agent runs `kubectl get pods` but `cat $KUBECONFIG` returns permission denied. Real isolation, modest infrastructure.
- **Posture 3 — sidecar / managed connector.** A `kubernetes` managed connector along the lines of `postgresql`/`mongodb`: typed tools (`kubectl_get`, `kubectl_apply`), read-only enforcement, credential never enters the agent container. Highest isolation, most code.
- **ssh-agent socket forwarding.** For `ssh_key`, a sidecar runs `ssh-agent` and mounts its Unix socket at `$SSH_AUTH_SOCK`. The private key never touches the agent's filesystem. The agent can `ssh somehost` (signing happens in the sidecar) but cannot `cat ~/.ssh/id_*`.
- **Vault references in lieu of stored contents.** Once [[deployment]]'s Vault + ESO path is live, a credential file datasource could carry a Vault path (`vault://kv/jobs/<id>/kubeconfig`) instead of inline contents. The orchestrator dereferences at materialization time. Aligns with the deployment roadmap's deferred Vault work.
- **Credential rotation and expiry.** Datasource rows could carry `expires_at` and a webhook for rotation. Not in scope for v1.
- **Network egress allowlisting.** For kubeconfigs pointing at clusters outside the VPN sidecar, the agent will not be able to reach the API server (per [[hardened_container]] NetworkPolicy). Either route the target cluster through the VPN sidecar or extend the policy. Document at credential-creation time.

## Resolved decisions

- **SSH key default `target_path`** — `~/.ssh/<ds-slug>` (one file per datasource). Multiple SSH-key datasources can coexist on a job. The user wires their own `~/.ssh/config` for host-specific key selection; we surface this in the trust-model notice.
- **Kubeconfig scope** — one datasource per cluster (multi-context uploads accepted but all contexts get the ds-slug prefix). At job start the orchestrator merges all attached kubeconfigs into a single `~/.kube/config` via `kubectl config view --flatten --merge`. The agent works against one canonical kubeconfig with predictable, collision-free context names.

## Related

- [[datasources]] — Parent datasource architecture.
- [[datasource_redesign]] — The "generic / repository / managed connector" lineage doc; this design adds a fourth pattern (file-materialized credentials) that lives alongside generic.
- [[repo_datasource]] — Existing SSH-key handling for git; this doc supersedes its "Future: General Credentials Store" section.
- [[hardened_container]] — Filesystem constraints, writable mounts, `/proc/self/environ` leakage, NetworkPolicy egress.
- [[deployment]] — Vault + ESO is the eventual production secret source; relevant to the "Vault references" future work item.
