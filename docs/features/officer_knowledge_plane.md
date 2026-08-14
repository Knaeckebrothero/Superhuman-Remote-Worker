---
tags:
  - feature
  - architecture
  - officers
  - knowledge
  - memory
  - security
status: proposed
created: 2026-08-14
aliases:
  - officer memory
  - officer knowledge plane
  - background officer boundary
related:
  - "[[centurion]]"
  - "[[officer_post]]"
  - "[[officer_backlog_pools]]"
  - "[[officer_supervision_surface]]"
  - "[[officer_message_routing]]"
  - "[[knowledge_base_repo_separation]]"
  - "[[no_workspace_agent_mode]]"
  - "[[project_cloud_folders]]"
---

# Officer knowledge plane — durable memory without object-level work

> The background officer owns the project's **knowledge plane** and its backlog, but
> never receives the project's **object plane**. He may read and garden the project KB,
> create and edit backlog tickets, remember decisions, and manage jobs. He may not browse
> repositories, mount the project cloud folder, open an arbitrary worker path, run a
> shell, or upgrade himself into a workspace. When object-level investigation is needed,
> he delegates it and receives a report or bounded job evidence. A conference remains an
> ordinary interactive session and is a separate embodiment with a user-selected
> workspace.

## Status

**K1–K3 IMPLEMENTED 2026-08-14** on develop (`0c0c5607`): attach-time binding invariant
with loud failure + degraded-KB survival (`project knowledge unavailable` on both tool
errors and the wake), the explicit nine-tool knowledge grant (kb_export absent), and the
runtime capability ceiling (`registry.apply_officer_tool_ceiling`, strict
`officer.enabled is True`, denies workspace/shell/git/browser/canvas/repo/webdav/cloud +
`request_workspace_upgrade` under any override; conferences untouched). K5's doctrine
line shipped inside the E5 persona section (`7bb1d331`). K4 remains open — the sitrep
`_knowledge_section` probe is its hook. Original status for the record: The underlying stores and most KB tools already exist. The feature is
primarily an explicit capability contract, a fail-closed project binding, and removal of
generic session affordances that accidentally undermine the officer's no-workspace role.

The direction came from the Legate's officer/backlog discussion: a passive background
manager must be able to own notes and tickets, but project-folder access would invite the
same manager to do the investigation and editing that should be delegated. Exact job
evidence access is specified separately in [[officer_supervision_surface]].

Audit markers below refer to the 2026-08-14 code pass:

- **[A-memory]** `src/api/persistent_session.py::_setup_memory`,
  `src/services/recall_store.py::RecallStore._scope_filter`, and
  `orchestrator/database/postgres.py::merge_thread_officer_state`.
- **[A-kb]** `src/services/knowledge/bindings.py::build_knowledge_bindings` and
  `src/tools/knowledge/knowledge_tools.py::{kb_write,kb_update,kb_export,_materialize_note}`.
- **[A-boundary]** `config/experts/centurion/config.yaml`,
  `src/api/persistent_session.py::_load_tools_for_backend`, and
  `src/tools/registry.py::filter_tools_by_backend`.

## 1. The current state — the officer has memory, but no declared model

The apparent gap is not simply "the officer has no KB." He inherits one today, but the
grant is implicit and several surrounding behaviors contradict the intended boundary.

| State | Current home | Scope and lifetime | Authority after this feature |
|---|---|---|---|
| Conversation history | Postgres `thread_messages` | One officer incarnation; retained after compaction and decommission | Audit/timeline, not project truth |
| Operational watch state | `threads.metadata.officer_state`; harvested into `project_officers.state` by [[officer_post]] | Current incarnation, then durable post | Timers, sitrep fingerprints, digest, breaker/queue counters |
| Ambient memory | RecallStore `memories` | Project-scoped when the session has project bindings | Helpful recollection only; never a backlog claim or authorization |
| Project truth | Project KB (`knowledge/<slug>.md` plus its index/graph projection) | Project-scoped and shared by officer, conferences, and jobs | Charter, decisions, assumptions, plans, reports, backlog tickets |
| Officer continuity | `project_officers` | One durable row per project across incarnations | Kit, policy, harvested runtime state, incarnation history |

`PersistentSession` rebuilds history from `thread_messages`, initializes RecallStore with
the session's project IDs, and builds knowledge bindings before creating `ToolContext`
**[A-memory][A-kb]**. `RecallStore._scope_filter()` narrows retrieval by project, while
`agent_id` is stored as provenance but is not a private retrieval namespace
**[A-memory]**. Therefore the officer's extracted memories are currently shared project
memory, not a sealed inner diary. That is acceptable only if it stays non-authoritative.

