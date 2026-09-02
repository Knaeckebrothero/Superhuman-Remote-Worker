# KB gardening experiment harness

Host-side scripts behind the experiments in
`knowledge-base/knowledge/features/kb_gardening_retire_consolidate_purge.md` §8.
Results as run on 2026-09-02 are filed under
`knowledge-base/knowledge/research/kb_gardening/results/`.

Corpus: `BetterResavio-KB/` at the repo root (gitignored; 3,112 notes extracted from the
pre-split Better Resavio vault — see its README). Run from the repo root with `.venv`.

| script | experiment | needs |
|---|---|---|
| `corpus.py` | E0 profile; loader used by the others (parses with the repo's `parse_note_md`) | corpus |
| `embed.py` | MiniLM embeddings + pair counts at 0.85–0.99 (proxy for the prod centroid) | `sentence-transformers` |
| `e1_prefilter.py` | E1 deterministic rules (exact/near-dup, same-title twins, orphan nursery) vs KEEP/RETIRED/UNKNOWN classes | `emb_cache.npz` from embed.py |
| `e1b_reachability.py` | E1b GC-style reachability from active durable roots (rule R4) | corpus |
| `e2_llm_prune.py [model]` | E2 unguarded prune / E3 guarded / E3F tool-filtered, via `claude -p` as the aux model; regret = anchored notes retired | `claude` CLI |
| `e4_concurrency_inpod.py` | E4 concurrent writers + delete on the real materialize endpoint — run INSIDE the orchestrator pod (`kubectl exec -i <pod> -- env PROJECT=<uuid> python3 - < e4_concurrency_inpod.py`) | k3d + a project with a knowledge repo |
| `e6_lanes_inpod.py` | E6 prefilter + purge lanes end-to-end with the pod's DB facades (seeds rows by hand; local k3d has no embedding) | same |

The E3 rerun that gates G6 (nursery TTL widening) should point `e2_llm_prune.py` at the
deployment's real aux model instead of `claude -p`: replace `ask()` with a call through
the auxiliary LLM path and keep the sample/metrics unchanged.
