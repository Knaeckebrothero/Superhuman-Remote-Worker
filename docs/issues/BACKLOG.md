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
| [embedding_batch_overflow_skips_citation_source_embeddings](embedding_batch_overflow_skips_citation_source_embeddings.md) | OPEN. P1 research-quality and load defect.** Source registration |
| [deliverable_lost_to_nested_repo…](deliverable_lost_to_nested_repo_commit_and_stranded_mode_a_job.md) (Defect 1 residue) | **Blocks the bench thread.** The gate and critic verify against the *remote branch*, so a failed push is indistinguishable from a lazy agent — the 08-01/08-02 CWD-banner outage burned all 5 of `cd3bfe52`'s critic rounds and both of `bbce4bed`'s bounces on phantom findings, and any future transport failure will corrupt bench results the same way. Fix: hold on `has_unpushed_commits()` at seal instead of emitting findings. Also: 5 of 6 `git_mgr.push()` call sites in `src/core/phase.py` still discard the return value. |
| [job_finalization_decisions_held_only_in_process_memory](job_finalization_decisions_held_only_in_process_memory.md) | Orchestrator restart mid-finalization loses decisions (restarts happen on every deploy). |
| [pod_oom_kill_protection](pod_oom_kill_protection.md) | Umbrella for the recurring OOM incident class. |

## P2 — real but bounded (open, workaround exists or blast radius small)

