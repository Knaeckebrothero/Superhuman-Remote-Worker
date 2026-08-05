# Job Bench: infra-exclusion gate misses mid-flight provider outages

**Status:** Open, analysis-level workaround exists. Found 2026-08-05 during
`baseline-02` (run `885008dc`), the component's first live run.

## What happened

The homelab WAN link dropped ~11:46Z–14:44Z (Cloudflare 530 on ingress, LLM
egress dead, cluster itself healthy throughout). Two bench jobs were mid-flight:

- `D1-wordfreq-kata` r1 (`4c72c3a8`): 178-minute gap in its request timeline,
  one 227s-latency retry at 14:44Z succeeded, job continued normally.
- `S4-csv-totals` r1 (`7eff03bc`): identical gap, retry at 14:44Z returned
  (182s latency), job **failed** immediately after — 18 requests, 0 archives,
  185min wall. Its first 7 minutes were on pace to beat its baseline-01 twin
  (which completed 3/3 at ~31min/90req).

## The gap

`compute_bench_report` classifies `infra` as "never made a first LLM call"
(design doc §5.4). A job that ran fine for minutes and then died of a provider
outage made plenty of calls, so it lands as `classification: job` and its
failure counts against the config in the aggregates — a false regression
signal. Wall/latency metrics of *survivors* (D1 here) are silently poisoned
too (+3h wall, two ~200s latencies).

## Workaround (analysis level)

Manual exclusion, which medians-with-ranges makes cheap: flag any job whose
request timeline contains a gap ≫ the model's retry ceiling, and drop (or
range-annotate) affected replicates. For baseline-02: exclude S4 r1 entirely,
exclude D1 r1 for wall/latency (token metrics arguably still usable), and
eyeball S1 r1 (`37d199ef`, submitted 14:30Z into the outage tail).

## Fix sketch (v1.1)

Per-job max inter-request gap is already computable from the audit rows the
report reads. Add `max_request_gap_minutes` to the per-job report row and an
`outage_suspect` flag (gap > N min, N≈15 default) that:
1. excludes the job from aggregates like `infra` does, and
2. surfaces in the report so a human can override.

Cheap, no schema change, uses data the report already fetches.
