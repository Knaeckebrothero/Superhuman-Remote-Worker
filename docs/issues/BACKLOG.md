# Issue Backlog — ranked index of open work

**Built 2026-08-06** from a status-line sweep of all 197 docs in this
directory (61 open, 22 built-but-unshipped, ~80 ambiguous/undated).
Truth lives in each doc's own **Status** line — this index is the ranked
*view*, curated at the top, mechanically complete at the bottom.
**Maintenance:** when you close or pick up a doc, update its Status line
first, then move/remove its row here. One issue per session works well;
link the session's findings back into the doc.

The big-picture thread this backlog protects: the worker-runtime
measurement loop (`docs/features/worker_runtime_strategy.md`; latest
results `phase_model_overhead_amnesia_loop.md` §13). Items marked as
blocking it should be taken before the next bench campaign.

## P0 — fix first (production pain, security, data loss)

| doc | why now |
|---|---|
| [gitea_admin_credential_in_every_agent_workspace](gitea_admin_credential_in_every_agent_workspace.md) | **Security.** The Gitea admin credential lands in every agent workspace. Blast radius: any job/agent can act as git admin. |

### P0-adjacent, tracked outside this directory

- **Mode A cloud review dead zone** — Defect 3 of
  `deliverable_lost_to_nested_repo_commit_and_stranded_mode_a_job.md`:
  219/220 Mode A jobs stranded, zero exports ever. Direction already decided
  (retire Mode A per `docs/features/workspace_and_change_records.md` §6.3),
  needs execution. (Defect 1 of that doc is **closed 08-06** — fix `22b2511e`
  deployed, blast radius settled as universal across a ~32 h window, not
  conditional as two earlier passes claimed. Residue promoted to P1 above.)
- ~~Stale-agent detector SQL crash~~ — **resolved + deployed to prod 07-12**
  (`docs/done/stale_agent_detector_sql_crash_disables_recovery_sweeps.md`);
  open residue = job-execution-lease stages 4–5 and two feature docs
  uncommitted.
- ~~New Session 400 on dev~~ — fix **committed `6b727734` (07-29)**;
  verify it deployed, then close.

## P1 — next (confirmed defects, high value; first row blocks the bench thread)

