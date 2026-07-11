# Critic (verification) subjobs fail systemically — three independent bugs (autonomy-ceiling GrantDenied, unroutable jobs-repo clone on inherited VMs, and an API that hides `error_message`)

**Status:** IMPLEMENTED + LOCALLY VERIFIED 2026-07-11 (uncommitted working
tree). Code, migration (`0053_jobs_runner_kind.sql`), regenerated
`schema_current.sql`, API/MCP/Cockpit surfacing, and focused tests all landed.
**Verification:** `ruff check` + `ruff format --check` clean; **80/80** tests
green across `test_capability_grants_api.py`, `test_subjob_inherited_workspace.py`,
`test_internal_auth.py`, `test_formatters_confidence.py`,
`test_get_dispatchable_jobs.py`. The as-built review is in "Implementation
status" below. Root causes were confirmed live on dev (`main` / namespace
`superhuman-remote-worker`).

**Remaining (post-deploy follow-ups, not blockers):**
1. Deploy via CI + live smoke on dev: watch `b9878681`'s next scheduled run —
   its critic + scholar should dispatch instead of GrantDenied.
2. Grant-revocation cleanup (manual, after code is live): revoke the leaky
   `autonomy_ceiling:"full"` grants **except loop owners** — see Finding #2
   "Migration + acceptance test". Needs a heads-up to the affected users first.
3. Confirm the open loop-policy decision (loops stay owner-ceiling-gated) — the
   implementation went with that default (loop spawner left untouched).

Three distinct defects surfaced while diagnosing two failed critics the user
flagged. Decisions:
- **Finding #1** (subjob dispatched onto dead/not-ready inherited workspace →
  jobs-repo clone fails): chosen fix **(b) resolve workspace at dispatch** —
  **already shipped** in commit `5a6f5a49` (deployed ~07-10 22:00); both
  incidents predate it. The residual stale-`ready`-snapshot gap is closed
  locally; remaining work is live verification.
- **Finding #2** (system-spawned lifecycle subjobs forced to `autonomy:full`
  are denied against the owner's ceiling — the dominant failure, 25/30d):
  chosen **runner/owner service-principal split** (over the per-key exemption).
  **Implemented locally.**
- **Finding #3** (`GET /api/jobs/{id}` + MCP hide `error_message`): chosen
  **add the columns to the read path + surface in Cockpit/MCP.** **Implemented
  locally.**

Implementation order + concrete task list at the end ("Implementation plan").

## Implementation status (as-built, 2026-07-11)

Reviewed the working tree against the plan — it matches, including the subtle
safety lines.

- **Task 3 (error surfacing).** `postgres_db.get_job` now selects
  `error_message, error_details, runner_kind`; the two sibling read paths
  (`list_jobs` / `get_job_summary` queries) gained `error_message`. MCP
  `format_jobs` / `format_job_detail` / `format_job_summary`
  (`services/formatters.py`) print the failure reason. Cockpit `job-list`
  renders a `jobs.failureReason` line for failed rows (en + de-DE i18n added).
- **Task 2 (runner/owner split).** Migration `0053_jobs_runner_kind.sql` adds
  `jobs.runner_kind TEXT NOT NULL DEFAULT 'user'` + CHECK
  (`user|lifecycle|service`); `schema_current.sql` regenerated.
  `create_job` carries a `runner_kind` param through INSERT/RETURNING. New
  `_resolve_runner_grants()` returns owner grants with **only**
  `autonomy_ceiling` overridden to `"full"` for `runner_kind=='lifecycle'`
  (admin still bypasses); `_enforce_dispatch_grants` consumes it and all three
  PDP call sites (dispatch/resume/resume-endpoint) pass the job's `runner_kind`.
  Scholar (`main.py:9851`) and critic (`main.py:10640`) stamp `'lifecycle'`;
  **`project_loops.py` is untouched** (loops stay owner-gated, as decided).
  Public `POST /api/jobs` runs `_strip_public_job_reserved_markers` for
  non-internal callers (nulls `parent_job_id` et al., strips reserved
  context/config keys incl. `runner_kind`/`verification_target`); `JobCreate`
  has no `runner_kind` field, so lifecycle is unforgeable.
