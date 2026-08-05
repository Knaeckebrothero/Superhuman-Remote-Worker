# K3d + Playwright live validation — project-scoped connector auto-attach

**Type:** deployed end-to-end acceptance runbook. This is not a pytest file.
It requires the local `srw` k3d cluster, Tilt, Chromium through Playwright, and
disposable test data.

**Status:** EXECUTED 2026-08-05 — **no product failures found in 35 assertions
across 6 spec files**, but P1 coverage is incomplete, so this is *not* a full
live acceptance. Verdict and per-row results in section 11. The run required a
local migration-ledger repair first; see the finding at the end of section 11.

**Implementation baseline:** commit `aec2e5da` (`feat(datasources): add project
scopes and auto attach defaults`). The tested `HEAD` may be newer, but this
commit must be an ancestor.

**Design and acceptance source:**
[`docs/features/project_scoped_connector_auto_attach.md`](../features/project_scoped_connector_auto_attach.md)

**Copy-ready agent prompt:**
[`docs/done/project_scoped_connector_auto_attach_k3d_test_agent_prompt.md`](project_scoped_connector_auto_attach_k3d_test_agent_prompt.md)

This runbook validates the deployed contract, not just the browser's checked
state. Every selection assertion must be corroborated by the authenticated API
and, for created work, by the materialized PostgreSQL rows or thread metadata.

---

## 1. Safety and pass contract

- Use only Kubernetes context `k3d-srw`, namespace `srw`, and the local origins
  `https://localhost`, `https://api.localhost`, `https://auth.localhost`,
  `https://git.localhost`, `https://mcp.localhost`, and
  `http://localhost:10350`. MCP may also be reached at the same-origin
  `https://localhost/mcp` path.
- Stop immediately if the active target is not the local k3d cluster. Never run
  these steps against `main`, a homelab context, or a public deployment.
- Do not reset, stash, clean, reformat, commit, push, or modify product source.
  The checkout may contain unrelated staged and unstaged user work.
- `deployment/values-local.yaml` is gitignored and may be changed only to set
  the two test flags. Record each key's original presence/absence and boolean
  value, then restore that exact shape. Never print, screenshot, or copy other
  values from that file, and do not overwrite concurrent overlay edits.
- Do not run `tilt trigger srw`. That is a custom-deploy Force Update and first
  uninstalls the Helm release. If an already-running Tilt stack must re-render
  changed local values, trigger an image resource such as
  `tilt trigger srw-orchestrator` and allow its normal `srw` dependency update.
- Do not delete the cluster, namespace, Helm release, PVCs, default project,
  native project knowledge connector, or pre-existing rows.
- Use a unique run prefix and delete only identifiers recorded by this run.
- Never print, quote, stage, or share connector credentials, authorization
  headers, cookies, local overlay values, full connection URLs, or unfiltered
  logs. Raw traces/storage state are credential-bearing temporary local
  artifacts: keep them only in the ignored run directory, cite sanitized
  extracts, and delete them after triage. Use generic connectors for the policy
  matrix so screenshots contain no secrets.
- A screenshot alone is not proof. Prefer accessible DOM assertions, captured
  request/response bodies, API reads, and narrow database queries.
- Results are `PASS`, `FAIL`, `BLOCKED`, or `NOT RUN`. An environmental block is
  not a pass. Continue independent scenarios after a failure when it is safe.
- Do not fix failures during this run. Capture a minimal reproduction and
  report the suspected component and source location.

### Completion levels

- **P0 — release gate:** rollout gates, creation/edit UI, eligibility matrix,
  job/session preselection, explicit empty selection, persistence, and primary
  authorization boundaries.
- **P1 — full live contract:** owner-versus-member behavior, automations,
  project loops, root omission semantics, MCP when available, native project
  knowledge policy, reconciliation, and existing-work immutability.
- **P2 — hardening:** stale revisions, delayed/out-of-order reads, fail-closed
  loading, zero-link behavior, and pagination. P2 failures must still be filed,
  but may be triaged separately from a P0 release decision.

The feature is live-accepted only when every P0 and P1 row passes. A P0/P1
`BLOCKED` row makes the overall verdict `BLOCKED` unless the human owner later
accepts that disposition; the test agent cannot self-accept it. P2 must have no
security or silent credential-expansion failure.

---

## 2. Evidence record

Fill this before changing flags or creating fixtures.

| Item | Value |
|---|---|
| UTC date / tester | 2026-08-05 14:08–17:10Z / Claude Code |
| Git `HEAD` | `a7bc1c6a` at start → `797e2e1d` at end (see note) |
| `aec2e5da` is an ancestor | Yes, at both SHAs |
| Dirty-worktree warning recorded | Yes — unrelated in-progress user work present throughout |
| Dirty paths overlapping Orchestrator/Cockpit/MCP | `orchestrator/services/canvas.py`, `orchestrator/routers/shared_browser.py`, `orchestrator/services/shared_browser_canvas.py`, `src/api/persistent_app.py`, several `cockpit/src/app/**` canvas/chat files. **None touch datasource scope/auto-attach.** |
| Tilt live-update/build state | `tilt up` running; orchestrator/cockpit rebuilt during the run (values-overlay edits + unrelated user commits) |
| Kubernetes context / namespace | `k3d-srw` / `srw` |
| Helm revision | 92 |
| Orchestrator image ID | `tilt-68d9791dffae67ae` (R0) → `tilt-8d75d669aff55ffa` (end) |
| Cockpit image ID | `tilt-f59a658e183beecc` → rebuilt to `685dc4588` pod during R1 |
| MCP image ID, if tested | `tilt-1aa62f79660bd4df` — **not exercised** |
| Playwright version / browser | `@playwright/test` 1.59.0 (repo pin) / Chromium 1217 |
| Run prefix | `DS-SCOPE-E2E-20260805T1445Z` |
| Admin user ID | `d32df192-77e7-4c5b-8c1d-9f7ace423b08` (`test`) |
| Owner user ID | `bd3f873e-abb0-42ee-a286-675d6737d1c3` (`pending2`, `is_admin=false`) |
| Member user ID | `7ce7dd19-ed1e-4d88-bca3-120f239dc4c3` (`pending3`, `is_admin=false`) |
| Owner default project ID | `9c966cb8-6057-45a7-8724-ff3cfe9d6a1d` |
| Member default project ID | `cd7a7e26-5682-4aa8-8ff4-495589079ccd` |
| Project A ID | `a220ffc3-13f0-4577-a457-7ab7df8a8e2a` (deleted in cleanup) |
| Project B ID | `4ecc4ec4-b4b6-417d-842e-7653d150bcb1` (deleted in cleanup) |
| Project C ID | Not created — retained-only/authority-loss rows NOT RUN |
| Native-KB deletion project ID | Covered by deleting A / B / ZERO; all three native KB rows cascaded cleanly |
| Zero-link project ID | `60521b00-6a3f-4913-8eaf-f223786b55f4` (deleted in cleanup) |
| Other disposable project IDs | None |
| Created connector IDs | 8 fixtures: `ae87ceab` R0-CANARY, `476ef22a` ALL-AUTO, `4e439bdd` ALL-MANUAL, `1c626b71` A-AUTO, `f4d2ded0` A-MANUAL, `0e68b787` AB-AUTO, `079ccbac` DEFAULT-AUTO, `bebd2a54` ZERO-LINK (all deleted) |
| Created job IDs | 16, all deleted in cleanup |
| Created thread IDs | 3: `70f12d05`, `105d11c3`, `27fcbf67` (all deleted) |
| Automation ID | None — R1-AUTO NOT RUN |
| Loop ID / spawned job IDs | None — R1-LOOP NOT RUN |
| Temporary memberships/grants/role changes | Member added as `editor` to A and B; both removed in cleanup. No `public_datasources` grant created. Keycloak passwords set for `pending2`/`pending3` (still in place). |
| Disposable Gitea repository ID, if any | None |
| Artifact directory | `cockpit/test-results/datasource-scope-autoattach-20260805T1445Z` |
| Follow-up issue links | Migration-ledger finding + two runbook inaccuracies, both recorded in section 11 |

