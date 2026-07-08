---
tags:
  - feature
  - architecture
  - tooling
  - mcp
  - sessions
  - builder
  - inventory
aliases:
  - tool surface baseline
  - application tool inventory
  - app tools baseline
related:
  - "[[shared_application_action_layer]]"
  - "[[mcp]]"
  - "[[builder_to_sessions_consolidation]]"
  - "[[session_job_management_toolset_rework]]"
  - "[[automations_v0]]"
  - "[[project_self_improvement_loop]]"
---

# Application Tool Surface Baseline

**Status:** Proposed inventory - created 2026-07-08.

This document inventories the current tool surfaces and candidate missing tools
so the shared application action layer can decide which actions belong in
sessions, MCP, both, or operator-only bundles.

## How To Read This

- **Session-default** means available to persistent sessions today without a YAML
  group explicitly listing it.
- **Agent-runtime** means available through the LangChain tool registry, usually
  controlled by expert YAML, phase, datasource, and workspace-backend gates.
- **MCP-current** means exposed by the current FastMCP server in
  `orchestrator/mcp/server.py`.
- **Parked** means removed with the builder but preserved as reference machinery.
- **App-only** means the Cockpit/orchestrator API supports it, but it is not a
  current session tool and is not exposed through MCP.
- **Candidate** means useful for the shared application action layer, subject to
  grants, project scope, and product decisions.

The intent is not that every candidate becomes a default session tool. This is a
baseline for choosing the right bundles.

## Headline Findings

- Persistent sessions currently get only **8 application-control tools** by
  default, all focused on worker-job delegation and control.
- Persistent sessions also get **3 lightweight task tools** by default.
- The agent registry contains **103 agent-runtime tools** across workspace,
  shell, research, citation, datasource, knowledge, git, todo, delegation, and
  evaluation categories.
- The MCP server currently exposes **103 MCP tools**. It mixes product actions,
  job inspection, project CRUD, knowledge actions, sudo approvals, raw table
  access, diagnostics, and agent administration in one surface.
- The removed builder left **5 parked artifact-authoring schemas** for
  instructions, config, and description edits. They are not live tools today.
- The former builder history also included cross-job workspace edit proposal
  schemas and a large set of operator/API inspection tools. Those are not live
  today and should not be recreated as a separate builder toolset.
- Major app capabilities that are missing from sessions include project
  repositories, experts/skills authoring, automations, project loops, rich job
  inspection, diff accept/reject, project/datasource management, notifications,
  and session lifecycle introspection.
- Major MCP gaps include automations, project repositories, experts/skills CRUD,
  project loops, uploads, notification management, job diff accept/reject, and
  several session/workspace lifecycle operations.

## Current Session Tool Surface

### Session-Default Application Tools

Persistent sessions auto-inject these from
`src/api/persistent_session.py::_load_tools_for_backend`, even when the YAML
`orchestrator` group is empty.

| Tool | Current purpose | Notes |
| --- | --- | --- |
| `create_worker_job` | Create a worker job from the session | Sends `thread_id` when available so the orchestrator can infer owner/project context. |
| `list_worker_jobs` | List visible jobs, optionally by status | Current output truncates IDs; tracked as a bug in the session job-management issue. |
| `get_worker_job` | Read worker job details | Thin summary only. |
| `get_job_workspace_file` | Read a file from a worker job workspace | Calls a workspace file endpoint; read-only. |
| `approve_worker_job` | Approve a pending-review job | Completion semantics are orchestrator-owned. |
| `resume_worker_job` | Resume a paused/frozen job, optionally with feedback | Uses job resume endpoint. |
| `cancel_worker_job` | Cancel a running or paused job | Mutating/destructive and should stay grant/confirmation aware. |
| `pause_worker_job` | Request a running job to pause | Stops at next safe point. |

### Session-Default Task Tools

Persistent sessions also auto-inject lightweight session task tools:

- `task_add`
- `task_complete`
- `task_list`

These are local session-planning tools, not orchestrator application actions.

### Persistent Defaults From YAML

`config/persistent_defaults.yaml` lists these runtime tool groups for normal
persistent sessions, then backend gates remove unsupported categories for lite
workspace tiers:

- `workspace`: `read_file`, `write_file`, `edit_file`, `use_skill`,
  `list_files`, `delete_file`, `search_files`, `file_exists`, `move_file`,
  `rename_file`, `copy_file`, `get_document_info`, `create_directory`,
  `delete_directory`
- `research`: `web_search`, `extract_webpage`, `crawl_website`, `map_website`,
  `search_papers`, `download_paper`, `get_paper_info`, `research_topic`
  plus stale names `browse_website` and `download_from_website` in config
- `browser_direct`: `browser_navigate`, `browser_snapshot`, `browser_click`,
  `browser_type`, `browser_select`, `browser_scroll`, `browser_screenshot`,
  `browser_back`, `browser_close`
