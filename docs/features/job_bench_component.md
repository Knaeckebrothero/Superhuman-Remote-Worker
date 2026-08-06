---
tags:
  - feature
  - orchestrator
  - agent
  - measurement
related:
  - "[[worker_runtime_strategy]]"
  - "[[phase_model_overhead_amnesia_loop]]"
  - "[[automations_v0]]"
aliases:
  - job bench
  - bench component
  - job testing center
  - expert testing
---

# Job Bench — server-side benchmark runs (seed of the job testing center)

> **Goal.** Move the paired-run suite (`bench/`, strategy doc §9) from a
> laptop process into the orchestrator, and shape it so it grows into the
> "test and optimize experts" component without rewrite. A bench run =
> **pinned task set × replicates × arms**, executed and reported entirely
> server-side.

**Status:** v1 SHIPPED and field-proven, 2026-08-06. First live run
(`baseline-02`, run `885008dc`, 30 pairs) validated §5 acceptance: unattended
completion through an orchestrator rollout *and* a 3-hour WAN outage (§5.1),
server-computed latency-based strategic share (§5.2), infra-exclusion
exercised for real (§5.4), k3d create→sweep→report smoke (§5.5). Still open:
§5.3 two-arm run (next: the P-4 floor-trim A/B). Field findings + operating
notes: `docs/issues/phase_model_overhead_amnesia_loop.md` §13,
`bench/README.md` "Operating notes", and the three bench issue docs
(sweeper multi-replica race — fix required before the next unattended run —
infra-exclusion gap, resume-lane brief starvation).

Originally: Design, 2026-08-04. Motivated by two field failures of the
laptop runner in one night (machine shutdown, no home network): the runner
must live where the jobs live. v0 CLI + stopgap pod exist; this component
replaces both.

## 1. Why orchestrator-native (not a standalone runner pod)

Every ingredient already lives in the orchestrator:

- **Job creation** — internal call path, no Cloudflare/WAF, no token
  gymnastics, correct ownership (fixes the laptop runner's auth fragility).
- **Audit reader** — per-request `token_usage` *and* `latency_ms* (the
  public REST summary omits latency; the CLI reporter had to fall back to
  token-share. Server-side reporting restores the latency-based metric).
- **Gitea archive listing** — phase segmentation source, already a service.
- **Background-loop precedent** — `cron_dispatcher`, sweepers; the bench
  sweeper is one more small loop with the same shape.
- **Automations** — a weekly regression canary later = an automation whose
  action is "start bench run", zero new scheduling infra.

## 2. v1 data model (one migration)

`bench_runs`: `id`, `name`, `status` (`running|paused|done|cancelled`),
`created_at/by`, `spec` JSONB, `state` JSONB.

- `spec` = the **frozen** run definition captured at creation: resolved
  task list (full descriptions + `required_deliverables` inlined — the
  pinning story: a run is reproducible from its row alone), `replicates`,
  `max_in_flight`, `arms: [{name, config_name|expert_id, config_override}]`,
  `model` pin per arm.
- `state` = submission ledger (the manifest, moved server-side):
  `[{task, arm, replicate, job_id, final_status}]`.
- Jobs additionally carry `context.bench = {run_id, task, arm, replicate}`
  (already the v0 tagging), so everything is reconstructable from `jobs`
  even if a run row is lost.

Per the DB rules: numbered migration + regenerate `schema_current.sql`.

## 3. v1 surface

- `POST /api/bench/runs` — body ≈ spec above; tasks come inline (the CLI
  reads `bench/tasks.yaml` and sends it; no server-side task registry yet).
- `GET /api/bench/runs` / `GET /api/bench/runs/{id}` — status + ledger.
- `GET /api/bench/runs/{id}/report` — server-computed per-job metrics and
  per-task×arm aggregates: the §10 methodology (phase segmentation from
  archives, strategic share by latency *and* tokens, per-turn floor,
  tail-anomaly flag, medians-with-ranges across replicates).
- `POST /api/bench/runs/{id}/cancel` (and optionally `/pause`).
- **Sweeper**: every ~30s, for each `running` run: refresh non-terminal
  job statuses; while in-flight < `max_in_flight`, create the next
  (task, arm, replicate) with the arm's config + pins. Interleaved
  task order per replicate (seeded shuffle) as in v0.

Routes go in a separate `APIRouter` module included from `main.py` with a
one-line hookup (deliberate deviation from routes-in-main.py: the monolith
is slated for decomposition, and a small self-contained router keeps the
diff surgical). Logic in `orchestrator/services/bench.py`.

`bench/` CLI becomes a thin client (`submit.py` → POST the spec;
`report.py` → GET the report). `tasks.yaml` stays in the repo as the
task-set source of truth for now.

## 4. The extension path (why arms exist from day one)

1. **Expert A/B** — two arms over the same task set (`baseline` vs
   `candidate` expert/config). Already expressible in v1; the report's
   per-task×arm table *is* the comparison.
2. **Regression canary** — automation fires a weekly single-arm run;
   report deltas vs. the previous run of the same name.
3. **Testing center UI** — cockpit page listing runs, arm comparisons,
   per-task drill-down (reads the same report endpoint).
4. **Expert optimization loop** — generate a candidate expert variant
   (config/prompt mutation), bench it as an arm against current, adopt on
   win. This is the RSI-loop organ for *config* changes — the officer
   steers nightly work; the bench scores config changes. Out of scope for
   v1; the data model must simply not preclude it (it doesn't).

## 5. v1 acceptance criteria

1. A run created via `POST /api/bench/runs` completes unattended with the
   laptop off; sweeper survives orchestrator restart (state is in the DB;
   `running` runs resume from their ledger).
2. Report endpoint reproduces the §10 metrics for a finished run,
   including latency-based strategic share (which the CLI could not get).
3. Two-arm run works end-to-end (same tasks, different `config_override`)
   and the report groups by arm.
4. Infra-failure exclusion: jobs that never made a first LLM call are
   reported as `infra` and excluded from aggregates, not counted as task
   failures.
5. k3d: create → sweep → report smoke passes locally before dev deploy.

## 6. Explicitly out of scope for v1

Server-side task registry/versioning, cockpit UI, automations wiring,
auto-diagnosis tagging, statistical testing beyond medians/ranges, and
any auto-optimization. Each lands on the extension path above.
