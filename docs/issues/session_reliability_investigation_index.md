# Session-reliability investigation — findings index (2026-06-26 → 07-02)

**What this is:** the pickup index for one connected debugging arc that began with operator session `7692637b-9c60-4698-9875-b57ec34e66a6` ("Cloud storage file inspection and summary" — gpt-5.5, Supervised, main cluster) and fanned out into session-lifecycle, tooling, and infra findings. Every finding below has a detailed doc; this page is the map + suggested pickup order.

**Status:** All docs listed here are **uncommitted on `develop`** as of 2026-07-02 (except where a doc notes its own fix has been implemented).

**Unifying theme — silent failure:** a dead-looking live channel, a timed-out permission gate, an over-quota search key, and a quota-tripped model each got flattened into something benign-looking instead of surfacing the real cause. Several of these turned out to be the *same incident* seen from different angles (see #1 ↔ #6).

## Findings & their docs

| # | Finding | Doc | Status |
|---|---------|-----|--------|
| 1 | Resumed session shows no output / no "generating". **Root cause: session-attach starvation** — the bound agent was blocked ~10 min by a stuck codex-cooldown job; `probe_ready` + `ensure_route` gate *both* the `425` and the WS `504`. *(Corrected from the initial service-worker hypothesis, which was disproven — streams are already `ngsw-bypass`'d.)* | `resumed_session_dead_stream_and_supervised_gate_timeout_as_denial.md` — Defect A + "Update 2026-07-02" | Root cause confirmed; session-side fixes open |
| 2 | Supervised tool-gate that **times out** is reported to the LLM as `"User denied this tool call."` → agent concludes the user refused and abandons real work | same doc — Defect B | Confirmed; fix open |
| 3 | Service-worker `/api/**` freshness-cache audit — SSE streams are protected only by a *fragile per-URL* `?ngsw-bypass=true`; the stateful `/connection` handshake, binary downloads, and IDE proxy are still cached | same doc — "Update 2026-06-27" follow-ups 1 & 2 + live-endpoint inventory | Audited; carve-out fix open |
| 4 | `web_search` reports "No web results found" when Tavily returns an error (432 over-quota / 401 / 429) — `_direct_web_search` ignores `response["error"]` | `web_search_masks_tavily_errors_as_no_results.md` | Quota fixed operationally; masking code fix open |
| 5 | Workspace pod likely **OOMKilled** on a build/test job (hardcoded 4 Gi limit, Burstable QoS, no `priorityClass`, `livenessProbe` under `restartPolicy: Never`) | `agent_workspace_pod_resource_headroom.md` | Original "informational" verdict superseded by the 2026-06-29 OOMKill update |
| 6 | Loop job ran `gpt-5.3-codex-spark` (not the selected `gpt-5.5`) then hung 5+ h on a multi-day `model_cooldown` 429. **This is the job-side hang that starved #1's agent** — same agent `90e7445b`, same job `8bf2be7e` | `loop_ran_codex_spark_not_selected_model_then_hung_on_cooldown.md` | Defects A/B implemented+verified; C1/C2 implemented, C3 deferred |
| 7 | Agent deliverables saved to workspace `output/` aren't reachable from the "Files" (cloud) button — only via the IDE | `session_deliverables_in_workspace_output_not_in_cloud_files_button.md` | Confirmed; fix options open |
| 8 | Suspended threads stay `status=active`/green forever ("stuck active") — no idle auto-end, so an idle/suspended session is indistinguishable from a live one | `persistent_thread_lifecycle.md` | Proposal |
| 9 | Memory reranker `403` (`qwen3-reranker-8b` not registered) — non-fatal, degrades to legacy order (present throughout the agent logs we read) | `litellm_reranker_model_unregistered.md` | Open, non-fatal |

## Loose ends (captured here so they're not lost)

- **Thread list shows "Tokens: 0" for every session.** A thread-level usage-accounting gap. Likely the same root as `docs/done/litellm_streaming_usage_not_surfaced.md` (streaming turns don't surface usage) — **verify** whether the thread-list `0` is that, or a separate list-aggregation miss. Not yet its own doc.
- **`CLAUDE.md` is stale on routing.** It says "All routes in a single file (`orchestrator/main.py`)", but session routes now live in `orchestrator/routers/sessions.py` (and sibling routers). Worth a one-line CLAUDE.md fix so future navigation isn't misled.
- **`task_add` is gated in Supervised mode** — a no-risk bookkeeping tool that shouldn't need per-call approval; it was the first domino in #2. (Noted as "Secondary" in doc #1.)

## Suggested pickup order (by impact)

1. **Session-attach starvation** (#1, "Update 2026-07-02" follow-ups 1/3/4) — the actual session-breaker. Fix #2 there (multi-day-cooldown fail-fast) is *already done* via #6's Defect C.
2. **Gate-timeout-as-denial** (#1, Defect B) — silent, high-impact, small fix.
3. **`web_search` error masking** (#4) — tiny fix, big clarity win; also makes a spent key visible.
4. **Workspace OOMKill / configurable resources** (#5) — real crashes on build/test loops; make workspace resources a Helm value + add a `priorityClass`.
5. **SW carve-out** (#1 follow-ups 1 & 2) — defense-in-depth so a newly-added streaming endpoint isn't silently broken by `/api/**`.
6. **Files/deliverables surfacing** (#7), **thread auto-end** (#8).
7. **Reranker registration** (#9), **thread token accounting** (loose end) — lower impact.