- **Task 1 (Finding #1 residual gap).** The stale-`ready`-snapshot short-circuit
  in `_resolve_subjob_inherited_workspace` (`main.py:~3244`) is removed, so the
  resolver always re-reads the parent's live state before trusting an inherited
  `ready` snapshot.
- **Tests assert the invariants that matter.** Lifecycle runner allows
  `autonomy:full`; a `user` runner is still denied; **a lifecycle runner
  requesting a VM without the `vm_workspace` grant is still denied on
  `vm_workspace` but not `autonomy_ceiling`** (capabilities stay owner-clamped);
  a public caller's forged `parent_job_id` / `runner_kind:lifecycle` /
  `verification_target` are stripped while legit `config_override.autonomy` is
  preserved (then correctly ceiling-denied).

**Motivating incidents:**
- Critic `42bbf782-bc39-491f-98f6-c421ed8431d0` (parent `7e45c299`,
  "Wissensdatein einspeisen", **container** backend) — failed 2026-07-10
  21:32:28.
- Critic `d8f09a2c-ff26-4051-9f3c-faff7dfd0c5d` (parent `2dbe6854`,
  "Hotel ERP UI Design Research", **VM** backend) — failed 2026-07-10
  20:59:05.

Both died at workspace init with the same recorded error:

```
Failed to clone project jobs repo 'project-<slug>-jobs' — refusing to fall
back to a disconnected git init (work would be lost on teardown). Check
jobs-repo URL reachability from this backend.
```

## TL;DR

Auto-spawned verification (critic) subjobs are the single largest source of
`failed` jobs on dev, and the failures are **not** the critic doing its job
badly — the critic never runs. Over 30 days:

| Class | Count | First → last | Root cause |
|---|---:|---|---|
| **autonomy_ceiling GrantDenied** | **19** (+6 scholar) | 06-19 → 07-10 | Finding #2 (dominant, **not yet fixed**) |
| **VM workspace connection lost** | 12 | 06-16 → 07-04 | VM reachability (adjacent) |
| other | 4 | 06-12 → 06-18 | — |
| **jobs-repo clone failed (init)** | 2 | 07-10 | Finding #1 (**fix shipped `5a6f5a49`; 0 since**) |
| reranker timeout | 2 | 07-05 | `reranker_timeout_kills_loop_job` |
| gpt-5.5 quota cooldown | 1 | 07-07 | model quota |
| LLM no-progress | 1 | 06-30 | — |

Query used:

```sql
SELECT
  CASE
    WHEN error_message LIKE 'Failed to clone project jobs repo%' THEN 'jobs-repo clone failed'
    WHEN error_message LIKE 'config exceeds your capability grants%' THEN 'autonomy_ceiling GrantDenied'
    WHEN error_message LIKE 'VM workspace connection lost%' THEN 'VM workspace connection lost'
    ... END AS error_class,
  count(*)
FROM jobs
WHERE config_name='critic' AND status='failed' AND created_at > now() - interval '30 days'
GROUP BY 1 ORDER BY 2 DESC;
```

The table counts `config_name='critic'` only. Finding #2 is **not**
critic-specific: the same `autonomy_ceiling` failure hits **scholar** subjobs
too (6 more, same root cause) — so the dominant class is really **25**
system-spawned-lifecycle-subjob GrantDenials, not 19. Scholars are rarer purely
because they spawn less often, not because they're exempt.

When a critic dies, the parent is **not** lost: the `unstick_reviewing_parents`
watchdog (30-min grace, `stale_verification_sweeper`,
`orchestrator/database/postgres.py:2997`) flips the parent `reviewing` →
`pending_review` with *"Automated verification did not complete (critic
pipeline died); returned to manual review."* Confirmed live for `7e45c299` at
22:07:40. So the user-visible cost is: **automated verification silently never
happens, and every such job falls back to manual review.**

---

## Finding #1 — subjobs dispatched onto a dead/not-ready inherited workspace fail cloning the jobs repo at init

**Severity:** medium. **Backend:** VM (primary) + container (transient).

