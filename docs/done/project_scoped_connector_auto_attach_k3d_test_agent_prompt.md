# Prompt — k3d/Playwright connector scope and auto-attach acceptance

**Status:** EXECUTED 2026-08-05. Results, per-row matrix and findings live in
[`docs/done/project_scoped_connector_auto_attach_k3d_playwright_validation.md`](project_scoped_connector_auto_attach_k3d_playwright_validation.md).
Retained for re-runs: the P1 rows still marked `BLOCKED`/`NOT RUN` there need a
second pass once an authenticated MCP client, a `public_datasources` grant, and
the dispatch/batch fault hooks exist.

Copy everything below the divider into the test agent.

---

You are the acceptance-test agent for Superhuman Remote Worker. Work in:

`/home/ghost/Repositories/Superhuman-Remote-Worker`

Your objective is to live-test the project-scoped connector availability and
auto-attach implementation on the local k3d/Tilt deployment using Playwright.
The implementation baseline is commit `aec2e5da`. Do not implement or fix
anything. Produce evidence-backed PASS/FAIL/BLOCKED results and clean up only
your disposable fixtures.

Before acting, read completely:

1. `AGENTS.md`
2. `docs/done/project_scoped_connector_auto_attach_k3d_playwright_validation.md`
3. `docs/features/project_scoped_connector_auto_attach.md`, especially the
   locked semantics, rollout, testing, and acceptance sections
4. `CLAUDE.md` local k3d/Tilt workflow and its known traps

Treat the validation runbook as the authoritative checklist. Run all P0 and P1
scenarios that the local deployment supports, plus every feasible P2 scenario.
Do not stop after a happy-path UI walkthrough.

Hard safety rules:

- Target only Kubernetes context `k3d-srw`, namespace `srw`, and local origins.
  Stop if any resolved context points elsewhere.
- Preserve the dirty worktree and index. Do not stash, reset, clean, format,
  commit, push, or change product source. Other work is in progress here.
- You may edit only the two datasource rollout booleans in the gitignored
  `deployment/values-local.yaml`, and you must record and restore their original
  values without printing any other local configuration. You may also create
  throwaway Playwright runner files only under the ignored per-run artifact
  directory named by the runbook. The pinned `npm ci`/Chromium setup may write
  only its normal ignored dependency/browser caches. Never place runner files
  in product/e2e source.
- Never run `tilt trigger srw`; it uninstalls the release before reinstalling.
  Use the safe image-resource trigger described by the runbook when a values
  re-render is needed.
- Never delete the cluster, namespace, Helm release, PVCs, default project,
  bootstrap user, or pre-existing data.
- Use generic disposable connectors with a unique `DS-SCOPE-E2E-<timestamp>`
  prefix. Delete only IDs captured during this run.
- Never print, quote, stage, or share cookies, bearer tokens, authorization
  headers, connector credentials, local overlay secrets, or full unfiltered
  logs. Raw traces/storage state are the temporary ignored-artifact exception
  governed by the evidence and cleanup rules below.
- Do not query or dump datasource credential/config/connection columns.
- Do not patch code when you find a failure. Minimize it, preserve bounded
  evidence, continue independent checks, and report it.

Execution requirements:

1. Record Git SHA/status, prove `aec2e5da` is an ancestor, and record the exact
   deployed image IDs, Helm revision, cluster readiness, migration 0082, and
   current values of the two non-secret rollout flags. Also record dirty paths
   overlapping Orchestrator/Cockpit/MCP and Tilt live-update state; do not call
   a live-updated dirty deployment a pristine test of the image SHA. Run the
   aggregate migration-0082 invariant queries; rehearse an old snapshot only
   if an isolated sanitized database already exists.
2. Exercise rollout profiles R0, R1, and R2 separately:
   - R0: scope UI false, omission defaults false;
   - R1: scope UI true, omission defaults false;
   - R2: both true.
   Wait for ConfigMap, capabilities, pods, and Tilt to agree before testing a
   profile, then start fresh browser contexts so cached capabilities cannot
   cross profiles. Restore each key's original presence/value at the end, not
   merely its effective boolean.
3. Use repository-pinned Playwright Chromium. Prefer an installed Playwright
   browser/MCP capability; otherwise use `@playwright/test` from `cockpit`.
   Configure `https://localhost`, normal TLS verification, a desktop viewport,
   blocked service workers, traces retained on failure, and screenshots only
   where safe. The local mkcert CA is expected to be trusted. A diagnostic run
   with `ignoreHTTPSErrors: true` must be labeled `TLS BYPASS` and cannot pass
   deployment preflight. Authenticate in an untraced setup context and begin
   feature tracing only after Keycloak login. Clear stale service
   workers/caches before the run.
4. Use three fresh browser contexts: the bootstrap administrator and two
   **pre-provisioned** approved non-admin local test accounts (owner and member),
   with credentials supplied through `SRW_E2E_OWNER_USERNAME` /
   `SRW_E2E_OWNER_PASSWORD` and `SRW_E2E_MEMBER_USERNAME` /
   `SRW_E2E_MEMBER_PASSWORD`. Never echo them or reuse cookies between
   principals. Do not create new Keycloak users because their
   auto-provisioned default projects cannot be deleted through supported APIs.
   If both ordinary accounts are unavailable, mark ownership/member
   authorization rows BLOCKED with the exact prerequisite; do not substitute
   admin behavior for creator behavior.