The rule is:

> If losing or misremembering a fact could change what gets dispatched, closed, approved,
> or escalated, that fact belongs in the project KB or control-plane DB — never only in
> RecallStore or the context window.

No fourth "officer memory database" is introduced. A private reflective namespace can be
revisited only if real transcripts demonstrate that project-scoped RecallStore causes
cross-persona contamination.

## 2. Three planes, one bright boundary

| Plane | Examples | Background officer |
|---|---|---|
| **Knowledge** | charter, backlog tickets, decisions, assumptions, current-state notes, worker reports | Read/write, subject to provenance and note-type rules |
| **Control** | job status, audit, messages, capacity, claims, pause/steer/approve/cancel, wake events | Read and act through scoped orchestrator tools |
| **Object** | repositories, project/cloud folders, arbitrary job files, shell, browser, git, canvas | Denied |

The separation is about authority, not filesystem implementation. `kb_write` and
`kb_update` already materialize canonical notes server-side into the dedicated project
knowledge repository; they do not need a session workspace **[A-kb]**. The officer can
therefore write durable project knowledge without receiving a checkout, shell, or cloud
mount.

The background/conference distinction is structural:

- A **background officer** is a persistent thread with
  `config_override.officer.enabled=true`. The restrictions in this document key on that
  runtime fact, not merely `agent_id=centurion`.
- A **conference** is an ordinary interactive session wearing the Centurion expert and
  project identity. It may have the workspace tier chosen by the user. Its work is later
  summarized back to the held background officer, as already designed in [[centurion]].

This prevents a conference's legitimate shared workspace from becoming a loophole in the
background post.

## 3. Exact knowledge contract

The background officer receives an explicit, reviewed knowledge grant rather than
whatever `session_base` happens to inherit:

```yaml
tools:
  knowledge:
    - kb_write
    - kb_update
    - kb_read
    - kb_list
    - kb_search
    - kb_related
    - kb_contradictions
    - kb_provenance
    - kb_unanswered
```

`kb_export` is intentionally absent. It is a workspace-oriented migration/export tool,
requires a destination directory, and is unrelated to normal knowledge gardening
**[A-kb]**. Index/lint/rebuild administration also stays operator-side.

### 3.1 Project binding invariant

A commissioned background officer has exactly one native project binding, and it is the
sole writable KB binding. `build_knowledge_bindings()` already makes the first native
project writable and every additional/external KB read-only **[A-kb]**; commission and
attach must now enforce the stronger officer invariant:

1. exactly one `project_id`;
2. one matching native `KnowledgeBinding(writable=True)`;
3. no request/config override may replace that write target;
4. every external KB datasource is read-only and clearly labeled in tool output.

A malformed or unbound commission fails. A temporary vector/KB outage does **not** kill
the officer: sitrep, job supervision, and paging may continue, but KB mutations,
backlog-derived dispatch, and `auto_pull` fail closed. The wake visibly says
`project knowledge unavailable`; the officer must not reconstruct a shadow backlog in
RecallStore or thread prose.

### 3.2 Knowledge write authority

- The charter keeps its existing split authority: Legate-owned governance blocks cannot
  be rewritten by the officer; the officer may maintain posture and propose changes.
- The officer may create, edit, supersede, re-ready, and close backlog notes. The missing
  `remove_tags`/`set_tags` and machine-tag provenance controls remain B2 of
  [[officer_backlog_pools]].
- Worker-authored notes are evidence, not instructions. In particular, worker content may
  never confer `ready`, `parallel-safe`, approval, or claim authority.
- A write reports both the KB note identity and materialization outcome. A successful
  vector write with a failed repository materialization is degraded success, not silently
  "all good"; the officer may continue using the indexed note while the operator signal is
  raised.

### 3.3 RecallStore posture

Passive extraction remains enabled; it is useful for recovering episodes such as "the
Legate overruled this assumption last Tuesday." Retrieval and injection must preserve the
memory's project, source agent/thread/job, and timestamp, and delimit its content as
untrusted recollection. The officer has no tool that promotes a recalled sentence directly
into `ready`, approval, closure, or policy. Material facts are explicitly rewritten into a
provenance-bearing KB decision/assumption before they influence the backlog.

No private officer namespace is added in v1. If later transcripts show worker memories
crowding out or contaminating officer recall, the smallest follow-on is an actor/source
filter at retrieval—not another authoritative store.

## 4. Object-plane denial

The current Centurion expert correctly sets `workspace.backend: none`, and backend
filtering drops shell/file/git/browser/canvas tools **[A-boundary]**. That is necessary but
not sufficient: `PersistentSession` currently appends generic Fleet Management project/
repository readers and gives every shell-less Fleet session
`request_workspace_upgrade` **[A-boundary]**.