| doc | why |
|---|---|
| [homelab_wan_outage_severs_cluster_from_own_llm](homelab_wan_outage_severs_cluster_from_own_llm.md) | Cheap CoreDNS rewrite makes every job immune to WAN outages (08-05 cost a 3 h cluster-wide LLM blackout + 1 job). |
| [embedding_batch_overflow_skips_citation_source_embeddings](embedding_batch_overflow_skips_citation_source_embeddings.md) | **FIX SHIPPED 08-06 (batch #3)** — batching seam + transient-only retry + NaN guard + typed per-source state + coverage stats + backfill script (k3d dry-run: 54 pairs / 5 jobs). Owed: `--apply` run on main + live re-run acceptance. |
| [deliverable_lost_to_nested_repo…](deliverable_lost_to_nested_repo_commit_and_stranded_mode_a_job.md) (Defect 1 residue) | **Largely closed 08-07** — a failed push is no longer indistinguishable from a lazy agent. `push()`'s return is consumed at all four job-ending sites and sets `delivery_failed` on the freeze (`abe4d1d1`); the deliverable gate skips instead of bouncing (`233b2649`) and the verification trigger escalates instead of spawning a critic (`728214bf`), so the phantom-findings path that burned `cd3bfe52`'s 5 critic rounds and both of `bbce4bed`'s bounces is cut. Chosen mechanism is the freeze flag rather than this doc's proposed `has_unpushed_commits()` hold at seal — same failure mode, and it also carries the reason to `error_message`. **Open:** the seal-time `has_unpushed_commits()` check would additionally catch a push that never ran at all (vs. ran and failed); `_complete_phase_with_git` still discards its return by design (mid-job boundaries are recoverable). |
| [pod_oom_kill_protection](pod_oom_kill_protection.md) | Umbrella for the recurring OOM incident class. |
| [kb_reindex_sync_dns_retries_stall_orchestrator_liveness](kb_reindex_sync_dns_retries_stall_orchestrator_liveness.md) | NEW 08-07 (P-4 bench night-watch): KB materialization routes every agent note through the tiktoken chunker; cold vocab cache + DNS failure = sync retries ON the event loop → liveness killed the orchestrator and orphan-paused the in-flight job pair. Grows with vault size; dev exposure on any egress blip. Fixes: bake vocab into image, reindex off-loop, fail-fast on first DNS error. |
| [hnsw_indexes_never_used_inside_hybrid_search_functions](hnsw_indexes_never_used_inside_hybrid_search_functions.md) | NEW 08-07 (job `204d0ed1` subagent-timeout investigation): **B2's unfinished half.** The halfvec HNSW indexes exist, but all six hybrid-search SQL functions seq-scan anyway — `kb_search` 12–14 s, memory retrieval 22–46 s, against 1.9–3.5 s for the *identical* body hoisted to a top-level PREPARE (index scan). B2's "planner-gated hedge" has never fired at any scope size; k3d missed it because btree+sort really is fast when small. Per-turn tax on every job and session (~60 s/turn of memory+KB injection in `204d0ed1`), and it is what blew the 240 s light-reader deadline. **No config fix** — `enable_seqscan=off` doesn't flip it, so the index path is never *considered*, not merely out-costed. **FIX BUILT + k3d-TESTED 08-07, UNCOMMITTED, NOT DEPLOYED** — migration `0017_hybrid_search_plpgsql_dynamic_execute.sql` converts all 6 fns to `plpgsql` + dynamic `EXECUTE`; signatures unchanged, no app code. KB path fixed: 5.0–5.9 s → 0.52–0.61 s, Index Scan (~10×). Applied via the real orchestrator runner (3 ms, checksum tracked); semantically identical on real data (0 ordered mismatches, both fns); lint + 1192 tests green; `vector_schema_current.sql` regenerated. Removing the `SET` clause to allow inlining was tested and **does not work**. **Memory path only ~6× (9.1 → 1.5 s): a SECOND, independent defect** — the `memories` content arm seq-scans even at top level with a literal probe, because `embedding` is TOASTed and the seq-scan cost model can't see the detoast (planner: index 3985 vs scan 2106; forced, the index is 15.5 ms vs 1143 ms = 74×). `enable_seqscan=off` per-function and `iterative_scan=off` both tested and **rejected**. Owed: that second defect, plus ANN-vs-exact recall at scale before prod. |

| [live_config_update_buries_extra_and_empties_the_shell_group](live_config_update_buries_extra_and_empties_the_shell_group.md) | NEW 08-11 (dev session `1930dec9`): granting the shell group mid-session bound **only `shell_read`** — no `run_command`/`shell_execute`/`cancel_command` — while `get_session_context` still reported `Supports shell: True`. Root cause is **not** shell-specific: live `config.update` round-trips through `dataclasses.asdict` → `load_agent_config_from_dict`, and `"extra"` is missing from `known_fields`, so the whole `extra` namespace is re-buried as `extra["extra"]` and **every** `extra` key is lost on **every** live update. The de-nesting repair exists but only in `load_config_from_resolved`. Shell is merely the loudest victim: the name list then falls to the stateless floor while the boot-snapshot `tool_context` stays persistent, and the bind is their intersection = `{shell_read}`. Silent — nothing diffs requested vs bound names. Fix 1 (log the delta) + fix 2 (round-trip identity `load(asdict(cfg)).extra == cfg.extra`) are the ones that matter; 3–5 are exposed debts. Workaround: end + resume the session. |

*(~~job_finalization_decisions_held_only_in_process_memory~~ — **FIXED 08-06
(batch #3)**, moved to docs/done/: journal-before-observe end-to-end, both
k3d kill-tests passed — worker decision survived a pod kill via resume
hydration; critic verdict survived via the checkpointed mirror + ledger.)*

## P2 — real but bounded (open, workaround exists or blast radius small)

| doc | status |
|---|---|
| [live_permission_mode_change_never_persisted](live_permission_mode_change_never_persisted.md) | Open, found 2026-08-08 on the k3d tier-row gate. Live permission-mode changes apply to the running agent but never reach `threads.permission_mode`; survives a page reload (agent reports in-memory state), so it silently reverts on pod restart. Pre-existing — reproduced on the pre-change build. Transport is fine; only the "top-level column sync" is missing. |
| [bench_infra_exclusion_misses_midflight_outages](bench_infra_exclusion_misses_midflight_outages.md) | Open, analysis-level workaround exists. Found 2026-08-05 during |
| [project_scoped_memory_deadlocks_under_parallel_jobs](project_scoped_memory_deadlocks_under_parallel_jobs.md) | Containment tier **SHIPPED 08-06 (batch #2)** — ordered locking + contained/retried access stats + heartbeat telemetry. OPEN: the semantic per-consumer TTL model (criterion 3) and pinned-budget share (criterion 5). |
| [phase_boundary_tags_are_moved_then_rejected_by_remote](phase_boundary_tags_are_moved_then_rejected_by_remote.md) | Core **FIXED 08-06 (batch #2)**; duplicate phase-completion transition **CLOSED 08-06 (batch #3)** — archive_phase exactly-once guard on the checkpointed instance key. Last residual: tag-independent review evidence (direction 5). |
| [dispatcher_resume_pep_twin_still_fails_open](dispatcher_resume_pep_twin_still_fails_open.md) | OPEN, filed 2026-08-04 by the whole-branch review of the fix for |
| [registered_tools_no_config_can_grant](registered_tools_no_config_can_grant.md) | OPEN — sweep-corrected 08-06: items 1 (curator kb_lint/kb_index) + 3 (`catalog_authoring`) ARE shipped; truly unreachable now = the `delegate_work` pair; tests/lint (items 4-5) unbuilt |
| [kb_duplicate_frontmatter_ids_collide_on_reindex](kb_duplicate_frontmatter_ids_collide_on_reindex.md) | OPEN.** Still reproducible in code at HEAD — `note_fields` continues to take |
| [memory_noise](memory_noise.md) | Open |
| [litellm_reranker_model_unregistered](litellm_reranker_model_unregistered.md) | Open · root cause confirmed · non-fatal (memory degrades gracefully) |
| [minimax_m3_auxiliary_structured_output_flaps](minimax_m3_auxiliary_structured_output_flaps.md) | OPEN. P2 quality/overhead and model-capability mismatch.** The |
| [reasoning_capture_regressions_on_routing_and_factory_changes](reasoning_capture_regressions_on_routing_and_factory_changes.md) | OPEN (systemic guard not built).** The *pattern* is documented here; |
| [session_contacts_never_register_on_default_project](session_contacts_never_register_on_default_project.md) | OPEN. Found by the virtual-directories cloud-sync live gate on local k3d, 2026-08-04. |
| [orchestrator_mcp_query_surface_too_coarse_for_investigation](orchestrator_mcp_query_surface_too_coarse_for_investigation.md) | OPEN — usability/observability gap. Confirmed 2026-07-13 while |
| [job_mode_reasoning_pick_silently_reset](job_mode_reasoning_pick_silently_reset.md) | OPEN, filed 2026-08-03. The **session-mode** instance of this defect |
| [sudo_freeze_cockpit_blind_spot](sudo_freeze_cockpit_blind_spot.md) | Open |
| [snapshot_capture_ssh_failure](snapshot_capture_ssh_failure.md) | Open — observed 2026-05-27 on the dev cluster (`superhuman-remote-worker`). |
| [expert_prompts_instruct_a_removed_browser_tool](expert_prompts_instruct_a_removed_browser_tool.md) | OPEN. |
| [settings_pane_never_refetches_a_threads_config](settings_pane_never_refetches_a_threads_config.md) | OPEN. Pre-existing for `config_override`; the tool-groups endpoint |
| [settings_pane_shows_fallback_tool_policy_without_saying_so](settings_pane_shows_fallback_tool_policy_without_saying_so.md) | OPEN. |
| [ci_migration_lint_bypassed_by_deploy](ci_migration_lint_bypassed_by_deploy.md) | Open — partial mitigation in place per incident (offending file added to |
| [local_test_runs_cannot_reproduce_ci_environment](local_test_runs_cannot_reproduce_ci_environment.md) | OPEN. Both instances are fixed (`177df2c3`, `5eb436eb`); the gap |
| [helm_toolchain_pin_drift](helm_toolchain_pin_drift.md) | OPEN. No production impact observed; this is drift risk plus a |
| [verification_unreachable_for_hand_repaired_jobs](verification_unreachable_for_hand_repaired_jobs.md) | DIAGNOSED, UNFIXED.** No code written. |
| [agent_phase_guardrails_burn_legitimate_work](agent_phase_guardrails_burn_legitimate_work.md) | as of 2026-07-15 all findings are **open / unbuilt** except as noted below. |
| [job_runtime_containment_gap](job_runtime_containment_gap.md) | OPEN — MAIN-CLUSTER SCHOLAR SUCCESS PATH PASSED; STAGE A |
| [persistent_session_midturn_message_loss](persistent_session_midturn_message_loss.md) | Open. Design. This doc started as "mid-turn crash loses the turn's |
| [cockpit_backend_unreachable_blank_state](cockpit_backend_unreachable_blank_state.md) | Open |
| [cockpit_session_startup_timers_transient_sse](cockpit_session_startup_timers_transient_sse.md) | OPEN — cosmetic, not yet fixed. Found 2026-06-17 on k3d while testing |
| [cockpit_workspace_button_deadlink_when_no_repo](cockpit_workspace_button_deadlink_when_no_repo.md) | Backlog — small, low-risk cockpit fix. Filed 2026-06-13. |
| [job_cloud_export_open_blocked](job_cloud_export_open_blocked.md) | Popup + affordance FIXED; sharing gap OPEN (needs a one-time user login per cluster) |
| [mcp_capability_completion_audit](mcp_capability_completion_audit.md) | Source remediation implemented; disposable live acceptance pending |
| [gemini3_thinking_temperature_loop](gemini3_thinking_temperature_loop.md) | Partial fix landed on `develop` (code-only, lint-clean). **NOT yet |

## P3 — cleanup, deferred-by-choice, designs awaiting build

| doc | status |
|---|---|
| [agent_egress_networkpolicy_enablement](agent_egress_networkpolicy_enablement.md) | Open — the policy is implemented and shipped **default-off |
| [lifecycle_session_agents_without_thread_never_drain](lifecycle_session_agents_without_thread_never_drain.md) | CONTAINED 08-06 (batch #2): the guarded reap was already committed (`46ea64d2`) + live-verified twice on k3d (synthetic thread-less orphan reaped at grace+tick; healthy parked session untouched). Doc stays open for the unified-lifecycle proper fix only. |
| [agent_fast_freeze_on_dead_workspace](agent_fast_freeze_on_dead_workspace.md) | Designed 2026-07-04, not yet implemented. Work on `develop`. |
| [cloud_folder_invisible_until_owner_signs_into_cloud](cloud_folder_invisible_until_owner_signs_into_cloud.md) | OPEN (narrowed 08-07) — historical hole swept by `scripts/backfill_session_folder_shares.py`; retry only fired on resume, so ended threads stayed unshared. User-facing affordance still unbuilt. |
| [datasource_legacy_dead_code](datasource_legacy_dead_code.md) | Open — cleanup, no functional impact. Filed 2026-06-11 |
| [db_schema_hygiene](db_schema_hygiene.md) | open — first slice landed on `develop` (`f4160780`, 2026-06-11); |
| [delegation_light_mode_missing](delegation_light_mode_missing.md) | Open. Enhancement / design gap, **not** a regression — the existing |
| [dual_app_persistent_app_redundancy](dual_app_persistent_app_redundancy.md) | 🔴 **OPEN** — structural debt, filed for a deliberate later fix. |
| [failed_job_pvc_reclaimed_without_grace_period](failed_job_pvc_reclaimed_without_grace_period.md) | Designed 2026-07-25 from the job-`52949749` salvage. Not yet |
| [gitmanager_local_git_fallback](gitmanager_local_git_fallback.md) | Open — deferred hardening. Filed 2026-06-11 (fallout from the |
| [loop_critic_producer_identity_bias](loop_critic_producer_identity_bias.md) | OPEN / backlog. Not scheduled. Low urgency (see "Why this is latent, |
| [phases](phases.md) | Open |
| [remove_workspace_md_vestiges](remove_workspace_md_vestiges.md) | Backlog — deferred cleanup. Filed 2026-06-03. |
| [scholar_delegation_not_exercised](scholar_delegation_not_exercised.md) | Open. Behavioral issue (model instruction-following), **not** a config regression. The delegation capability is fully wired and was live for the affec |
| [session_db_experts_cannot_customize_interactive_prompt](session_db_experts_cannot_customize_interactive_prompt.md) | Deferred (intentionally). Low urgency — filed to capture the |
| [session_tool_group_enablement_is_computed_in_two_places](session_tool_group_enablement_is_computed_in_two_places.md) | OPEN by choice, not oversight. Drift is currently caught by a test |
| [unify_scholar_critic_subjob_provisioning](unify_scholar_critic_subjob_provisioning.md) | Backlog — deferred refactor (touches completion flow). Filed 2026-06-13. |

## Built but unshipped — finished work awaiting commit / deploy / live gate

Quick wins: the engineering is done; what remains is shipping and verification.
*(Batch #3 sweep, 2026-08-06: 19 of the 22 rows that used to sit here were
verified SHIPPED at HEAD — every "uncommitted" claim had in fact landed,
mostly under rebased SHAs — and moved to docs/done/ with evidence notes. The
three rows below are what actually remains.)*

| doc | status |
|---|---|
| [llm_infra_404_misclassified_permanent_kills_jobs](llm_infra_404_misclassified_permanent_kills_jobs.md) | Slices 1–3 SHIPPED (code since relocated to `src/core/llm_retry.py`); Slice 4 (reranker parity) not built; k3d e2e outage replay owed |
| [session_reliability_investigation_index](session_reliability_investigation_index.md) | Index doc — accurate as written (spot-checked 08-06; its one ✅ claim verified; the CLAUDE.md routing staleness it flags is still true) |
| [tool_configuration_defects_and_fix_roadmap](tool_configuration_defects_and_fix_roadmap.md) | Phases 0–3 shipped + pushed; open: Phase 2 literal YAML sweep (`session_base.yaml` still `shell: []`) + the job-mode reasoning-reset twin |

## Diagnosed / investigated, unranked (triage when touching the area)

*(Batch #3 sweep, 2026-08-06: 18 rows verified fully shipped/superseded and
moved to docs/done/; the corrected rows below carry the sweep's precise
status.)*

| doc | status |
|---|---|
| [agent_tool_fixed_vocabularies_invisible_to_model](agent_tool_fixed_vocabularies_invisible_to_model.md) | P0–P5 BUILT + **COMMITTED `781285c9`** (sweep 08-06; 41/41 vocab tests green). Open: `sudo_request_status` Literal miss on REST/MCP filters + the disclosed tier-3 left-to-dos |
| [approving_a_critic_wedges_target_in_reviewing](approving_a_critic_wedges_target_in_reviewing.md) | Reachable, unfixed — but severity downgraded to low (sweep 08-06): the ledger-aware unstick sweeper (`f2d054bd`) bounds the wedge at the grace window; no longer permanent |
| [bound_skill_missing_from_resume_blob_deadlocks_phase_transition](bound_skill_missing_from_resume_blob_deadlocks_phase_transition.md) | Delivery-path defect CONFIRMED in code + live incident; still a silent skip at HEAD (re-verified 08-06) |
| [deprecate_docker_compose_stack](deprecate_docker_compose_stack.md) | Proposed. Migration to local k3d verified end-to-end 2026-05-28; zero acceptance criteria executed yet (re-verified 08-06) |
| [drain_freeze_overwrites_critic_verdict](drain_freeze_overwrites_critic_verdict.md) | Overwrite still in code, but consequence structurally prevented for BOTH decision classes (critic ledger + batch #3 completion journal); severity low, guard = defense-in-depth |
| [feedback_resume_restricted_closure_toolset](feedback_resume_restricted_closure_toolset.md) | Observed 2026-07-26 on dev (job `52949749`, round-2 correction); not yet root-caused (re-verified 08-06) |
| [jsonb_isinstance_guard_without_parse_silent_dead_paths](jsonb_isinstance_guard_without_parse_silent_dead_paths.md) | 1 of 4 instances fixed (site 4 cockpit, `0ba7c754`); sites 1–3 byte-for-byte unchanged; `_get_vm_context()` helper exists but unapplied to sites 1–2 |
| [loop_advance_nonatomic_wedges_loop](loop_advance_nonatomic_wedges_loop.md) | Superseded by Loop Unified Engine Phase 1 (07-19): atomicity achieved by the barrier rewrite; heal + age gate survive under new names; UI stalled badge still unbuilt |
| [mcp_client_timeout_retry_false_failure_shared_auth_headers](mcp_client_timeout_retry_false_failure_shared_auth_headers.md) | Defects 2+3 FIXED via `99b87008` (client now `src/shared/orch_surface/`); defect 1 (flat 30s client timeout) open |
| [orchestrator_tool_surface_fragmentation](orchestrator_tool_surface_fragmentation.md) | decided and in execution — S1 committed `99b87008`; next S2 (see unified_orchestrator_tool_surface.md) |
| [phase_model_overhead_amnesia_loop](phase_model_overhead_amnesia_loop.md) | 🟡 **IN PROGRESS** — filed 2026-07-31 after a code-side deep |
| ~~recovery_pause_repersists_stale_freeze_invisible_job~~ | **FIXED 08-06 (batch #2)**, moved to docs/done/ — completion freeze-echo guard + `pause_job_shed_freeze` on the recovery arm. |
| [results](results.md) | pending_review, confidence 1.0 |
| [snapshot_restore_dead_for_jobs](snapshot_restore_dead_for_jobs.md) | Confirmed at code level, not fixed; A/B decision still unmade (re-verified 08-06: `check_idle_all` still has zero live callers, reap path still snapshot→delete) |
| [task_clearance_user_feedback](task_clearance_user_feedback.md) | Core search_files bug fixed via the backend refactor (server-side grep); 5 design proposals unbuilt |
| [transient_db_error_hard_fails_job_and_destroys_vm](transient_db_error_hard_fails_job_and_destroys_vm.md) | All 8 defects shipped + tested; the 2 named live-gate items (NetworkPolicy egress-block, VM Defect-1b reaper interaction) still owed (re-verified 08-06) |
| [vm_guest_boots_to_emergency_shell](vm_guest_boots_to_emergency_shell.md) | Root cause undetermined; proposed no-register-timeout guard still absent (re-verified 08-06) |
| [vm_session_thread_repo_clone_unroutable_gitea_url](vm_session_thread_repo_clone_unroutable_gitea_url.md) | Defect site confirmed live at HEAD (08-06): thread/session path still stores raw `git_remote_url`; the job path's `externalize_gitea_url` rewrite was never applied to it |
| [vm_workspace_snapshot_unreachable_from_orchestrator](vm_workspace_snapshot_unreachable_from_orchestrator.md) | Partial fix shipped (`ed26ebfa`, skipped-capture honesty); persistent-rootdisk flag still default-off; live gate owed (re-verified 08-06) |
| [web_search_masks_tavily_errors_as_no_results](web_search_masks_tavily_errors_as_no_results.md) | Error-masking defect verbatim unchanged at HEAD (re-verified 08-06): `_direct_web_search` still ignores `response["error"]` |

## No machine-readable status line (mostly notes/designs — triage on touch)

[agent_lifecycle_management](agent_lifecycle_management.md), [agent_loop_mode_pod_reuse](agent_loop_mode_pod_reuse.md), [backlog_post_merge_reindex_resurrects_closed_ticket](backlog_post_merge_reindex_resurrects_closed_ticket.md), [backlog_priority_silently_resets_to_normal](backlog_priority_silently_resets_to_normal.md), [backlog_tail_fallback_names_a_tool_that_cannot_see_the_pool](backlog_tail_fallback_names_a_tool_that_cannot_see_the_pool.md), [citation_llm_api_key_isolation](citation_llm_api_key_isolation.md), [cloud_sync_phantom_readme_pull_spam](cloud_sync_phantom_readme_pull_spam.md), [cockpit_native_select_popup_misposition](cockpit_native_select_popup_misposition.md), [critic_failure_leaves_parent_job_stuck_reviewing](critic_failure_leaves_parent_job_stuck_reviewing.md), [delegation_freeze_lifecycle_gaps](delegation_freeze_lifecycle_gaps.md), [deliverable_lost_to_nested_repo_commit_and_stranded_mode_a_job](deliverable_lost_to_nested_repo_commit_and_stranded_mode_a_job.md), [deployment](deployment.md), [egress_proxy_pool](egress_proxy_pool.md), [gemma_session_findings](gemma_session_findings.md), [helm_fresh_deploy_issues](helm_fresh_deploy_issues.md), [langchain_responses_api_streaming](langchain_responses_api_streaming.md), [loop_advances_into_active_model_cooldown](loop_advances_into_active_model_cooldown.md), [mcp_created_jobs_ownerless_capability_grant_denied](mcp_created_jobs_ownerless_capability_grant_denied.md), [nats_subject_acl_hardening](nats_subject_acl_hardening.md), [orchestrator_main_py_monolith](orchestrator_main_py_monolith.md), [persistant_shell](persistant_shell.md), [persistent_chat_component_style_budget](persistent_chat_component_style_budget.md), [persistent_chat_silent_disconnect](persistent_chat_silent_disconnect.md), [persistent_session_restored_messages_no_ids](persistent_session_restored_messages_no_ids.md), [persistent_thread_lifecycle](persistent_thread_lifecycle.md), [scholar_pending_review_silent_success](scholar_pending_review_silent_success.md), [session_restore_drops_repo_checkouts](session_restore_drops_repo_checkouts.md), [session_router_ingress_host_helper](session_router_ingress_host_helper.md), [subjob_branch_merge_model](subjob_branch_merge_model.md), [sudo_plugin_research_review](sudo_plugin_research_review.md), [surface_silent_aux_failures](surface_silent_aux_failures.md), [test_coverage](test_coverage.md), [tests](tests.md), [tool_configuration_deferred_findings](tool_configuration_deferred_findings.md), [verification_fail_closed_followups](verification_fail_closed_followups.md), [version_upgrade_drain_livelock](version_upgrade_drain_livelock.md), [vm_daemon_http_transport](vm_daemon_http_transport.md), [workspace_upgrade_drops_cloud_mount](workspace_upgrade_drops_cloud_mount.md)

*(Closed/resolved docs are deliberately not listed; the sweep counted 34
clearly closed. Rebuild the mechanical tail anytime by re-running the
status sweep.)*

## Session log 2026-08-06 (batch fix session)

Autonomous ~3.5 h implementation session on `develop` (local k3d verification
via Tilt). Six tasks, eight commits, nothing pushed. Recurring theme: several
"UNFIXED" docs were already fixed at HEAD — always re-verify a doc's Status
against code before building.

**Task 0 — k3d job path (`k3d_workspace_ssh_key_rejected`, now docs/done/)** —
commit `ca5b681e` (docs only; the code fix pre-existed). Root cause was NOT
key drift: `4f27f581` (08-03) added the SSH-auth readiness gate, exposing that
the chart projects the Secret key 0444, which root-running OpenSSH refuses
("bad permissions" → the quoted error was ssh's truncated known-hosts
warning). `52c1ba80` (08-04, hours after filing) added the 0600 staging fix
(`_stage_runtime_ssh_key`) and nobody re-ran a job. Verified: staged-key probe
from the orchestrator pod exits 0; smoke job `58ba61ef` (plain POST
/api/jobs) provisioned (SSH auth attempts=1), processed, made LLM calls, and
COMPLETED; all later smoke jobs provisioned identically. Residual papercut
kept in the doc: provisioning failures write `failed` with an empty `error`
column.

**Task 1 — critic freeze_data wedge (`critic_feedback_resume_parent_freeze_data_wedge`, now docs/done/)** —
commit `45f615dd`. Already fixed at HEAD by `4dba9836` (07-22,
`queue_job_for_resume` clears `freeze_data`); audited every sibling resume
path clean (blocking-message/urgent/LLM-triage, explicit Resume, sudo
approve/deny, deliverable-gate). Added the missing regression pins: DB-level
critic-flavored test (real Postgres via testcontainers —
`test_critic_returned_resume_clears_freeze_so_dispatcher_sees_job`) and a
seam pin (`test_returned_resume_routes_through_freeze_clearing_write`; the
old wiring test stubbed `_internal_resume_job` wholesale). Verified:
`pytest tests/test_queue_job_for_resume.py tests/test_verification_flow.py`
(76 passed). Residual same-class defect tracked separately in
`recovery_pause_repersists_stale_freeze_invisible_job.md` (`pause_job` keeps
freeze_data).

**Task 2 — bench sweeper race (`bench_sweeper_multi_replica_race`, now docs/done/)** —
commit `ac4173fc`. `sweep_tick` now claims session-scoped
`pg_try_advisory_lock(hashtext('bench_sweep'))`, claim+release on ONE held
pool connection (`BenchStore.try_sweep_lock`); loser skips the tick; key
mirrored in `database/lock_ids.py`. Verified: 6 new unit tests (two
concurrent ticks → single submission + unique (task,arm,replicate); release
between ticks and under a raising sweep; loser never unlocks; one-session
contract); live k3d run `e2233fba` (1×1×1) → sweeper submitted exactly one
job (`50a7fa16`, completed), single ledger entry. Note: the doc's claim that
leader election has "no precedent" is stale — `run_when_leader` exists; the
sweeper escaped that audit because it starts from a router lifespan.

**Task 3 — resume-lane brief starvation (`fresh_job_dispatched_as_resume_skips_seeding`, now docs/done/)** —
commit `66169c2c` (+ `6132789e` endpoint-inventory regen). (a) dispatcher +
admin-assign use `resume_lane_applies` + `PostgresDB.job_has_checkpoint`
(there is NO `jobs.started_at` column — checkpoint presence is the probe;
fails open to the resume lane on sqlite/probe-error). (b) the virtual
`task_brief.md` provider reads `_job_metadata` LIVE, and resumes with no
description backfill from the new internal `GET /api/jobs/{id}/brief`
(agent-DB fallback, non-fatal). (c) tripwire
`_note_resume_without_checkpoint`: ERROR + hydration + Phase-0 seed commit.
Verified: 71 unit tests across dispatch_guards/manual-assign/phase0-seed;
live k3d: paused never-started row `d8f004fa` → "dispatching via the fresh
/job/start lane" and its first LLM request contained description + kickoff +
Task Brief header (audit `llm_requests` probe `t|t|t`); paused mid-run →
resume lane + "hydrated task brief on resume (description=123 chars)".

**Task 4 — critic lifecycle bundle** — commits `1d7ceaa6` (docs) +
`e6c16d40` (code). (b) `stale_critic_waiting_status_escapes_reaper` and
(c) `reviewing_parent_pod_reaped_under_critic` were ALREADY FIXED at HEAD
(`f2d054bd`, `656b31ec`) with tests — docs closed to docs/done/ with the
deviation note for (b) ('waiting' sits in the top-level filter by design;
do not narrow it back). (a) rejected-verdict cap implemented:
`increment_verdict_rejections` (atomic, per-critic row), cap 3 in
`_record_verification_round_impl`, escalation via `_escalate_target`
(loop-aware), 409 flagged `escalated: true`, agent client turns it into a
stop order ("Do NOT resubmit") instead of the livelocking retry
instruction. Verified: `pytest tests/test_verification_flow.py
tests/test_critic_loop.py` (108 passed). NOT live-exercised (would need a
real 3× invalid-verdict critic); fix direction 2 (wall-clock watchdog arm
for live-critic reviews) deliberately remains OPEN — doc stays in
docs/issues/ with the split Status.

**Task 5 (stretch) — resume clone fallback (`resume_fresh_workspace_no_clone_fallback`, now docs/done/)** —
commit `e3996909`. The pod-handoff clone gate already existed but was dead
code: `JobResumeRequest` had no `git_remote_url` and the orchestrator never
sent it. Threaded the remote through the resume wire (payload + model + both
handlers) and added `workspace_init_path=reattach|clone|snapshot|existing`
logs plus a WARNING on blank-init-under-resume. Verified: 4 new wire tests;
live k3d: `ed7f93b4` paused mid-run, workspace pod+PVC+Service deleted,
released → resume lane → "Pod handoff: cloning workspace" →
`workspace_init_path=clone` → fresh workspace contained the repo files.
Bonus finding while rigging this: a job re-queued after terminal `failed`
has pruned checkpoints, so the new lane probe correctly sends it down the
FRESH lane, which also clones the repo (`58ba61ef` observed live) — both
lanes now recover history.

**Environment observations (not fixed, worth knowing):**
- Every orchestrator image rebuild on this k3d cluster causes a rocky
  ~5-10 min rollout: health probes time out (3s) while the app churns
  through startup + the `ide_settings` sweeper hammers dead workspace IPs
  ("No route to host" spam), kubelet restarts the container 2-3× before it
  settles. Low CPU throughout — smells like event-loop starvation during
  startup, pre-existing (the pre-session pod restarted the same way).
- A requeue after a workspace-provisioning failure replays the recorded
  failure from `context.workspace_container.status='failed'` until that key
  is shed (`shed_workspace_context` / `context - 'workspace_container'`) —
  by design, but easy to trip over when hand-requeueing.
- Synthetic SQL-inserted job rows fail dispatch with `connector_unavailable`
  unless `context.datasource_selection.selected_ids` matches the junction
  table (fail-closed materialization contract) — use the API to create test
  jobs, or include `{"datasource_selection": {"selected_ids": []}}`.

**Pytest**: full suite after all commits = 11 failed / 14178+ passed
(`13 failed` pre-inventory-fix − inventory − a metering flake that passes on
rerun). All 11 residuals are pre-existing env noise on this py3.14 host
(local-Postgres-required `test_database_phase1`, the `test_mcp_manager` /
`test_mcp_agent_wiring` stack, `tools/research` installed-client contracts)
— none touch modules changed this session; memory baseline was ~8 on py313.

### Addendum (same session, later): the two deferred live exercises ran

- **Task 1 live criterion met organically**: job `d3a16617`
  (`autonomy: review` + `verification.enabled`) → completed → `reviewing` →
  critic `6a21f0f5` dispatched; `returned` verdict (1 finding) recorded on
  the ledger; critic completed on its own → "Queued job … for auto-dispatch
  with feedback" → re-dispatched **4 s later**; parent freeze cleared
  (`last_freeze_data.freeze_type=job_complete` stashed) and the round-2
  agent's LLM context carried the finding text. The wedge is dead
  end-to-end.
- **Task 4(a) live criterion met**: round-2 critic `7f086fe8` pinned
  pre-dispatch; three invalid verdict POSTs against the real endpoint →
  409, 409, then 409 + `"escalated": true`; target `d3a16617` →
  `pending_review` carrying the rejection reason; critic
  `context.verdict_rejections = 3`; "Verification escalated target …"
  WARNING logged. Test critic cancelled afterwards.

## Session log 2026-08-06 (batch #2)

Autonomous ~3.5 h implementation session on `develop` (local k3d via Tilt),
evening after batch #1. Six tasks, seven commits, nothing pushed. Batch #1's
lesson held again: one of the six "unfixed" P0s was already fixed at HEAD.
Subagent-driven: 6 recon agents + 3 worktree implementers; final
verification + commits from the main loop. Note for future batches: the
Agent tool's worktrees are created from MAIN, not develop — all three
implementers had to be redirected to `git checkout --detach <develop-sha>`.

**Task 0 (hygiene)** — commit `2b081109`. The usage-v2 metering endpoints
(`bad31b0a`) landed without the mandatory inventory regen, so
`test_endpoint_inventory` failed ×2 at HEAD (baseline 13 failed vs batch
#1's closing 11). Regenerated via `scripts/check_endpoint_auth.py --write`.

**Task 1 — session workspace wiped on attach (P0 data loss)** — commit
`33fe6684`; doc → docs/done/. Ported the job path's guard to
`PersistentSession._attach_existing_workspace`: probe the real backend
before `initialize()` — `.git` → attach handle in place; git-less content
(list_dir minus lost+found) → `_initialize_git()` around the files;
genuinely empty → normal clone. Probe failures preserve. Every attach logs
`session_workspace_init_path=fresh|reattach|attach-content`. Live k3d
(thread `1131cea3`): fresh attach logged `=fresh` + cloned; a probe file
survived BOTH detach shapes — agent-pod delete → orphan-sweep `ended` →
resume, and idle-suspend → resume — each reattach logging `=reattach`,
file intact. 8 new tests (guard + wiring).

**Task 2 — session agents without a thread never drain (P0)** — no code
needed: the guarded reap was ALREADY COMMITTED at HEAD (`46ea64d2`,
06-23) — the doc's "uncommitted, pending review" was stale. Live-verified
both arms on k3d: (a) negative control — a healthy thread-bound parked
session sat ≥9 min across ~9 detector ticks, `intents={}`, untouched;
(b) a real orphan (thread_id nulled under a live heartbeating agent) was
stamped +25 s and its pod gone within grace+tick (attribution shared with
the orphan-thread teardown, hence:) (c) a synthetic THREAD-LESS orphan
(fake hostname, heartbeat kept fresh) was stamped +23 s and reaped at
stamp+5:00 exactly — "Reaped orphaned session agent … deleted=True" —
with no thread anywhere in the shape, only the reap can claim it. Doc
status corrected (stays open solely for the unified-lifecycle proper fix);
BACKLOG row moved P0 → P3.

**Task 3 — project-memory deadlocks, containment tier (P0)** — commit
`33e326a5`. `decrement_ttl` + the access-stat write now lock rows via
id-ordered `SELECT … FOR UPDATE` CTEs; the access-stat write moved to
`_record_access_stats` (sorted ids, deadlock-only bounded retry, ALWAYS
contained — an uncaught deadlock here used to abort the whole retrieval,
which was the bulk of the 138); `MemoryHealth` counters ride the heartbeat
as `metrics["memory"]` into `agents.metadata`. Tests: mock-level shape/
retry/containment pins + a real pgvector testcontainers concurrency suite
(two stores, one project, opposite-order hammering, exact accounting) +
heartbeat wiring pins ×3 apps. Live: the new-image verification job ran
with zero deadlock/containment lines. The semantic per-consumer TTL model
stays open in the doc (criterion 3); BACKLOG row moved P0 → P2.

**Task 4 — residuals bundle** — commit `ef2d7c4d`; two docs → docs/done/.
(a) `complete_job` gates freeze persist on
`should_persist_completion_freeze` (a `workspace_unavailable` completion
can only echo a stale blob); the recovery arm pauses via the new
`pause_job_shed_freeze` (processing-CAS + stash-and-clear) so
paused-implies-dispatchable holds. (b)
`_provision_parent_workspace_for_scholar` now surfaces the provisioner's
recorded error on the job row (the dispatcher's own arm was already fixed
by `4f27f581`; the done-doc's papercut note updated). (c) The wall-clock
watchdog arm: `unstick_reviewing_parents_wallclock` — EXISTS-live-critic
complement of the dead-critic arm, `REVIEWING_WALLCLOCK_CEILING_MINUTES`
(default 60, 0 disables), distinct "critic did not render a verdict in N
minutes" message, sweeper tick step 3 + owner notify. Tests: 45 unit + 26
real-Postgres (3 new wall-clock behavioral). Live k3d: a synthetic
reviewing parent (updated_at −75 m) with a live 'processing' critic was
escalated on the next in-cluster tick with the exact message + WARNING.

**Task 5 — ide_settings sweeper dials dead workspaces** — commit
`1e01c893`; doc → docs/done/. Worklist gains parent-status gates (jobs
NOT IN terminal, threads NOT IN ended/deleted) — live worklist collapsed
48 rows (24/24 jobs terminal, 22/24 threads ended) → 1 (the live
session); new `evict_dead_workspaces` probes container rows via
`workspace_pod_live` and clears confirmed-dead context; the
`delete_workspace` 404 branch (the leak) now clears status + pod_ip; the
sweeper is `run_when_leader`-gated (disabled branch parks). Live k3d:
before = 275 "No route to host"/90 min; after = ZERO dead-endpoint probes
across all observed cycles, and the evictor fired on two planted dead-pod
rows ("evicting thread … workspace pod confirmed dead"). Live pulls of
the real session workspace ran clean; an affirmative "synced N file(s)"
line was not observed because newest-mtime-wins kept existing store
content — merge semantics untouched by this fix and pinned by the
existing unit tests. Rollouts visibly calmer (the serial dial stall was
part of the rocky-rollout pattern batch #1 recorded). Residual hardening
kept in the doc: dial the stable Service DNS instead of pod_ip.

**Task 6 (stretch) — phase tags moved then rejected** — commit
`78899407`. `tag()` is create-once (no `-f`; no-op at same HEAD; typed
`TagInvariantViolation` ERROR + refusal on divergence), commit-then-tag in
`_complete_phase_with_git`, per-ref delivery via new `push_ref`
(`push()` default `tags=False` — no more `--tags` spray incl. external
repos). Live k3d A/B: old-image job `803df0bd`'s
`phase-1-tactical-complete` dereferences to the todo commit BEFORE the
completion commit (defect live); new-image job `012ea267`'s three tags
dereference EXACTLY to their completion commits; zero already-exists/
invariant lines in its agent logs. Residuals kept in the doc: the
duplicate phase-completion transition (graph exactly-once) + tag-
independent review evidence.

**Commits (7, in order):** `2b081109` inventory regen · `33fe6684`
session-wipe guard · `ef2d7c4d` residuals bundle · `33e326a5` memory
containment · `78899407` phase tags · `1e01c893` ide sweeper · plus this
docs/BACKLOG commit. Nothing staged beyond these; `HomeLab/` and the
strategy session's parallel commits (`171e7e06` et al., interleaved on
develop mid-session) left untouched. NOTE: that session also REBASED the
unpushed stack mid-flight, renaming every SHA once already — until the
branch is pushed, the commit SUBJECTS above are the stable identifiers,
not these hashes.

**Pytest**: baseline at session start = 13 failed / 14351 passed
(batch #1's 11 env-noise residuals + the 2 inventory breaks). Final =
**11 failed / 14410 passed** — failure diff vs baseline is exactly the
two inventory tests fixed; the 11 residuals are the same known env-noise
set (local-Postgres `test_database_phase1`, the `test_mcp_manager`/
`test_mcp_agent_wiring` stack, `tools/research` installed-client
contracts). +59 net new passing tests.

**Environment notes for the next batch:**
- Agent-tool worktrees base on MAIN — detach onto the develop SHA before
  implementing (bit all three implementers).
- `git mv` after editing a file stages the PRE-EDIT blob; `git add` the
  moved path explicitly or the commit silently carries old content (bit
  this session twice).
- Tilt syncs every orchestrator edit immediately — batch such edits;
  expect multi-ReplicaSet churn windows; pool agent pods keep old images
  until recycled, so verify fixes by runtime log lines, not image tags
  (the ConfigMap tag updates before existing pods do).
- Session-path verification rig that works end-to-end via API only:
  POST /api/persistent/threads (id_token auth per the smoke-auth recipe)
  → agent attaches server-side without any WebSocket client; delete the
  agent pod or wait for idle-suspend to force detach; POST …/resume
  re-attaches. Synthetic SQL rows work for sweeper/watchdog arms (insert
  reviewing-parent + live-critic pairs; thread rows with dead-pod
  container context — use status='awaiting_user' to dodge the session
  reconciler re-provisioning them, which 'active' triggers).
- Test jobs left on the cluster: `803df0bd`, `012ea267` (both
  pending_review, deliberate A/B pair), session thread `1131cea3` (live),
  `0870dec3` (ended). Safe to clean.

## Session log 2026-08-06 (batch #3)

Autonomous ~4.5 h implementation session on `develop` (local k3d via Tilt),
night after batch #2. Four tasks, five commits, nothing pushed by this
session. Mid-session a parallel actor pushed the pre-existing 19-commit
stack to origin (origin/develop == pre-batch-#3 HEAD) and edited
`.github/workflows/*` in the shared working tree — those files were left
strictly unstaged.

**Task 1 (main) — finalization decisions journal
(`job_finalization_decisions_held_only_in_process_memory`, now docs/done/)**
— commit `fix(finalization): journal-before-observe — job-ending decisions
survive restarts`. The two job-terminating decisions lived only in
module-level dicts; any restart converted "I decided" into "no decision"
and phase.py fabricated a placeholder report. Now: `job_complete` journals
the decision on the job row through the orchestrator BEFORE returning
(`POST/GET /api/jobs/{id}/completion-decision`, idempotent on
`(job_id, tool_call_id)` via `InjectedToolCallId`, CAS vs terminal rows,
one atomic statement on the row the status transition later updates); the
audited tool node mirrors journaled decisions into checkpointed state
(`completion_decision`/`verdict_decision` + a real `is_final_phase=True` —
the flag was never set anywhere before); finalize reads cache →
checkpointed mirror; resume hydrates the cache from the durable record
(never on feedback resumes — `queue_job_for_resume` voids the journal in
the same statement, opt-out for the re-provisioning park); the placeholder
fabrication is dead (worker → loud reject_transition; no-verdict critic →
honest zero-confidence report + ledger escalation). Verified: 20 new unit
tests + 4 real-Postgres pins + endpoint-inventory regen, and BOTH k3d
kill-tests: worker job `965b0935` — journal at 22:30:08, pod force-killed
the same second (freeze report never delivered), re-dispatch at 22:31:57
logged `Hydrated completion decision … (tool_call_id=chatcmpl-tool-8b6e…)`
and froze with the ORIGINAL summary field-for-field; critic `97c2dcd6` —
ledger round at 22:37:02, pod killed +1s, resumed critic logged `Recovered
critic verdict from graph state (process cache empty after restart)`,
rebuilt the APPROVED verdict freeze, orchestrator resolved the target
`pending_review` from the ledger. No migration needed (JSONB context).

**Task 2 — embedding batch overflow
(`embedding_batch_overflow_skips_citation_source_embeddings`, stays in
issues/ for the ops tail)** — commit `fix(embeddings): split batches at the
provider cap`. `EmbeddingService.embed_batch` is now THE batching seam:
order-preserving slices ≤ `EMBEDDING_MAX_BATCH_SIZE` (default 64 = the TEI
limit), transient-only retry via the shared `llm_retry` policy with
deterministic 422/400 pinned `never_retry` (the shared classifier defaults
unknown 4xx to "transient" — caught by the new tests), typed
`EmbeddingInvalidVectorError` for NaN/Inf, per-source
`metadata.embedding_state` (complete/failed + typed reason),
`embedding_coverage` in citation statistics, and
`scripts/backfill_source_embeddings.py` (dry-run default). Verified: 13
unit tests against a mock 64-cap-422 provider; backfill dry-run against
the k3d vector DB reported the live gap (54 (job, source) pairs across 5
jobs). Owed: `--apply` run against main (where the 359 skipped sources
live) + a source-heavy re-run for the zero-422 acceptance criterion.

**Task 3 — doc-truth sweep** — commit `docs(sweep): batch #3 doc-truth
sweep`. 8 read-only sonnet agents verified 83 docs (every
BUILT/IMPLEMENTED/uncommitted claim + both ambiguous BACKLOG tiers)
against HEAD. Tally: **52 confirmed shipped/archive → docs/done/** with
inline evidence notes · **15 statuses corrected** in place · **16 verified
accurate** (no edit) · **0 vanished** — every "uncommitted" claim had
actually landed, mostly under rebased SHAs. The surprising failure mode
was the OPPOSITE of the feared one: two docs claimed unbuilt work that was
fully built (`codex_session_gateway_baseurl_401` superseded-but-resolved;
`session_turn_hard_fails_on_transient_llm_outage` tracks 1+2 shipped), and
two memory-index conflicts resolved in favor of the code (tool-enum-vocab
fix committed `781285c9`; tool-surface S1 committed `99b87008`). Four NEW
gaps discovered and recorded: `_send_session_attach` 4th call site missing
`config_name=` (sessions.py:363), `sudo_request_status` REST/MCP filters
never Literal-typed, `registered_tools` doc self-contradiction (resolved),
`mcp_client` defect 1 (flat 30s timeout) still open. BACKLOG tables
pruned: Built-but-unshipped 22 → 3 rows; Diagnosed 40 → 23 rows. Memory
index corrected (3 files + 4 MEMORY.md lines).

**Task 4 — batch-#2 residuals** — commit `fix(residuals): sweeper dials
stable Service DNS; archive_phase exactly-once`. (a) `resolve_ssh_target`
prefers `workspace_container.host` (headless-Service DNS, survives pod
restarts) over the ephemeral `pod_ip` (legacy fallback kept); per-cycle
dial-mix log line live on k3d: "IDE settings sweeper: 1 workspace(s), 1
dialed via stable service DNS" — ide_settings done-doc now fully closed.
(b) `archive_phase` exactly-once per phase instance via the checkpointed
`last_archived_phase` key: the rejection-retry route no longer re-archives
the same boundary (phase-tags doc direction 4 closed; direction 5 is the
doc's last residual). 4+3 new tests; new agent image content-verified on
k3d (`tilt-7c7fa845` carries guard + journal).

**Commits (5, in order):** `docs(sweep): batch #3 doc-truth sweep — 52
verified-shipped docs to done/, 15 status corrections` ·
`fix(embeddings): split batches at the provider cap — citation sources
index again` · `fix(finalization): journal-before-observe — job-ending
decisions survive restarts` · `fix(residuals): sweeper dials stable
Service DNS; archive_phase exactly-once` · plus this docs/BACKLOG commit.
Subjects are the stable identifiers until push. Nothing else staged; the
parallel session's `.github/workflows/*` edits and `HomeLab/` left
untouched.

**Pytest**: baseline at session start = 11 failed (batch #2's known
env-noise set). Final = **11 failed / 14542 passed / 26 skipped** — the
failure set is byte-identical (local-Postgres `test_database_phase1`, the
`test_mcp_manager`/`test_mcp_agent_wiring` stack, `tools/research`
installed-client contracts); zero regressions from this batch. +132 net
new passing tests vs batch #2's close (44 new test functions from this
session: 20 journal + 4 real-PG queue + 13 embedding + 4 archive-guard +
3 ide-target, plus parametrizations and the parallel session's
contributions).

**Environment notes for the next batch:**
- Read-only sweep agents share the main working tree — one misread my
  in-flight main-loop edits as "unauthorized subagent changes" and spent
  effort re-verifying around them. Brief such agents explicitly that the
  main loop edits files concurrently.
- A parallel actor can push origin and edit the working tree mid-session:
  re-check `git status` before every staging step; the explicit-path
  staging rule (never `git add -A`) is what kept the workflow edits out.
- Kill-test rig (reusable, scripts in the batch-#3 scratchpad pattern):
  API-created job with `config_override {autonomy: review, verification:
  {enabled: true}}`; watcher greps agent logs for "marked as final phase"
  (worker) or orchestrator logs for the verification-rounds POST (critic),
  then `kubectl delete pod --grace-period=0 --force`; orphan re-dispatch
  landed in ~2 min. Transient `kubectl run` pod (`imgcheck-*`) is the
  cheap way to content-verify a freshly built image tag.
- The verdict POST → checkpoint window is tight (~1s) but the checkpoint
  captured the tool result even with the same-second kill — the
  state-mirror recovery path is reachable in practice, not just in theory.
- Test jobs left on the cluster: `965b0935` (pending_review, the kill-test
  A/B pair's target) + critic `97c2dcd6` (completed). Safe to clean.

## Session log 2026-08-06 (shell-gating + date injection)

Short interactive session, started from a user question about one dev-cluster
session — *"why did the agent try to use a shell tool if he doesn't even have
access to it?"* Session `c90f83b7` (`session_base`, supervised, `virtual`
backend) called `shell_execute`, waited 53 s, got `Tool 'shell_execute' not
found`, and could not answer whether a steam railway runs *today*. Diagnosis
found three defects, all fixed, committed `f36a9713` and pushed to
`origin/develop` by a parallel actor mid-session. Full writeup:
`docs/done/session_calls_absent_shell_tool_and_cannot_resolve_today.md`.

**What was actually wrong.** The capability gate was correct — `virtual`
declares `supports_shell=False` and `session_base` has `tools.shell: []`, so
60 requested tools bound down to 43 with zero shell. What failed was
everything around it: (1) the prompt advertised shell unconditionally in every
family variant, and in *persistent-mode wording* ("shell **tabs**"), which is
why the model reached for `shell_execute` rather than `run_command` — the
prompt named the flavour; (2) nothing anywhere injected the current date, so
an agent asked about "heute" needed a shell to run `date` — this was the only
defect that changed the user-visible answer; (3) the tool-existence check sat
*after* the permission gate, so a supervised user was shown an approval card
for a tool that could not run either way, which is where the 53 s went.

**Fixes.** `with_current_date()` stamps `Current date: YYYY-MM-DD (Weekday,
UTC)` into both branches of `get_phase_system_prompt`, re-stamped every turn by
`persistent_graph` (sessions run for weeks — a date baked in at setup freezes
on creation day). Day granularity is deliberate: the system message heads the
provider prompt-cache prefix, so a per-turn timestamp would bust the cache
every turn. Rewritten in place, not appended, so the product-guide floor stays
the tail. New `{% if has_shell %}` gate across 4 interactive + 7 tactical + 3
strategic + 10 datasource-CLI blocks, backed by `_has_shell_tools` reading the
registry category (the shell tools are mid-rename; a name list would rot).
`tool_map.get()` moved above the permission gate, batch announce filtered to
bound names, and `_unavailable_tool_message` now names the cause and says "do
not retry".

**Verification.** New `tests/test_prompt_shell_gating.py` runs against the
*shipped* templates (a synthetic template would not have caught this), plus 3
date tests and 7 gate-ordering tests — **6 of those 7 fail against pristine
HEAD**, checked in a throwaway worktree, so they catch the old behaviour rather
than describe it. Full suite 14154 passed / 12 failed, the 12 being the known
environment set. k3d smoke passed on two `virtual` sessions: autonomous
answered the date question with zero tool calls (audited reasoning trace: *"The
current date provided in the system prompt is 2026-08-06 (Thursday, UTC)"*);
pushed to run `date`, the model *did* emit `shell_execute` and the log shows
`rejecting before the permission gate`, recovering in one turn without
retrying; the supervised repeat left **no** `thread_permission_requests` row
for the phantom.

**Two traps worth carrying forward:**
- `srw_cloud_status` is `category: "shell"` but `grant: "code"` and is
  re-appended *after* `filter_tools_by_backend`, so "any shell-category tool ⇒
  has shell" re-opens the gated blocks on exactly the virtual-tier-with-cloud
  sessions the gate protects.
- `{% endif -%}` strips the whitespace after it, eating a following blank line.
  Put the blank inside the block, and prove the gate is a no-op by diffing
  rendered output against `git show HEAD:<file>` with a shell bound — it must
  be byte-identical.
- Tilt built a partial edit again (image had `_CATEGORY_LABELS` used but not
  defined — a latent `NameError` behind a healthy-looking tag). md5sum image
  contents against the working tree before trusting any smoke result.

**Left open:** the worker path's tool-not-found handling (LangGraph `ToolNode`
in `src/graph.py`) was not touched — this fix is the session loop only.

## Session log 2026-08-07 (delivery-failure chain)

Closed `git_push_fails_silently_via_workspace_backend` → `docs/done/`, and
retired its "job `40efbb39`'s real push cause still undiagnosed" row. That row
was wrong on a timezone: the 08-06 sweep read `f41970ae`'s local-time commit
stamp (`12:41:31 +0200`) against the job's UTC log timestamps and concluded the
incident predated the CWD-banner regression. `12:41 +0200` is `10:41 UTC`; the
job first failed to push at `11:27 UTC`, 45 minutes later, on the image built
from that very commit (`sha-f41970a`). Same cause, already diagnosed by
`22b2511e`. Nothing was owed.

What was genuinely open was the *consequence* handling — a failed push was not
treated as a failure anywhere. Four commits close that end to end:

| Layer | Before | Now |
|---|---|---|
| `src/core/phase.py` | `push()`'s return discarded at all four job-ending sites | ERROR log + `delivery_failed` / `delivery_error` on the freeze record (`abe4d1d1`) |
| deliverable gate | every manifest entry read "missing" → **bounced** the agent to redo work it had already done | fifth fail-open skip case, carrying the agent's reason (`233b2649`) |
| verification trigger | spawned a critic against an empty repo → returned the job for undelivered work | escalates with the real reason (`728214bf`) |
| `_escalate_target` | — | reason lands in `error_message`; loop jobs stay terminal (inherited) |

Ordering mattered more than the individual guards: the deliverable gate runs
*before* verification and a bounce early-returns, so until `233b2649` landed,
`728214bf` could never fire for any job carrying a `required_deliverables`
manifest. Triaging the gate as "a separate, lower-priority slice" was wrong.

Also shipped in the same session (`a4929b17`): `VIRTUAL_DIRS_ENABLED` removed.
Its "off" position materialized `instructions.md` / `task_brief.md` into the
workspace root, which on a workspace-inheriting subjob reopened
`critic_brief_lands_in_shared_workspace_and_misleads_target` every time the
lever was pulled. The guarantee it stood in for — never boot an agent with no
task — moved to `src/graph.py`, which now raises instead of warning when both
briefs resolve empty, covering every cause rather than that one.

**Still owed, and unchanged by any of this:** the verification loop has never
been observed converging (return → fix → approve). Two live-gate attempts died
before round 2 on unrelated infrastructure — the second on the silent-push
defect above, which is now fixed. See
`verification_fail_closed_followups.md` for what a third run needs.

## Session log 2026-08-08 (stateless agents — S1 spine build night)

Overnight subagent-driven implementation of `docs/features/stateless_agents.md`
§9/S1, all on branch `feature/stateless-agents` (chart-gated off; lane
per-thread via `threads.execution_lane`, default `pinned`). Five build
milestones + a live fault-injection matrix on k3d; full lab notebook in
`docs/research/stateless_agents/implementation_log.md`.

Shipped: migrations 0115 (`run_queue` + `execution_lane`) / 0116
(`events_seq_hwm`); `src/shared/run_queue/` (claim/heartbeat/fence/complete/
reap, 73 real-PG tests) + `src/shared/event_journal/`; conditional epoch
resolution (reuse unless terminal — kills the reattach cache-wipe cascade for
the pinned lane too, live-verified); `--mode stateless` turn executor with
lease-fenced persistence and pop-first embedding-env scrub (closes a live
cross-tenant residue); orchestrator enqueue-on-input / claim-bundle (lease
proof) / leader reaper (`turn.interrupted` frames) / admin read model;
`agent-stateless` helm Deployment. Fault matrix all PASS: steal at t+88 s with
exactly-once answers, frozen-zombie heartbeat abort (`lease lost` live line),
skip-if-answered < 8 s, FIFO drain across a steal, epoch +1 per steal / 0 per
clean handoff. Found+fixed live: NULL consumed-watermark floor re-answered
pre-queue history → creation now initializes `consumed_seq = input_seq - 1`.
Final gate 539 + 73 tests green, ruff clean.

**Open (ranked):** (1) affinity fingerprint never matches (volatile bundle
credentials) → every turn pays the full ~100 s re-attach; fix = stable-subset
fingerprint + DB-side last-pod claim bias, then drain-in-lease batching.
(2) attach-cost decomposition/caching per doc §5.3.3. (3) provisioning gate
(3 recorded locations; `/prepare` needs a cockpit design decision) + cockpit
compat. (4) live cross-tenant scrub probe (needs a second user identity);
interrupt on the lane is 501.

## Session log 2026-08-08 (stateless agents — S1 performance tuning)

**Turn latency 99.6 s → 5.4 s cold / 3.0 s warm** on the same k3d thread, all
numbers from running pods. Branch `feature/stateless-agents`.

Shipped: attach-time + teardown cloud `pull_all` skipped on the stateless lane
(duplicate full-tree walks, 41 s each); one `Depth: infinity` PROPFIND replaces
the `webdav3` per-directory walk (39.9 s → 0.55 s, capability-probed with a
permanent fallback); bulk `list_files_with_sizes` + parallel stat batches;
attach fingerprint excludes `resolved_config.resolved_at` (the sole volatile
field, and the reason the warm cache never hit); **migration 0117**
`run_queue.last_leased_by` + a 2 s affinity grace in the general claim, cleared
on release and reaper steal; executor stays in fast poll cadence while warm and
drops a warm session after 300 s idle; scoped metadata index on the virtual
backend (setup 51 rclone spawns / 7.2 s → 13 / 1.5 s) plus idempotent `mkdir`.

Measured and deliberately NOT built: a persisted resolved-config cache (server
resolution is **70 ms**/claim) and drain-in-lease (with affinity working, a
3-message burst drains on one pod in 11 s with `attach=0.00s`; the only saving
left is the 0.05–0.09 s bundle fetch). Both recorded with their numbers in
`docs/features/stateless_agents.md` §5.3.3/§5.3.4 so neither gets re-proposed
from code reading.

Verified: fault matrix re-run after the claim-SQL change — pod force-deleted
mid-turn → steal at t+97 s → exactly one answer, epoch +1, affinity hint
cleared. 81 real-PG run_queue tests (harness now applies 0115 **and** 0117),
11 new scoped-index tests. Full gate: **15 013 passed / 12 failed**, ruff
clean; 11 of the 12 reproduce on `develop` (py3.14 env noise). The 12th was
real and pre-existing on this branch — `APP_CURRENT_MIGRATION_HEAD` was left
at `0114` when session 1 added 0115/0116, so that tripwire had been red since
the spine landed. Fixed to 0117.

Commits `c290f525`→`6a360848` (5), on top of session 1's `e506fdfa`→`af5c38a0`.
Not pushed; `develop` untouched. Sibling docs updated with the pieces that
live outside the feature doc: `no_workspace_agent_mode.md` §5.1 (virtual-tier
op count + scoped metadata index), `cloud_collaboration_model.md` §4 (one
Depth-infinity PROPFIND per turn boundary; why the lane's attach/teardown
pulls were removed).

Gotchas re-confirmed: Tilt ships partially-edited images (`updateStatus: ok`
proves nothing — `kubectl exec … grep` every pod); **`git checkout` while Tilt
is up DEPLOYS that branch** — a few seconds on `develop` left a pod
crash-looping on `--mode: invalid choice: 'stateless'`; `kill -9 1` inside a
container is ignored, use `kubectl delete pod --force --grace-period=0`;
admin-cli `id_token` expires in ~15 min and fails as a silent 401.

Left: the remaining 13 setup store ops; `AFFINITY_GRACE_SECONDS` should derive
from the poll interval if cadences ever diverge; warm-TTL value unmeasured;
the fault harness needs a deliberately long turn now that turns are <5 s.

## Session log 2026-08-09 (stateless agents — consolidation)

Three branches merged back to one: `feature/stateless-agents` (unpushed,
`develop` untouched). `feature/stateless-sessions-s1-completion` fast-forwarded
in; `feature/stateless-workers-s3` merged with four conflicts; both topic
branches safe-deleted after verifying they were fully merged.

The conflict worth remembering: S1 restructured `register_agent` so hostname is
no longer an ownership credential, which moved the job-pause block out of the
`if existing:` branch, while S3 had added the `execution_lane='pinned'`
predicate to that same query. Taking either side alone silently drops the
other's fix. `schema_current.sql` auto-merged and was regenerated from the
merged migration set to confirm rather than assume — no diff.

Gate: 15,180 passed against the same 11 environment failures, ruff clean,
migrations 0115–0121 all applied on k3d, a turn answers end to end, worker lane
still off (zero `worker_batch` rows, zero non-pinned jobs).

Docs de-staled: §9.1's S1 and S2/S3/S4 sections rewritten as one current picture
(they had become layered dated appendices from three work streams); both Codex
briefs bannered as historical with what is still open; the completion-path
inventory's line numbers marked as drifted anchors post-merge.

Status of record is `docs/features/stateless_agents.md` §9.1. Session lane
functionally complete; worker lane blocked on Gate 3 (§5.4.5).