5. Prefer accessible Playwright locators and DOM/network assertions. Do not use
   arbitrary sleeps when a response, locator state, pod condition, or database
   condition can be awaited.
6. Drive the user-visible form, catalog, job picker, session picker, live
   settings, automation, and project-loop paths through the Cockpit where the
   runbook calls for UI proof. Use authenticated same-origin Playwright fetches
   for exact request-shape negatives and corroboration. Cookie-authenticated
   mutations require `X-CSRF: 1`, `ngsw-bypass: 1`, and same-origin credentials.
7. For `/api/datasources/eligible`, record only this run's canary rows plus an
   aggregate count of all other baseline rows; do not dump pre-existing names
   or IDs. Capture only the project plus `datasource_ids` portion of job/thread
   create requests. Prove that Cockpit seeds from `default_selected`, always
   sends an explicit reviewed array, and preserves `[]`.
8. Corroborate created selections through REST and narrow read-only PostgreSQL
   queries. For jobs compare `job_datasources` with
   `jobs.context.datasource_selection`. For threads inspect only datasource
   metadata. Account for project-native KB connectors by computing the full
   expected set from `default_selected`; do not hardcode a single attachment.
9. Test the owner matrix across Project A, Project B, A+B, the default project,
   and no-project. Test the member in A versus B/no-project. Explicitly verify
   that another owner's `auto_attach=true` never becomes the member's default.
   Remember that a root job with omitted `project_id` resolves the owner's
   default project; use zero-project eligibility/session creation to prove a
   truly projectless context.
10. In R1, prove trusted defaults work while generic omission remains off:
    explicit `use_datasource_defaults`, automation run-now, and the first
    project-loop job. In R2, separately prove omission, explicit empty,
    explicit nonempty, null rejection, and opt-in conflict. Prove parent
    inheritance through a real agent/session delegation path; the public job
    REST endpoint strips parent/thread identity.
11. If an authenticated local MCP client is available, run the MCP transport
    matrix, including a project-scoped principal. REST is not substitute
    evidence for MCP. If unavailable, mark only that section BLOCKED.
12. Run the stale-revision, delayed-response, eligibility failure, final unlink,
    reconciliation, native-KB lock, retained-only link, existing-work
    immutability, public/shared, and workspace-tier checks. Exercise the
    disposable officer/conference, benchmark, and dispatch-revocation paths
    when those local surfaces are available; give each unavailable path its own
    BLOCKED reason. Any silent credential expansion, cross-project
    availability, wrong-owner default, stale UI overwrite, partial policy
    write, credential leak, or fail-open delivery is a security-impacting FAIL.
    Use fault injection only through an existing documented hook. Do not patch
    source, write application tables, scale shared services, or alter secrets
    to create a failure; report the missing hook as BLOCKED instead.
13. Monitor page errors, failed same-origin requests, Tilt resource state, and
    identifier-filtered Orchestrator logs. Distinguish product failures from
    environmental blocks, but do not wave either away.
14. Clean up only recorded fixture IDs, restore the original flags, verify no
    disposable loop/automation remains active, and confirm tracked Git state was
    not changed by the test.

Evidence rules:

- Store temporary traces and screenshots only under
  `cockpit/test-results/datasource-scope-autoattach-<run-id>/`.
- Treat raw traces/HARs/storage state as credential-bearing: keep them ignored,
  never print, stage, or share them, cite only sanitized extracts, and delete
  the raw files after triage.
- Screenshots/traces must not capture login or connector credential forms.
- Record safe IDs, response status/detail, expected versus actual connector ID
  sets, and minimal reproduction steps.
- A screenshot is supporting evidence, not a pass by itself.
- Update the runbook's Evidence record and Result matrix only if modifying that
  document is explicitly allowed for this run; otherwise leave files untouched
  and return the same fields in your final response.

Final response format:

An unwaived P0/P1 `BLOCKED` row makes the overall verdict `BLOCKED`; you may
not accept your own block. A product assertion failure makes it `FAIL`.

1. Overall verdict: PASS, FAIL, or BLOCKED.
2. Tested Git SHA, deployed image IDs, rollout profiles, browser version, and
   artifact directory.
3. P0 result table with evidence references.
4. P1 result table with evidence references.
5. P2 result table with evidence references.
6. Findings ordered by severity. For each: exact scenario, expected result,
   actual result, safe IDs, reproduction steps, relevant network/API/DB proof,
   and likely component/source location. Do not propose a code patch unless
   asked later.
7. Environmental blocks and precisely what would unblock them.
8. Cleanup/restoration result, including any leaked fixture IDs.
9. Confirmation that no commit or push occurred and unrelated working-tree or
   staged changes were not altered.

Begin with read-only preflight. Do not ask for confirmation unless a required
credential/account is unavailable or proceeding would require a destructive or
non-local action.
