# bench/ — the paired-run measurement suite

The instrument behind the guardrail-loosening loop: a fixed set of job
templates with machine-checkable outcomes, a submitter that pins inputs and
fires replicates against the dev cluster, and a reporter that computes the
phase/ceremony metrics from `knowledge-base/knowledge/issues/phase_model_overhead_amnesia_loop.md`
§10. Protocol and rationale: `knowledge-base/knowledge/features/worker_runtime_strategy.md` §9.

The core rules the tooling encodes:

- **Pinned inputs.** Every job pins the model (`config_override.llm.model`)
  and runs `autonomy: full` so nothing parks in `pending_review`. Task text
  is fixed in `tasks.yaml` — never edit a task in place; add a new id.
- **Outcomes over vibes.** Tasks declare `required_deliverables`, which the
  orchestrator's deliverable gate enforces at `/complete` — so
  `status=completed` *is* the outcome check.
- **One variable per comparison.** An A/B run changes exactly one thing
  between arms (a config key, a prompt file). The first use is no A/B at
  all: baseline replicates to quantify within-config variance.
- **Exclusion rules.** Jobs that die on infrastructure (provisioning,
  provider outage) are excluded, not counted as failures of the config.
  Jobs with >15% of requests after their last phase archive are flagged
  `tail_anomaly` (two-episode jobs — see the `8302c195` case in issue doc
  §10) and reported separately.

## Quickstart

```bash
export SRW_API_URL=https://api.srw.works       # dev-cluster orchestrator
export SRW_TOKEN=<mcp token>                    # X-MCP-Token value

# Baseline: every task × 3 replicates, ≤2 jobs in flight, pinned model
python bench/submit.py --run-id baseline-01 --replicates 3

# Watch / resume after interruption (already-submitted pairs are skipped)
python bench/submit.py --run-id baseline-01 --replicates 3 --resume

# Report (works on partial runs; terminal jobs only in group stats)
python bench/report.py --run-id baseline-01

# Or hand the same resolved task set to the server-side Job Bench. The returned
# UUID is the durable run id used by the report command; no laptop process stays
# alive after submission.
python bench/submit.py --server --run-id baseline-01 --replicates 3
python bench/report.py --server --run-id <returned-run-uuid>
```

Runs land in `bench/runs/<run-id>/` (gitignored): `manifest.json` written
incrementally by the submitter, `report.json` by the reporter.

In `--server` mode the frozen spec and submission ledger instead live in the
orchestrator's `bench_runs` row. The CLI reads and resolves `tasks.yaml` before
posting it; the server never reads a mutable task registry.

## Task families

| prefix | family | config | what it exercises |
|---|---|---|---|
| S* | small | worker_base | the ceremony floor (plan → tiny execute → review) |
| M* | medium | worker_base | multi-part deliverables, single job |
| D* | dev | developer | multi-phase spec/red/green flow, self-contained code katas |
| R* | research | scholar | exploration sweeps + subjob spawning (noisy by design) |
| A* | automation-shaped | worker_base | the recurring-digest shape, inputs inlined |

All tasks are self-contained (inputs embedded in the description) so the
task itself cannot drift between runs. Real-repo fix tasks are deliberately
absent from v1 — they change as the repo changes, which breaks pinning.

## Known limitations (v1)

- **Project memory couples replicates.** Jobs in the same project share
  Memory-Light recall, so replicate 3 can recall replicate 1's memories.
  v1 accepts this (it matches production behaviour); pass `--project-id`
  to scope a run to a dedicated project, and interleaved submission order
  spreads any order effect across tasks. Per-arm projects are the plan for
  real A/Bs.
- **Scholar tasks are high-variance** (subjob fan-out). One task in the
  set on purpose; don't let R* dominate group medians.
- **Cost attribution is main-loop only.** The reporter counts
  `call_type=main` requests; auxiliary calls (memory extraction, curation)
  are not in the ceremony numbers, matching the §10 methodology.

## Operating notes (from `baseline-02`, the first live server-side run)

- **Auth:** the orchestrator wants the token in the `Authorization: Bearer`
  slot (`X-MCP-Token` alone 401s on every route). `bench/_api.py` sends both.
- **Run row shape:** `state` on `/api/bench/runs/{id}` is the submission
  ledger itself — a JSON **list**, not `{jobs: [...]}`.
- **Reading requests:** `/api/jobs/{id}/llm-requests` returns
  `{entries, total, ...}` — parse `entries` (and prefer `total` over
  `len(entries)`); `requests`/`items` do not exist.
- **Ledger staleness:** an entry's `final_status` freezes at the sweeper's
  first terminal observation. Cancelling a job *after* that leaves the old
  label (e.g. `waiting_for_reply`) in the ledger — check the job row for
  truth.
- **`waiting_for_reply` counts as terminal** to the sweeper: a parked job
  frees its in-flight slot; the run does not block on it.
- **Multi-replica orchestrators double-submit:** with 2 replicas the tick
  race fired 3×/30 pairs (twins 2–5 ms apart). Until the advisory-lock fix
  lands (`knowledge-base/knowledge/issues/bench_sweeper_multi_replica_race.md`), watch a running
  run for duplicate (task, arm, replicate) pairs and cancel the younger twin
  (`PUT /api/jobs/{id}/cancel`).
- **Reports heal retroactively:** the report endpoint recomputes from audit
  rows at read time, so a run that finished during an observability gap
  yields full metrics afterwards — nothing is lost by not watching.
- **Mid-flight provider outages** classify as task failures, not infra
  (`knowledge-base/knowledge/issues/bench_infra_exclusion_misses_midflight_outages.md`) — scan
  per-job request timelines for ≫retry-ceiling gaps before trusting a
  failed row.

## Operating notes (from `p4-floor-trim-01` + `s4m2-rerun-01`, 2026-08-07)

- **Two-arm runs:** `submit.py --server` builds a single arm only. For A/Bs,
  build the spec yourself (tasks from `tasks.yaml`, `arms: [{name, model,
  config_override}, ...]`) and POST `/api/bench/runs` directly. The sweeper
  schedules arms adjacent within each replicate's shuffled wave — that
  adjacency is what neutralizes time-varying confounds (pool growth,
  contention), so never split arms across runs or clusters.
- **Running on k3d:** mint the test id_token per the auth memo (admin-cli
  password grant, use `id_token` not `access_token`); `POST /api/projects`
  requires `user_id` in the body. Give memory-sensitive experiments a
  `project_id` in the spec — without a shared project the pool stays empty
  and memory-related treatments measure nothing.
- **`max_in_flight` can be raised mid-run** via
  `jsonb_set(spec,'{max_in_flight}', ...)` on the run row — the sweeper
  re-reads the spec every tick. It is an operational knob, not a pin; note
  the change in the analysis (contention shifts absolute walls). Observed:
  effective concurrency lands ~1 below the setting (dispatch cooldowns +
  pair staggering), so mif 2→4 bought ~1.5×, not 2×.
- **Cross-cluster wall poisoning:** concurrent bench runs on k3d and dev
  share the same LLM server — each inflates the other's walls (~2× per-req
  latency observed). Within-run A/Bs survive (symmetric); single-arm runs
  meant for wall comparison must not overlap another run. Token/request
  metrics are contention-immune.
- **Stuck-watch calibration:** D-family tasks legitimately exceed 3.5 h
  under contention; triage stuck alerts by iteration cadence in agent logs
  (advancing ≈30–60 s/turn = grinding, not wedged) before intervening. A
  bench job struggling on its task is *data*; only infra wedges get
  rescued/excluded.
