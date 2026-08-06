---
tags:
  - issue
  - embeddings
  - citation-engine
  - research
  - reliability
related:
  - "[[reranker_transient_fault_hard_fails_job]]"
  - "[[overnight_minimax_m3_scholar_batch_2026-08-03]]"
aliases:
  - embedding backend batch limit 64
  - citation auto-embed 422 storm
  - source embeddings silently skipped
---

# Oversized embedding batches silently skip CitationEngine source embeddings

**Filed:** 2026-08-04 from the five-job main-cluster overnight Scholar batch.

**Status:** **FIX SHIPPED 2026-08-06 (batch #3) — operational backfill still
owed.** Fixes 1–5 built at the single seam: `EmbeddingService.embed_batch`
now splits at a configurable provider cap (`EMBEDDING_MAX_BATCH_SIZE`,
default 64) with global order preserved, retries transient classes only
(shared `llm_retry` policy; deterministic 422/400 pinned via `never_retry`),
rejects NaN/Inf vectors with typed `EmbeddingInvalidVectorError`, and the
CitationEngine persists per-source `metadata.embedding_state`
(complete/failed + typed reason) surfaced in `get_statistics()` as
`sources.embedding_coverage`. Fix 6 shipped as
`scripts/backfill_source_embeddings.py` (dry-run default / `--apply`).
Verified: `tests/test_embedding_batch_limit.py` 13/13 against a mock
64-cap-422 provider (TEI-exact), and the backfill dry-run against the k3d
vector DB reported the live gap (54 (job, source) pairs across 5 jobs).
**Remaining:** run the backfill with `--apply` against the main cluster
(where the 359 skipped sources live) and re-run a source-heavy job to
confirm zero oversize 422s (final acceptance criterion).

## Live evidence

The five archived worker logs contain 380 CitationEngine auto-embedding
failures across 359 unique source IDs:

| Job | Auto-embed failures |
|---|---:|
| control | 35 |
| 10-turn readers | 86 |
| 24-turn readers | 48 |
| paper review | 43 |
| web comparison | 168 |
| **Total** | **380** |

Of those failures:

- 374 were deterministic HTTP 422 responses because the request contained more
  than the embedding backend's maximum 64 inputs;
- attempted batch sizes ranged from 65 to 1,458 (mean approximately 157);
- five returned vectors containing `NaN`, rejected by pgvector; and
- one received HTTP 429 `Model is overloaded`.

The batch registered 1,083 sources in total. At least 359 distinct registered
source records—about one third of that total—therefore missed their automatic
embedding during the run. Some source IDs were retried and failed more than
once, explaining the 380 warning lines.

The failure is logged as:

```text
Auto-embed failed for source [N], skipping:
422 ... batch size N > maximum allowed batch size 64
```

It is not represented in normal citation statistics, which continue to count
the source as registered.

## Source-level cause

`CitationEngine._embed_source_content()` chunks the complete source and passes
the entire chunk list to one call:

```python
chunks = self._get_chunker().chunk(content)
embeddings = await service.embed_batch(chunks)
```

`EmbeddingService.embed_batch()` forwards that list as one OpenAI-compatible
embeddings request. It has no provider-batch limit, no splitting, and no retry
for a transient 429. The deployed TEI-compatible backend rejects more than 64
inputs.

`_auto_embed_source()` then catches every exception and logs “skipping.” It does
not persist an embedding-status/error field, schedule a bounded retry, or mark
the source as absent from semantic search. Registration therefore succeeds
while its search index silently remains incomplete.

This exact transport limit was previously noted as a related defect in
`reranker_transient_fault_hard_fails_job.md`; the overnight run shows it is not
limited to a background KB reindexer. It affects foreground CitationEngine
research at high frequency.

## Consequences

- `search_library(mode="semantic"|"hybrid")` cannot retrieve the missing
  source chunks through the vector channel.
- A source count or citation count does not describe actual index coverage.
- Large pages produce deterministic 422 traffic and may be retried by later
  registration/reindex paths, creating avoidable backend load.
- Direct URL citations can still verify, so the defect is easy to miss in a
  successful report.
- `NaN` vectors and 429 overload are collapsed into the same generic “skipping”
  state as deterministic oversize requests, preventing targeted recovery.

## Fix direction

1. Give `EmbeddingService.embed_batch()` a configurable provider maximum
   (64 for the deployed endpoint), split inputs into bounded batches, and
   preserve global result ordering.
2. Apply the same batching seam to every caller (CitationEngine, memory, KB
   indexing) instead of repairing each caller independently.
3. Retry only transient classes such as 429/5xx/timeouts with a small bounded
   policy. Do not retry deterministic validation failures unchanged.
4. Validate vectors for finite values before database insertion. Surface and
   count invalid-vector responses separately.
5. Persist per-source embedding state (`complete`, `pending`, `failed` plus
   typed reason) and expose index coverage in citation stats/operator telemetry.
6. Backfill/reindex source IDs that were registered successfully but have no
   `source_embeddings` rows after deployment.

## Acceptance criteria

- A source producing 1,458 chunks is embedded as ordered batches no larger than
  the provider limit.
- Partial batch failure cannot misalign chunks and vectors.
- Transient overload is retried within a bound; structural 4xx is not looped.
- Non-finite vectors are rejected with an explicit typed metric/state.
- Citation/source statistics expose registered-vs-embedded coverage.
- Re-running the five-job source volume produces zero oversize 422 responses
  and no permanently unindexed source without an operator-visible reason.
