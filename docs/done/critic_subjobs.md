# Critic Verification Jobs — Root Cause Analysis

**Date:** 2026-03-05
**Investigated job:** `7ddf958a-c8b5-4892-ab61-85b47630251d` (critic reviewing `431541b3`)
**Model:** `openai/gpt-oss-120b`

## Summary

Critic verification jobs were broken because the `evaluation` tool category (`approve_job`, `return_job_with_feedback`) was silently dropped during config loading. The root cause — missing fields in `ToolsConfig` — has been fixed. Two of three secondary issues have also been fixed; one remains open.

---

## Root Cause (Fixed)

### The Bug

`ToolsConfig` dataclass in `src/core/loader.py` had hardcoded fields for only 8 tool categories, missing `sql`, `mongodb`, and `evaluation`. These categories were silently dropped during config loading, so critic agents never got verdict tools.

### What the Critic Actually Did (Job 7ddf958a)

Without `approve_job` or `return_job_with_feedback`, the critic called `job_complete` instead — completing its own job but leaving the target job stuck in `pending_review`. It also read the first critic's approval report (biasing its own conclusion) and never used `shell_execute` to independently verify deployment claims.

### Fix Applied (2026-03-05)

All changes verified — 1387 tests pass, evaluation tools survive the full config loading round-trip.

| Fix | File | What changed |
|-----|------|-------------|
| `ToolsConfig` dataclass | `src/core/loader.py:535-549` | Added `sql`, `mongodb`, `evaluation` fields |
| `load_agent_config()` | `src/core/loader.py:837-849` | Reads all 11 tool categories |
| `load_agent_config_from_dict()` | `src/core/loader.py:980-992` | Reads all 11 tool categories |
| `get_all_tool_names()` | `src/core/loader.py:1913-1925` | Concatenates all 11 categories |
| `serialize_resolved_config` | `src/core/loader.py:1819-1829` | Serializes all 11 categories (also added missing `git`) |
| Critic default config | `config/experts/critic/config.yaml:45` | Removed `evaluation` from defaults — injected via `config_override` only |
| Verification subjob creation | `src/api/orchestrator_client.py:495-499` | Injects `evaluation: [approve_job, return_job_with_feedback]` via `config_override` |

---

## Open Issues

### 1. Verification Instructions Don't Require Independent Verification — FIXED

**File:** `config/experts/critic/verification_instructions.md`

Section "2. Inspect the Deliverables" now includes a bullet requiring `shell_execute`-based independent verification for deployment/infrastructure jobs (SSH, curl, service checks).

### 2. Critic Can See Previous Critics' Reports

The second critic (7ddf958a) read the first critic's (7a7625f0) verification report showing approval, likely reinforcing a "looks good" conclusion. Each critic should form an independent opinion. Options:
- Strip prior verification reports from the workspace before spawning a new critic
- Add a warning in the instructions: "Ignore any prior verification reports — form your own independent assessment"

### 3. Model Selection for Quality Gating — FIXED

**File:** `src/api/orchestrator_client.py` — `create_verification_job()`

Verification jobs now override the LLM to `openrouter/minimax/minimax-m2.5` with `reasoning_level: xhigh` for both strategic and tactical phases via `config_override`. This ensures a capable model is always used for quality gating regardless of the critic's default config.
