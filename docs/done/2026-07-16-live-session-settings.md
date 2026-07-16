---
tags:
  - feature
  - cockpit
  - sessions
  - orchestrator
  - agent
related:
  - "[[job_settings_overhaul]]"
  - "[[session_header_streamline]]"
  - "[[workspace_tier_upgrade]]"
  - "[[instant_landing_session]]"
  - "[[dynamic_canvas]]"
---

# Live Session Settings

> Let the user edit the session configuration — model, temperature, permission
> mode, tool groups, datasources, workspace tier — while the session is
> running, through the same shared settings surface the New Session page
> already uses (`AgentSettingsComponent`), hosted in a side pane next to the
> chat (like the canvas). This is the natural completion of the
> instant-landing story: start with an empty default session, then add what
> you need the moment you discover you need it, instead of abandoning the
> conversation and re-creating the session with different checkboxes.

**Status:** Refined design — drafted 2026-07-16, then reworked the same day
after a five-agent research pass (three codebase audits: agent runtime,
cockpit, orchestrator; two web-research sweeps: competitor UX, dynamic-tool
best practices). Line anchors verified on `develop` 2026-07-16.
**P0 groundwork BUILT 2026-07-16** (all five items, unit-tested; see the
per-item notes in the build order below).
**Slice A BUILT 2026-07-16** (settings pane live on k3d; see the notes at the
end of the Slice A section).
**Slice B BUILT 2026-07-16** (live datasources, k3d e2e-verified add → query →
remove; see the notes at the end of the Slice B section).
**Slice C BUILT 2026-07-16** (owner-facing disconnected-session PATCH +
config-change audit, k3d e2e-verified edit-while-ended → resume → attach
pickup; two adjacent bugs found and fixed — see the notes at the end of the
Slice C section).
**Slice D BUILT 2026-07-16** (fit ladder + boundary sanitizer + family
mutation smokes, k3d e2e-verified reject/compact-then-swap/sanitize; see the
notes at the end of the Slice D section). **All slices complete.**
**Scope:** Cockpit (settings pane, header cleanup) + one contained
orchestrator/agent slice (live datasources) + protocol touch-ups + a small
owner-facing endpoint (disconnected-session editing) + a hardening slice for
the already-live model hot-swap.

## Motivation

With instant landing sessions (`instant_landing_session.md`), users jump into
a conversation with server-resolved defaults and zero upfront configuration.
That's the right entry UX, but it moves the configuration moment to the middle
of the conversation: halfway through you realize the agent is missing the job
tools, or a datasource, or has too much surface enabled. Today the only
remedies are the header popover (model + temperature + mode + narration only)
or abandoning the session and re-creating it through `/sessions/new`.

## What already exists (verified 2026-07-16)

The backend for live mutation is largely built. Inventory, with anchors:

### The live `config.update` path

Cockpit `PersistentChatService.updateConfig()`
(`persistent-chat.service.ts:2482`) sends a `config.update` WS control frame →
agent `_handle_config_update` (`src/api/persistent_app.py:5329`) → for
credential- or authorization-bearing changes, the agent calls the
orchestrator's internal `agent_update_thread_config`
(`orchestrator/main.py:18478`, `X-Internal-Key`), which:

- re-validates tool overrides against the closed session vocabulary
  (`src/core/session_tool_overrides.py` — shared framework-free module used by
  the create boundary, the runtime sanitizer
  (`persistent_app.py:1239`), and the orchestrator boundary),
- enforces the owner's capability grants (`_enforce_session_create_grants`,
  `main.py:4084` — fail-loud 422; admin owners bypass; ownerless threads skip),
- enriches model swaps with resolved `base_url`/`api_key` plus explicit `None`
  transport sentinels so the agent-side deep-merge *clears* the previous
  model's transport (`main.py:18553-18560`),
- durably merges a **redacted** copy into `threads.metadata.config_override`
  (`merge_thread_config_override`, `postgres.py:3311` — Python-side deep
  merge, lists replace wholesale; survives detach/resume; secrets never
  persisted).

**Live-mutable today, end-to-end:**

| Field | Mechanism | UI today |
|---|---|---|
| `llm.model`, `llm.temperature` (and in fact any `llm.*`, e.g. `reasoning_level`) | LLM rebuild + `refresh_context_limits()` | header popover |
| `auxiliary.*` | aux LLM rebuild + archiver rewire + prompt re-resolution | none |
| `env_keys` (embedding) | embedding singleton reset | none |
| permission mode | dedicated `mode.set` frame (`persistent-chat.service.ts:2471`) or `config.update` `interactive.permission_mode`; top-level column sync | header popover |
| narration mode | dedicated `narration.set` frame or `config.update` | header popover |
| `tools.{orchestrator,agent_catalog,workflows,canvas}` | `resetup_tools_for_backend()` rebind (`persistent_session.py:1316`) | **none** |
| workspace backend (upgrade only) | separate `upgrade-to-workspace` control frame (`persistent-chat.service.ts:2329`) | `/upgrade-workspace` slash command only |
| title | `PATCH /api/persistent/threads/{id}` (title-only, `main.py:20580`) | inline rename |

Changes take effect at the **next turn**: `run_persistent_loop` re-reads
`(llm_with_tools, tools)` after each `get_user_input`
(`persistent_graph.py:630-633`); the turn-local `tool_map` is frozen for the
duration of a turn (`persistent_graph.py:1887`). No graph rebuild.

**Not live-mutable today:** datasources (attach-time only), expert, projects.

### Known gaps in the existing path (found by the audit — they predate this feature)

These are pre-existing defects the feature would otherwise sit on top of.
They're listed here because the build order below fixes them first (P0):

1. **The rebuilt system prompt never reaches the live conversation.**
   `resetup_tools_for_backend` rebuilds `session.system_prompt`
   (`persistent_session.py:1347-1357`), but the loop inserts the prompt into
   `messages[0]` once at start (`persistent_graph.py:596-597`) and nothing
   ever rewrites it. Today's live tool-group toggles change the *binding* but
   not the prompt the model sees until the next detach/attach.
2. **Hydrated attaches drop the agent-side datasource enrichment.**
   `_attach_session` applies the datasource→tool-category map
   (`_ds_tool_map`, `persistent_app.py:1500-1548`) and
   `extra._cli_datasources` (`:1551`) to a local override — but when a
   `resolved_config` blob is present, hydration discards that mutation
   (`:1562-1578`). The warm-pool path compensates orchestrator-side
   (`main.py:2875-2905`); the **dedicated-pod path does not**
   (`main.py:18145-18147`), and `_cli_datasources` is set nowhere
   orchestrator-side. Net effect: on hydrated dedicated-pod attaches,
   read-only managed-connector tools don't load and the CLI-datasource prompt
   block never renders.
