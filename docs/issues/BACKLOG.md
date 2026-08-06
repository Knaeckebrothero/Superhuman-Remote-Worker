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
| [fresh_job_dispatched_as_resume_skips_seeding](fresh_job_dispatched_as_resume_skips_seeding.md) | **Active regression (08-05).** Never-started jobs routed down `/job/resume` start brief-less and strand; every legit resume serves an empty virtual br |
| [session_workspace_wiped_by_agent_clone_on_attach](session_workspace_wiped_by_agent_clone_on_attach.md) | **Data loss.** Unguarded `rm -rf`+clone on attach now wipes a *durable* PVC. Fix = port the job-path content probe. |
| [lifecycle_session_agents_without_thread_never_drain](lifecycle_session_agents_without_thread_never_drain.md) | **Active resource leak.** Session agents without a thread never drain — pods accumulate until manual cleanup. |
| [rejected_verdict_livelocks_critic_and_wedges_parent](rejected_verdict_livelocks_critic_and_wedges_parent.md) | **Live critic wedge.** Rejected verdict livelocks the critic and wedges the parent (105 min before manual cancel). Same family: stale_critic_waiting_s |
| [project_scoped_memory_deadlocks_under_parallel_jobs](project_scoped_memory_deadlocks_under_parallel_jobs.md) | **P1-rated concurrency defect** in project-scoped memory under parallel jobs — directly undermines multi-job projects. |

### P0-adjacent, tracked outside this directory

- **Mode A cloud review dead zone** — Defect 3 of
  `deliverable_lost_to_nested_repo_commit_and_stranded_mode_a_job.md`:
  219/220 Mode A jobs stranded, zero exports ever. Direction already decided
  (retire Mode A per `docs/features/workspace_and_change_records.md` §6.3),
  needs execution.
- ~~Stale-agent detector SQL crash~~ — **resolved + deployed to prod 07-12**
  (`docs/done/stale_agent_detector_sql_crash_disables_recovery_sweeps.md`);
  open residue = job-execution-lease stages 4–5 and two feature docs
  uncommitted.
- ~~New Session 400 on dev~~ — fix **committed `6b727734` (07-29)**;
  verify it deployed, then close.

## P1 — next (confirmed defects, high value; first row blocks the bench thread)

| doc | why |
|---|---|
| [bench_sweeper_multi_replica_race](bench_sweeper_multi_replica_race.md) | **Blocks the main measurement thread** — 3 duplicate submissions in 30 pairs (10%) on 2 replicas; advisory lock required before the next unattended ru |
| [homelab_wan_outage_severs_cluster_from_own_llm](homelab_wan_outage_severs_cluster_from_own_llm.md) | Cheap CoreDNS rewrite makes every job immune to WAN outages (08-05 cost a 3 h cluster-wide LLM blackout + 1 job). |
| [embedding_batch_overflow_skips_citation_source_embeddings](embedding_batch_overflow_skips_citation_source_embeddings.md) | OPEN. P1 research-quality and load defect.** Source registration |
| [phase_boundary_tags_are_moved_then_rejected_by_remote](phase_boundary_tags_are_moved_then_rejected_by_remote.md) | OPEN. P1 phase evidence / Git observability defect.** Branch |
| [critic_feedback_resume_parent_freeze_data_wedge](critic_feedback_resume_parent_freeze_data_wedge.md) | Confirmed in code + live DB; part of the critic-wedge family. |
| [resume_fresh_workspace_no_clone_fallback](resume_fresh_workspace_no_clone_fallback.md) | CONFIRMED in code + live incident 2026-07-25. UNFIXED. |
| [job_finalization_decisions_held_only_in_process_memory](job_finalization_decisions_held_only_in_process_memory.md) | Orchestrator restart mid-finalization loses decisions (restarts happen on every deploy). |
| [pod_oom_kill_protection](pod_oom_kill_protection.md) | Umbrella for the recurring OOM incident class. |
| [reviewing_parent_pod_reaped_under_critic](reviewing_parent_pod_reaped_under_critic.md) | Filed + diagnosed 2026-07-04, on the **main cluster** (ns `superhuman-remote-worker`), investigating a batch of failed research-loop jobs. **Not fixed |
| [stale_critic_waiting_status_escapes_reaper](stale_critic_waiting_status_escapes_reaper.md) | CONFIRMED in code + live DB on dev 2026-07-18. UNFIXED. |

## P2 — real but bounded (open, workaround exists or blast radius small)

| doc | status |
|---|---|
| [bench_infra_exclusion_misses_midflight_outages](bench_infra_exclusion_misses_midflight_outages.md) | Open, analysis-level workaround exists. Found 2026-08-05 during |
| [k3d_workspace_ssh_key_rejected](k3d_workspace_ssh_key_rejected.md) | Open, environment-only (k3d cluster `srw`). Dev cluster unaffected |
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
| [ide_settings_sweeper_probes_stale_workspace_endpoints](ide_settings_sweeper_probes_stale_workspace_endpoints.md) | Filed + diagnosed (2026-06-27, during the loop-job `19707fa1` OOM investigation; promoted out of a sub-note in `audit_metadata_config_duplication_ooms |
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
| [recovery_pause_repersists_stale_freeze_invisible_job](recovery_pause_repersists_stale_freeze_invisible_job.md) | Observed 2026-07-26 on dev (job `52949749`), manually unwedged. |
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
