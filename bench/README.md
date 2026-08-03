# bench/ — the paired-run measurement suite

The instrument behind the guardrail-loosening loop: a fixed set of job
templates with machine-checkable outcomes, a submitter that pins inputs and
fires replicates against the dev cluster, and a reporter that computes the
phase/ceremony metrics from `docs/issues/phase_model_overhead_amnesia_loop.md`
§10. Protocol and rationale: `docs/features/worker_runtime_strategy.md` §9.

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
```

Runs land in `bench/runs/<run-id>/` (gitignored): `manifest.json` written
incrementally by the submitter, `report.json` by the reporter.

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