**`HEAD` note:** two unrelated commits (`54c6a324` helm codex-proxy floor,
`797e2e1d` docs) landed from a parallel session mid-run. The only
`orchestrator/main.py` delta from `aec2e5da..HEAD` is a comment path change, and
`0082` was byte-identical throughout, so the results describe the `aec2e5da`
feature code as deployed.

The Git SHA and image IDs are provenance, not proof of a pristine checkout.
Tilt may be serving live-updated dirty files that postdate an image. Record any
dirty path overlapping the tested components and describe the result as the
exact live workspace state, not simply “commit `aec2e5da`.”

Capture at minimum:

- one screenshot of the connector policy form;
- one screenshot each of the Project A and Project B picker states;
- the safe fields for this run's fixture rows from
  `/api/datasources/eligible`, plus only an aggregate count for pre-existing
  baseline rows, for each context;
- the create-job and create-thread request bodies with only project and
  `datasource_ids` fields retained;
- materialized job/thread selections;
- a trace for every browser failure;
- a short, identifier-filtered orchestrator log excerpt only when needed.

---

## 3. Cluster and deployment preflight

Run from the repository root:

```bash
git rev-parse HEAD
git merge-base --is-ancestor aec2e5da HEAD
git status --short

k3d cluster list
kubectl config current-context
kubectl --context=k3d-srw get nodes
kubectl --context=k3d-srw -n srw get pods
kubectl --context=k3d-srw -n srw get pods \
  -o custom-columns='NAME:.metadata.name,IMAGES:.spec.containers[*].image,IMAGE_IDS:.status.containerStatuses[*].imageID'
helm --kube-context=k3d-srw -n srw status srw
tilt get uiresources
```

Pass conditions:

- `git merge-base` exits zero;
- the explicit context is `k3d-srw` and its node is Ready;
- Postgres, Keycloak, Orchestrator, Cockpit, and the services needed by the
  chosen scenarios are Ready;
- Tilt has no stale failed build for Orchestrator or Cockpit;
- the application opens at `https://localhost/`.

Confirm the migration without exposing database credentials. Note: this run
predates the renumber — the migration shipped as `0083` after
`0082_usage_cloud_rate_cards.sql` claimed `0082` from a parallel branch, and it
now lands alongside `0084_datasource_scope_validate_constraints.sql` and
`0085_datasources_auto_attach_owner_idx.notx.sql`. The findings below are
recorded as they occurred, under the original `0082` name.

```bash
kubectl --context=k3d-srw -n srw exec -i statefulset/srw-postgres -- sh -lc \
  'PGPASSWORD="$POSTGRES_PASSWORD" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At' <<'SQL'
SELECT filename
FROM schema_migrations
WHERE filename = '0083_datasource_scope_auto_attach.sql' AND success;
SQL
```

Expected: exactly `0083_datasource_scope_auto_attach.sql` (`0082` on any
cluster last migrated before the renumber).

Corroborate the migration's safe postconditions with aggregate counts only:

```bash
kubectl --context=k3d-srw -n srw exec -i statefulset/srw-postgres -- sh -lc \
  'PGPASSWORD="$POSTGRES_PASSWORD" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At' <<'SQL'
SELECT 'bad_native_policy', COUNT(*)
FROM datasources d
WHERE d.job_id IS NULL
  AND d.type = 'kb'
  AND NULLIF(d.config->>'native_project_id', '') IS NOT NULL
  AND (d.scope_mode <> 'projects' OR d.auto_attach IS NOT TRUE);

SELECT 'bad_native_link', COUNT(*)
FROM datasources d
JOIN projects p
  ON p.id = CASE
    WHEN d.config->>'native_project_id' ~*
         '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
    THEN (d.config->>'native_project_id')::uuid
    ELSE NULL
  END
WHERE d.job_id IS NULL
  AND d.type = 'kb'
  AND NOT EXISTS (
    SELECT 1 FROM project_datasources pd
    WHERE pd.project_id = p.id AND pd.datasource_id = d.id
  );

SELECT 'bad_ownerless_linked_policy', COUNT(*)
FROM datasources d
WHERE d.job_id IS NULL
  AND d.created_by IS NULL
  AND d.is_global = FALSE
  AND NOT (d.type = 'kb' AND NULLIF(d.config->>'native_project_id', '') IS NOT NULL)
  AND EXISTS (SELECT 1 FROM project_datasources pd WHERE pd.datasource_id = d.id)
  AND (d.scope_mode <> 'projects' OR d.auto_attach IS NOT FALSE);

SELECT 'missing_legacy_job_link', COUNT(*)
FROM datasources d
WHERE d.job_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM job_datasources jd
    WHERE jd.job_id = d.job_id AND jd.datasource_id = d.id
  );

SELECT 'missing_job_selection_snapshot', COUNT(*)
FROM jobs j
WHERE NOT (COALESCE(j.context, '{}'::jsonb) ? 'datasource_selection');
SQL
```

Expected: every count is zero. This proves current-cluster invariants, not the
transformation from an older database. If a sanitized pre-0082 snapshot and a
separate disposable database are available, rehearse the migration there under
P2. Otherwise report migration transformation as `NOT RUN`; never downgrade or
rewrite the live application database to manufacture that evidence.

Inspect only the two non-secret flags:

```bash
kubectl --context=k3d-srw -n srw get configmap srw-config \
  -o jsonpath='{.data.DATASOURCE_SCOPE_AUTO_ATTACH_V1_ENABLED}{" "}{.data.DATASOURCE_DEFAULTS_ON_OMISSION}{"\n"}'
```

Record the starting values. The full run uses these profiles in order:

| Profile | Scope/UI flag | Generic omission flag | Purpose |
|---|---:|---:|---|
| R0 | `false` | `false` | Mixed-version-safe rollout posture |
| R1 | `true` | `false` | Policy UI plus explicit/trusted defaults |
| R2 | `true` | `true` | Final root omission semantics |

Set only these keys in the gitignored local overlay:

```yaml
agent:
  datasourceScopeAutoAttachV1Enabled: "true"
  datasourceDefaultsOnOmission: "false" # R1; change to true for R2
```

For a first startup, set the profile before running
`./scripts/local-dev-tilt-up.sh`. If Tilt is already running, change the local
overlay, run `tilt trigger srw-orchestrator`, wait for the normal downstream
`srw` apply and Reloader rollout, and then re-read the ConfigMap. Do not begin
browser assertions until every relevant pod is Ready and the capability read
matches the profile.

The authenticated `GET /api/users/me/capabilities` response must advertise:

```json
{
  "features": {
    "datasource_scope_auto_attach_v1": true,
    "datasource_defaults_on_omission": false
  }
}
```

with the values changed to match each profile.

---

## 4. Playwright operating pattern

Use the repository-pinned `@playwright/test` package. If the test agent already
has Playwright browser tools, it may use them; otherwise:

```bash
cd cockpit
npm ci
npx playwright install chromium
```

The resulting ignored `cockpit/node_modules/`, browser cache, and per-run
Playwright files are permitted test-harness artifacts, not product changes.
Do not run an unpinned global Playwright install.

Use Chromium with:

- `baseURL: 'https://localhost'`;
- normal TLS verification (`ignoreHTTPSErrors: false`); the bootstrap installs
  the local mkcert CA for Chromium;