- `citation`: `cite_document`, `cite_web`, `list_sources`, `get_citation`,
  `list_citations`, `edit_citation`, `annotate_source`, `get_annotations`,
  `tag_source`, `search_library`, `generate_bibliography`
- `shell`: `run_command`, `shell_read`
- `git`: `git_log`, `git_show`, `git_diff`, `git_status`, `git_tags`
- `knowledge`: `kb_write`, `kb_update`, `kb_read`, `kb_list`, `kb_search`,
  `kb_related`, `kb_contradictions`, `kb_provenance`, `kb_unanswered`,
  `kb_export`
- Empty by default: `orchestrator`, `core`, `graph`, `sql`, `mongodb`,
  `evaluation`, `delegation`, `communication`

`request_workspace_upgrade` is also injected for lite tiers that do not support
shell execution.

For a normal sandbox/VM persistent session, the known default loaded set is about
70 tools after stale config entries are skipped. `browse_website` and
`download_from_website` still appear in `persistent_defaults.yaml` but are no
longer registered.

### Registered Agent-Runtime Tools

These are registered in `src/tools/registry.py`. They are runtime capabilities,
not necessarily application actions.

| Category | Registered tools |
| --- | --- |
| `workspace` | `read_file`, `write_file`, `edit_file`, `list_files`, `delete_file`, `search_files`, `file_exists`, `move_file`, `rename_file`, `copy_file`, `get_document_info`, `create_directory`, `delete_directory`, `use_skill` |
| `core` | `next_phase_todos`, `todo_complete`, `todo_list`, `todo_rewind`, `mark_complete`, `job_complete`, `request_workspace_upgrade` |
| `session_task` | `task_add`, `task_complete`, `task_list` |
| `orchestrator` | `create_worker_job`, `list_worker_jobs`, `get_worker_job`, `get_job_workspace_file`, `approve_worker_job`, `resume_worker_job`, `cancel_worker_job`, `pause_worker_job` |
| `research` | `web_search`, `extract_webpage`, `crawl_website`, `map_website`, `search_papers`, `download_paper`, `get_paper_info`, `research_topic` |
| `browser_direct` | `browser_navigate`, `browser_snapshot`, `browser_click`, `browser_type`, `browser_select`, `browser_scroll`, `browser_screenshot`, `browser_back`, `browser_close` |
| `citation` | `cite_document`, `cite_web`, `list_sources`, `get_citation`, `list_citations`, `edit_citation`, `annotate_source`, `get_annotations`, `tag_source`, `search_library`, `generate_bibliography` |
| `knowledge` | `kb_write`, `kb_update`, `kb_read`, `kb_list`, `kb_search`, `kb_related`, `kb_contradictions`, `kb_provenance`, `kb_unanswered`, `kb_export`, `kb_lint`, `kb_index` |
| `git` | `git_log`, `git_show`, `git_diff`, `git_status`, `git_tags` |
| `shell` | `run_command`, `shell_execute`, `shell_read`, `srw_cloud_status` |
| `sql` | `sql_query`, `sql_schema`, `sql_execute` |
| `mongodb` | `mongo_query`, `mongo_aggregate`, `mongo_schema`, `mongo_insert`, `mongo_update` |
| `graph` | `cypher_query`, `cypher_execute`, `get_database_schema` |
| `webdav` | `webdav_list`, `webdav_read`, `webdav_info`, `webdav_write`, `webdav_delete` |
| `communication` | `send_message` |
| `delegation` | `delegate_work`, `resume_delegation_child`, `spawn_subagent` |
| `evaluation` | `approve_job`, `return_job_with_feedback` |

## Parked Builder Tool Surface

The builder itself is removed. The only builder-specific tool delta that was
kept is parked in `orchestrator/services/_parked/builder_artifact_tools.py` and
the matching Cockpit `_parked` service. These are not live session or MCP tools.

| Parked tool | Purpose |
| --- | --- |
| `update_instructions` | Replace full instructions content. |
| `edit_instructions` | Exact find/replace inside instructions. |
| `insert_instructions` | Insert or append instructions content. |
| `update_config` | Merge agent config overrides, including model/tool/autonomy/memory settings. |
| `update_description` | Replace the job description. |

These are strong candidates for a future canvas/session job-authoring bundle,
but they should probably become typed artifact actions rather than raw hidden
form mutations.

The removed builder's broader LLM-visible history was larger than these five
schemas: it also included two cross-job workspace edit proposal schemas
(`write_workspace_file`, `edit_workspace_file`) and dozens of server-side
inspection/action schemas that overlapped heavily with the REST API and current
MCP surface. That broader operator-style surface should be handled through the
shared action registry and MCP/operator bundles, not rebuilt as a builder-specific
toolset.

## Current MCP Tool Surface

The MCP server exposes 103 tools today. They are REST proxy wrappers with MCP
token/user/scope headers, and many return formatted strings.