3. **No ack correlation.** `config.changed` echoes only
   `{model, temperature, permission_mode}` to the requesting socket only
   (`persistent_app.py:5595-5603`); failures are generic `error` frames; the
   orchestrator client collapses a 422 grant denial to `None` and discards
   the detail (`orchestrator_client.py:914-915`). Two in-flight updates are
   indistinguishable.
4. **The durable merge is an unlocked read-modify-write**
   (`postgres.py:3350-3379`) — concurrent updates can silently drop one.
5. **`GET /api/experts/defaults` returns the worker `defaults.yaml`, not
   `persistent_defaults.yaml`** (`main.py:23934-23941`) — the create form's
   "resolved default" markers are already wrong for no-expert sessions.
6. **Datasource env/CLI injection is append-only process mutation.**
   `process_datasources` writes `os.environ` and appends to the agent pod's
   `~/.pg_service.conf` with no manifest (`datasource_setup.py:113, 478,
   504-560`) — nothing to undo on removal, and it leaks across sequential
   pool attaches. Worse: those env vars never reach the remote workspace
   shell (`shell_manager.py:139-147` exports a fixed env; `remote.py`
   forwards nothing), so prompted CLI-mode datasource access appears
   inert on remote backends anyway. **Verify during P0; if confirmed dead,
   drop CLI-mode from the live-mutation scope entirely.**

### The shared settings surface

`job_settings_overhaul.md`'s shared surface was built: `AgentSettingsComponent`
(`cockpit/src/app/views/agent-settings/`) with `mode: 'job' | 'session'`
(`agent-settings.types.ts:7`) and sub-groups that each own override state with
a uniform **null = "use resolved default"** pattern — `execution-group`
(autonomy/permission), `model-group`, `tools-group`, `datasources-group`,
`advanced-accordion` (incl. workspace backend), plus a Resolved-JSON tab.
Collection is submit-time-only (`getOverrides()` deep-merge; the `(change)`
output is void — no per-control payload exists yet). Capability greying is
already wired via pure helpers (`capability-gates.ts`) and works live for free
by passing `capabilities.grants()`.

Two facts that reframe "reuse the create screen":

- **The create path itself only honors a subset.** `create_thread` persists
  `model`, `temperature`, `permission_mode`, validated `workspace.*`, and the
  four closed tool groups (`main.py:19370-19412`); the advanced-accordion's
  session-mode output (limits, memory, shell, browser, auxiliary, idle
  timeout, …) never reaches thread metadata. "Everything you could configure
  before starting" was already aspirational. The live pane should expose the
  honest set, not the create form's full render.
- **The session tools-group renders 12 categories
  (`SESSION_TOOL_CATEGORIES`, `agent-settings.types.ts:28-36`), but only 4
  survive validation.** Unfiltered, 8 of 12 toggles would silently no-op.
  Live mode needs a `LIVE_TOOL_CATEGORIES` filter (or disabled+hint rows).

## What the research established (2026-07-16)

Condensed from the two web sweeps; details and sources in the reports.