- viewport at least `1440 × 1000` for the desktop picker;
- `serviceWorkers: 'block'` for the transient acceptance context;
- trace `retain-on-failure` and screenshot `only-on-failure`;
- artifacts under the ignored `cockpit/test-results/` directory;
- a fresh browser context per user, never shared cookies between users.

Dispose all feature contexts and open fresh ones after each R0/R1/R2 rollout.
Capabilities are loaded during Cockpit bootstrap, so reusing a live SPA can
silently test cached flags from the previous profile.

If Chromium rejects the certificate, record the deployment preflight as
`BLOCKED` and repair the mkcert trust described by `scripts/local-dev-up.sh`.
It is acceptable to repeat functional scenarios once with
`ignoreHTTPSErrors: true` for diagnosis, but label that evidence `TLS BYPASS`;
it cannot turn the deployment preflight into a pass.

There is no generic live-k3d Playwright config in the repository. Put any
throwaway config and spec under
`cockpit/test-results/datasource-scope-autoattach-<run>/runner/`, invoke it from
`cockpit/`, and put its `outputDir` in a sibling `raw/` directory. Do not reuse
`cockpit/e2e/canvas/playwright.config.ts`; that config starts a synthetic
fixture server and does not exercise k3d.

On the first page load, unregister a stale service worker and clear its caches,
then reload. A stale production service worker can otherwise serve an older
Cockpit bundle or intercept session traffic.

Log in to the local Cockpit through the normal Keycloak flow. The bootstrap
account documented by local setup as `test` / `test` is an administrator; use
it only for preflight and explicit admin cases. Run ownership/default tests as
a disposable approved **non-admin owner**, with a different approved non-admin
member for cross-user cases. Do not save storage state in a tracked path, and
keep both passwords out of screenshots and the report.

Authenticate before browser tracing starts. Either perform login in an
untraced setup context and keep its storage state inside the ignored per-run
directory, or start `context.tracing` manually only after the Keycloak form has
completed. Treat storage state as a credential and delete it during cleanup.

Prefer `getByRole`, `getByLabel`, and visible translated text. Stable English
labels for this feature include:

- `New Connector`;
- `Availability`;
- `Everywhere in my work`;
- `Selected projects`;
- `Attach by default to my new work`;
- `Search connectors…` (an input placeholder; use
  `getByPlaceholder(/Search connectors/)`);
- `Automatic`, `Manual`, `Everywhere`, and `Project scoped`.

Do not assert by color alone. Validate badge text and accessible state.
Some custom controls do not expose a native label association consistently.
When an accessible locator is unavailable, keep the fallback scoped to the
feature component, for example:

```ts
page.locator('label.availability-option')
  .filter({hasText: 'Everywhere in my work'}).locator('input');
page.locator('label.project-option')
  .filter({hasText: projectName}).locator('input');
page.locator('label.auto-attach-option input');
page.locator('app-datasources-group label.ds-option')
  .filter({hasText: connectorName}).locator('input');
```

Keycloak's stable local-login controls are `#username`, `#password`, and
`#kc-login`. Never capture the login form in screenshots or traces.

### Authenticated API reads and controlled mutations

Playwright API corroboration must use the same authenticated browser context.
For state-changing `fetch` calls, use same-origin credentials plus headers
`X-CSRF: 1`, `ngsw-bypass: 1`, and `Content-Type: application/json`. Return and
record a bounded object containing status, `detail`, and safe response fields;
never dump cookies or raw headers.

UI actions remain the primary proof for form behavior. Authenticated API calls
are appropriate for fixture setup, exact negative cases, and corroboration.

Track console errors, failed same-origin requests, and uncaught page errors.
Known unrelated third-party noise must be named; it must not be silently
discarded.

---

## 5. Disposable fixture model

Use one unique prefix for every created name. Create two ordinary projects,
Project A and Project B, owned by the connector owner, in addition to that
user's existing default project. Add the disposable member as editor to both
A and B so target-bound sharing can be distinguished from simple lack of
project membership. For the retained-only check, let the member own Project C
and add the connector owner only as an editor; the P1 link scenario describes
the temporary promotion/demotion sequence. Record role changes for cleanup.
Do not rename or delete either user's default project.

Create these generic connectors as the owner. Generic connectors avoid real
credentials and external network dependencies.

Create the project/account fixtures before R0 if needed, but defer this full
connector table until R1. R0 uses one separate `...-R0-CANARY` created through
REST with Project A scope and auto attach enabled; delete it after R0 or record
it explicitly if it is reused.

| Fixture | Scope | Projects | Auto | Purpose |
|---|---|---|---:|---|
| `...-ALL-AUTO` | `all` | none | true | Automatic in every authorized context, including projectless |
| `...-ALL-MANUAL` | `all` | none | false | Available but never preselected |
| `...-A-AUTO` | `projects` | A | true | Main restricted automatic connector |
| `...-A-MANUAL` | `projects` | A | false | Restricted explicit-selection connector |
| `...-AB-AUTO` | `projects` | A and B | true | All-match multi-project control |
| `...-DEFAULT-AUTO` | `projects` | owner's default | true | Default-project is an ordinary scope |
| `...-ZERO-LINK` | `projects` | temporary project/link | true | Later unlink/delete without widening |

For the extended authorization/tier checks, add these only when the necessary
local grant/service is available:

| Fixture | Scope | Projects/links | Auto | Purpose |
|---|---|---|---:|---|
| `...-ALL-AUTO` sharing link | `all` | link only to A | true | Private all-scope grant remains target-bound for the member |
| `...-PUB-ALL-AUTO` | `all` | none | true | Public eligibility; never another user's default |
| `...-PUB-A-AUTO` | `projects` | A | true | Public visibility does not override project scope |
| `...-REPO-A-AUTO` | `projects` | A | true | Lite-tier implicit-filter versus explicit-reject behavior |

Publishing requires the existing `public_datasources` grant. If the ordinary
owner lacks it, the admin may add a temporary **user-scoped** grant for that
owner through the normal Admin UI, record it, and remove it during cleanup;
do not modify a global or project grant. A repository fixture must use a
disposable local Gitea repository and no reusable token. If either prerequisite
cannot be established safely, mark only those extended rows `BLOCKED`.

Projects may already have native knowledge connectors. Treat them as an
intentional baseline: calculate the complete expected selection from the
server's `default_selected` values and separately assert the fixture IDs. Do
not hardcode “only one connector is selected.”

The private A/AB links are the sharing grants; do not publish those connectors
merely to make the member test pass. A private all-scope connector is visible
to its creator everywhere but becomes available to the member only in an
explicitly linked target project.

### Ordinary-user prerequisite

Use two already provisioned, approved non-admin local test accounts and provide
their credentials through `SRW_E2E_OWNER_USERNAME`,
`SRW_E2E_OWNER_PASSWORD`, `SRW_E2E_MEMBER_USERNAME`, and
`SRW_E2E_MEMBER_PASSWORD`; never echo those variables. Confirm each with
`GET /api/auth/me`, and record only its application user ID and
`is_admin=false`. Do not create a Keycloak user during this run: first login
also provisions a default project, and the supported APIs intentionally refuse
to delete default projects, so that approach cannot meet this runbook's cleanup
contract.

If two suitable accounts are unavailable, admin-only form and transport checks
may continue. Mark every creator/member ownership, wrong-owner default,
retained-only, and authority-loss row `BLOCKED` with prerequisite “two
pre-provisioned ordinary local users.” Do not emulate either user with the
bootstrap administrator.

---

## 6. R0 — rollout-safe disabled state

With both flags false:

- [ ] Capabilities advertise both feature values as false.
- [ ] Connector management still loads and legacy create/edit remains usable.
- [ ] The availability and auto-attach controls and new policy catalog filters
      are hidden.
- [ ] A create/update request from the legacy UI does not send `scope_mode`,
      `project_ids`, `auto_attach`, or `policy_revision`.