MCP transport/auth notes:

- HTTP transport uses MCP token verification or the OAuth proxy when enabled.
- Stdio transport skips MCP auth and should be treated as local/operator mode.
- The server forwards identity to REST through `X-MCP-User-Id`, `X-MCP-Scope`,
  and `X-Internal-Key`.
- Legacy MCP scopes are `user`, `all`, and `project:<uuid>`. `all` should not
  broaden a non-admin user into admin access; backend checks still need to
  enforce the resolved user's role.
- The current implementation uses a shared `AsyncCockpitClient` with mutable
  default headers in `_get_client()`. In multi-user HTTP mode, request-scoped MCP
  headers should not be stored on a shared mutable client.

### Jobs And Job Control

- `list_jobs`
- `get_job`
- `get_job_summary`
- `create_job`
- `create_project_job`
- `approve_job`
- `resume_job_with_feedback`
- `cancel_job`
- `pause_job`
- `delete_job`
- `assign_job`
- `promote_job`
- `get_frozen_job`
- `get_job_progress`
- `get_memory_stats`
- `list_project_jobs`

### Job Audit, Logs, Todos, And Trace Inspection

- `get_audit_trail`
- `get_audit_bulk`
- `search_audit`
- `get_audit_timerange`
- `get_chat_history`
- `get_chat_bulk`
- `get_graph_changes`
- `get_llm_request`
- `list_llm_requests`
- `get_job_log`
- `get_shell_state`
- `get_todos`
- `get_current_todos`
- `list_todo_archives`
- `get_todo_archive`

### Job Repository, Diff, And Workspace Inspection

- `list_job_commits`
- `get_job_diff`
- `get_job_file`
- `list_job_files`
- `list_job_tags`
- `get_workspace_file`
- `get_workspace_overview`

### Sources, Citations, And Research Artifacts

- `list_job_sources`
- `get_source_detail`
- `list_job_citations`
- `get_citation_detail`
- `search_job_sources`
- `get_source_annotations`
- `get_source_tags`
- `get_citation_stats`

### Projects, Membership, And Project Knowledge

- `list_projects`
- `get_project`
- `create_project`
- `update_project`
- `delete_project`
- `list_project_members`
- `add_project_member`
- `update_project_member`
- `remove_project_member`
- `list_project_experts`
- `get_project_expert`
- `get_knowledge_summary`
- `list_knowledge_notes`
- `get_knowledge_note`
- `search_knowledge`
- `update_knowledge_note`
- `delete_knowledge_note`
- `export_knowledge`
- `reindex_knowledge`

### Datasources And Raw Tables

- `list_datasources`
- `create_datasource`
- `update_datasource`
- `delete_datasource`
- `test_datasource`
- `list_project_datasources`
- `link_datasource_to_project`
- `unlink_datasource_from_project`
- `list_tables`
- `query_table`
- `get_table_schema`

### Experts, Skills, And Models

- `list_experts`
- `get_expert`
- `reload_experts`
- `list_skills`
- `get_skill`
- `reload_skills`
- `list_models`

### Agents, Statistics, And Operator Actions

- `list_agents`
- `get_agent_system_info`
- `deregister_agent`
- `get_job_stats`
- `get_daily_stats`
- `get_agent_stats`
- `get_stuck_jobs`
- `list_sudo_requests`
- `approve_sudo_request`
- `deny_sudo_request`

### Job Messaging And Persistent Threads

- `list_message_threads`
- `send_message_to_job`
- `get_message_thread`
- `create_persistent_thread`
- `list_persistent_threads`
- `get_persistent_thread`
- `end_persistent_thread`
- `resume_persistent_thread`
- `get_persistent_thread_messages`
- `get_persistent_thread_ide`

## App Capabilities By Domain

This section maps current app capabilities to current tools and candidate shared
actions.

### Jobs

Current app capabilities:

- List, get, create, delete, cancel, pause, resume, approve, assign, and promote
  jobs.
- Get progress, frozen data, snapshot status, logs, LLM requests, shell state,
  audit, chat, graph changes, todos, workspace files, repo files, commits, diffs,
  tags, datasources, memory stats, sources, citations, and source annotations.
- Accept or reject job diffs.
- Send messages to jobs and reply in message threads.
- Export job output to a shared folder.

Current tools:

- Session has only create/list/get/read-workspace-file/approve/resume/cancel/pause.
- MCP covers many read/control paths but does not currently expose diff
  accept/reject or export-to-shared-folder.
- Current session `cancel_worker_job` and `pause_worker_job` call the cancel and
  pause routes with `POST`, while the backend/Cockpit routes use `PUT`. This
  should be fixed before using these as the foundation for shared actions.

Candidate shared actions:

- `jobs.list`, `jobs.search`, `jobs.resolve_id`
- `jobs.get`, `jobs.get_summary`, `jobs.get_progress`, `jobs.get_activity`
- `jobs.create`, `jobs.create_in_project`
- `jobs.approve`, `jobs.resume`, `jobs.pause`, `jobs.cancel`, `jobs.delete`
- `jobs.assign`, `jobs.promote`
- `jobs.get_frozen`, `jobs.get_logs`, `jobs.get_llm_requests`,
  `jobs.get_shell_state`
- `jobs.get_audit`, `jobs.search_audit`, `jobs.get_chat`
- `jobs.get_todos`, `jobs.get_todo_archive`
- `jobs.get_workspace_overview`, `jobs.read_workspace_file`,
  `jobs.write_workspace_file`
- `jobs.get_repo_tree`, `jobs.read_repo_file`, `jobs.get_repo_commits`,
  `jobs.get_repo_diff`, `jobs.get_repo_tags`
- `jobs.get_diff`, `jobs.get_diff_file`, `jobs.accept_diff`, `jobs.reject_diff`
- `jobs.send_message`, `jobs.list_message_threads`, `jobs.get_message_thread`

Recommended default:

- Sessions should get a small `jobs.core` plus richer read-only inspection for
  jobs they created or jobs in the current project.
- MCP product should get the same product actions, with MCP scope intersection.
- Raw audit/log/LLM inspection should be a separate `jobs.inspect` or
  `ops.debug` bundle.

### Projects

Current app capabilities:

- Create, list, get, update/archive, and delete projects.
- Manage members.
- Manage project datasources.
- Manage project repositories.
- List project jobs and create a project job.
- Read project experts.
- Manage contacts.
- Store project API keys by provider.
- Read project memory stats and knowledge.

Current tools:

- MCP has project CRUD, members, project jobs, project experts, datasources, and
  project knowledge.
- Sessions have no project tools except implicit `project_id` on job creation
  and project-scoped knowledge tool context.
- MCP does not expose project repository CRUD, contacts, or project API keys.

Candidate shared actions:

- `projects.list`, `projects.get`, `projects.get_current`
- `projects.create`, `projects.update`, `projects.archive`, `projects.delete`
- `projects.list_members`, `projects.add_member`, `projects.update_member`,
  `projects.remove_member`
- `projects.list_jobs`, `projects.create_job`
- `projects.list_experts`, `projects.get_expert`
- `projects.list_contacts`, `projects.add_contact`, `projects.remove_contact`
- `projects.get_memory_stats`
- `projects.get_capabilities`

Recommended default:

- Sessions should at least get `projects.get_current`, `projects.list_jobs`, and
  project read metadata.
- Project writes should be grant/role gated.

### Project Repositories

Current app capabilities:

- List, add, update, and remove repositories on a project.
- Jobs also expose provisioned repo contents, files, commits, diffs, and tags.

Current tools:

- Session has git tools for the current workspace only.
- MCP has job repo inspection but not project repository management.

Candidate shared actions:

- `repositories.list_project`
- `repositories.add_project_repo`
- `repositories.update_project_repo`
- `repositories.remove_project_repo`
- `repositories.get_default`
- `repositories.test_access`
- `repositories.list_job_tree`
- `repositories.read_job_file`
- `repositories.get_job_diff`

Recommended default:

- Sessions should get read access to project repositories and job repo state.
- Add/update/remove should require project editor role and confirmation.

### Datasources

Current app capabilities:

- List global datasources and eligible datasources.
- Create, read, update, delete, and test datasources.
- Generate SSH keys for datasource setup.
- Link/unlink/update datasources on projects.
- List datasources attached to a job.
- Runtime datasource tools exist for PostgreSQL, MongoDB, Neo4j, and WebDAV when
  a matching datasource is attached.

Current tools:

- MCP exposes datasource CRUD/test/list and project link/unlink.
- Session has no datasource management tools. It only receives datasource-backed
  runtime query tools when context provides them.

Candidate shared actions:

- `datasources.list`, `datasources.list_eligible`, `datasources.get`
- `datasources.create`, `datasources.update`, `datasources.delete`,
  `datasources.test`
- `datasources.generate_ssh_key`
- `datasources.list_for_project`, `datasources.link_to_project`,
  `datasources.update_project_link`, `datasources.unlink_from_project`
- `datasources.list_for_job`

Recommended default:

- Sessions should get read/list/eligible/current datasource actions.
- Create/link/update/delete/test should be role and grant gated.

### Experts

Current app capabilities:

- List experts.
- Read expert detail.
- Create, update, delete, duplicate, export, import, and reload experts.
- Read project-materialized expert config.

Current tools:

- MCP only has list/get/reload and project expert reads.
- Sessions have no expert management tools.

Candidate shared actions:

- `experts.list`, `experts.get`, `experts.get_project_version`
- `experts.create`, `experts.update`, `experts.delete`
- `experts.duplicate`, `experts.export`, `experts.import`
- `experts.reload`
- `experts.validate_config`
- `experts.diff_from_default`
- `experts.bind_skill`, `experts.unbind_skill`