**Industry consensus that validates the design:** next-turn application
semantics are universal (Claude.ai documents "changes apply starting with the
next response" verbatim); the full transcript is silently carried to the new
config everywhere; **no product handles the removed-source-residual-context
problem** — our inline disclaimer is ahead of the field; grouped tool toggles
at conversation scope are the winning pattern (Cursor/Windsurf users loudly
demand in-chat toggles they don't have; VS Code caps at 128 tools/request).

**Industry signal that changes the design:** model/mode switching lives in or
next to the **composer/header in every product surveyed** (ChatGPT moved it
into the composer; Claude puts it next to send; Copilot/Cursor/Windsurf/
Gemini/Perplexity all composer-adjacent). Side panels exist (Open WebUI "Chat
Controls", LibreChat "Parameters") but only for the long tail — LibreChat
users filed a request to bring parameters *back* to the top bar. → The pane
must not be the *only* path to the model switch (see Slice A).

**Trust lesson:** Perplexity's silent model-downgrade scandal (UI showed the
selected model, not the actual one) → per-answer model attribution chips.
→ We stamp the transcript when config changes apply (see protocol work).

**Provider mechanics (engineering sweep):**

- Removing *some* tools is safe on every major provider — none validates
  historical tool_calls against the current `tools` array. Removing the
  *last* tool while history contains tool calls is the poisoned edge:
  LiteLLM-style proxies hard-400, Anthropic can return silently-empty
  responses, strict local chat templates (gpt-oss) crash. → never bind zero
  tools (floor rule below).
- **Model hot-swap is the #1 documented session-killer**, not tool mutation:
  cross-provider tool-call-ID format walls (Mistral's exactly-9-chars,
  Anthropic's regex), signed thinking-block validation (foreign thinking
  blocks → 400), Responses-style reasoning-item pairing, and context-window
  downsizing (fit discovered on the user's next message). → Slice D.
- Every tool/system-prompt change invalidates the entire prompt-cache prefix
  (tools → system → messages hierarchy; ~12.5× one-turn cost spike on
  Anthropic-style pricing, full re-ingest everywhere). → coalesce changes at
  the turn boundary; deterministic tool ordering, stable-first.
- Phantom calls to removed tools are a measured failure mode that *worsens*
  with reasoning-tuned models (arXiv:2510.22977 — relevant to our MiniMax
  fleet), and prompt notes alone don't eliminate it. → runtime unknown-tool
  guard with a teaching error (already half-exists: absent names return
  `"Tool 'X' not found"` ToolMessages, `persistent_graph.py:1888-1900`;
  extend the message to name currently-valid tools).
- Turn-boundary application is the de-facto ecosystem standard (MCP
  `list_changed` handling in Claude Code ≥2.1.0, OpenAI Assistants per-run
  tool overrides, LangGraph per-model-call middleware). Our per-turn re-read
  is the same shape; keep it.

## Design principles

1. **The orchestrator stays the authorization boundary.** Every
   authorization- or credential-bearing change flows through the orchestrator
   and the existing fail-loud behavior stays: if the orchestrator is
   unreachable, a tools/datasource update is *rejected*, never applied
   locally (`persistent_app.py:5400-5428` pins this for tools; datasources
   inherit it).
2. **Live removal is a convenience, not a security boundary.** The
   conversation, compaction summaries, and extracted memories may retain
   content from a removed datasource or tool. The UI states this inline at
   the point of removal. Real revocation happens at the datasource/credential
   level.
3. **One settings surface.** `AgentSettingsComponent` grows a `live` mode; the
   header popover's editors are retired into it — **except quick model/mode
   access, which stays one click from the composer** (industry-universal).
4. **Turn-boundary semantics, stated once, and coalesced — and the cost
   disclosed.** Changes apply on the next turn ("applies starting with the
   next response"). Agent-side, multiple deltas arriving between turns are
   batched into **one** rebind + **one** system-prompt rebuild — N toggles
   cost one cache invalidation, not N. The residual cost is disclosed to the
   user in the pane (see Slice A): a tools/datasource/model change resets the
   conversation's prompt cache, so the next response re-processes the full
   history — slower, and on cache-priced APIs noticeably more expensive for
   that one turn. Cosmetic edits (temperature, permission mode, narration)
   don't touch the cache prefix and don't warn.
5. **Config changes are visible in the transcript.** When a change takes
   effect, the agent emits a small persistent system event ("model →
   minimax-m3", "datasource *Sales DB* removed — earlier context may still
   reference it"). This is the removal disclaimer's durable record and the
   answer to "which config produced this answer?".
6. **Pin, don't reset.** The sub-groups' null="default" reset affordances
   cannot round-trip over a deep-merge (omission = no change; there is no
   clear-override op in the live protocol). Live mode hides reset affordances
   and treats every edit as an explicit pin. A `null`-clearing protocol
   convention is a possible later extension; not v1.

## Build order

### P0 — groundwork (fixes that stand alone) — **BUILT 2026-07-16**

Each of these repairs something already broken and unblocks a slice:

1. **Refresh `messages[0]` when the system prompt is rebuilt** (gap 1). Small
   change in the loop or session; makes today's live tool toggles honest and
   is a hard prerequisite for datasource guidance appearing live.
   ✅ Done: `run_persistent_loop` gained a `get_current_system_prompt`
   callback, re-read per turn next to `get_current_tools`; messages[0] is
   mutated **in place** (preserves message identity for persistence). Wired
   as `lambda: _session.system_prompt`, so any future writer (Slice B's
   `resetup_datasources`) is covered for free. Tests:
   `test_persistent_graph.py::TestRunPersistentLoopSystemPrompt`.
2. **Fix hydrated-attach datasource enrichment on the dedicated-pod path**
   (gap 2). ✅ Done, agent-side (uniform for warm-pool AND dedicated): the
   attach path now folds the datasource tool categories + `_cli_datasources`
   into `resolved_config["agent"]` before hydration
   (`_apply_datasource_enrichment_to_resolved`, persistent_app.py; note
   `_cli_datasources` goes at the blob's top level — the serializer flattens
   `extra` there). The two divergent type→tool maps are reconciled into ONE
   shared function `datasource_tool_categories()` in
   `src/core/datasource_setup.py`; the orchestrator's
   `_build_datasource_tool_override` delegates to it (orchestrator already
   imports `src.core`, same pattern as `session_tool_overrides`). The
   orchestrator's semantics won (all-read-only per type group → read tools;
   any read-write managed → CLI mode; stale categories stripped). Tests:
   `test_datasource_tool_categories.py`.
3. **Protocol touch-up**. ✅ Done: `config.update` accepts a client
   `request_id`, echoed on the `config.changed` ack and every error frame
   from the handler; the ack now carries the `applied` fragment
   (secret-scrubbed via `_scrub_secret_values`) and is **broadcast** to all
   subscribers (landing in the event journal — the transcript-stamp
   substrate for Slice A). `orchestrator_client.update_thread_config` raises
   a typed `ThreadConfigUpdateDenied(status, detail)` on 4xx instead of
   returning None; the handler surfaces the detail. Two behavioral fixes fell
   out: a grant-denied **model swap** no longer falls back to applying the
   raw override locally, and the cosmetic-path persist now runs BEFORE the
   local permission-mode apply (a denied escalation no longer takes effect
   in-RAM). Cockpit `updateConfig()` mints and returns the request_id.
   Tests: `TestHandleConfigUpdateAckProtocol`, `TestUpdateThreadConfig`,
   updated service spec.
4. **Advisory lock around `merge_thread_config_override`** (gap 4).
   ✅ Done: `pg_advisory_xact_lock` taken on the merge's own connection
   (no second pool slot → no pool-exhaustion deadlock), with a
   domain-salted key (`blake2b("config_override:" + thread_id)`) so merges
   never queue behind the minutes-long provisioning lock that shares the
   unsalted key. Tests: `TestMergeThreadConfigOverride` (also pins the
   deep-merge/list-replace semantics — a known test-landscape hole).
5. **Verify CLI-mode datasource env reachability** (gap 6). ✅ Verified:
   **CONFIRMED DEAD on remote backends** — env vars/`pg_service.conf` land on
   the agent pod; the remote tmux shell gets only `NONINTERACTIVE_ENV_EXPORT`;
   provisioning seeds nothing; and read-write managed connectors got no tool
   connection either, so they had NO access path at all (jobs and sessions).
   Filed as `docs/issues/datasource_cli_mode_dead_on_remote.md`, and the
   **fix (direction 1) was implemented the same day**: read-write managed
   connectors now get real connections + write tools via the shared map;
   `cli_ds_types` is always empty (the `_cli_datasources` prompt block no
   longer renders, its plumbing kept as the seam for a future genuine
   CLI-forwarding feature — see the issue doc). Net effect for this feature:
   Slice B mutates connection-backed tools only, and the read-write case is
   no longer a special "no tools" branch.

### Slice A — the settings pane (cockpit-only)

Everything already live-mutable becomes visible and editable. No backend
changes required (P0.3 improves it but doesn't gate it).

**Placement.** The chat page's existing `as-split` gains a content-switched
right pane: **canvas or settings, one at a time** (a `paneContent` signal in
`chat-page.component.ts`), reusing the canvas open/close/focus/mobile
machinery. Not a third split-area — the min-size math, mobile behavior, and
`configureSplitterAccessibility` (`chat-page.component.ts:284`, hard-coded
panel ids) all assume two areas. Known real work: the **canvas auto-open
effect** (`chat-page.component.ts:211-244`) force-opens the pane on a new
canvas source and would steal it from settings — teach it "settings open →
badge the canvas toggle instead". That effect is pinned by ~8 focus/a11y
specs which must stay green.

**Entry points.** A settings toggle projected via the existing
`[chatHeaderAction]` slot (like the canvas toggle) opens the pane; the status
chips become click-throughs to it. Per the industry signal, **model and
permission mode stay directly reachable at the header**: clicking the model
chip opens the pane *scrolled to / focused on* the model control (cheap
compromise that keeps one source of truth; a composer-level quick-switcher is
a v2 option). The `.settings-panel` collapsible row dies — along with
**four** device-local display prefs that move to a small view menu:
reasoning, tool-calls, reading width, text size (all `chatPrefs`/localStorage;
the doc previously listed only two). Deleting the popover breaks **zero
component specs** (verified — nothing pins `showSettings` or its handlers);
only the upgrade-notice copy pins in `persistent-chat.service.spec.ts:2704+`
need updating when a real button replaces "type /upgrade-workspace".

**Component wiring.** Host `AgentSettingsComponent` with `mode="live"` in a
new `settings-pane.component.ts`:

- `SettingsMode` gains `'live'`; new `LIVE_TOOL_CATEGORIES` = the four closed
  groups; non-live categories and non-honored advanced sections are hidden
  (not disabled — they'd imply a power the path never had).
- A payload-bearing apply channel: per-control emit `{path, value}` in live
  mode (design (b) from the audit — exact semantics; the fragment-diff
  alternative can't express resets, which live mode doesn't offer anyway,
  principle 6).
- Prefill: overrides from `GET /api/persistent/threads/{id}`
  `metadata.config_override` (redacted, includes tools) + live signals
  (`modelName`, `temperature`, `permissionMode`, `narrationMode`); resolved-
  default markers from `GET /api/experts/{expert_id}` when an expert is set.
  For no-expert sessions the markers are knowingly approximate until P0's
  defaults fix or a `persistent_defaults` variant of `/api/experts/defaults`
  lands (cheapest accurate option; small orchestrator change — accept or
  schedule consciously).
- Apply mapping per control: model/temp/tools → `updateConfig(...)`;
  permission/narration → the existing dedicated `setMode`/`setNarrationMode`
  verbs (they broadcast `mode.changed` and persist; don't duplicate via
  config.update); workspace → a new **public** `upgradeWorkspace(tier)`
  service method wrapping the existing frame (`persistent-chat.service.ts:2329`
  is private-slash-command-only today), enablement = upgrade-only paths
  (virtual→sandbox, virtual→vm, sandbox→vm; vm additionally gated by
  `can_use_vm`), with progress from the existing `workspace_upgrade.*`
  frames; title → thread PATCH.
- Feedback: v1 = optimistic set + reconcile on ack + one shared error strip
  (what the popover does today); upgrade to per-row correlated status when
  P0.3 lands.
- **Cache-reset cost disclosure** (principle 4): one static fine-print note
  in the pane, paired with the turn-boundary line and placed by the controls
  it applies to (tools group, datasources group, model control) — copy along
  the lines of: *"Changes apply starting with the next response. Changing
  tools, datasources, or the model resets the conversation's prompt cache —
  the next response re-processes the full history, which is slower and can
  cost noticeably more for that turn."* Static text, no per-toggle nag or
  confirm dialog (coalescing already means N toggles = one invalidation).
  Temperature, permission mode, and narration are exempt — they don't touch
  the cache prefix (the hierarchy is tools → system → messages; inference
  params and client-side modes aren't in it). Grounding: the provider sweep
  measured any tool/system change as a full prefix re-ingest, ~12.5× the
  cache-read input price for that turn on Anthropic-style pricing;
  latency-only on self-hosted vLLM.
- Datasources section renders the **attached set read-only** (pass attached
  rows + `disabled` — dodges the missing `setSelection` API until Slice B);
  expert and projects read-only with "set at creation" hints.
- Housekeeping the audit flagged: call `modelService.load()` on the chat
  page; exclude datasources from the "modified" tab badge in live mode;
  debounce the temperature slider (fires per tick today); keep the
  `.settings-panel` CSS class (citations/cloud-diff drawers reuse it).

**Availability.** Connected sessions only (the control WS URL only resolves
once an agent is bound — `_openControlWs`, `persistent-chat.service.ts:1467`).
Disconnected editing is Slice C.

**✅ BUILT 2026-07-16.** As designed, with the deltas worth knowing:

- **Apply channel = host-side desired-state diff**, not per-control
  `{path, value}` emissions. The pane (`settings-pane.component.ts`) keeps the
  sub-groups' existing void `(change)` + `getOverrides()` contract, flattens
  the tracked surface (llm.model/temperature/reasoning_level,
  interactive.permission_mode/narration_mode, four tool groups as
  enabled-booleans) and diffs against the last-applied snapshot — identical
  wire behavior with no refactor of four sub-components, and resets can't
  exist in live mode anyway (principle 6). Deltas debounce 400 ms into ONE
  `config.update` per batch (principle 4's coalescing, client side too).
  **Trap fixed during the k3d smoke**: the diff baseline must anchor to the
  CONFIG-ONLY state after the thread fetch (never to `getOverrides()`), and
  an apply firing before the baseline exists must reschedule — otherwise a
  pin made while the fetch is in flight is silently absorbed as baseline.
- Narration lives in the execution group and temperature in the model group
  as live-only rows (the create forms don't render them); permission +
  narration dispatch via their dedicated verbs, the rest via `config.update`.
- Re-enable payloads use `SESSION_TOOL_GROUP_NAMES` — a cockpit mirror of the
  closed vocabulary, drift-pinned by `tests/test_session_tool_group_mirror.py`.
- Transcript stamps (criterion 8) render from the P0.3 broadcast ack's
  `applied` fragment (`describeAppliedConfig`, persistent-chat.service.ts) —
  journaled, so replays keep them.
- Workspace tier is a pane-level row; `upgradeWorkspace(tier)` is now a
  public service method (the `/upgrade-workspace` slash command delegates to
  it) with progress via new `workspaceTier`/`workspaceUpgradeInProgress`
  signals; the "type /upgrade-workspace" notice copy now points at the pane.
- k3d-verified end-to-end: pane open → tool toggle → coalesced
  `config.update` → grant-validated merge (`metadata.config_override.tools`
  confirmed in DB) → rebind → broadcast `config.changed` → exactly one
  transcript stamp; re-enable passed the closed-vocabulary validation; next
  turn logged `System prompt refreshed live` (P0.1 live proof). This smoke
  also discharged P0.3's owed live verification.
- Residuals: VM upgrade button is fail-closed while grants load (shown only
  with an explicit `vm` grant / admin-null — on the dev cluster the test
  user's fetch resolved without it; verify grant wiring when Slice C lands);
  mobile full-screen pane and the read-only datasources rendering are
  unit-covered but not browser-verified; the model picker's "Default" label
  shows the config name until the welcome frame delivers the real model
  (pre-existing `modelName` seeding oddity).

### Slice B — live datasources (the real backend work)

**Corrected flow** (the original draft named the wrong pipeline — datasource
credentials never touch `config_override`; they travel as a separate
`datasources` payload key, and `redact_config_override` would NOT strip
connection URLs/usernames if they leaked into the override merge):

1. Cockpit sends the desired full `datasource_ids` set (idempotent,
   matches create semantics; eligible list from
   `GET /api/datasources/eligible?project_id=…` fed with the thread's
   `project_ids` — tolerate per-project 403s from revoked memberships).
2. Agent forwards over a new field on the internal PATCH
   (`AgentThreadConfigUpdateRequest` grows `datasource_ids`; the
   `needs_enrichment` gate at `persistent_app.py:5380` learns the key). Same
   fail-loud rule as tools: no orchestrator, no change.
3. Orchestrator: authorize via `_authorize_thread_datasource_ids`
   (`main.py:19160`) **passing the thread's current `workspace_backend`** —
   the revalidation wrapper deliberately passes `None` to avoid breaking
   existing threads (`main.py:19234-19238`), but a live add is create-like
   and must keep the lite-tier/repository rejection (`main.py:19184-19194`)
   alive. Persist via a **new** `metadata.datasource_ids` writer
   (`datasource_ids` is a top-level metadata key; `merge_thread_config_override`
   can't touch it — no helper exists yet, follow the vm/workspace-context
   merger pattern in `postgres.py:3064/3200`). Compute the tool-category flip
   (`_build_datasource_tool_override`, `main.py:15384`) and run
   `_enforce_session_create_grants` on the **flipped** fragment so a
   `datasource_tools`-denied principal fails at the PATCH, not at next attach.
4. Agent applies. The simplest correct transport for the enriched blocks: the
   agent **re-fetches `GET /api/agents/threads/{id}/workspace`**
   (`orchestrator_client.get_thread_workspace`, `orchestrator_client.py:551`),
   which already returns freshly revalidated `datasources` with per-attach
   credentials — zero new payload-shape code. Then, agent-side
   `resetup_datasources()` (new, sibling of `resetup_tools_for_backend`):
   - diff by **id→type** — the connection registry is *type-keyed with
     last-one-wins* (`datasource_setup.py:138`), so the multi-same-type case
     must be handled explicitly;
   - for removals: close via `close_datasource_connections`
     (`datasource_setup.py:152`), **mutating `tool_context.datasources` in
     place** (shared reference, `persistent_session.py:1119` — rebinding like
     `cleanup()` does would orphan the ToolContext);
   - for additions: `process_datasources` (`datasource_setup.py:47`) for the
     new entries;
   - apply the datasource-derived tool categories **directly to
     `session.config.tools.*`** exactly as `_attach_session` does
     (`persistent_app.py:1536-1548`) — they must NOT ride the validated
     `tools` override, whose closed vocabulary silently drops
     `sql/graph/mongodb/webdav` at both boundaries;
   - update `config.extra["_cli_datasources"]` (prompt template input,
     `loader.py:3925`);
   - **rewrite** (not re-append) the workspace `datasources.md` index
     (`inject_datasource_index` appends today, `datasource_setup.py:952-957`);
   - run repo clones for added repository datasources
     (`clone_repository_datasources`) — on removal, drop the
     `workspace_manager.source_repos` entry and document that clone + SSH key
     remain on the workspace (cheap honesty; scrubbing is not a security
     boundary per principle 2);
   - then `resetup_tools_for_backend()` + the P0.1 `messages[0]` refresh.
   - **kb-type datasources are out of scope for v1** — they feed
     `knowledge_bindings`, which ToolContext holds as a *copy*
     (`persistent_session.py:1130`) and which wire into memory/KB machinery;
     the picker filters them out with a hint.
5. Next turn picks up the new belt via the per-turn re-read; the transcript
   stamp (principle 5) records the change.

**Removal timing (corrected semantics).** Datasource tools capture their
connection in a closure at load time (`sql/postgresql.py:68`,
`webdav/tools.py:73`, `mongodb/mongo.py:108`, `graph/neo4j.py:80`). With
eager close, an in-flight turn's call **errors** ("connection is closed" →
caught, returned as a tool-error ToolMessage, turn continues) — it does *not*
"succeed once". Decision: **defer `close_datasource_connections` until the
turn ends** (`_turn_in_flight`, `persistent_app.py:330`) so the observable
rule stays clean — "changes apply at the next turn" — in both directions.
The disclaimer wording covers retained *context*, not a last successful call.

**Floor rule (from provider research).** Never rebind to an empty toolset
when the history contains tool calls: proxies 400, Anthropic degrades to
empty responses, strict templates crash. If the effective belt would be
empty, keep a minimal built-in bound. (Reachable today only on `none`-backend
sessions with all four groups off — cheap guard, real edge.)

**✅ BUILT 2026-07-16.** Implemented as designed, with these notes:

- **Wire shape**: `datasource_ids` rides the existing `config.update` frame
  as a sibling key (desired FULL set; `undefined` = no change, `[]` = detach
  all), coalescing with any config fragment into one frame → one PATCH → one
  cache invalidation. Same key on `AgentThreadConfigUpdateRequest`; the
  accepted normalized set comes back in the PATCH response. Persist is a new
  `set_thread_datasource_ids` full-replace writer (idempotent desired-state,
  so no advisory lock needed, unlike the config_override RMW).
- **Agent apply = full registry rebuild**, not surgical per-id diff:
  `resetup_datasources` re-runs `process_datasources` on the whole new
  payload (the exact attach path), so multi-same-type + RO-first-sort
  precedence stay correct by construction; the add/remove summary and repo
  bookkeeping diff by `(type, name)` because `_build_datasources_payload`
  strips ids. Replaced connections are returned unclosed and
  `_close_datasources_after_turn` polls `_turn_in_flight` before closing.
- **`inject_datasource_index` now rewrites** (cuts everything from the
  `## Available Datasources` marker before appending the fresh section) and
  writes an explicit `_No datasources attached._` state on remove-all.
- **Picker**: eligible union (`GET /api/datasources/eligible` with the
  thread's project_ids) minus unattached kb entries; attached kb render
  checked-but-locked (`lockedIds`); the group's untouched default is the
  ATTACHED set (`initialSelectedIds`), not all-eligible; attached ids the
  picker can't show (hidden kb, revoked visibility, eligible-fetch failure)
  are preserved in every dispatched set so an unrelated toggle can't silently
  detach them.
- **k3d e2e (session 219cd282, 2026-07-16)**: pane toggle → one frame
  `{config, datasource_ids, request_id}` → PATCH authorized + flip
  grant-checked → `metadata.datasource_ids` confirmed in DB → agent
  "Connected to postgresql datasource: Slice B Smoke DB (read-write)",
  75→78 tools, index rewritten (1 entry) → stamp `datasource "Slice B Smoke
  DB" attached` → next turn the agent ran `sql_schema`/`sql_query` and read
  the scratch table → toggle off → "0 attached (0 added, 1 removed)",
  "Closed 1 replaced datasource connection(s) after turn end", 78→75 tools,
  DB `[]`, `datasources.md` = exactly one section with the empty-state line
  (agent read it back verbatim), stamp `datasource … detached`.
- **Residuals**: (1) `_sendControl` is best-effort — the smoke's first toggle
  was silently dropped by a mid-churn control WS (pre-existing transport
  semantics, now user-visible for datasources too; a request_id-keyed
  ack-timeout + rollback would fix it); (2) the pane's diff baseline can pick
  up "riders" when live signals move via acks after anchoring (modelName
  seeds as the config name until the welcome frame; the temperature ack
  echoes the matrix-resolved value) — the riders re-assert current server
  state so they're semantically no-ops, but they pollute stamps and burn a
  cache invalidation; folding `config.changed` acks into `lastApplied` would
  quiet them; (3) kb-type changes stay attach-time (locked in the picker,
  `kb_deferred` in the ack if forced via API); (4) the lite-repo rejection
  and grant-flip denial paths are unit-tested, not smoke-tested (no lite+repo
  fixture on the dev cluster).

### Slice C — disconnected-session editing

Add an owner-facing config PATCH (or extend `update_thread`,
`main.py:20580`): `require_thread_owner`, then the same helpers the internal
endpoint uses (`_validated_session_tool_overrides`,
`_enforce_session_create_grants` keyed to the thread owner, datasource
authorization from Slice B), then redacted merge. Confirmed cheap by the
audit:

- **No enrichment needed** — all three attach paths re-read and re-resolve
  `metadata.config_override` + `datasource_ids` per attach
  (`main.py:18126-18137`, `21176-21196`, `_resolve_session_config`
  `main.py:1305-1410` — "no freeze"), and LLM/datasource credentials are
  injected in-flight at attach. Validate → grant-check → merge is the whole
  endpoint.
- **The response must be redacted** (`redact_config_override`) — the internal
  endpoint intentionally returns plaintext transport secrets to the agent;
  copying that shape to a browser-facing endpoint would leak provider keys.
- **Gate on disconnected state** (`agent_id IS NULL` / suspended): there is
  no orchestrator→agent config-push channel — the only orchestrator-initiated
  signals are heartbeat `intents` (`main.py:18993-18997`). A connected-session
  edit through this endpoint would silently go stale until next attach. If
  connected-session server-side editing is ever wanted, a `config_changed`
  heartbeat intent is the extension point; not v1.
- Emit an audit record: no config-change audit exists today; follow the
  `log_security_event` app-DB pattern (`security/access.py:107`) with
  `event_type='session_config_updated'` — it covers both this endpoint and
  the internal one.

**✅ BUILT 2026-07-16.** As designed, with these notes:

- **Endpoint**: `PATCH /api/persistent/threads/{thread_id}/config`
  (`require_thread_owner`), body `{config_override?, datasource_ids?}` —
  the `datasource_ids` key has Slice B semantics (full desired set). 400 on
  an empty body; response = the REDACTED accepted fragment +
  normalized `datasource_ids`.
- **Shared core extracted**: the internal endpoint's whole
  validate → datasource-authorize → flip-grant-check → enrich → redacted
  merge → pm-column sync → datasource persist pipeline now lives in
  `_apply_thread_config_update` (main.py), called by both endpoints — so the
  owner PATCH cannot drift from the live path. Enrichment (transport `None`
  sentinels) runs here too, deliberately: without it, an offline model swap
  would inherit a previous swap's persisted `base_url`. Only the response
  differs (internal: enriched plaintext for the agent; owner: redacted).
- **Connected gate**: 409 when `agent_id` is bound AND status is live
  (`created`/`active`/`awaiting_user`). Two live findings shaped it:
  thread CREATE eagerly binds an agent, so even a never-attached fresh
  thread 409s (correct — its config may be mid-resolution); and END leaves a
  stale `agent_id` on the row, so suspended/ended states are editable
  regardless of `agent_id` (a drainless pod kill must not wedge editing).
- **Audit**: both endpoints now emit a `session_config_updated`
  security event (the `log_security_event` pattern) after every persist step
  succeeds — key paths only (`keys=llm.temperature,tools.canvas
  datasource_ids=1`), never values (the enriched fragment holds secrets);
  `user_id` = caller for the owner route, NULL for the internal route (the
  recorded path distinguishes them). The endpoint-inventory manifest was
  regenerated (`gated:require_thread_owner`).
- **k3d e2e (thread dba52e19, 2026-07-16)**: fresh thread → PATCH 409 (agent
  eagerly bound) → end → PATCH `{llm.temperature: 0.55, tools.canvas: []}`
  200 with redacted echo → DB merge confirmed (existing create-time keys
  preserved) + audit row (`user_id=d32df192…`, detail
  `keys=llm.temperature,tools.canvas`) → resume → attach: agent built its
  LLM with `temp=0.55` and loaded 62 tools with NO canvas tools → PATCH
  while connected → 409. Acceptance #16 fully proven.
- **Two adjacent bugs found live and fixed in this slice**:
  1. `GET /api/persistent/threads[/{id}]` returned `metadata` as a JSON
     **string** (`_redact_thread_metadata` "preserved the original
     representation" of asyncpg's JSONB-as-string) while every Cockpit
     consumer is typed against an object — silently nulling the settings
     pane's config/tools prefill (Slice A), the attached-datasource default
     on a fresh pane open (Slice B), and the REST model/temperature seeding
     in `persistent-chat.service`. This was the actual root cause of the
     twice-documented "model shows the config name until the welcome frame"
     residual. Fixed: metadata now always leaves as a parsed object
     (contract pinned by `TestRedactThreadMetadataShape`); browser-verified
     — fresh pane open shows temp 0.55, Canvas unchecked, model
     `gemma-4-moe` before any welcome frame.
  2. The pane's VM-upgrade gate checked grant key `vm`, but the PDP key is
     `vm_workspace` (`src/core/capability_grants.py`) — granted users could
     never see the button (fail-closed; the server gate enforces the same
     grant anyway). Fixed + browser-verified positive case with a temporary
     DB grant. This discharges the Slice A "verify VM-grant wiring" residual.
- **Residuals**: no Cockpit surface drives this endpoint yet — sessions
  auto-attach on open, so the pane only exists connected; today's consumers
  are API/MCP callers, and a sessions-list "edit settings without opening"
  affordance is the natural v2 surface. Connected-session server-side
  editing stays out of scope (heartbeat `config_changed` intent is the
  extension point).

### Slice D — model hot-swap hardening (protects what's already live)

The research ranked cross-provider model swap the most dangerous mutation in
the whole surface, and it has been live in the popover for months. Ship
independently of the pane:

1. **Fit-check ladder on swap**: pre-check history tokens against the new
   model's window → if over, compact **with the old model still bound** → if
   still over, reject the swap with a message. Never discover the overflow on
   the user's next message. (`refresh_context_limits` handles the *threshold*
   side today; the proactive check/compact/reject ladder is new.)
2. **Provider-boundary history sanitizer**, applied only on swaps that cross
   providers/families: remap tool-call IDs to the target's format (both the
   assistant and tool-result sides, consistently — LiteLLM's
   `_sanitize_anthropic_tool_use_id` is the reference), strip/convert
   foreign reasoning/thinking content (signed thinking blocks must never be
   replayed cross-provider; `reasoning_content`+`tool_calls` combos crash
   gpt-oss templates), verify tool_use/tool_result pairing afterwards. Keep
   it at the transport seam; persisted history stays provider-native.
   (Overlaps the existing tool-pairing-400 backlog item — same invariant, new
   writers.)
3. **Per-family mutation smoke tests** on local vLLM (minimax, gpt-oss,
   qwen): {tool-bearing history} × {some tools removed, all tools removed,
   model swapped}. Template strictness is the least documented layer.

**✅ BUILT 2026-07-16.** All three rungs, k3d e2e-verified:

- **Fit ladder** — `_model_swap_fit_ladder` (persistent_app), called from
  `_handle_config_update` before the swap is applied, and only when the
  model itself changes (temperature-only `llm` fragments skip it). Check:
  `max(bare-history count with the CANDIDATE's tokenizer, provider anchor)`
  vs the candidate's derived `context_threshold_tokens` (per-model
  `context_window` reaches the live swap via the enrichment PATCH's
  `model_max_context_tokens` setdefault). Over → compact **on the live
  ContextManager** with its limits rolled forward to the candidate (rolled
  back on failure/rejection; a scratch manager would lose the
  `_record_compaction` plumbing — progress frames, boundary id, stats),
  `force=True` so the progressive keep-window + emergency-truncation ladder
  runs, `trigger="model_swap"` on the banner + summary row. Still over the
  candidate's hard window after compaction → reject with a user-facing
  detail; the session keeps working on the old model. A turn in flight +
  over budget → reject (compacting the durable list concurrently with a
  running turn can drop its appends); note `_turn_in_flight` stays true
  through post-turn work (title gen, memory extraction), so a swap right
  after an answer can hit this branch — retry succeeds.
  - **Fixed-overhead accounting** (found live): the post-compaction verdict
    is `bare count + max(0, anchor − pre-compaction bare count)` — the
    system prompt + tool schemas ride every request and compaction can't
    shrink them. Without it a 2.2k bare history passed a 3k window while
    the real request measured 17.6k → 413 on the next turn.
  - **Adjacent fix**: compaction success now clears the provider-usage
    anchor (`ContextManager._note_compaction_success`, both success sites) —
    the stale anchor kept the trigger floored above threshold and re-fired a
    needless compaction on the next check after any compaction (worst
    post-swap; also affected manual `/compact`).
- **Boundary sanitizer** — `sanitize_history_for_provider_boundary`
  (core/context): strips reasoning/thinking content blocks (`thinking`,
  `redacted_thinking`, `reasoning`, `reasoning_content` — langchain-openai
  passes `redacted_thinking` + Responses-style `reasoning` blocks through
  to chat/completions) and reasoning `additional_kwargs`; drops
  `invalid_tool_calls` (serialized outbound with no matching result); drops
  assistant turns left with no content and no calls; remaps nonconforming
  tool-call ids deterministically (blake2b) and consistently on both sides —
  Mistral's strict `^[a-zA-Z0-9]{9}$` special-cased, `[a-zA-Z0-9_-]{1,40}`
  everywhere else; ends in `repair_tool_pairing`. Pure-sync + idempotent;
  in-memory working set only (persisted `thread_messages` stay
  provider-native). Applied on swaps where `family_of` or `provider`
  changes, and at restore (`_sanitize_restored_history`, both Path A/B,
  after pairing repair) since Slice C's offline PATCH means restored
  histories can be foreign to the bound model — restore rows are already
  reasoning-flattened, so the restore rung is effectively the id remap.
- **Family mutation smokes** — `tests/test_family_mutation_smoke.py`,
  opt-in via `SRW_SMOKE_LLM_ENDPOINTS` (JSON endpoint list; unset → module
  skips, CI never hits the network). Matrix per endpoint over the repo's
  own `create_llm` transport: native tool-bearing history × {some tools
  removed, floor-bound (prod's never-bind-zero), zero tools (the poisoned
  case), foreign Anthropic-shaped history through the sanitizer}. **8/8
  green live** against gemma-4-moe (self-hosted vLLM) and MiniMax-M3
  (cloud); the harness takes any endpoint list for gpt-oss/qwen when one is
  reachable.
- **k3d e2e evidence** (sessions df10461f, 9eaaeafc, both deleted after):
  gemma tool-turn history → swap to MiniMax-M3@400 rejected with
  "~2,246 tokens after compaction — more than … (400 tokens)" and the
  ladder's summarize→elide→emergency-truncate visible in the agent log,
  chip stayed gemma; window restored → swap succeeded with
  `Provider-boundary sanitize (target family=minimax-m3): … 1 reasoning
  kwarg` + `LLM hot-swapped: model=MiniMax-M3`, and MiniMax answered
  correctly **from the gemma-produced history** ("20"; verbatim greeting
  recall on the second session); swap to MiniMax@3000 with only ~2.1k bare
  history rejected with the overhead-aware "~13,831 tokens … (including the
  system prompt and tool schemas)" message — the exact case that 413'd
  pre-fix; compact-then-swap rung observed at gemma@3000→ (compaction
  triggered at 2,274 tokens, then `LLM hot-swapped`).
- **Unit tests**: `tests/test_model_swap_hardening.py` (34: sanitizer
  reasoning/id/pairing/idempotency/no-mutation, ladder branches incl.
  overhead rejection + rollback, anchor-clear, source pins for the
  `_handle_config_update` wiring and both restore paths).
- **Residuals**: the pane's model select keeps the rejected value after a
  "Model switch rejected" error (baseline doesn't roll back; the chip shows
  the truth — fold error acks into the diff baseline like the config.changed
  rider fix); `_turn_in_flight` lagging the visible answer makes a
  swap-right-after-a-turn hit the busy branch (cosmetic, retry works);
  cross-model overhead estimate uses the old model's anchor (biased but
  order-correct).

## Out of scope

- **Expert switching mid-session.** Persona prompt + toolset + model preset
  resolved at attach; swapping identities mid-thread is semantically murky
  (LibreChat's attempt is a bug farm). The industry's cleaner future model is
  *additive* per-message persona injection (ChatGPT's @-mention-a-GPT), which
  maps to "expand the expert into an override and apply" — explicitly not v1.
  Shown read-only.
- **Project membership changes.** Projects scope memory, KB, and datasource
  eligibility; not turn-bounded. Read-only.
- **kb-type datasources** (v1) — see Slice B.
- **Per-message one-off overrides** (send this one message with a bigger
  model / tools off): clear v2 pattern from the industry survey — composer-
  level, ephemeral, never mutates the session default. The session-scoped
  machinery built here is its foundation.
- **Worker jobs.** Different lifecycle; nothing here touches the job path.
- **Workspace downgrades.** The upgrade frame stays upgrade-only.
- **CLI-mode datasource env forwarding** — pending the P0.5 verification;
  likely a separate fix.

## Acceptance criteria

P0:
1. Toggling a tool group live changes both the binding **and** the system
   prompt the model sees on the next turn (messages[0] refreshed).
2. A dedicated-pod hydrated attach loads read-only managed-connector tools
   and renders the CLI-datasource prompt block (parity with warm-pool).
3. `config.changed` carries `request_id` + the applied fragment and is
   broadcast; a grant-denied change surfaces its 422 detail to the client.

Slice A:
4. A connected session shows the settings pane (desktop: right split pane
   shared with canvas via content switch; mobile: full-screen) opened from a
   header action and from the status chips; the old `.settings-panel` rows
   are gone; all four display prefs moved to a view menu; canvas pushes while
   settings is open badge the canvas toggle instead of stealing the pane, and
   the existing canvas focus/a11y specs stay green.
5. Model, temperature, permission mode, narration, and the four session tool
   groups are editable live; only the four validated groups render; a toggled
   group is usable on the next turn and survives detach/resume.
6. Workspace upgrade is a button (upgrade-only, vm grant-gated) with progress
   from the existing frames; the "type /upgrade-workspace" copy is gone.
7. Datasources/expert/projects render read-only with hints; no reset-to-
   default affordances render in live mode.
8. Every applied change produces a transcript stamp visible to all viewers.
9. The pane shows the turn-boundary + cache-reset cost disclosure as static
   text next to the tools/datasources/model controls; cosmetic controls
   (temperature, permission mode, narration) carry no such warning; no
   per-toggle nag or confirm dialog is introduced.

Slice B:
10. Adding a datasource mid-session makes its tools available on the next
    turn, with credentials resolved orchestrator-side, never entering
    `config_override`, and never persisted in thread metadata (only
    `datasource_ids` persists, via the new metadata writer).
11. Removing one closes its connections **after** any in-flight turn ends,
    removes its tools from the next turn onward, rewrites `datasources.md`
    without duplication, and stamps the transcript with the removal note.
12. A live add of a repository datasource to a lite-backend session is
    rejected at the PATCH (workspace_backend passed to authorization); a
    `datasource_tools`-denied principal is rejected at the PATCH (flip-then-
    grant-check ordering).
13. A datasource update with the orchestrator unreachable is rejected loudly,
    never applied locally.
14. Detach/resume after live add/remove reconstructs the same set from
    `metadata.datasource_ids`.
15. **Pairwise state preservation** (the LibreChat regression class): for
    every pair in {model, tools, datasources, permission mode}, change A then
    B and assert A survived — as automated tests.

Slice C:
16. Editing a disconnected session's config persists (redacted response,
    owner-auth, audited) and takes effect at next attach; the endpoint
    refuses (or clearly flags) connected sessions.

Slice D:
17. A swap to a smaller-window model with an over-budget history compacts
    first or rejects cleanly — never a broken next turn.
18. A cross-provider swap with tool-bearing history passes the family
    mutation smoke tests (no 400s from IDs/thinking blocks/pairing).

## Test landscape (what exists, what's missing)

Existing anchors the work extends:
`tests/test_persistent_app.py::TestHandleConfigUpdateEnrichmentGate` (fail-
loud tools gate, sanitizer, aux/embedding rebuilds),
`tests/test_session_config_plumbing.py` (closed-vocabulary boundaries; attach
refuses revoked datasources), `tests/test_kb_datasource_api.py`
(`_authorize/_revalidate_thread_datasource_ids` contracts incl. lite/repo
rule and enumeration-oracle), `tests/test_capability_grants*.py` (PDP + 422
paths), `tests/test_datasource_repo_clone.py`, `tests/test_datasource_redesign.py`
(payload credential withholding), cockpit specs per component (the popover
itself is pinned by nothing — free to delete).

Known holes to fill alongside the slices: nothing exercises
`agent_update_thread_config` end-to-end (validation→grants→enrich→merge
ordering), nothing pins `merge_thread_config_override`'s deep-merge/list-
replace semantics, nothing covers `_attach_session`'s `_ds_tool_map`
injection or `process_datasources`' persistent-path branches, and no cockpit
spec covers `config.changed` handling.