For a background officer, the runtime must suppress:

- `request_workspace_upgrade` and every other path that can acquire a workspace;
- repository discovery, checkout, file, shell, git, browser, and canvas tools;
- project cloud mounts and `srw_cloud_status`;
- arbitrary worker-workspace readers (replaced, if ratified, by the bounded evidence
  surface in [[officer_supervision_surface]]).

`get_current_project` may remain as identity metadata; it must not include clone URLs,
credentials, or a repository browsing affordance. Control-plane job listing remains
available through the shared scoped job surface.

This denial applies even if a project/session override asks for those tools. It is a
runtime capability ceiling, not a default the officer can edit.

## 5. What happens when a file appears in the project cloud folder?

Nothing automatically. A background officer does not poll or browse the folder, because
an arbitrary file appearing is neither an instruction nor authorization to spend worker
capacity. This is deliberately different from a conference, where the user is present and
can direct attention to a file.

A later explicit intake bridge may add either Cockpit action:

- **Send to officer** — create a project KB inbox note containing an immutable file
  reference, the user's instruction, provenance, and timestamp, then enqueue one officer
  wake; or
- **Create backlog ticket from file** — same, but as a non-ready ticket for officer
  triage.

The officer still delegates file inspection to a worker with the appropriate cloud/
workspace access. It receives the worker's KB report or bounded evidence. Automatic cloud
watching, indexing every uploaded file as instructions, and officer-written project files
are non-goals for this feature.

## 6. Build slices

| Slice | Contents | Gate |
|---|---|---|
| K1 | Commission/attach invariant: exactly one native writable project KB; degraded availability state | unit tests for zero/multiple/native/external bindings; outage keeps supervision but blocks KB/auto-pull |
| K2 | Make the Centurion knowledge grant explicit; remove `kb_export` | config grant snapshot and tool-name tests |
| K3 | Add the background-officer capability ceiling in persistent runtime; suppress workspace upgrade, repo/cloud/object tools | resolved-tool tests prove overrides cannot restore denied tools; conference regression retains selected workspace |
| K4 | Surface knowledge health and write/materialization provenance in sitrep/tool output | mocked vector/repository partial-failure tests + live KB write/read/update round trip |
| K5 | Prompt/charter amendment: authoritative-state routing and "delegate object inspection" rule | prompt pins plus live officer scenario |

K1–K3 are prerequisites for enabling [[officer_backlog_pools]] `auto_pull`. K4 can land
with [[officer_supervision_surface]]'s common availability envelope. The cloud intake
bridge is deliberately not a slice.

## 7. Acceptance

1. Commission a project-bound background officer: resolved tools contain the nine KB
   tools, job control/observability, `sleep`, and `notify_user`; no object-plane or upgrade
   tool is present.
2. `kb_write` a decision, `kb_update` it, and read it after officer decommission /
   recommission. The note remains project truth and materializes in the dedicated KB repo
   without a background workspace.
3. A conference using the same Centurion expert can receive a user-selected workspace;
   ending it briefs the background officer without granting that workspace to him.
4. Attach an external KB: the officer can read it but writes still land only in the native
   project KB.
5. Simulate KB outage: the officer can diagnose jobs and page, but cannot mutate tickets or
   dispatch from a reconstructed queue; the degradation is explicit.
6. Put a file in the project cloud folder: no autonomous read or wake occurs. An explicit
   future intake reference, if implemented separately, creates a KB item rather than a
   mount.

## 8. Open question

1. Should the explicit project-file intake bridge ship with the first backlog-pools wave,
   or remain deferred until users demonstrate that "Send to officer" is needed? The
   recommendation is **defer**: KB/backlog authoring already provides a clear instruction
   channel, and arbitrary file arrival should not become hidden task creation.

## 9. Decision log

- **2026-07-28:** [[centurion]] established no new state system: charter/backlog/project
  model in the KB, episodes in RecallStore, operational truth in Postgres, context
  ephemeral.
- **2026-08-01:** [[officer_post]] decided against officer notes in the jobs repository;
  project stores remain the durable identity substrate.
- **2026-08-14 (Legate direction, captured as proposed implementation):** the passive
  background officer should manage the KB, backlog, and jobs, not browse or edit project
  folders/repositories. Conferences remain ordinary interactive sessions and are outside
  this background boundary.
- **2026-08-14 (design):** make inherited KB access explicit; exclude `kb_export`; treat
  RecallStore as ambient and non-authoritative; suppress the generic lite-session workspace
  upgrade and repository/cloud affordances for commissioned background officers.