Recommended default:

- Sessions should get read-only expert discovery by default.
- Expert authoring belongs in a grant-gated `experts.authoring` bundle, likely
  tied to canvas/session collaboration.

### Skills

Current app capabilities:

- List skills.
- Read skill detail.
- Create, update, delete, duplicate, export, import, and reload skills.
- Skill editor supports multi-file skill packages.
- Runtime `use_skill` reads bound skill content in the agent workspace.

Current tools:

- MCP only has list/get/reload.
- Sessions only have `use_skill` for already-resolved workspace skills, not
  application-level skill authoring.

Candidate shared actions:

- `skills.list`, `skills.get`
- `skills.create`, `skills.update`, `skills.delete`
- `skills.duplicate`, `skills.export`, `skills.import`
- `skills.reload`
- `skills.validate`
- `skills.create_file`, `skills.update_file`, `skills.delete_file`
- `skills.search_content`
- `skills.bind_to_expert`, `skills.bind_to_project`

Recommended default:

- Sessions should get skill discovery and maybe `skills.get` for available
  product skills.
- Creating or editing skills should require a clear authoring grant.

### Builder Replacement And Canvas Authoring

Current app capabilities:

- Job-create form stores instructions, config override, and description.
- Parked builder artifact machinery can apply LLM tool calls to those fields.
- Experts and skills have full CRUD editors.

Current tools:

- None live for sessions.
- Parked builder schemas are reference only.

Candidate shared actions:

- `drafts.create_job_draft`
- `drafts.update_description`
- `drafts.update_instructions`
- `drafts.edit_instructions`
- `drafts.insert_instructions`
- `drafts.update_config`
- `drafts.attach_datasource`
- `drafts.attach_repository`
- `drafts.select_expert`
- `drafts.promote_to_job`
- `drafts.create_expert_draft`
- `drafts.create_skill_draft`

Recommended default:

- Do not expose hidden form-mutation tools directly.
- Expose these as visible canvas/session artifact actions with user-visible
  draft state and review controls.

### Automations

Current app capabilities:

- Create, list, get, update, delete, run-now, pause, resume, and list runs.
- v0 is cron-based. The broader automation docs also discuss event-based job
  lifecycle triggers.

Current tools:

- No session automation tools.
- No MCP automation tools.

Candidate shared actions:

- `automations.list`, `automations.get`, `automations.list_runs`
- `automations.propose`
- `automations.create_disabled`
- `automations.create_active`
- `automations.update`
- `automations.pause`, `automations.resume`, `automations.run_now`
- `automations.delete`
- `automations.validate_schedule`

Recommended default:

- Sessions should get list/get/propose and disabled create when the user has the
  right project role.
- Active recurring automation creation should require an explicit grant and
  likely a confirmation step.
- MCP product should get the same actions behind token/project scope.

### Project Loops

Current app capabilities:

- Start, get, pause, resume, stop, and list jobs for a project self-improvement
  loop.

Current tools:

- No session project-loop tools.
- No MCP project-loop tools.

Candidate shared actions for sessions/MCP:

- `project_loops.get`
- `project_loops.list_jobs`
- `project_loops.explain_state`

Keep as Cockpit/backend-only actions:

- `project_loops.start`
- `project_loops.pause`
- `project_loops.resume`
- `project_loops.stop`

Recommended default:

- Sessions can safely get read/status/list-jobs for the current project.
- Do not expose loop start/pause/resume/stop to sessions. These actions can
  create or extend expensive recurring work and do not save enough user time to
  justify agent control. Keep loop lifecycle control in Cockpit or another
  explicit user-driven surface.

### Knowledge And Memory

Current app capabilities:

- Project knowledge summary/list/get/search/update/delete/export/reindex.
- Job memory stats and memory listing.
- Agent-runtime KB tools can write, update, read, search, relate, lint, index,
  and export notes from inside the workspace context.

Current tools:

- MCP exposes project knowledge read/search/update/delete/export/reindex.
- Sessions expose runtime KB tools when knowledge context is available.
- App REST does not currently expose a simple create-note endpoint matching
  `kb_write`; notes are created by runtime ingestion/write paths.

Candidate shared actions:

- `knowledge.summary`, `knowledge.list`, `knowledge.get`, `knowledge.search`
- `knowledge.create_note`, `knowledge.update_note`, `knowledge.delete_note`
- `knowledge.export`, `knowledge.reindex`
- `knowledge.ingest_workspace`
- `knowledge.get_related`, `knowledge.get_contradictions`,
  `knowledge.get_unanswered`
- `memory.get_job_stats`, `memory.list_job_memories`,
  `memory.get_project_stats`

Recommended default:

- Sessions should keep runtime KB tools, but shared app actions should cover
  project-level read/search and deliberate note creation/editing.