- [ ] Seed one disposable, eligible `auto_attach=true` scoped policy through
      the current REST API. Edit only its name/description through the
      rollout-hidden legacy form, and confirm the omitted policy fields
      preserve scope, links, auto flag, and revision.
- [ ] The hidden UI flag is not an authorization flag: the seeded scoped
      connector remains absent outside its project and direct out-of-scope
      attachment is denied while R0 is active.
- [ ] Root job/thread omission retains compatibility behavior (no owner
      defaults despite that proven automatic candidate), while explicit
      `datasource_ids: []` remains empty.

Do not create the full fixture set until R1 is active.

---

## 7. R1 — policy UI and trusted defaults

Enable the scope/UI flag and keep generic omission off.

### P0.1 Shared form controls and validation

Open **Connectors → New Connector**.

- [ ] Availability controls render for each visible user-creatable connector
      type: Generic, Repository, OKF Knowledge Base, PostgreSQL, Neo4j,
      MongoDB, WebDAV, Email, Kubeconfig, SSH Key, Generic file, and MCP Server when
      its separate deployment capability is enabled.
- [ ] On an unscoped new-connector route, neither scope radio is initially
      selected and Save remains disabled until the user makes a deliberate
      choice.
- [ ] Opening `/datasources?project=<A>` as Project A's owner preselects
      Selected projects + A. Project C, where this user is only an editor,
      cannot be newly selected.
- [ ] `Everywhere in my work` accepts no project IDs.
- [ ] `Selected projects` renders a searchable multiselect, selected chips,
      selected count, and pagination control when applicable.
- [ ] Selected-project mode with zero projects cannot be saved and does not
      silently fall back to Everywhere.
- [ ] Auto-attach is independent from scope and shows the correct impact copy.
- [ ] When a publishable connector is public/shared, the copy still states
      that automatic selection applies only to its creator's work.
- [ ] Create and create-then-test send the same policy fields.
- [ ] Editing restores scope, every selected/retained-only project, auto flag,
      and policy revision before Save is enabled.
- [ ] Switching a connector from project scope to Everywhere preserves its
      existing project links unless the user explicitly removes them.
- [ ] A link removal presents the override/knowledge warning.
- [ ] A scope-target load failure shows Retry and cannot widen the connector.
- [ ] Raw create validation rejects `project_ids: null`, projects scope with
      an omitted/empty set, and all scope with a nonempty set.
- [ ] Raw update validation rejects explicit null for `scope_mode`,
      `project_ids`, `auto_attach`, or `policy_revision`, and rejects any
      policy/link change without the loaded `policy_revision`.
- [ ] `project_ids` omission preserves links and their settings; an explicit
      whole-list replacement is atomic and an explicit empty list is accepted
      only when the resulting scope is `all`.

Create the fixture connectors and retain their IDs and first policy revisions.

### P0.2 Management catalog

- [ ] With the project filter cleared, all owner-created fixtures remain
      visible in **Connectors**, including currently unavailable rows.
- [ ] Visibility, Availability, and Auto are distinct columns/badges.
- [ ] Search and the project, availability, automatic/manual, ownership, and
      visibility filters change the server request and produce the expected
      rows.
- [ ] `A-AUTO` can be found through Project A and Project scoped + Automatic.
- [ ] Filtering Project B excludes `A-AUTO` but retains `AB-AUTO`.
- [ ] Clearing filters restores every owner fixture with no duplicates.

### P0.3 Eligibility/default matrix

Read `GET /api/datasources/eligible` with repeated `project_id` parameters.
For each returned fixture, retain only `id`, `name`, `scope_mode`,
`auto_attach`, `policy_revision`, and `default_selected` in evidence.

| Owner context | `ALL-AUTO` | `ALL-MANUAL` | `A-AUTO` | `A-MANUAL` | `AB-AUTO` | `DEFAULT-AUTO` |
|---|---|---|---|---|---|---|
| Project A | present/default | present/manual | present/default | present/manual | present/default | absent |
| Project B | present/default | present/manual | absent | absent | present/default | absent |
| A + B | present/default | present/manual | absent | absent | present/default | absent |
| Default project | present/default | present/manual | absent | absent | absent | present/default |
| No projects | present/default | present/manual | absent | absent | absent | absent |

- [ ] Every row matches the table.
- [ ] No-project is genuinely requested with no `project_id` parameter.
- [ ] The server response, not `auto_attach` alone, is the source of
      `default_selected`.
- [ ] Set the `AB-AUTO` Project A override read-write and Project B override
      read-only. A+B eligibility returns one row and one policy revision, never
      duplicate rows. Eligibility does not expose the effective override; that
      is checked at the session resolution boundary below.

Repeat the safe subset as the member. Owner-created automatic preferences must
never become that member's defaults:

| Member context | Private `ALL-AUTO` linked only A | `A-AUTO` | `AB-AUTO` | `PUB-ALL-AUTO` | `PUB-A-AUTO` |
|---|---|---|---|---|---|
| Project A | present/manual | present/manual | present/manual | present/manual | present/manual |
| Project B | absent | absent | present/manual | present/manual | absent |
| A + B | absent | absent | present/manual | present/manual | absent |
| No projects | absent | absent | absent | present/manual | absent |

If public fixtures are blocked, record those two columns as `BLOCKED` while
still executing the private target-bound cases.

### P0.4 Job creation picker and materialization

Use **Jobs → New Job** and intercept eligibility and create requests.

- [ ] Project A initially checks exactly the response rows whose
      `default_selected` is true. `A-MANUAL` is visible and unchecked.
- [ ] Project B removes A-only connectors and defaults `AB-AUTO`.
- [ ] The default-project picker does not show A-only connectors and includes
      `DEFAULT-AUTO`. For the job form's **No project** option, capture both the
      eligibility request and the created job's effective project: because an
      omitted `project_id` currently resolves to the user's default project,
      the reviewed picker context must resolve the same way. A zero-project
      eligible set submitted into a default-project job is a context-mismatch
      FAIL. Use a zero-project session/eligible call for genuine projectless
      semantics.
- [ ] Switching A → B → A cannot let a late A/B response overwrite the current
      context.
- [ ] A user-touched selection is preserved for retained connectors, new IDs
      initialize from their defaults, and out-of-scope IDs are removed.
- [ ] Reset refetches and restores current server defaults.
- [ ] Deselecting every connector sends `"datasource_ids": []`, creates zero
      `job_datasources` rows, and stores an authoritative empty selection in
      `jobs.context.datasource_selection`.
- [ ] Selecting only `A-MANUAL` sends and persists exactly that explicit ID;
      automatic connectors are not added beside it.
- [ ] Direct job/thread attachment with an out-of-scope connector ID, an
      inaccessible private connector ID, and a nonexistent connector UUID all
      return 403 with the same non-enumerating
      `One or more selected connectors are unavailable` detail and create no
      partial work. Do not apply this expectation to invalid `project_id`
      parameters on `/eligible`, which use normal project authorization.
- [ ] A default Project A job persists the full reviewed/default ID set once.
- [ ] `GET /api/jobs/{id}/datasources`, `job_datasources`, and
      `jobs.context.datasource_selection.datasource_ids` agree.

Cancel or delete disposable jobs after their selection is captured so they do
not consume agent capacity.

### P0.5 Persistent session creation and live settings

Use **Sessions → New Session**, preferably with the `none` workspace tier for
selection-only checks.

- [ ] Project A, Project B, A+B, default-project, and no-project chips produce
      the same eligibility matrix as the API.