**STATUS — largely already fixed (verify + close one residual gap).** The
resolve-workspace-at-dispatch fix (chosen option (b) below) **already shipped**
in commit `5a6f5a49` ("Introduce robust workspace inheritance handling for
subjobs", 2026-07-10 21:01), deployed to dev in `sha-20130c3` ~2026-07-10
22:00. **Both incidents predate it** (`d8f09a2c` 20:59, committed 2 min before
the fix; `42bbf782` 21:32, before the deploy). Empirically, **all 5 jobs-repo
clone failures ever recorded predate the deploy; zero since** (they also hit
`scholar` and `product-qa` subjobs on 07-08/07-09 — never a critic-only bug).
The remaining work is verification + one narrow residual gap (see "Remaining
work" below), not a rebuild.

### Mechanism

A project critic clones the project **jobs repo as its workspace root on the
backend** (`src/core/workspace.py:406-429`,
`initialize_project_workspace`). If `GitManager.clone` returns falsy it fails
loud by design (F29 hardening — it refuses to silently `git init` and lose
every push on teardown):

```python
git_mgr = GitManager.clone(jobs_repo["repo_url"], self._workspace_path, backend=self._backend)
if not git_mgr:
    raise RuntimeError(f"Failed to clone project jobs repo '{repo_name}' — refusing to ...")
```

The jobs-repo URL is the cluster-internal Gitea host
(`http://…@srw-gitea:3000/srw/project-<slug>-jobs.git`). A VM backend is a
tailnet node that **cannot resolve/route `srw-gitea:3000`**. The dispatcher
already knows this and rewrites the URL to the ingress-routable host via
`externalize_gitea_url` — **but only inside the VM-injection branch, which is
gated on the VM being `ready`** (`orchestrator/main.py:1948-1965`):

```python
vm_ctx = _get_vm_context(job)
if vm_ctx.get("status") == "ready" and vm_ctx.get("ssh_host"):
    ...
    # F29: VMs are tailnet nodes and can't resolve srw-gitea:3000 ...
    git_remote_url = externalize_gitea_url(git_remote_url)
    for _repo in repositories_payload or []:
        if _repo.get("repo_url"):
            _repo["repo_url"] = externalize_gitea_url(_repo["repo_url"])
```

`_trigger_verification_on_complete` copies the parent's `context.vm` into the
critic **by value** at spawn time (`main.py:10520-10521`). When the parent VM
is no longer `ready` at critic dispatch — e.g. it was torn down / went
`workspace_unavailable` after the parent froze — the `status == "ready"` guard
is False, the whole VM branch is skipped, the URL is **never externalized**,
and the agent is handed `srw-gitea:3000`. The clone can't reach it → F29
RuntimeError → `failed`.

### Evidence (`d8f09a2c`, live)

Inherited VM context on the critic row:

| field | value |
|---|---|
| `context.vm.status` | `deleted` |
| `context.vm.previous_error` | `workspace_unavailable` |
| `context.vm.ssh_host` | `100.64.24.8` (tailnet) |
| `context.snapshot.error` | `unroutable tailnet target from orchestrator` |
| `context.git_remote_url` | `http://…@srw-gitea:3000/srw/project-68137e29-jobs.git` (NOT externalized) |
| `total_requests` | 0 |

So at dispatch the inherited VM was already `deleted`; the externalization
guard failed; the critic got the internal URL and failed the clone.

### Companion case (`42bbf782`, container) — same error, different cause

`42bbf782` ran on a **container** backend (`workspace_container` pod
`10.42.3.64`), which resolves `srw-gitea:3000` fine, and per
`workspace_intervals` the pod was alive `20:49:39 → 21:33:29` — still up when
the critic ran. Yet it hit the **same** F29 RuntimeError. The likely cause is
transient: the critic was dispatched to a warm-pool agent (`96726062`; its
sibling `2ad4568e` was born 21:18 and reaped together at 21:32:2x) that was
being **drained mid-clone**, so `GitManager.clone` was interrupted. This is an
interrupt, not a routing problem — but it shows the F29 fail-loud path also
turns a transient clone hiccup into a hard `failed` with no retry.

### Chosen solution — (b) resolve the workspace at dispatch (DECIDED 2026-07-11; already shipped in `5a6f5a49`)

The dispatcher resolver `_resolve_subjob_inherited_workspace`
(`orchestrator/main.py:3200`, wired at `main.py:4251`) now runs for **every**
inheriting subjob (scholar **and** critic; comment at `main.py:4245`), covers
**both** container and VM, and:
- re-reads the parent's **live** context and overlays it when the parent
  workspace is `ready` (so the existing VM externalization at
  `main.py:1948-1965` then fires correctly — the `status=="ready"` gate is
  satisfied by the live overlay, not the stale snapshot); and
- returns `("fail", <diagnosable message>)` **at dispatch** when the parent
  workspace is `deleted`/`failed` or the parent is terminal
  (`main.py:3300-3317`) — so the subjob is never sent onto a corpse to fail
  with an opaque clone error on the agent.

For `d8f09a2c` (inherited `vm.status="deleted"`, parent VM dead) this path now
returns `"fail"` at dispatch with *"Parent job … workspace is unavailable …
subjob cannot inherit it"* instead of the clone RuntimeError.

Options (a) "externalize the URL for any VM-intent job" and (c) "bounded retry
on the F29 clone" were **not chosen** — (b) subsumes the routing case (a live
overlay re-satisfies the readiness gate) and converts the transient container
case into a clean pre-dispatch decision, so neither is needed now.

### Remaining work (for the implementing agent)

1. **Verify on live dev** that a critic inheriting a dead VM now fails at
   dispatch with the resolver message (no agent dispatch, no clone attempt).
   Force or wait for a real case; confirm zero new `Failed to clone project
   jobs repo` rows post-`sha-20130c3`.
2. **DONE locally — close the residual "trusts a stale `ready` snapshot" gap.** The resolver
   short-circuits `("proceed")` when the subjob's *own* inherited snapshot is
   already `status=="ready"` (`main.py:3244`) **without re-verifying liveness**.
   If an inherited workspace was `ready` at spawn but the pod/VM died before
   dispatch, the subjob is still sent onto a dead workspace. Tighten to re-read
   the parent's live row (or liveness-probe) before trusting a `ready`
   snapshot, at least for critics spawned at parent-completion (when the parent
   workspace is most likely being reaped). Implemented by re-reading the parent
   row even when the child snapshot says `ready`.
3. **Confirm the healthy-VM path** still externalizes the Gitea URL after the
   resolver overlays a live `ready` parent VM (walk one VM-backed critic
   end-to-end).

---

## Finding #2 — system-spawned lifecycle subjobs (critic + scholar) force `autonomy: "full"` but are admitted against the parent-owner's grants → GrantDenied every time (dominant, 25/30d)

**Severity:** high (19 critic + 6 scholar failures; one scheduled workload
fully losing verification daily). **Backend:** any. **Roles affected:** critic
and scholar (both hardcode `autonomy: "full"`). Curator is the same *class*
(system-spawned lifecycle subjob) and inherits the fix, but does not carry the
hardcode and is not currently a bleeder.

### Mechanism

Both subjob factories hardcode full autonomy:

```python
# critic — main.py:10528, _trigger_verification_on_complete
config_override = {"autonomy": "full", "tools": {"evaluation": ["approve_job", "return_job_with_feedback"]}}
# scholar — main.py:9746, _spawn_scholar_subjob
scholar_override = {"scholar": {"enabled": False}, "verification": {"enabled": False},
                    "curator": {"enabled": False}, "autonomy": "full"}
```

Both are created with the **parent's `user_id`** (`main.py:10557`, `:9771`)
and `parent_job_id` set. At dispatch, `_enforce_dispatch_grants`
(`main.py:3477`) resolves grants against `runner_user_id = job['user_id']` —
i.e. the human owner — and the check (`src/core/capability_grants.py:165-172`)
defaults `autonomy_ceiling` to `"review"`:

```python
grants.get("autonomy_ceiling", "review")
... f"autonomy_ceiling: autonomy '{fragment.get('autonomy')}' exceeds the ceiling"
```

The dispatcher records
`error_message=_grant_violations_detail(gd.violations)`
(`main.py:2185`, `:2359`; message at `main.py:3410`) and fails the job. Any
owner whose ceiling is below `full` gets a **dead-on-arrival subjob** — the
config the system itself injected is rejected by the system's own gate. The
PDP's own docstring flags this as unresolved: *"runner = job owner
(job['user_id']); … spec defers transitive checks."*

Note this is **not a capability leak** — `autonomy_ceiling` gates a *pause
policy* (when to stop for a human), not a power. The dangerous capability keys
(`vm_workspace`, `shell_tools`, `delegation`, `model_selection`, `browser`,
`datasource_tools`) are a separate concern; a verifier that never pauses is
inherent to what verification *is* (forcing it to pause = "review the
reviewer").

### Evidence (live)

- **Both roles fail identically.** Failed jobs with the autonomy violation, by
  `config_name`: **critic 19, scholar 6.** Scholar is not exempt — critics just
  dominate because verification fires on *every* completion while a scholar only
  spawns when research is enabled at creation.
- **Concentrated in one workload.** All 19 critic failures are the same user
  `b9878681` (`project_id` NULL), a different parent each day ~09:xx — a daily
  scheduled job. That user's subjob tally: **critic 22 failed / 4 completed;
  scholar 1 failed.**
- **Clean temporal cutover = enforcement rollout.** `b9878681`'s critics
  *completed* 2026-05-13 → 06-15 and have *failed every run since 06-16* — grant
  enforcement went live mid-June, and every subjob's hardcoded `autonomy: full`
  has tripped the ceiling since. Nothing about the subjobs changed; the gate
  turned on.
- **The current de-facto workaround is the anti-pattern.** 5 users were manually
  granted `autonomy_ceiling: "full"` (scope=user) between 06-26 and 07-09 to
  stop their subjob failures. That also lets those users' *own primary jobs* run
  fully unattended — exactly the leak we want to avoid — and it's whack-a-mole:
  `b9878681` never got the grant (only `shell_tools` + `delegation`, 06-18), so
  it keeps failing. It also merely uncovers the next bug: owner `7241eaa3` got
  `autonomy_ceiling: full` on 07-09, so its critic `42bbf782` on 07-10 cleared
  the autonomy gate and then died on Finding #1 (jobs-repo clone) instead.

### Chosen solution — separate the *runner* principal from the *owner* (service-principal model)

**Decision (2026-07-11):** go with the proper runner/owner split, not the
per-key exemption. It names the concept the system is missing exactly once and
reuses it (it is the same abstraction the ownerless-MCP-jobs issue needs), and
it preserves the capability boundary while lifting only the pause policy.

**Principle.** Split the two identities today conflated in `jobs.user_id`:
- **Owner** (`user_id`, unchanged) — the human the job belongs to. Drives
  attribution, Cockpit visibility, data-scoping, project membership.
- **Runner** — the principal whose grants the dispatch PDP evaluates. Today the
  runner *is* the owner. For system-spawned lifecycle subjobs (scholar / critic
  / curator) the runner becomes a dedicated **lifecycle service principal**.

**Effective runner grants — the crux (do NOT make the principal admin/allow-all).**
An allow-all principal would over-grant in the *other* direction: a critic
could then request a VM/shell/model the owner can't. Instead the principal's
effective grants are the **owner's grants with only the pause-policy axes
elevated**:

```
runner_grants := owner_grants, overridden with:
    autonomy_ceiling = "full"
    permission_mode  = "autonomous"     # only if a lifecycle subjob needs it
# every capability key (vm_workspace, shell_tools, delegation,
# model_selection, browser, datasource_tools) stays EXACTLY the owner's.
```

This encodes the reframe as a first-class rule: pause policy is elevated for
system runners; capabilities stay clamped to the owner. A lifecycle subjob thus
provably can never exceed what the owner's own jobs may do — it only runs
unattended. (It is an explicit override, not a `meet`: `autonomy_ceiling` /
`permission_mode` are `restrict_only` in the catalog and `meet` can only
narrow, so runner elevation is a deliberate policy exception, applied above
`resolve_grants`, not a grant-resolution widening.)

**Non-forgeable runner identity.** Add a job attribute set ONLY by internal
spawn paths — e.g. `jobs.runner_kind` (`'user'` default | `'lifecycle'`),
stamped in `_spawn_scholar_subjob` / `_trigger_verification_on_complete` / the
curator spawn. The dispatch PDP keys the elevation off this column, never off
`parent_job_id`/`config_name`/`context` (all user-settable, see hardening). Add
`runner_kind`/`runner_principal_id` as a nullable column now; the ownerless-MCP
work can extend the same seam.

**PDP change (minimal, `evaluate()` untouched).** Introduce
`resolve_runner_grants(job)` consumed by `_enforce_dispatch_grants`:
- owner `is_admin` → bypass (unchanged);
- `runner_kind == 'lifecycle'` → evaluate against `elevate(owner_grants)` above;
- else → evaluate against `owner_grants` (unchanged).

Only the grant set fed to the PDP changes. The hardcoded `autonomy: "full"` in
the factories then becomes safe (config `full` ≤ runner ceiling `full`).
**Decision: keep the hardcode** — it guarantees the verifier never pauses,
independent of the critic/scholar expert config.

**Locked sub-decisions (2026-07-11):**
1. Storage: a nullable **`jobs.runner_kind` enum column** (`'user'` default |
   `'lifecycle'` | `'service'`) — *not* a separate principal table yet.
2. Elevate **`autonomy_ceiling` only** (to `full`). Leave `permission_mode`
   alone — its default ceiling is already `auto_accept`, which is what the
   subjobs use, so nothing is blocked there today. Revisit only if a lifecycle
   subjob ever needs `autonomous`.
3. **Keep** the `autonomy: "full"` hardcode in the factories (above).
4. **Elevation applies to SUBJOBS only, never to top-level loop jobs.** There
   are three birthplaces of hardcoded `autonomy: "full"`: scholar subjob
   (`main.py:9746`), critic subjob (`main.py:10528`), and the **loop spawner**
   (`orchestrator/services/project_loops.py:729`, which stamps it on every
   loop-role job incl. curator). Stamp `runner_kind='lifecycle'` at the **subjob**
   spawners only (`main.py:9763`, `10551`, + the curator-subjob path if it is
   ever revived — the verification "waiting curator" resume at `main.py:11757`
   is currently dormant, nothing creates it). **Do NOT stamp `project_loops.py:828`.**
   Rationale: a verification/research subjob is a substep of one user-submitted
   job (elevating its non-pausing is inherent); a loop is a standing *unattended
   automation the user set up*, which is exactly what `autonomy_ceiling` is meant
   to govern — so loop jobs stay `runner_kind='user'`, owner-ceiling-gated. (Data
   confirms: all 26 autonomy failures are verification subjobs, `is_loop=f`; the
   sole loop owner `52b14734` doesn't fail only because they hold a full grant.)
5. **Delegation children stay owner-bound** (`runner_kind='user'`). Only the
   scholar / critic / curator *subjobs* get `'lifecycle'`. Delegation is the
   user's own agent fanning out real work and must remain ceiling-clamped.
6. Land **lifecycle-first**; converge with the ownerless-MCP service principal
   when that work starts (shared `resolve_runner_grants`).

> **Open policy question (confirm before the revoke step):** should loops stay
> owner-ceiling-gated (assumed above), or should unattended loops be allowed
> below the ceiling? If the latter, add a distinct "run unattended automations"
> grant rather than overloading `autonomy_ceiling` or `runner_kind='lifecycle'`.
> Current decision: **loops stay owner-gated.**

**Shared with ownerless-MCP jobs.** `docs/issues/mcp_jobs_ownerless_grant_denied.md`
proposes a service principal for jobs with no human owner. Same abstraction: a
non-human runner. Ownerless = `runner_kind='service', owner=system`; subjob =
`runner_kind='lifecycle', owner=human`. Build both through
`resolve_runner_grants`.

**Security hardening (required, ship with or before the change).** The public
`POST /api/jobs` currently passes caller-supplied `parent_job_id`
(`main.py:6784`) and `config_override` straight through (only `user_id` is
forced). Strip/reject caller-supplied `runner_kind`, `parent_job_id`, and
reserved `context`/`config_override` markers on the non-internal path so a user
can never self-declare a system runner. This is the linchpin that keeps the
elevation unforgeable (and closes a latent privilege-escalation seam regardless
of this fix).

**Migration + acceptance test.**
1. Ship runner elevation + create-path hardening.
2. **Revoke the leaky `autonomy_ceiling: "full"` grants — but SPARE loop
   owners.** Of the 5 users granted full (06-26 → 07-09), `52b14734` owns a
   project loop; under decision #4 above loop jobs stay owner-gated, so that
   user legitimately needs the grant — **keep it.** Revoke only the 4 that were
   handed out purely to work around the critic bug: `1898ea71`, `082c4027`,
   `7241eaa3`, `48de2860`. Before revoking, re-derive the loop-owner set live
   (`SELECT DISTINCT user_id FROM jobs WHERE context->>'loop_id' IS NOT NULL`)
   in case more loops exist by then. **Give the affected users a heads-up
   first** — their *own* jobs drop back to the `review` ceiling.
3. Verify those users' critics/scholars still dispatch (via runner elevation)
   while their *own primary jobs* drop back to the `review` ceiling.
4. Verify `b9878681`'s daily job's critic + scholar now dispatch instead of
   failing — the headline metric for this finding.

### Rejected alternatives

- **Per-key exemption (quick-fix).** Skip only the `autonomy_ceiling` /
  `permission_mode` violations for `runner_kind='lifecycle'` jobs inside the
  PDP, capabilities still owner-bounded. Correct and smaller, but a point patch
  that leaves the runner/owner conflation in place and doesn't generalize to
  ownerless-MCP or future system actors. Kept as a fallback if the runner
  column lands later than needed.
- **Clamp subjob autonomy to the owner ceiling + auto-apply the verdict.** Runs
  the critic at `review`/`partial` and reworks completion so its verdict applies
  without a human pause. Autonomy also governs phase-boundary pausing, so this
  introduces pauses we don't want and is invasive to the graph for no security
  gain. Rejected.
- **Grant every user `autonomy_ceiling: full`.** The status-quo workaround.
  Leaks full autonomy to users' own jobs; rejected by definition of the
  problem.

Related: `docs/issues/session_permission_mode_grant_denied.md`,
`docs/issues/mcp_jobs_ownerless_grant_denied.md`.

---

## Finding #3 — `GET /api/jobs/{id}` (and MCP `get_job`) never return `error_message` / `error_details`

**Severity:** medium (observability). This is why a failed job looks reasonless
in Cockpit and over the API, and why the first diagnosis of `42bbf782`
mis-attributed the cause: the API reported `error_message: null` while the DB
row held the real clone error the whole time (both written atomically at
failure, `updated_at == fail time`).

### Mechanism

`postgres_db.get_job` (`orchestrator/database/postgres.py:806-841`) selects an
explicit column list that **omits `error_message` and `error_details`**:

```sql
SELECT j.id, j.status, j.config_name, j.expert_id, j.config_override, j.resolved_config,
       j.assigned_agent_id, j.user_id, j.project_id, j.parent_job_id, j.priority,
       j.branch_name, j.repo_name, j.merge_status, j.repo_merge_statuses, j.freeze_data,
       j.cloud_diff_baseline_commit, j.diff_status, j.exported_folder_handle, j.exported_at,
       j.creation_order, j.worktree_path, j.delegation_context,
       j.created_at, j.updated_at, j.description, j.context, ...
FROM jobs j ...
-- no error_message, no error_details
```

The `/api/jobs/{id}` handler (`main.py:6618`) returns this dict verbatim, so
the fields are absent (serialize as null). MCP `get_job` uses the same read
path, so `get_job` / `get_job_summary` never surface a failure reason either.

### Evidence (live)

```
API  GET /api/jobs/42bbf782... → error_message: None
API  GET /api/jobs/d8f09a2c... → error_message: None
DB   both rows → error_message = "Failed to clone project jobs repo …"
```

### Chosen solution (DECIDED 2026-07-11)

Add `j.error_message, j.error_details` to the `get_job` SELECT
(`orchestrator/database/postgres.py:806-841`) and **surface them in both the
Cockpit failed-job view and MCP** (`get_job` / `get_job_summary` formatters).
Audit sibling read paths (`list_jobs`, `get_job_summary`, MCP formatters) for
the same omission and add the columns wherever a job's failure reason should be
visible. Pure read-path change — no migration, no write-path impact.

---

## Implementation plan (for the implementing agent)

Do them in this order — #3 first (restores diagnosability so you can watch the
others land), then #2 (the actual bleed), then #1 (verify + small gap).
**Verify each locally on the k3d cluster before committing** (CLAUDE.md
"Plan → Develop → Verify"). Run `ruff check src/ orchestrator/ tests/` +
`ruff format` and the relevant `pytest` files at file granularity. **Never
`git add -A`.**

### Task 3 — surface `error_message` (smallest; do first)
- `orchestrator/database/postgres.py:806-841` `get_job()` — add
  `j.error_message, j.error_details` to the SELECT.
- MCP: add both to the `get_job` / `get_job_summary` formatters
  (`orchestrator/services/formatters.py` and/or `orchestrator/mcp/`).
- Cockpit: show the failure reason in the job-detail / failed-job view
  (grep `cockpit/src` for the job-detail component; add an `error_message`
  row shown when `status==='failed'`).
- Audit siblings (`list_jobs`, any other job read path) for the same omission.
- No migration. Tests: assert `get_job` returns `error_message` for a failed
  job; a formatter test. Verify: `curl …/api/jobs/<failed-id>` now returns the
  message; Cockpit shows it.

### Task 2 — runner/owner split (the main fix)
1. **Migration.** New `orchestrator/database/migrations/app/0053_jobs_runner_kind.sql`
   (next number after `0052_cloud_ro_mounts.sql`): add
   `jobs.runner_kind text NOT NULL DEFAULT 'user'` (+ a CHECK or comment for
   `user|lifecycle|service`). **Then regenerate the schema snapshot with
   `scripts/schema-snapshot.sh`** — CI fails without it. Applied at startup by
   `run_migrations()`; see `docs/db_migration.md`.
2. **create_job plumbing.** Add a `runner_kind: str = 'user'` param to
   `postgres_db.create_job` (`postgres.py:843`) and persist it.
3. **Stamp lifecycle SUBJOBS** `runner_kind='lifecycle'` at their create_job
   calls: scholar (`main.py:9763`), critic (`main.py:10551`). Curator-subjob:
   the verification "waiting curator" resume (`main.py:11757`) has no live
   creator today (dormant); stamp it if/when that path is revived. **Do NOT
   stamp the loop spawner (`orchestrator/services/project_loops.py:828`)** — its
   jobs are top-level unattended automations that stay owner-ceiling-gated
   (decision #4), even though it also hardcodes `autonomy: "full"` at
   `project_loops.py:729`. This is the line that keeps the ceiling meaningful.
4. **`resolve_runner_grants(job)`** (new; alongside `_enforce_dispatch_grants`
   in `main.py`, or in `services/grants_service.py`): owner `is_admin` → bypass;
   `runner_kind=='lifecycle'` → owner grants with `autonomy_ceiling` overridden
   to `"full"` (capabilities untouched); else owner grants. Wire it into
   `_enforce_dispatch_grants` (`main.py:3477`) — it replaces the plain
   `resolve_grants_for(...)` result fed to `evaluate()`. `evaluate()` itself
   (`src/core/capability_grants.py:130`) is unchanged.
5. **Public-API hardening** (`create_job` endpoint, non-internal branch at
   `main.py:6656`): drop caller-supplied `runner_kind` and `parent_job_id`
   (force to defaults) and reject/ignore reserved `context`/`config_override`
   markers, so a user can't self-declare a system runner. Add `runner_kind` to
   the `JobCreate` model only if you want it internally settable; otherwise keep
   it off the model entirely.
6. **Tests.** `resolve_runner_grants`: a lifecycle runner gets `autonomy: full`
   but every capability key stays clamped to the owner (e.g. owner without
   `vm_workspace` → lifecycle subjob requesting a VM still denied). PDP dispatch
   test: a `review`-ceiling owner's critic now dispatches. Create-path test: a
   non-internal caller supplying `parent_job_id`/`runner_kind` gets them
   stripped.
7. **Migration/cleanup** (after the code is live + verified): revoke the 5
   leaky `autonomy_ceiling:"full"` user grants (see "Migration + acceptance
   test" above) — **heads-up to those 5 users first**.
8. **Verify on dev:** `b9878681`'s next scheduled run — its critic + scholar
   dispatch instead of failing; a `review`-ceiling user's *own* primary job at
   `autonomy: full` is still denied.

### Task 1 — verify the shipped resolver + close one gap
- Confirm zero new `Failed to clone project jobs repo` rows since
  `sha-20130c3`; watch a real dead-inherited-workspace critic fail cleanly at
  dispatch with the resolver message.
- Close the stale-`ready`-snapshot gap in
  `_resolve_subjob_inherited_workspace` (`main.py:3244`): re-verify the
  parent's live workspace before trusting the subjob's own `status=="ready"`
  snapshot (see Finding #1 "Remaining work"). Test the stale-ready case.

---

## Appendix — dev-cluster forensics recipe

- MCP `main-dev-cluster` = the remote dev cluster, reachable by kube context
  `--context=main -n superhuman-remote-worker` (NOT local k3d, which is a
  separate DB).
- App DB: `srw-postgres-0`, `psql -U srw -d srw`. **Trust the DB, not the API,
  for `error_message`** (finding #3).
- Audit store: `srw-auditdb-0`, `psql -U srw -d srw_audit` (tables:
  `llm_requests`, `agent_audit`, `chat_history`, `usage_events`). A critic that
  fails at init has **0** rows in all of these.
- `workspace_intervals` (owner_id = job id) gives pod/VM start/end times.
- REST: `https://api.srw.works` + `Authorization: Bearer <MCP token from repo
  .mcp.json>`.
- Orchestrator pod logs retain only since the last restart — dispatch-time
  logs for a failure older than that are gone.