### Reviews, Approvals, And Sudo

Current app capabilities:

- Pending action counts.
- Job approve/resume/pause/cancel.
- Job diff accept/reject.
- Persistent-thread approval endpoint.
- Sudo requests list/get/approve/deny/approve-upgrade/resume-without-vm.
- Sudo rules list/create/delete.

Current tools:

- Session has job approve/resume/pause/cancel.
- MCP has job approve/resume/pause/cancel and sudo list/approve/deny.
- MCP does not expose diff accept/reject, sudo request detail, upgrade approval,
  resume-without-vm, or sudo rule management.

Candidate shared actions:

- `approvals.list_pending`
- `approvals.approve_job`, `approvals.return_job_with_feedback`
- `approvals.accept_job_diff`, `approvals.reject_job_diff`
- `approvals.approve_thread_action`
- `sudo.list_requests`, `sudo.get_request`, `sudo.approve`, `sudo.deny`
- `sudo.approve_upgrade`, `sudo.resume_without_vm`
- `sudo.list_rules`, `sudo.create_rule`, `sudo.delete_rule`

Recommended default:

- Product sessions can get job review actions scoped to visible/current project
  jobs.
- Sudo tools should be operator/admin only unless there is a very explicit
  interactive approval UX.

### Persistent Sessions

Current app capabilities:

- Create, list, get, patch, delete/end, resume, stream, input, interrupt,
  approve, read messages, get IDE status, upload files, TTS, TTS planning,
  transcription, and voice capability discovery.
- Separate session prepare/connection endpoints provide web socket URL and token.

Current tools:

- MCP exposes create/list/get/end/resume/get messages/get IDE.
- Sessions do not have self-introspection app actions beyond local task tools.

Candidate shared actions:

- `sessions.list`, `sessions.get`, `sessions.create`
- `sessions.update`
- `sessions.resume`, `sessions.end`
- `sessions.get_messages`, `sessions.send_input`, `sessions.interrupt`
- `sessions.approve_action`
- `sessions.get_ide`
- `sessions.upload_file`
- `sessions.get_lifecycle`

Recommended default:

- MCP product can expose session lifecycle tools.
- A running session should probably get only self-introspection and artifact
  upload/read actions, not broad control over other sessions by default.

### Notifications, Inbox, Contacts, And Messages

Current app capabilities:

- List notifications, update notification state, and stream notification events.
- Project contacts CRUD.
- Job message threads and replies.
- Headless notification services and inbox-related UI exist.

Current tools:

- MCP exposes job message thread list/get/send.
- Sessions have no notification or contacts tools.

Candidate shared actions:

- `notifications.list`, `notifications.mark_read`,
  `notifications.mark_all_read`
- `messages.list_threads`, `messages.get_thread`, `messages.send_to_job`
- `contacts.list_project`, `contacts.add_project`, `contacts.remove_project`
- `inbox.list`, `inbox.triage`, `inbox.reply`

Recommended default:

- Sessions can benefit from notifications and job-message awareness.
- Contact and inbox sending tools need identity/authorization rules before being
  defaulted.

### Workspace, VM, IDE, Snapshots, And Cloud

Current app capabilities:

- Global workspace status.
- VM create/list/get/delete.
- Job IDE start/get/stop.
- Thread IDE get.
- Ensure workspace access.
- Job workspace provision/status.
- Agent/thread suspend, release-agent, upgrade-to-VM, abort VM upgrade,
  upgrade-to-workspace.
- Snapshot get/delete/pin and stats.
- Cloud mount status through runtime shell tool `srw_cloud_status`.

Current tools:

- Session has `request_workspace_upgrade` for lite tiers and runtime workspace
  file/shell/git tools when backend supports them.
- MCP has job workspace overview/file and persistent-thread IDE read.
- MCP does not expose most lifecycle/snapshot/VM operations.

Candidate shared actions:

- `workspace.get_status`
- `workspace.ensure_access`
- `workspace.request_upgrade`
- `workspace.get_job_status`
- `workspace.provision_job`
- `workspace.get_snapshot`, `workspace.delete_snapshot`,
  `workspace.pin_snapshot`, `workspace.get_snapshot_stats`
- `ide.get_job`, `ide.start_job`, `ide.stop_job`, `ide.get_thread`
- `vm.list`, `vm.get`, `vm.create`, `vm.delete`
- `cloud.get_mount_status`

Recommended default:

- Sessions should keep `request_workspace_upgrade` and self workspace status.
- VM and snapshot management should be operator/admin or explicitly granted.

### Models, Providers, Config, Grants, Users, And Usage

Current app capabilities:

- User/provider API keys.
- Admin provider keys, endpoint CRUD/test/discovery, model catalog CRUD/test,
  model reload, Codex auth/status/models/usage, config overrides, bundled config
  catalog, provider defaults, families/detect, system readiness/settings,
  grants, users/admin-users/security-events, usage/breakdown/timeseries.