- [ ] A+B excludes A-only connectors and includes `AB-AUTO`.
- [ ] Materialize an A+B session with `AB-AUTO`, then corroborate the actual
      attach/resolution boundary through a projection containing only connector
      ID and `project_read_only`. It contains `AB-AUTO` once with
      `project_read_only=true`: the resolver applies `BOOL_OR` across the A
      read-write and B read-only links. Never save or log the raw payload,
      because it can contain connector credentials.
- [ ] Deselect-all sends an explicit empty array and thread metadata stores an
      authoritative empty list.
- [ ] A reviewed default selection is stored once in
      `threads.metadata.datasource_ids` with selection provenance/revisions.
- [ ] The instant/draft session waits for default project and eligibility
      resolution before first send, exposes `Default connectors (N)`, and
      allows opt-out before credentials are attached.
- [ ] Failing the draft eligibility request blocks first-send creation, shows
      Retry, and succeeds only after the current-context retry resolves.
- [ ] Opening live settings seeds from attached IDs, not current auto defaults.
- [ ] Saving Deselect all from live settings persists authoritative
      `metadata.datasource_ids: []`; the next attach/resume keeps it empty and
      does not recompute current defaults.
- [ ] If an attached connector is made unavailable, live settings shows an
      unavailable row and does not silently add, remove, or replace it.
- [ ] A late response for another thread cannot repaint the active thread.

Delete disposable threads after evidence capture.

### P1.1 Defaults independent of generic omission

While R1 still has `DATASOURCE_DEFAULTS_ON_OMISSION=false`:

- [ ] `POST /api/jobs` with `use_datasource_defaults: true` and no
      `datasource_ids` materializes the current owner/project defaults.
- [ ] `POST /api/persistent/threads` behaves the same.
- [ ] An opted-in root job with omitted project resolves the owner's default
      project first and includes `DEFAULT-AUTO`; an opted-in zero-project
      thread includes only all-scope owner defaults.
- [ ] Plain omission without the opt-in stays empty in this compatibility
      profile.
- [ ] Supplying both the opt-in and `datasource_ids` is rejected with 422.

### P1.2 Automation run-now

Create a disabled or far-future cron automation owned by the test owner and
scoped to Project A, then use **Run now** once.

- [ ] The created job belongs to Project A and to the automation owner.
- [ ] It materializes the live Project A defaults at fire time.
- [ ] `A-AUTO` is present, `A-MANUAL` is absent, and no B-only/out-of-scope
      connector is attached.
- [ ] Changing a connector default before a second run affects only the second
      run; it does not mutate the first job.
- [ ] Automation run history points to both jobs and each job's selection is
      independently inspectable.

Delete the automation after stopping/cancelling its disposable jobs.

### P1.3 Project loop

Start a Project A standard loop with `max_iterations: 1`, one simple role, and
the lightest available workspace tier. Stop it as soon as its first job is
captured.

- [ ] The first loop job uses owner defaults for Project A rather than every
      project-linked connector.
- [ ] `A-AUTO` is attached and `A-MANUAL` is not.
- [ ] The selection provenance identifies the loop/system creation path and
      the authoritative owner/project.
- [ ] Stopping the loop prevents additional disposable jobs.

### P1.4 Owner versus project member

As the approved non-admin member of both Projects A and B:

- [ ] The owner's private A-only fixtures are available only in Project A.
- [ ] The owner's `auto_attach=true` does **not** make them
      `default_selected` for the member.
- [ ] The member can explicitly attach an authorized A connector in Project A.
- [ ] The same connector is absent in Project B and projectless contexts, and
      a guessed explicit ID there is rejected.
- [ ] `AB-AUTO` is available in A, B, and A+B but is unchecked for the member.
- [ ] Membership in both projects does not make a private `scope_mode=all`
      connector linked only to A portable into B or projectless work.
- [ ] The member cannot edit connector policy or re-share it to B.
- [ ] An administrator's broad catalog visibility does not add somebody
      else's private connector to the administrator's own defaults.
- [ ] The administrator may explicitly select another user's private connector
      where its scope matches, but the same project-scoped connector is still
      rejected outside that linked project.

### P1.5 Native project knowledge connector

Use a separate disposable project (not A/B/C) whose native knowledge
provisioning succeeded:

- [ ] Its connector is `scope_mode=projects`, linked only to that project, and
      automatic under the project-managed exception.
- [ ] The form renders `Included with project` and locks scope, link set, and
      auto policy.
- [ ] Owner/admin/member policy mutation attempts fail without changing the
      row.
- [ ] Direct deletion returns 409, and attempts to relink or move it to another
      project fail without changing its single-project invariant.
- [ ] Explicit empty selection still materializes an empty datasource set; the
      project's native knowledge remains available through the separate
      implicit project-knowledge path and is not evidence of attachment.
- [ ] Deleting a dedicated disposable project removes its synthetic native-KB
      datasource row rather than leaving an orphan.

### P1.6 Existing work and revalidation

- [ ] Create a job and thread with `A-AUTO` attached, then turn auto-attach off.
      Both existing selections remain unchanged.
- [ ] Narrow or unlink the connector after selection. The materialized IDs and
      revisions remain auditable; the next resume/credential-delivery boundary
      fails closed rather than silently running with fewer connectors.
- [ ] Re-enabling auto-attach affects only subsequently created work.

### P1.7 Project Details and retained-link authorization

- [ ] The member as independent Project C owner can link the ordinary owner's
      public all-scope connector, but cannot make that owner's unlinked private
      connector a candidate merely through catalog visibility.
- [ ] A connector creator with target-project management authority can add a
      private connector link. An ordinary project member cannot transitively
      link/re-share it to another project.
- [ ] The member as independent Project C owner may remove the ordinary
      owner's linked connector from C without acquiring the right to edit its
      connector-level scope or auto policy.
- [ ] Switching a projects-scoped connector to all-scope with `project_ids`
      omitted retains the A/B junction rows and their `read_only`, description,
      and `linked_at` settings. Clearing links is a separate warned action.
- [ ] For Project C, establish a link while the connector creator has owner
      authority, then demote that user to editor. The edit form retains C as
      selected and marks it `Retained only`; unrelated edits preserve it and
      the creator can revoke it, but cannot re-add it after removal.
- [ ] A full-set update racing a Project Details link/settings change returns
      409 rather than erasing the concurrent change.
- [ ] A connector policy update emits one `datasource_policy_updated` security
      event with actor/resource, old/new scope and auto values, project counts,
      and revision, but no project names, configuration, URL, or credentials.

### P1.8 Public/shared policy and response redaction

Run this section only after the ordinary owner has the bounded user-scoped
publish grant described in the fixture setup. Do not grant broad capability.

- [ ] `PUB-ALL-AUTO` is explicitly selectable by other authorized users in
      any context but remains unchecked because its creator is different.
- [ ] `PUB-A-AUTO` is present for a member in A and absent in B/projectless;
      public visibility does not widen its project scope.
- [ ] Owner-facing catalog rows show the Auto policy and full authorized
      project count. Shared non-owner rows say `Project scoped` without leaking
      hidden project IDs or a partial/hidden count.
- [ ] List, get, eligible, error, notification, screenshot, and logs do not
      expose credential fields or sensitive connection-URL components. Use a
      synthetic canary only; never a real secret. The bounded
      `connection_url_redacted` indicator must match the actual redaction.

### P1.9 Workspace-tier boundary

Use `REPO-A-AUTO`, an ordinary automatic generic/KB fixture, and Project A.

- [ ] In a `virtual`/`none` picker, the repository is unavailable for explicit
      selection and is excluded from the submitted array; the ordinary KB or
      centrally indexed connector remains usable.
- [ ] A system-created lite job filters an implicitly defaulted repository
      without failing the otherwise-valid job.
- [ ] Explicit repository selection on a lite backend returns 400 rather than
      silently dropping it.
- [ ] A sandbox/VM-backed job can explicitly select the repository, subject to
      normal authorization and scope.

