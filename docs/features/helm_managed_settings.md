# Helm-Managed Settings (Per-Field Provenance)

Let operators choose, per setting, whether Helm or the in-app UI is authoritative — without forcing them to pick one mode for the whole deployment. Settings declared in `values.yaml` are reapplied on every `helm upgrade`; settings the operator omits stay UI-managed forever. The Cockpit shows a "Managed by Helm" badge on the rows where a chart upgrade will revert UI edits, so users see the trade before they make it.

This is a Longhorn-style hybrid, but with the per-field provenance marker that Longhorn was missing in its first iteration ([longhorn issue #2570](https://github.com/longhorn/longhorn/issues/2570)) and that Rancher's `Setting` CR uses in production. We already have the substrate for it — `system_api_keys.seeded_from` exists today as a breadcrumb but isn't enforced on upgrade. This proposal turns it into a real source-of-truth marker, generalises it to a few more tables, and adds the seed job behaviour and UI affordances around it.

## Problem

The deployment story currently bifurcates badly between two operator personas, and we serve neither well:

- **The "minimal Helm" operator** (small team, handful of users, GitOps not in scope) wants to deploy with `helm install` and a few env-var-shaped values, then change everything else in the Cockpit. Today this works for user-level settings, but every system-level setting we add — `system_api_keys`, `system_settings.main_cloud`, model defaults — is split between "must be in `values.yaml`" and "must be set in the UI", and there's no way to do the same setting from either side.
- **The "Helm as source of truth" operator** (large org, ArgoCD/Flux, multi-environment, regulated change control) wants `values.yaml` in Git to be the single audit-traceable record. Today even when they put values in the chart, a Cockpit admin can edit them in the UI and the next `helm upgrade` won't reclaim those rows — the chart silently loses authority and nobody notices until an audit.

We already half-built this twice. `system_api_keys.seeded_from` (`orchestrator/database/schema.sql:273-312`) was added to track helm-vs-manual provenance, but the `seed/llm_config.py` job is insert-only — `ON CONFLICT DO NOTHING` — so a Helm-declared key never overwrites a UI-edited row even when the operator wants it to. `system_settings` (`schema.sql:1250`) has no provenance column at all; the `main_cloud` row gets PUT'd by `/api/admin/system-settings/main_cloud` and lives forever in the DB regardless of what `values.yaml` says.

We also have `models.seeded_from` and similar fields scattered across other tables, all stuck at the same insert-only boundary.

The result: the Helm chart looks declarative but isn't. Operators report "I changed `values.yaml` and ran `helm upgrade`, my key didn't update" — exactly the Longhorn complaint that drove their fix.

## Scope

**In scope**
- A uniform per-row `source` enum on the small set of system-scoped tables that operators actually want to manage from Helm: `system_api_keys`, `system_settings`, `llm_endpoints` (system-scoped rows only), `models` (system-curated rows only).
- A migration from the current `seeded_from` text columns to the new enum, preserving history.
- Extension of the existing `llm-seed-job.yaml` post-install/post-upgrade hook to **reconcile** rather than insert-only — overwrite rows where `source='helm'`, leave rows where `source='ui'` alone, and write a clear log line for both.
- A "Managed by Helm" Cockpit badge with a tooltip explaining that the operator's edit will be reverted on the next chart upgrade, plus a confirmation prompt when an admin overrides a Helm-managed value.
- An admin endpoint to "release" a Helm-managed row (flip `source` from `helm` to `ui`), so an operator can intentionally hand control to the UI without deleting and recreating.
- A read-only `/api/admin/helm-managed` endpoint that lists every row currently marked `source='helm'`, for ops dashboards and audit.
- Documentation for the `values.yaml` schema additions and a worked example of "minimal Helm" vs "full Helm" deployments.

**Out of scope**
- A custom Kubernetes operator / CRD-based reconciliation loop. The existing post-install Job is sufficient and matches Rancher's pragmatism. We can graduate to a controller later if drift-during-upgrade becomes a real concern.
- Per-user settings. `users.settings` JSONB stays UI-only — Helm has no business reaching into a single user's preferences.
- Agent-behaviour config (`config/defaults.yaml`, `config/experts/*.yaml`). These are shipped in the image and not user-editable; Helm-vs-UI provenance does not apply.
- A global "Helm-managed mode" toggle. Rejected after research — no mature project ships one and the failure modes are bad (silent audit-trail loss, surprise wipes during upgrade). Per-field provenance is what every battle-tested project converged on.
- Continuous reconciliation between Helm runs. If a Cockpit admin edits a `source='helm'` row, the edit sticks until the next `helm upgrade` — same model as Longhorn's docs, predictable, no controller required.

## Design

### Provenance enum

```sql
CREATE TYPE setting_source AS ENUM ('default', 'helm', 'ui');
```

- `default` — row was created by an init seeder shipped in the image, not declared in `values.yaml`. Eligible to be overwritten by either Helm or UI.
- `helm` — row was last written by the post-install/post-upgrade Job. Will be overwritten by the next `helm upgrade` if still declared in values; the UI shows a badge.
- `ui` — row was last written by an admin via the Cockpit. Helm leaves it alone even when the same key appears in `values.yaml`.

Each affected table gets:

```sql
ALTER TABLE system_api_keys
    ADD COLUMN source setting_source NOT NULL DEFAULT 'ui',
    ADD COLUMN helm_value_hash text,             -- sha256 of the last helm-applied value, for drift detection
    ADD COLUMN source_updated_at timestamptz NOT NULL DEFAULT now();
```

`helm_value_hash` is a sha256 of the value Helm last applied. We deliberately don't store the plaintext: it lets the UI show "Differs from Helm declaration" without leaking the chart's value alongside the live one, and the rule applies uniformly across secret and non-secret tables. This is a coarser signal than "Helm wants X, you have Y" but it's already better than what most systems offer (a generic upgrade-time warning), and the uniform rule keeps the table-by-table logic simple. Same shape for `system_settings`, `llm_endpoints (WHERE user_id IS NULL)`, and `models (WHERE seeded_from IS NOT NULL)`.

Migration backfill: existing rows with `seeded_from = 'helm'` → `source = 'helm'`; everything else → `source = 'ui'` (conservative — assume an existing live row reflects an intentional admin edit).

### Reconcile-on-upgrade Job

The existing `helm/templates/orchestrator/llm-seed-job.yaml` runs today as a post-install + post-upgrade hook (weight 10, after schema migrations) with insert-only semantics. We promote it to a **pre-install + pre-upgrade hook at weight `-5`** so it runs in the same window as the keycloak bootstrap Job (also `-5`) and ahead of orchestrator/cockpit/mcp deployments. This matches the lifecycle ordering already established in `b994086` and means settings are reconciled before the orchestrator boots and starts serving Cockpit traffic, rather than racing the first user requests.

The Job continues to call `python -m seed.llm_config --payload /seed/llm.yaml`. Migrations still run inside `lifespan()` in the orchestrator process, so the seed Job can't pre-create the `setting_source` enum or the new columns — the Job will retry (`backoffLimit`, same as keycloak bootstrap) if it lands before the orchestrator's next pod has applied migrations. For first install the order is: namespace + ExternalSecrets (`-15`) → seed Job + keycloak bootstrap (`-5`, both with init containers that wait on their respective ESO Secrets) → orchestrator pod starts → migrations apply → Job's next retry succeeds. The retry loop is acceptable here; the alternative (running the seed inside `lifespan()`) couples settings reconciliation to pod restart and loses the audit-clean Job log.

The change in semantics is small:

1. `values.yaml` gains a `helm.settings.reconcile` block that lists exactly which keys Helm is claiming. Anything not listed is left alone:
   ```yaml
   helm:
     settings:
       reconcile:
         systemApiKeys: [openai, anthropic, groq]   # Helm owns these
         systemSettings: [main_cloud]                # Helm owns main_cloud
         llmEndpoints: [codex-proxy]                 # Helm owns this label
         models: []                                  # Helm doesn't own any model rows
   ```
   The rule is simple and matches user expectation: **"Managed by Helm" means "will be overwritten on `helm upgrade`."** If a key is in `reconcile`, Helm owns it and `helm upgrade` overwrites it. If a key is absent from `reconcile`, Helm does not own it — full stop.

2. The seed job runs in two passes, both inside a single Postgres advisory-lock'd transaction (same lock pattern as `orchestrator/database/migrate.py` so admin Cockpit saves briefly block during reconcile rather than racing it):
   - **Apply pass** — for each declared row in `values.yaml`, UPSERT with `source = 'helm'` and update `helm_value_hash`. This overwrites both `default` and `ui` rows; we explicitly want Helm to win for keys the operator listed.
   - **Release pass** — for each row currently `source = 'helm'` whose key is **no longer** in the reconcile list, flip `source` to `ui` (value untouched, badge disappears, drift indicator clears). Reasoning: removing a key from `reconcile` is the operator declaring "I'm not managing this anymore" — the row should immediately stop showing "Managed by Helm" rather than sit in a confusing in-between state where the badge says one thing and the chart says another.
3. The job writes a structured summary to its log and to a `helm_apply_log` audit table (see *Audit retention* below).

### Cockpit affordances

- **Badge**: every row with `source = 'helm'` gets a small "Helm" pill with a tooltip: "This setting is declared in the deployment's Helm values. Edits made here will be reverted on the next `helm upgrade` unless an operator updates `values.yaml`."
- **Override confirmation**: when an admin saves an edit to a `helm`-marked row, a confirmation modal appears: "This will flip control to the UI until an operator re-declares it in Helm. Continue?" Confirming flips the row to `source = 'ui'`.
- **Release action** in admin → providers / admin → system-settings: a "Release from Helm" button on each Helm-managed row that flips `source` to `ui` without changing the value, for operators who want to start managing a setting from the UI.
- **Drift indicator**: if `helm_value_hash` differs from `hash(current_value)` and `source = 'ui'` (i.e. someone overrode), show "Differs from Helm declaration" so the admin knows there's a pending revert risk.

### `values.yaml` ergonomics

The shape mirrors what `llm-seed-configmap.yaml` already does, with the secret-source pattern adopted from `keycloak.bootstrap` (commit `b994086`) so operators have a single mental model for "K8s Secret vs Vault path" across the chart. New top-level structure:

```yaml
helm:
  settings:
    # Operators list every key they want Helm to own. Anything not listed is UI-only.
    reconcile:
      systemApiKeys: []
      systemSettings: []
      llmEndpoints: []
      models: []

    # Then the actual values, only used if the key is in `reconcile.*`.
    # For each api key entry, set EXACTLY ONE of `secretRef` or `vaultPath`
    # (mutually exclusive — chart fails to render otherwise, mirroring the
    # `keycloak.bootstrap.adminCredentials*` pattern).
    systemApiKeys:
      openai:
        # Option A: pre-existing K8s Secret in the release namespace.
        secretRef:
          name: srw-llm-keys
          key: OPENAI_API_KEY
      anthropic:
        # Option B: Vault path. Chart renders an ExternalSecret as a
        # pre-install/pre-upgrade hook (weight -15, runs before the seed
        # Job at -5). Requires externalSecrets.enabled=true.
        vaultPath: homelab/srw/llm-keys/anthropic
    systemSettings:
      main_cloud:
        backend: opencloud
        url: https://cloud.example.com
        credentials_ref: env:OPENCLOUD_KEYCLOAK_CLIENT_SECRET
```

The two-level structure (`reconcile` + values) is intentional: it forces operators to make an explicit choice for each key, rather than accidentally Helm-managing every value they happen to set. The `secretRef`/`vaultPath` exclusivity is enforced at chart-render time with a `fail` directive, same shape as `helm/templates/keycloak/bootstrap-job.yaml:2-7`.

### Hook ordering and ESO race handling

The seed Job is a pre-install + pre-upgrade hook (this is a behaviour change from today's post-install hook — see *Migration plan*, phase 2). When a `vaultPath` entry is declared:

- The chart renders an additional `ExternalSecret` per Vault path, hook weight `-15` (matching the keycloak bootstrap precedent).
- The seed Job sits at hook weight `-5`, after the ExternalSecrets but before any chart resource that depends on settings being applied (orchestrator deployment, etc.).
- The Job's pod gets an init container that mounts each ESO-synced Secret read-only and `test -s`'s the expected keys before the main container runs — exact pattern from `helm/templates/keycloak/bootstrap-job.yaml:53-65`. The kubelet retries the volume mount until the Secret exists, so we get wait-for-Secret with no kubectl/RBAC.
- ExternalSecret `delete-policy` is `before-hook-creation` only (intentionally NOT `hook-succeeded`), so ESO keeps reconciling the Secret post-install. Vault credential rotation propagates without a chart upgrade — the seed Job re-runs only on the next `helm upgrade`, but the live API keys in `system_api_keys` are decrypted-on-read against `APP_ENCRYPTION_KEY`, not against Vault directly, so rotation only matters at next reconcile.

### Resource policy and lifecycle protection

Commit `b994086` added `helm.sh/resource-policy: keep` to every PVC in the chart, the namespace, and the stateful service templates. This matters for our design because **the per-row `source` provenance lives in the orchestrator Postgres PVC**. Concretely:

- `helm uninstall` does not delete `postgres-pvc`, `postgres-vector-pvc`, or the namespace — so `system_api_keys.source`, `system_settings.source`, `helm_apply_log` history all survive a chart removal and reinstall.
- A `helm install` after a previous uninstall sees existing rows in their last state. The seed Job's apply pass overwrites `source='helm'`/`source='default'` rows as expected and leaves `source='ui'` rows intact, so re-installing doesn't silently wipe operator UI edits made before the uninstall. This is the desired behaviour and we don't need to add code for it — Postgres state durability does it for us.
- Full reset is the documented two-step (`helm uninstall <release>` then `kubectl delete namespace <ns>`), same as the namespace.yaml comment block. The doc should reference this in the operator guide so nobody is surprised by the `keep` semantics.

## Worked deployment scenarios

**"Minimal Helm" — small team:**
```yaml
helm:
  settings:
    reconcile:
      systemApiKeys: []     # nothing — admins set keys in the UI
      systemSettings: []
      llmEndpoints: []
      models: []
```
The Cockpit admin onboards, adds API keys via the UI, configures cloud storage. Every row stays `source = 'ui'` forever. `helm upgrade` reapplies image tags, ConfigMap envs, and migrations, but never touches a settings row. No surprises.

**"Full Helm" — large org with ArgoCD:**
```yaml
helm:
  settings:
    reconcile:
      systemApiKeys: [openai, anthropic, groq]
      systemSettings: [main_cloud]
      llmEndpoints: [codex-proxy]
      models: []
    # ... full values for each declared key
```
Every infra setting is in Git. A Cockpit admin who tries to edit one sees the "this will be reverted on next upgrade" prompt. ArgoCD's `helm upgrade` reapplies the chart and the seed job overwrites any accidental overrides. The audit endpoint confirms which rows are Helm-owned at any given moment.

**"Hybrid" — typical mid-size:**
```yaml
helm:
  settings:
    reconcile:
      systemApiKeys: [openai]   # the production OpenAI key is org-managed
      systemSettings: [main_cloud]
      llmEndpoints: []
      models: []
```
The OpenAI key and cloud backend are in Git (compliance, billing). Admins add Anthropic, Groq, and custom endpoints via the UI as the team experiments. `helm upgrade` keeps OpenAI and main_cloud in sync, leaves the rest alone.

## Migration plan

1. **Phase 1 — Schema and types.** Add `setting_source` enum and the three new columns (`source`, `helm_value_hash`, `source_updated_at`) to the four affected tables. Backfill from `seeded_from`. New migration files under `orchestrator/database/migrations/app/`. No behaviour change yet.
2. **Phase 2 — Seed job rewrite.**
   - Update `seed/llm_config.py` to read `helm.settings.reconcile.*` from the payload and apply the two-pass apply/release logic, advisory-locked.
   - Move `helm/templates/orchestrator/llm-seed-job.yaml` from post-install to pre-install/pre-upgrade hook at weight `-5`.
   - Add the wait-for-Secret init container pattern (mirrors `helm/templates/keycloak/bootstrap-job.yaml:53-65`) so the seed Job can mount ESO-synced Secrets when `vaultPath` entries are declared.
   - Render one `ExternalSecret` per `vaultPath` entry in `helm.settings.systemApiKeys.*`, hook weight `-15`, `delete-policy: before-hook-creation` only (so ESO keeps reconciling post-install). Use the same `srw.componentLabels` and helper-template patterns as the keycloak bootstrap ExternalSecret.
   - Add the chart-render `fail` for mutually-exclusive `secretRef` vs `vaultPath`, same shape as the keycloak bootstrap mutual-exclusion check.
   - Job remains insert-only on every table not listed in `reconcile`, so the change is safe to roll out before any operator opts in.
3. **Phase 3 — Admin endpoints.**
   - `GET /api/admin/helm-managed` — list all `source='helm'` rows across affected tables.
   - `POST /api/admin/helm-managed/{table}/{key}/release` — flip `source` to `ui`.
   - Existing PUT/DELETE endpoints gain side-effects: editing a `helm`-marked row flips `source` to `ui` and emits an audit event.
4. **Phase 4 — Cockpit UI.** Badge component, override confirmation modal, release action, drift indicator. Wire up `getHelmManagedRows()` to drive the admin overview.
5. **Phase 5 — Docs and helm chart values.** Add the `helm.settings` block to `helm/values.yaml` (default empty), document in `helm/README.md` and a new `docs/deployment/helm_managed_settings.md` operator guide. The operator guide must call out:
   - The two-step uninstall (`helm uninstall` keeps the namespace + PVCs; `kubectl delete namespace` is the explicit reset), since this is the same surprise that the namespace.yaml comment already warns about.
   - The `secretRef` vs `vaultPath` mutual exclusion and what error to expect on chart render.
   - That declaring a key under `reconcile` and not under values is a chart-render error too (catch operator typos).

Each phase is independently shippable. After phase 2 the system supports Helm reconcile for operators who edit `values.yaml`; the UI work in phase 4 makes it discoverable for admins.

## Audit retention

`helm_apply_log` lives in Postgres alongside the rest of the orchestrator schema (not the MongoDB audit store — operators should be able to inspect Helm reconcile history with the same DB tooling they use for everything else). Each row TTLs after 90 days, enforced by a periodic delete in the orchestrator's existing maintenance loop. Schema:

```sql
CREATE TABLE helm_apply_log (
    id           bigserial PRIMARY KEY,
    applied_at   timestamptz NOT NULL DEFAULT now(),
    chart_version text,
    summary      jsonb NOT NULL    -- per-table counts: applied, released, skipped
);
CREATE INDEX idx_helm_apply_log_applied_at ON helm_apply_log (applied_at DESC);
```

The cleanup runs in the same scheduled maintenance task that already sweeps stale heartbeats and orphaned jobs, so no new infra. 90 days covers a typical quarterly audit window without unbounded growth.