Current tools:

- MCP has `list_models`, job/daily/agent/stuck stats, and raw table tools.
- Sessions have no admin/config/provider tools.

Candidate shared actions:

- `models.list`, `models.reload`
- `usage.summary`, `usage.breakdown`, `usage.timeseries`
- `providers.list_endpoints`, `providers.test_endpoint`,
  `providers.discover_models`
- `config.list_overrides`, `config.get_catalog`
- `grants.list`, `grants.set`, `grants.delete`
- `users.list`, `users.get`, `users.approve`, `users.update`
- `system.readiness`

Recommended default:

- Keep most of this in `ops.admin`.
- Product sessions may only need `models.list` and self capability discovery.

### Raw Tables And Diagnostics

Current app capabilities:

- List tables, query tables, and inspect table schemas.
- Stats and stuck-job diagnostics.
- Agent list/system-info/deregister.

Current tools:

- MCP exposes raw tables and operational diagnostics.
- Sessions do not.

Candidate shared actions:

- `ops.tables.list`
- `ops.tables.schema`
- `ops.tables.query`
- `ops.stats.jobs`
- `ops.stats.daily`
- `ops.stats.agents`
- `ops.jobs.stuck`
- `ops.agents.list`
- `ops.agents.get_system_info`
- `ops.agents.deregister`

Recommended default:

- Operator/debug MCP only.
- Do not expose raw table tools to ordinary sessions.

## Proposed Baseline Bundles

| Bundle | Candidate contents | Default surfaces |
| --- | --- | --- |
| `jobs.core` | Job list/get/create/control plus ID resolver and useful summaries | Session, MCP product |
| `jobs.inspect` | Progress, todos, files, diffs, logs, audit, LLM requests, repo inspection | Session read-only subset, MCP product, ops for raw trace |
| `projects.core` | Current project, project list/get/jobs/members read | Session, MCP product |
| `projects.manage` | Project create/update/delete, members write, contacts, API keys | Grant-gated session, MCP product/admin |
| `repositories.core` | Project repo list/read/test, job repo inspection | Session, MCP product |
| `repositories.manage` | Add/update/remove project repositories | Grant-gated session, MCP product |
| `datasources.core` | List/get/eligible/current/job/project datasource reads | Session, MCP product |
| `datasources.manage` | Create/update/delete/test/link/unlink/generate SSH key | Grant-gated session, MCP product |
| `experts.core` | List/get/project expert reads | Session, MCP product |
| `experts.authoring` | Create/update/duplicate/import/export/reload/validate | Grant-gated session/canvas, MCP product |
| `skills.core` | List/get/search skill content | Session, MCP product |
| `skills.authoring` | Create/update/files/duplicate/import/export/reload/validate | Grant-gated session/canvas, MCP product |
| `drafts.core` | Job/expert/skill draft artifact editing and promote-to-job | Session/canvas |
| `automations.core` | List/get/runs/propose/create-disabled/update/pause/resume/run-now | Grant-gated session, MCP product |
| `project_loops.core` | Get/list-jobs/explain | Session read-only, MCP product read-only |
| `knowledge.core` | Project knowledge summary/list/get/search/create/update/export/reindex | Session, MCP product |
| `approvals.core` | Pending actions, job approve/resume/feedback, diff accept/reject | Session, MCP product |
| `sessions.core` | Session list/get/create/update/resume/end/messages/IDE | MCP product, limited self-session actions |
| `notifications.core` | List/mark notifications, job messages, contacts, inbox actions | Session and MCP product after policy is clear |
| `workspace.core` | Workspace status, IDE, upgrade request, snapshots read, cloud status | Session, MCP product |
| `ops.debug` | Raw tables, audit bulk, logs, system stats, agents, sudo, diagnostics | MCP operator/admin only |
| `ops.admin` | Users, grants, provider settings, model catalog, system settings | MCP admin only |

## Initial Assignment Recommendation

The session should not receive a flat mirror of the Cockpit API or MCP server.
Direct session tools should stay small and app-oriented, especially because
lite/no-workspace sessions cannot rely on shell or a future CLI. A CLI can still
be added later for workspace-backed development workflows, but the direct
session surface should cover the minimum SRW control-plane actions needed to
understand and steer work.

### Session Always-On Direct Tools

These actions should be available even when the session has no shell workspace:

- `get_session_context`
- `request_workspace_upgrade`
- `jobs.list`
- `jobs.get`
- `jobs.create`
- `jobs.pause`
- `jobs.cancel`
- `jobs.resume`
- `jobs.approve`
- `jobs.read_workspace_file`
- `projects.get_current`
- `projects.get`
- `projects.list_jobs`
- `projects.list_members`
- `repositories.list_project`
- `repositories.get_default`
- `project_loops.get`
- `project_loops.list_jobs`
- `project_loops.explain_state`
- `experts.list`
- `experts.get`
- `experts.get_project_version`
- `skills.list`
- `skills.get`
- `skills.search`
- `datasources.list_for_project`
- `datasources.list_for_job`
- `knowledge.summary`
- `knowledge.search`
- `knowledge.list`
- `knowledge.get`
- `knowledge.create_note`
- `automations.list`
- `automations.get`
- `automations.list_runs`
- `automations.propose`
- `approvals.list_pending`