### P1.10 Other creation paths and immutable selection

Execute only the paths enabled in this local deployment; give each unavailable
surface its own `BLOCKED` reason.

- [ ] A projectless automation gets only owner all-scope automatic defaults.
- [ ] An A-scoped officer/Centurion session and conference session materialize
      the authoritative owner's current A defaults, not the publishing user's
      or another participant's defaults.
- [ ] A real delegated/child job inherits the exact materialized parent/thread
      selection before root defaults. Changing defaults between parent and
      child does not change it, and parent `[]` yields child `[]`.
- [ ] If inherited access is revoked, the child fails before credential
      delivery and the parent is unblocked/fails cleanly; no partial set runs.
- [ ] Benchmark replicas remain explicit-empty or use one deliberately frozen
      benchmark selection; ambient owner defaults are not injected.
- [ ] A system call with no authoritative effective user remains empty rather
      than borrowing an arbitrary connector owner's preferences.

### P1.11 Atomic policy and attachment creation

- [ ] For the A+B create race, first have the ordinary creator promote the
      member to a second Project B owner. Load the form as creator; from the
      member context demote/remove the creator's B-owner authority, then
      submit. The request fails and leaves neither a connector row nor an
      A-only partial link. Restore roles afterward.
- [ ] Load an edit containing a separate disposable project, delete that
      project from its independent owner context, then submit. No partial or
      broadened policy is committed; the prior authoritative state remains
      safe.
- [ ] If an existing attachment-batch fault hook is documented, inject one
      bounded job/thread creation failure. Neither the work row nor a partial
      attachment set becomes dispatch-visible. Otherwise mark this row
      `BLOCKED` and request an explicit release waiver; do not patch source or
      write application tables to simulate it.

### P1.12 Dispatch and attach-time revocation

- [ ] If a documented dispatch hold hook exists, hold a disposable job before
      credential delivery, attach multiple connectors, narrow one connector's
      scope, and release dispatch. The job fails closed with stable
      `connector_unavailable`; no subset reaches the agent. Otherwise mark this
      row `BLOCKED` pending an explicit release waiver.
- [ ] Exercise the same all-or-nothing result at a normal session attach/resume
      boundary. Do not require credentials already delivered to a running turn
      to disappear mid-turn; immediate revocation requires credential rotation
      and workspace reprovisioning.

### P1.13 Final unlink and project deletion never widen scope

- [ ] Remove the final link from `ZERO-LINK` through an authorized Project
      Details path. Its `scope_mode` stays `projects`, project count becomes
      zero, and it is unavailable in every picker.
- [ ] Repeat with deletion of a dedicated disposable project. The cascade does
      not broaden the connector to Everywhere.
- [ ] The ordinary owner still sees and can repair the connector in the
      unfiltered management catalog, where it displays `Unavailable` rather
      than `Everywhere`.

---

## 8. R2 — final omission semantics

Enable both flags and wait for the capability response to report both true.
Use authenticated same-origin requests and cancel created work promptly.

For both root jobs and root persistent threads:

- [ ] Omitting `datasource_ids` materializes available owner defaults.
- [ ] A root job with omitted `project_id` first resolves the owner's default
      project and includes `DEFAULT-AUTO`; a genuinely zero-project thread
      includes only eligible all-scope defaults.
- [ ] `datasource_ids: []` materializes none.
- [ ] A nonempty explicit array materializes exactly those authorized IDs and
      does not merge automatic IDs.
- [ ] `datasource_ids: null` is rejected with 422.
- [ ] `use_datasource_defaults: true` remains valid and produces the same
      default set.
- [ ] Opt-in plus any supplied `datasource_ids` is rejected with 422.
- [ ] Through a real agent/session delegation path, parented work inherits the
      parent's materialized selection before root defaults; explicit empty on
      the child opts out. Public `POST /api/jobs` strips `parent_job_id` and
      `thread_id`, so a raw browser REST call is not evidence for this row.

### MCP live check

If an authenticated local SRW MCP client is available, test it as a distinct
transport rather than treating REST as proof. Run omission once under R1 to
prove the client requests trusted defaults while generic omission is off, and
again under R2:

Use [`tests/mcp_connectors_live_validation.md`](../../tests/mcp_connectors_live_validation.md)
for MCP connector/runtime fixture safety. This run needs only the scope,
default-selection, and explicit-empty transport evidence below; unrelated MCP
transport breadth remains in that dedicated gate.

- [ ] Omitted connector IDs on root/project job and session tools request owner
      defaults (the updated client sends the trusted opt-in).
- [ ] An explicit empty array survives MCP → client → REST unchanged.
- [ ] An explicit nonempty list remains exact.
- [ ] A project-scoped MCP principal cannot select, create, rescope, or default
      a connector outside its authoritative project.
- [ ] MCP connector CRUD exposes `scope_mode`, `project_ids`, `auto_attach`, and
      the new `policy_revision` returned by an update.

If no safe authenticated MCP client is available, mark these rows `BLOCKED` and
state the missing prerequisite. Do not mark them passed from unit tests or REST.

---

## 9. P2 hardening and race checks

Fault injection is allowed only through an already documented, bounded test
hook. Do not patch source, write application tables, scale shared services, or
change secrets to manufacture a failure. Mark a scenario `BLOCKED` when no
such hook exists; name the missing hook rather than substituting unit-test
evidence.

### Stale optimistic update

Open the same connector in two isolated pages and retain the same
`policy_revision`.

- [ ] The first policy update succeeds and advances the revision.
- [ ] The stale second update returns 409, leaves its form populated, and does
      not erase the first update or retained project-link overrides.
- [ ] Two simultaneous writes with one starting revision result in one winner,
      one conflict, and no partial project set.

### Fail-closed browser reads

Use Playwright routing to delay, reorder, or fail only the named endpoint.

- [ ] A delayed Project A eligible response arriving after Project B cannot
      overwrite the Project B picker.
- [ ] A 500 from `/api/datasources/eligible` preserves visible context, shows
      Retry, and disables job/session creation until a successful retry.
- [ ] A 500 from `/api/projects/linkable-datasource-targets` cannot make an
      edit default to Everywhere or enable Save.
- [ ] A delayed response for a previous live thread cannot alter the current
      thread's attached connector state.

### Reconciliation

- [ ] Adding/removing a project link advances `policy_revision`. Observe its
      `(project_id, datasource_id)` queue row only if a documented worker-pause
      or reconciliation fault hook keeps it from draining; otherwise report
      direct enqueue observation `BLOCKED` separately from convergence.
- [ ] The worker either drains the row successfully or leaves a bounded retry
      with attempts/error metadata. No synchronous external-store failure
      rolls back the committed PostgreSQL policy.
- [ ] A rapid add/remove/add converges to the final PostgreSQL state.
- [ ] Prove that a stale claimed generation cannot acknowledge a newer one only
      through a sanctioned claim-race harness; otherwise mark claim-token
      fencing `BLOCKED` rather than inferring it from final convergence.
- [ ] Within the worker's normal polling bound (about 15 seconds), pgvector has
      exactly one deterministic `ds-<first 8 UUID hex characters>` note for a
      live link, reflects a safe name/description update, and removes it after
      final unlink. Neo4j is optional in local k3d and must degrade gracefully.
- [ ] If a documented reconciliation fault hook exists, make one fixture sync
      fail, perform a link update, then release the hook. The PostgreSQL policy
      commits, the queue retains a bounded sanitized retry, and it drains after
      recovery. Otherwise mark this row `BLOCKED`; do not scale pgvector or
      destabilize the shared local stack.

### Pagination

If the cluster can safely hold enough disposable fixtures to cross the
catalog's page limit:

- [ ] `Load more` has no duplicates or gaps.
- [ ] An owned connector older than unrelated rows remains reachable.
- [ ] Authorization and filters apply before pagination; inaccessible/hidden
      raw rows do not consume the caller's visible page limit.

### Migration rehearsal

If a sanitized pre-0082 database snapshot and an isolated disposable database
are already available, apply the normal migration runner and verify:

- [ ] ordinary top-level connectors become all-scope/manual;
- [ ] native project KB rows become their one-project scope/automatic and gain
      a missing canonical link when the marker is valid;
- [ ] ownerless private linked rows remain project-scoped/manual;
- [ ] legacy `datasources.job_id` associations appear in `job_datasources`;
- [ ] every historical job receives an immutable datasource-selection snapshot;
- [ ] rerunning normal startup reports the migration already applied.

If that input is unavailable, mark this section `NOT RUN` and state that only
the current-cluster aggregate invariants were checked. Never point a rehearsal
at the live PVC or downgrade its migration ledger.

---

## 10. Read-only database corroboration

Use only narrow columns and captured UUIDs. Never select `credentials`, full
`config`, `connection_url`, or entire rows.

Run bounded SQL over stdin without allocating a TTY or printing the password.
Place only the needed representative query below between the heredoc markers:

```bash
kubectl --context=k3d-srw -n srw exec -i statefulset/srw-postgres -- sh -lc \
  'PGPASSWORD="$POSTGRES_PASSWORD" psql -v ON_ERROR_STOP=1 \
   -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
-- paste one validated bounded query here
SQL
```

Representative safe queries:

```sql
-- Connector policy and optimistic revision.
SELECT id, name, type, scope_mode, auto_attach, policy_revision, created_by
FROM datasources
WHERE id IN (<captured connector UUIDs>)
ORDER BY name;

-- Canonical project links and retained safe overrides.
SELECT project_id, datasource_id, read_only, description, linked_at
FROM project_datasources
WHERE datasource_id IN (<captured connector UUIDs>)
ORDER BY datasource_id, project_id;

-- Exact job junction plus immutable provenance snapshot.
SELECT j.id,
       COALESCE(array_agg(jd.datasource_id ORDER BY jd.datasource_id)
                FILTER (WHERE jd.datasource_id IS NOT NULL), '{}') AS linked_ids,
       j.context->'datasource_selection' AS selection
FROM jobs j
LEFT JOIN job_datasources jd ON jd.job_id = j.id
WHERE j.id IN (<captured job UUIDs>)
GROUP BY j.id
ORDER BY j.id;

-- Thread materialization. Keep the output limited to datasource metadata.
SELECT id,
       metadata->'datasource_ids' AS datasource_ids,
       metadata->'datasource_selection' AS selection
FROM threads
WHERE id IN (<captured thread UUIDs>)
ORDER BY id;

-- Reconciliation state only for test fixtures.
SELECT project_id, datasource_id, policy_revision, claim_token,
       attempts, next_attempt_at, last_error IS NOT NULL AS has_error
FROM datasource_project_reconcile_queue
WHERE datasource_id IN (<captured connector UUIDs>)
ORDER BY updated_at;
```

Validate UUIDs before substituting them. Do not write directly to these tables
as part of the acceptance run.

For reconciliation only, use the same non-TTY pattern in pgvector:

```bash
kubectl --context=k3d-srw -n srw exec -i statefulset/srw-pgvector -- sh -lc \
  'PGPASSWORD="$POSTGRES_PASSWORD" psql -v ON_ERROR_STOP=1 \
   -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
-- paste the validated bounded query below here
SQL
```

Use the deterministic note IDs calculated from captured connector UUIDs. The
boolean canary proves a safe description update without dumping note content:

```sql
SELECT project_id, note_id, note_type, status,
       content LIKE '%DS-SCOPE-SAFE-V2%' AS has_safe_v2
FROM knowledge_index
WHERE project_id IN (<captured project UUIDs>)
  AND note_id IN (<captured ds-XXXXXXXX note IDs>)
ORDER BY project_id, note_id;
```

---

## 11. Result matrix

| ID | Scenario | Level | Result | Evidence / issue |
|---|---|---:|---|---|
| MIGRATION | Applied migration and current invariants | P0 | **PASS** (after local repair) | All 5 aggregate invariants 0. Repair required first — Finding 1. |
| R0 | Disabled rollout posture | P0 | **PASS** 6/6 | `r0.spec.ts`. Caps `false/false`; policy controls absent; legacy name/desc edit preserved scope+links+auto and did **not** bump `policy_revision`; canary absent in B/projectless; out-of-scope attach 403; omission and `[]` both empty despite a live auto candidate. |
| R1-FORM | Shared controls and validation | P0 | **PARTIAL PASS** | Validation contract fully proven: all 9 raw create/update probes → 422 with exact messages (R1.4). Controls proven *hidden* in R0. **Positive R1 form walkthrough (radio states, chips, search, pagination, Retry, warnings) NOT RUN.** |
| R1-CATALOG | Management catalog and filters | P0 | **NOT RUN** | Unfiltered catalog read exercised incidentally in P1.13; filter/search/badge assertions not performed. |
| R1-ELIG | Owner eligibility/default matrix | P0 | **PASS** | R1.2 — **0 deviations across all 30 cells**; A+B returned `AB-AUTO` exactly once (no duplicate rows). |
| R1-JOB | Job picker, explicit empty, persistence | P0 | **PASS** | P0.4a–d. Explicit `[]`→none; single manual ID gained no automatic neighbours; out-of-scope *and* nonexistent UUID both 403 with identical non-enumerating detail and **no rows created** (count 4→4); reviewed 5-ID set persisted once. DB: `junction == snapshot`, `agree=true` on all 8 jobs. |
| R1-SESSION | Session picker, metadata, live settings | P0 | **PARTIAL PASS** | Thread contract proven via API: opt-in stored exactly the 5 defaults, explicit `[]` stored authoritative `[]`, `null`→422, opt-in+ids→422, zero-project thread stored **only** all-scope `ALL-AUTO`. **Live-settings UI, draft/instant gating, late-response repaint NOT RUN.** |
| R1-TRUSTED | Trusted defaults with omission gate off | P1 | **PASS** | P1.1/P1.1t. Opt-in materialized defaults while plain omission stayed empty; root opt-in resolved the owner default project and attached `DEFAULT-AUTO`+`ALL-AUTO` only. |
| R1-AUTO | Automation run-now | P1 | **NOT RUN** | No disposable automation created. |
| R1-LOOP | Project loop first job | P1 | **NOT RUN** | No disposable loop started. |
| R1-MEMBER | Owner/member/admin boundaries | P1 | **PARTIAL PASS** | R1.3 + R2.6 — member matrix 0 deviations; **every** owner `auto_attach=true` fixture returned `default_selected=false` for the member; member omission attached only the project's own native KB. **Member policy-edit / transitive re-share / admin-explicit-override NOT RUN.** |
| R1-NATIVE | Native project KB invariant | P1 | **PASS** | P1.5. `projects`+auto+single link; rescope 409, relink 409, delete 409, row unchanged; project deletion cascaded the synthetic row with no orphan. |
| R1-EXISTING | Existing-work immutability/revalidation | P1 | **PASS** | P1.6. Existing selection unchanged after auto off (rev 2→3); a new job correctly dropped it (5→4 defaults). Fail-closed delivery-boundary revalidation NOT exercised. |
| R1-LINKS | Project Details and retained-only links | P1 | **NOT RUN** | Needs Project C + owner→editor demotion sequence. |
| R1-PUBLIC | Public/shared semantics and redaction | P1 | **BLOCKED** | Ordinary owner lacks `public_datasources`; no temporary user-scoped grant created. |
| R1-TIER | Lite repository/KB boundary | P1 | **NOT RUN** | No repository fixture (needs a disposable Gitea repo). |
| R1-CREATORS | Other system creation paths/inheritance | P1 | **BLOCKED** | Public `POST /api/jobs` strips `parent_job_id`/`thread_id`, so REST is explicitly not evidence; no agent/session delegation path driven. |
| R1-ATOMIC | Authority loss/deletion/batch atomicity | P1 | **NOT RUN / BLOCKED** | Authority-loss race not run; batch-fault row has no documented hook. |
| R1-DISPATCH | Dispatch/session revocation boundary | P1 | **BLOCKED** | No documented dispatch-hold hook. |
| R1-ZERO | Last unlink/project deletion | P1 | **PASS** | P1.13. After final unlink: `scope_mode` stayed `projects`, links `[]`, absent from all 3 eligible contexts, still in the unfiltered catalog. **Never widened to Everywhere.** |
| R2-REST | Root omission/empty/nonempty/null | P1 | **PASS** 7/7 | `r2-omission.spec.ts`. Omission→defaults; omitted project resolved owner default; `[]`→none; nonempty→exact, no merge; `null`→422; opt-in same set; opt-in+ids→422. |
| R2-MCP | MCP transport and project principal | P1 | **BLOCKED** | No authenticated local MCP client. REST is not substitute evidence. |
| P2-REV | Stale revision / atomic set | P2 | **PASS** (partial) | R1.5 — stale revision → 409 `Connector policy changed; reload it and try again`; first write survived. Genuinely simultaneous two-writer race NOT run. |
| P2-RACE | Delayed and failed browser reads | P2 | **NOT RUN** | Requires route interception against the pickers. |
| P2-RECON | Durable reconciliation | P2 | **PARTIAL / BLOCKED** | Queue drained to 0 rows with no error state after all link churn (convergence observed). **pgvector note check unrunnable — Finding 3.** Claim-token fencing BLOCKED (no sanctioned harness). |
| P2-PAGE | Filtered pagination | P2 | **NOT RUN** | Needs enough fixtures to cross the page limit. |
| P2-MIGRATION | Isolated pre-0082 rehearsal | P2 | **NOT RUN** | No sanitized pre-0082 snapshot or isolated disposable database. |