| doc | status |
|---|---|
| [bench_infra_exclusion_misses_midflight_outages](bench_infra_exclusion_misses_midflight_outages.md) | Open, analysis-level workaround exists. Found 2026-08-05 during |
| [project_scoped_memory_deadlocks_under_parallel_jobs](project_scoped_memory_deadlocks_under_parallel_jobs.md) | Containment tier **SHIPPED 08-06 (batch #2)** — ordered locking + contained/retried access stats + heartbeat telemetry. OPEN: the semantic per-consumer TTL model (criterion 3) and pinned-budget share (criterion 5). |
| [phase_boundary_tags_are_moved_then_rejected_by_remote](phase_boundary_tags_are_moved_then_rejected_by_remote.md) | Core **FIXED 08-06 (batch #2)** — create-once tags at completion commits, per-ref push, no `--tags` spray. OPEN residuals: duplicate phase-completion transition (graph exactly-once) + tag-independent review evidence. |
| [dispatcher_resume_pep_twin_still_fails_open](dispatcher_resume_pep_twin_still_fails_open.md) | OPEN, filed 2026-08-04 by the whole-branch review of the fix for |
| [registered_tools_no_config_can_grant](registered_tools_no_config_can_grant.md) | OPEN, diagnosed 2026-08-02 — the *invisible* half is fixed, the ten |
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
| [cloud_folder_invisible_until_owner_signs_into_cloud](cloud_folder_invisible_until_owner_signs_into_cloud.md) | OPEN — root-caused, deliberately not built. Deferred 2026-08-05. |
| [datasource_legacy_dead_code](datasource_legacy_dead_code.md) | Open — cleanup, no functional impact. Filed 2026-06-11 |
| [db_schema_hygiene](db_schema_hygiene.md) | open — first slice landed on `develop` (`f4160780`, 2026-06-11); |
| [delegation_light_mode_missing](delegation_light_mode_missing.md) | Open. Enhancement / design gap, **not** a regression — the existing |
| [dual_app_persistent_app_redundancy](dual_app_persistent_app_redundancy.md) | 🔴 **OPEN** — structural debt, filed for a deliberate later fix. |
| [failed_job_pvc_reclaimed_without_grace_period](failed_job_pvc_reclaimed_without_grace_period.md) | Designed 2026-07-25 from the job-`52949749` salvage. Not yet |
| [gitmanager_local_git_fallback](gitmanager_local_git_fallback.md) | Open — deferred hardening. Filed 2026-06-11 (fallout from the |
| [jobs_repo_clone_collision_on_first_dispatch_to_populated_workspace](jobs_repo_clone_collision_on_first_dispatch_to_populated_workspace.md) | FIX COMMITTED 2026-07-18 as `47c65582` on `develop`, NOT YET |
| [loop_critic_producer_identity_bias](loop_critic_producer_identity_bias.md) | OPEN / backlog. Not scheduled. Low urgency (see "Why this is latent, |
| [phases](phases.md) | Open |
| [remove_workspace_md_vestiges](remove_workspace_md_vestiges.md) | Backlog — deferred cleanup. Filed 2026-06-03. |
| [scholar_delegation_not_exercised](scholar_delegation_not_exercised.md) | Open. Behavioral issue (model instruction-following), **not** a config regression. The delegation capability is fully wired and was live for the affec |
| [session_db_experts_cannot_customize_interactive_prompt](session_db_experts_cannot_customize_interactive_prompt.md) | Deferred (intentionally). Low urgency — filed to capture the |
| [session_tool_group_enablement_is_computed_in_two_places](session_tool_group_enablement_is_computed_in_two_places.md) | OPEN by choice, not oversight. Drift is currently caught by a test |
| [unify_scholar_critic_subjob_provisioning](unify_scholar_critic_subjob_provisioning.md) | Backlog — deferred refactor (touches completion flow). Filed 2026-06-13. |

## Built but unshipped — finished work awaiting commit / deploy / live gate

Quick wins: the engineering is done; what remains is shipping and verification.

| doc | status |
|---|---|
| [cockpit_session_scroll_pin_misses_late_height_changes](cockpit_session_scroll_pin_misses_late_height_changes.md) | RO FIX BUILT + BROWSER-VERIFIED on k3d 2026-07-15 · UNCOMMITTED.** Fix 1 (the ResizeObserver detector), Fix 2 (the handler + `pinTarget`), Fix 3 (the |
| [codex_cached_tokens_not_metered](codex_cached_tokens_not_metered.md) | root-caused + FIX BUILT 2026-07-10 (uncommitted). Extraction fix across 4 files, unit-tested; k3d live-verification steps below. |
| [codex_proxy_context_window_cap](codex_proxy_context_window_cap.md) | BUILT 2026-07-10, uncommitted** — code + helm + unit tests + DB re-seed done; needs an agent image rebuild to deploy. Diagnosed from session `4b82e6db |
| [codex_session_gateway_baseurl_401](codex_session_gateway_baseurl_401.md) | FIX IMPLEMENTED** (develop, uncommitted) · root cause confirmed + empirically verified on dev · fix + RED-verified regression tests green locally (161 |
| [expert_prompts_shadowed_by_family_variants](expert_prompts_shadowed_by_family_variants.md) | Part 1 **implemented, verified on k3d, and committed** (2026-06-25). |
| [jobs_repo_clone_timeout_abandons_healthy_transfer](jobs_repo_clone_timeout_abandons_healthy_transfer.md) | fix implemented on `develop` 2026-07-19 (clone wait-and-verify in |
| [llm_infra_404_misclassified_permanent_kills_jobs](llm_infra_404_misclassified_permanent_kills_jobs.md) | Slices 1–3 BUILT (2026-07-18), unit-tested (`test_graph_helpers.py` |
| [openrouter_auxiliary_crashes_session_via_memory_reranker](openrouter_auxiliary_crashes_session_via_memory_reranker.md) | investigated + fix decided + **IMPLEMENTED & k3d-verified 2026-07-03 |
| [openrouter_auxiliary_misrouted_to_openai](openrouter_auxiliary_misrouted_to_openai.md) | ROOT-CAUSED + FIXED (uncommitted on `develop`); unit-tested; live |
| [persistent_session_idle_expiry_message_swallow](persistent_session_idle_expiry_message_swallow.md) | Fix implemented on `fix/bff-idle-session-message-swallow` (2026-06-23) · root cause **confirmed + isolated to the idle branch** + **reproduced end-to- |
| [remove_litellm_proxy_and_gateway_concept](remove_litellm_proxy_and_gateway_concept.md) | REMOVAL IMPLEMENTED.** We removed the self-hosted LiteLLM proxy and, with it, the "route all LLM traffic through a self-hosted gateway" concept — **fo |
| [remove_local_browser_fallback](remove_local_browser_fallback.md) | Implemented on develop 2026-06-11 — full inventory removed (plus |
| [reranker_transient_fault_hard_fails_job](reranker_transient_fault_hard_fails_job.md) | investigated 2026-07-04; **fix A+B IMPLEMENTED 2026-07-05** — bounded transient-only retry in `RerankerScorer._rerank` (2 extra attempts, exponential |
| [session_empty_response_gpt5_codex_stop](session_empty_response_gpt5_codex_stop.md) | Mitigation implemented 2026-06-23** (develop, branch `fix/session-empty-response-retry`) · root cause **isolated, not synthetically reproducible** (≈1 |
| [session_reliability_investigation_index](session_reliability_investigation_index.md) | All docs listed here are **uncommitted on `develop`** as of 2026-07-02 (except where a doc notes its own fix has been implemented). |
| [session_silent_failure_audit](session_silent_failure_audit.md) | 2026-06-12 — #1, #2, #3, #8, #9, #10, #11, #12, #13, #14, #16 **implemented** (same day, unit-verified: pytest + vitest green, not yet cluster-verifie |
| [session_turn_end_cloud_push_blocks_queued_input](session_turn_end_cloud_push_blocks_queued_input.md) | FIX BUILT 2026-08-06 — see "The fix" below. Live repro on dev |
| [session_uploads_never_extract_archives](session_uploads_never_extract_archives.md) | IMPLEMENTED 2026-08-02 on `develop`, not pushed and not |
| [shell_cwd_drifts_and_the_anchor_is_unreachable](shell_cwd_drifts_and_the_anchor_is_unreachable.md) | IMPLEMENTED** in `f41970ae` (all four slices, 15 files) and documented in `e7d29b2d`, |
| [subjob_inherits_stale_workspace_container_snapshot](subjob_inherits_stale_workspace_container_snapshot.md) | FIX IMPLEMENTED + k3d-verified 2026-07-10, UNCOMMITTED. Root cause |
| [tool_configuration_defects_and_fix_roadmap](tool_configuration_defects_and_fix_roadmap.md) | IMPLEMENTED **and live-gated** 2026-08-02/03 on `develop`, **not |
| [vm_ssh_readiness_probe_unroutable_from_orchestrator](vm_ssh_readiness_probe_unroutable_from_orchestrator.md) | IMPLEMENTED (2026-07-09, uncommitted on `develop`) — root cause confirmed |

## Diagnosed / investigated, unranked (triage when touching the area)

| doc | status |
|---|---|
| [agent_tool_fixed_vocabularies_invisible_to_model](agent_tool_fixed_vocabularies_invisible_to_model.md) | DIAGNOSED 2026-07-15 from live evidence + code audit (3-agent sweep) · P0–P5 BUILT + verified 2026-07-15 · UNCOMMITTED.** The failure was *observed*, |
| [approving_a_critic_wedges_target_in_reviewing](approving_a_critic_wedges_target_in_reviewing.md) | CONFIRMED reachable in code. Not observed live — a DB check is |
| [bound_skill_missing_from_resume_blob_deadlocks_phase_transition](bound_skill_missing_from_resume_blob_deadlocks_phase_transition.md) | Delivery-path defect CONFIRMED in code + live incident |
| [critic_brief_lands_in_shared_workspace_and_misleads_target](critic_brief_lands_in_shared_workspace_and_misleads_target.md) | Observed live. Root cause is a direct interaction between two |
| [datasource_cli_mode_dead_on_remote](datasource_cli_mode_dead_on_remote.md) | Diagnosed 2026-07-16 (live_session_settings.md P0.5 verification). |
| [deprecate_docker_compose_stack](deprecate_docker_compose_stack.md) | Proposed. Migration to local k3d verified end-to-end 2026-05-28. |
| [dev_snapshot_ssh_key_perms_0444](dev_snapshot_ssh_key_perms_0444.md) | - [x] Diagnosed + verified empirically (2026-06-22) |
| [drain_freeze_overwrites_critic_verdict](drain_freeze_overwrites_critic_verdict.md) | CONFIRMED in code. No live incident identified yet — but the |
| [feedback_resume_restricted_closure_toolset](feedback_resume_restricted_closure_toolset.md) | Observed 2026-07-26 on dev (job `52949749`, round-2 correction). |
| [git_push_fails_silently_via_workspace_backend](git_push_fails_silently_via_workspace_backend.md) | Root cause found for the *silence*. The underlying push failure is |
| [ide_settings_sweeper_probes_stale_workspace_endpoints](ide_settings_sweeper_probes_stale_workspace_endpoints.md) | **FIXED 08-06 (batch #2)** + live-verified (worklist 48→1, zero dead-endpoint probes, evictor + leader gate). Residual hardening only: dial the stable Service DNS instead of pod_ip. |
| [jsonb_isinstance_guard_without_parse_silent_dead_paths](jsonb_isinstance_guard_without_parse_silent_dead_paths.md) | Mechanism CONFIRMED in code. Individual instances tagged below — |
| [local_e2e_testing](local_e2e_testing.md) | response** (published by simulator): |
| [loop_advance_nonatomic_wedges_loop](loop_advance_nonatomic_wedges_loop.md) | investigated 2026-07-05 — root cause confirmed end-to-end from the live wedge, incident recovered by hand; **fix 2 (sweeper self-heal) IMPLEMENTED & k |
| [loop_job_workspace_lost_wedged_in_recovery](loop_job_workspace_lost_wedged_in_recovery.md) | Investigated on the live main cluster + a 5-agent code audit (2026-06-29). The recovery state machine is now **fully traced at code level** and the me |
| [loop_subagent_forensics](loop_subagent_forensics.md) | Analysis / findings.** The `spawn_subagent` light fan-out is **LIVE and working** on the |
| [main_cloud](main_cloud.md) | Audit / issues log — *not* a design doc. Captured 2026-06-05; **resolutions tracked inline (per-issue Status lines + the Status column below); last up |
| [maxsessions_parallel_tools_false_workspace_death](maxsessions_parallel_tools_false_workspace_death.md) | Designed 2026-07-24 from the 2026-07-23 incident (job `52949749`, |
| [mcp_client_timeout_retry_false_failure_shared_auth_headers](mcp_client_timeout_retry_false_failure_shared_auth_headers.md) | Filed 2026-08-01 from the VM-session re-gate teardown (dev, |
| [orchestrator_mongodb_cascading_failure_resilience](orchestrator_mongodb_cascading_failure_resilience.md) | Architectural gap. Surfaced during the 2026-05-12 outage |
| [orchestrator_tool_surface_fragmentation](orchestrator_tool_surface_fragmentation.md) | decided and in execution — see |
| [persistent_session_runaway_generation_context_explosion](persistent_session_runaway_generation_context_explosion.md) | (updated 2026-06-11):** the *wedge* and the surrounding hardening are |
| [persistent_session_swallowed_sends_and_truncated_history](persistent_session_swallowed_sends_and_truncated_history.md) | Root cause verified (all three). Fixes not started. |
| [phase_model_overhead_amnesia_loop](phase_model_overhead_amnesia_loop.md) | 🟡 **IN PROGRESS** — filed 2026-07-31 after a code-side deep |
| ~~recovery_pause_repersists_stale_freeze_invisible_job~~ | **FIXED 08-06 (batch #2)**, moved to docs/done/ — completion freeze-echo guard + `pause_job_shed_freeze` on the recovery arm. |
| [results](results.md) | pending_review, confidence 1.0 |
| [scholar_selfprovisioned_workspace_misclassified_as_inherited](scholar_selfprovisioned_workspace_misclassified_as_inherited.md) | ROOT CAUSE CONFIRMED on live local k3d 2026-07-13 **and observed on |
| [session_deliverables_in_workspace_output_not_in_cloud_files_button](session_deliverables_in_workspace_output_not_in_cloud_files_button.md) | Filed — root cause confirmed on live session `7692637b-9c60-4698-9875-b57ec34e66a6` (main cluster, cloud-mounted). **Reconfirmed + deeper root cause f |
| [session_turn_hard_fails_on_transient_llm_outage](session_turn_hard_fails_on_transient_llm_outage.md) | investigated 2026-07-10 from session `4b82e6db`. **Track 0 (codex-proxy memory bump) SHIPPED + live** — `helm/values.yaml` codex-proxy limit 256Mi→2Gi |
| [session_vm_backend_never_attaches](session_vm_backend_never_attaches.md) | Diagnosed 2026-07-26 from live dev thread |
| [snapshot_restore_dead_for_jobs](snapshot_restore_dead_for_jobs.md) | Filed from a 5-agent code audit (2026-06-29) prompted by job `19707fa1`. **Confirmed at code level, not fixed.** This is the systemic sibling of `loop |
| [subjob_trigger](subjob_trigger.md) | `pending_review` (should have been `reviewing` with critic spawned) |
| [task_clearance_user_feedback](task_clearance_user_feedback.md) | Needs fix |
| [transient_db_error_hard_fails_job_and_destroys_vm](transient_db_error_hard_fails_job_and_destroys_vm.md) | CONFIRMED against live dev (`--context main`). |
| [vm_guest_boots_to_emergency_shell](vm_guest_boots_to_emergency_shell.md) | Observed 2026-07-26 on dev, VM |
| [vm_session_thread_repo_clone_unroutable_gitea_url](vm_session_thread_repo_clone_unroutable_gitea_url.md) | Filed 2026-08-01 from the VM-session re-gate (dev, image `sha-99c9aba`, |
| [vm_upgrade_pause_workspace_reaped_before_approval](vm_upgrade_pause_workspace_reaped_before_approval.md) | investigated 2026-07-08 from a live incident on the main cluster |
| [vm_workspace_snapshot_unreachable_from_orchestrator](vm_workspace_snapshot_unreachable_from_orchestrator.md) | Found 2026-07-28 during the live gate of `6d66f7c4`; scope widened |
| [web_search_masks_tavily_errors_as_no_results](web_search_masks_tavily_errors_as_no_results.md) | Filed — root cause confirmed on the live main cluster. The *quota* instance was fixed operationally (Tavily key usage limit raised, 2026-06-26); the * |
| [workspace_suspension_infers_tier_from_metadata_presence](workspace_suspension_infers_tier_from_metadata_presence.md) | Found 2026-07-27 while fixing Defect 1 of |

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
