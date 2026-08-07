# kb_reindex's sync DNS retries stall the orchestrator event loop until liveness kills it

**Status:** Open. Observed 2026-08-07 00:31–00:42Z on local k3d during bench run
`e2721227` (p4-floor-trim-01); mitigated locally by seeding the tiktoken cache
into the running container (temporary — dies with the pod). The structural
defect is environment-independent.

## The chain (each link verified live)

1. **KB materialization made agent notes file-backed.** Since `be262a2d`,
   agent-written knowledge notes materialize as vault files. The old hiding
   condition in `reference_k3d_tiktoken_offline_blocks_kb_reindex` ("project
   KBs with only agent-written notes never invoke the chunker") is gone —
   reindex now chunks every project note.
2. **The chunker fetches its tokenizer over HTTP at runtime.**
   `tiktoken.get_encoding("cl100k_base")` downloads the BPE vocab from
   `openaipublic.blob.core.windows.net` on a cold cache — with **synchronous**
   requests/urllib3 retries.
3. **On k3d, that host never resolves** (the split-DNS trap; only
   coredns-custom-rewritten names like `ai.h4ll.app` work). Each note burned
   ~4 s of `NameResolutionError` retries, sequentially, on the orchestrator's
   event loop: log shows one note every 4 s (00:31:47 → :51 → :55 → :59 →
   00:32:03 …).
4. **Pass duration grows with note count.** Running jobs curate notes at every
   phase boundary, and `_kb_reindex_after_job` fires per completion — the more
   the project works, the longer each stall. Unbounded.
5. **Blocked loop → collateral damage in two directions.**
   - `/api/health` timed out → kubelet killed the orchestrator container
     (restart observed ~00:42Z, `Task cancelled, timeout graceful shutdown
     exceeded` in the dying worker).
   - Heartbeat processing stopped → the stale-agent detector orphan-paused the
     in-flight job pair (`d73529f2`/`1dc952ec`, both paused 00:33:07/:11 with
     no error and no freeze_data — the orphan signature). Auto-re-dispatch
     recovered both ~3 min later; walls inflated ~2–3 min each.

Host load was 0.7 throughout — this is pure event-loop blockage, not resource
pressure.

## Why this matters beyond k3d

- On dev/prod, any pod with a cold tiktoken cache during an egress/DNS blip
  reproduces the same stall — and the WAN-outage doc shows exactly such blips
  happen. A **KB maintenance task can preempt running jobs** (orphan-pause) and
  restart the orchestrator; that inversion of priorities is the real bug.
- Even with networking healthy, chunking is sync CPU work on the loop and
  scales linearly with vault size.

## Fixes (smallest that removes the class first)

1. **Bake the vocab into the orchestrator (and agent) images** — set
   `TIKTOKEN_CACHE_DIR` and COPY the encoding at build. Removes the runtime
   network dependency everywhere.
2. **Move the reindex pass off the event loop** (`asyncio.to_thread` around
   the chunk/tokenize step, or a worker task). The loop must never share fate
   with vault size.
3. **Fail the pass fast on the first resolution error** instead of retrying
   per note — one DNS failure predicts the next N; today's per-note retries
   multiply a 4 s penalty by the vault.
4. Optional: degrade to the estimator tokenizer (`len//4`) when tiktoken is
   unavailable, matching the graceful-degradation convention elsewhere.

## Local mitigation applied (2026-08-07)

```
URL=https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken
SHA=9b5ad71b2ce5302211f9c61530b329a4922fc6a4   # sha1(url)
curl -sSL "$URL" -o /tmp/$SHA                  # on the host (has internet)
kubectl -n srw exec $POD -c orchestrator -- mkdir -p /tmp/data-gym-cache
kubectl -n srw cp /tmp/$SHA srw/$POD:/tmp/data-gym-cache/$SHA -c orchestrator
```

Verified: `tiktoken.get_encoding('cl100k_base')` loads in 0.33 s offline
inside the container. Re-seed after any orchestrator pod restart until fix 1
lands.