### Final verdict

- **Verdict:** **BLOCKED** — not a full live acceptance. No product assertion
  failed anywhere (35/35 passed), but P1 rows R1-PUBLIC, R1-CREATORS,
  R1-DISPATCH and R2-MCP are `BLOCKED` and R1-AUTO, R1-LOOP, R1-LINKS, R1-TIER,
  R1-ATOMIC are `NOT RUN`. Per section 1 the feature is live-accepted only when
  every P0 and P1 row passes, and the test agent cannot self-accept a block.
- **P0 summary:** MIGRATION, R0, R1-ELIG, R1-JOB pass outright. R1-FORM and
  R1-SESSION pass their contract/validation halves but their UI walkthroughs did
  not run. R1-CATALOG did not run.
- **P1 summary:** R1-TRUSTED, R1-NATIVE, R1-EXISTING, R1-ZERO, R2-REST pass.
  R1-MEMBER passes its security-critical half. The rest are blocked or not run.
- **P2 summary:** P2-REV passes; P2-RECON shows convergence but its pgvector
  assertion is unrunnable as written; the remainder did not run.
- **Security-impacting findings:** **None.** The three properties most likely to
  leak all held: no cross-owner default expansion (the owner's four
  `auto_attach=true` connectors never defaulted for the member, in eligibility
  *or* in R2 omission), no cross-project availability (project-scoped rows absent
  in B and projectless, direct attachment 403), and no silent widening (final
  unlink and project deletion both preserved `scope_mode=projects`). Unavailable,
  inaccessible, and nonexistent IDs are indistinguishable to the caller.
- **Non-security findings:** Findings 1–3 below.
- **Environmental blocks:** authenticated MCP client; `public_datasources` grant
  for an ordinary owner; documented dispatch-hold and batch-fault hooks;
  sanitized pre-0082 snapshot.
- **Leaked test fixtures:** **None.** Every recorded ID deleted (all `200`); 0
  leftover connectors/projects/queue rows/orphans; jobs returned to exactly 85
  and users to 7. Both rollout keys restored to **absent**; ConfigMap and
  orchestrator env back to `false/false`.
- **Recommended next action:** close the P1 gaps — MCP transport, the automation
  and loop creation paths, retained-only links, and a real delegation
  inheritance test — before calling this live-accepted.

### Findings

**Finding 1 — local migration ledger diverged from the committed 0082
(environmental; blocks any k3d run of this feature).** The orchestrator built
from `aec2e5da` crash-looped with `checksum changed:
0082_datasource_scope_auto_attach.sql`. `0082` had been applied at 10:27Z from an
in-flight build (`sha 21e8d982`) and the file was finalized differently before
commit (`sha 185d6de8`). The applied version was materially older, not merely a
different hash: it lacked `datasource_project_reconcile_generation_seq`, the
`datasource_project_reconcile_queue.claim_token` stale-worker fence, and the
immutable job-selection backfill — `missing_job_selection_snapshot` was 85 of 85
where section 3 requires 0. A pre-commit pod kept serving, so the cluster
*looked* healthy while the committed code could not boot. Repaired locally by
applying only the delta (sequence, column, committed trigger body, the job
backfill) and then correcting the recorded checksum; job `context` keys went
521 → 606, exactly +85, with nothing lost. **This is local dev debris only** —
`0082` has never been applied on dev or prod, so the committed file applies
cleanly there. No product change is warranted; do **not** write a superseding
`0083`.

**Finding 2 — section 3's Tilt guidance does not match this deployment.** The
runbook directs the agent to `tilt trigger srw-orchestrator`; that resource does
not exist here (`tilt get uiresources` lists only `srw`, and triggering `srw` is
the forbidden uninstall path). Tilt's own file watch picked the overlay change up
unaided, but took ~360 s the first time. Section 3 should name the real resource
or state that editing the overlay and waiting for reconciliation is the supported
path.

**Finding 3 — section 10's pgvector snippet is unrunnable as written.** It
connects with `-d "$POSTGRES_DB"` against `statefulset/srw-pgvector`, but that
instance has no database named `srw`, so the `knowledge_index` note assertions
for P2-RECON cannot execute. Substitute the correct database name in section 10.

---

## 12. Cleanup and restoration

Cleanup uses the recorded IDs, not broad name globs.

1. Stop the disposable project loop and cancel its spawned jobs.
2. Delete the disposable automation.
3. Delete disposable threads and jobs where the API permits it.
4. Delete only the recorded test connectors.
5. Revoke the exact temporary user-scoped `public_datasources` grant, if this
   run created it, through the normal Admin UI/API and confirm it is absent.
6. Delete Project C while the member remains its owner; do not try to remove
   its last owner first. For A/B, restore the ordinary creator's owner role if
   a race test removed it, remove the member, then delete the projects from an
   authorized owner/admin context. Delete any remaining native-KB or zero-link
   project and only the recorded disposable Gitea repository.
7. Restore the two original values in `deployment/values-local.yaml`, reapply
   through the safe Tilt image-resource path, and confirm the ConfigMap values.
8. Confirm no pods are crash-looping and no active test loop/automation remains.
9. Delete Playwright storage-state files and raw traces after extracting
   sanitized failure evidence. Sanitized screenshots/reports may remain under
   the ignored artifact directory; do not stage them.
10. Run `git status --short` and confirm the test did not add or alter tracked
    files except this runbook's explicitly authorized result update, if any.

Do not delete or alter either pre-provisioned ordinary account, its default
project, or the bootstrap administrator.

If cleanup of one recorded fixture fails, do not broaden the deletion target.
Report its exact safe ID and the API error for manual follow-up.