This set answers the common session questions without requiring workspace
access: what project is this, what jobs are running, what is the loop doing,
what repository/datasource/expert/skill context exists, and what work should be
created or proposed next.

### Session Workspace-Only Direct Tools

These actions should only be visible when the session has a sandbox or VM
workspace:

- `repositories.checkout_project_repository`
- `workspace.get_status`
- `workspace.get_ide_status`
- `workspace.snapshot_status`

Shell, file, git, browser, research, citation, and datasource query tools remain
agent-runtime tools rather than shared app actions. The repository checkout tool
is the important bridge for project review workflows: after checkout, the agent
can use normal workspace tools to install dependencies, start the application,
run tests, and inspect the UI/API with the user.

### Session Grant-Gated Direct Tools

These should require the right project role, explicit capability grant, and
confirmation where appropriate:

- `projects.create`
- `projects.update`
- `projects.delete`
- `projects.add_member`
- `projects.remove_member`
- `repositories.add_project_repo`
- `repositories.update_project_repo`
- `repositories.remove_project_repo`
- `datasources.link_to_project`
- `datasources.unlink_from_project`
- `datasources.create`
- `datasources.update`
- `datasources.delete`
- `datasources.test`
- `experts.create`
- `experts.update`
- `experts.delete`
- `experts.duplicate`
- `experts.bind_skill`
- `skills.create`
- `skills.update`
- `skills.delete`
- `skills.create_file`
- `skills.update_file`
- `skills.delete_file`
- `skills.bind_to_expert`
- `skills.bind_to_project`
- `automations.create_disabled`
- `automations.create_active`
- `automations.update`
- `automations.pause`
- `automations.resume`
- `automations.run_now`
- `automations.delete`
- Notifications/contact sending and inbox replies.

### Recommended First Session Slice

Implement in this order:

1. `get_session_context`
2. Fix current job tools: full IDs, richer summaries, and cancel/pause method
   drift.
3. `projects.get_current`
4. `projects.list_jobs`
5. `repositories.list_project`
6. `repositories.checkout_project_repository`
7. `project_loops.get`
8. `project_loops.list_jobs`
9. `project_loops.explain_state`
10. `experts.list`, `experts.get`, `skills.list`, `skills.search`, and
    `skills.get`

MCP product:

- All session product actions, plus session lifecycle actions for external
  clients.
- Project-scoped write actions where MCP token scope and user/project role allow.

MCP operator/debug:

- Existing raw table, audit bulk, logs, LLM request, stats, agent, sudo, provider,
  grant, user, and system tools.
- This should be split from product tools before broadening MCP further.

Keep as agent-runtime, not shared app actions:

- Workspace file editing inside the agent workspace.
- Shell and browser execution.
- Git operations inside the workspace.
- Research/web/paper tools.
- Datasource query tools such as SQL, MongoDB, Neo4j, and WebDAV.
- Citation generation/editing inside a job workspace.
- Delegation/subagent runtime tools.
- Phase todo and job completion tools for batch worker jobs.

## Known Cleanup Items

- Session job ID truncation was fixed in the first implementation slice; keep
  regression coverage before expanding session job controls.
- Session cancel/pause HTTP method drift was fixed in the first implementation
  slice (`PUT` backend routes).
- Make session job list/detail outputs structured, with human summaries rendered
  by adapters.
- Decide whether session product actions are project-scoped by default. The
  recommended default is current thread project scope, with explicit
  cross-project actions.
- Fix MCP-created job attribution so user and project context are correct.
- Remove request-scoped MCP headers from the shared mutable MCP client.
- Re-check OAuth-requested `all` and `project:<uuid>` scopes server-side during
  token creation, not only in the consent UI.
- Split MCP into product and operator/debug bundles.
- Decide whether MCP should expose resources/prompts; it currently exposes tools
  only.
- Add capability/grant metadata before exposing automation, expert, skill,
  project, repository, or datasource write tools to sessions.
- Decide whether app-level project knowledge needs a first-class create-note API
  matching `kb_write`.
- Decide how canvas artifact state will represent job, expert, and skill drafts.
- Clean stale builder references separately from this action-layer work:
  `message_triage` still references a `builder` default model kind, local env/i18n
  assets still contain builder labels, and some docs/comments still describe
  builder-era model wiring.
- Fix the MCP package entrypoint if it is still expected to work:
  `orchestrator/mcp/__main__.py` imports `main` from `server.py`, while the
  runnable `main` lives in `run.py`.
