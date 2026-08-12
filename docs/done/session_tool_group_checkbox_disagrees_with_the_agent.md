---
tags:
  - issue
  - cockpit
  - orchestrator
  - sessions
  - config-resolution
---

# Settings→Tools shows a group ticked while the agent has none of its tools — the checkbox reads `config_override`, the agent reads the merged config

**Status:** FIXED 2026-07-26 (commit `ce9222f9`), deployed to dev in
`sha-5eb436e`, **live-verified 2026-07-28** on the motivating session — the
checkbox now renders unticked and matches the agent's real toolset.
**Severity:** medium — no data loss, but the UI asserted a capability the agent
did not have, and the honest reading ("it's already on") left the user with no
way to turn it on. Every session with a group absent from its `config_override`
was affected, which is the default.
**Component:** `cockpit/src/app/views/chat/settings-pane.component.ts`
(`liveConfig`, `desiredState`, `loadThread`);
`cockpit/src/app/views/agent-settings/tools-group.component.ts`
(`disabledToolCategoriesFromConfig`); `orchestrator/main.py`
(`_resolve_session_config`, `_session_tool_group_disabled_markers`);
`config/session_base.yaml`.
**Motivating incident:** dev session `b1758f38` ("Hotel Rheinland ERP Job
Status"), 2026-07-26. The agent refused to queue three Designer jobs, saying
"Fleet Management/job-creation tools are not currently loaded" and asking the
user to enable the group — while Settings→Tools showed Fleet Management already
ticked. **The agent was telling the truth.** Its pod bound 65 tools and not one
of the 13 `orchestrator` tools:

```
{"level":"INFO","logger":"src.api.persistent_session",
 "message":"Loaded 65 tools for persistent session"}
```

The 65 matched `session_base.yaml`'s non-empty groups exactly (workspace 14 +
research 8 + browser_direct 9 + citation 11 + git 5 + knowledge 10 + canvas 3 +
session tasks 3 + `read_product_guide` + `srw_cloud_status`).

## Root cause

Two components answered "is this group enabled?" from different data.

**Backend (authoritative).** `_resolve_session_config` derives the runtime
disable markers from the *fully merged* config — base + expert + project +
request override — at `orchestrator/main.py:1625`. `config/session_base.yaml`
ships `tools.orchestrator: []`, `agent_catalog: []` and `workflows: []`, so a
group the override never mentions still merges to `[]`, which
`_session_tool_group_disabled_markers` turns into
`_fleet_management_disabled: true`. The agent hydrates that blob
(`src/api/persistent_app.py:1618`; hydration deliberately skips the
`config_override` merge, so `_apply_session_tool_group_markers` never runs) and
`src/api/persistent_session.py:148/1249` strips the 13 tools. **Unset ⇒
disabled**, and that is intentional — `src/core/loader.py:3901` calls them "the
(default) sessions without the Fleet Management tool group".

**Cockpit (display only).** `liveConfig()` was
`deepMergeConfig(threadOverride(), live)`, where `live` carries only `llm` and
`interactive`. Both the render path (`disabledToolCategoriesFromConfig`, which
only disables on an explicit `[]`) and the diff path (`desiredState`, whose
`!(Array.isArray(current) && current.length === 0)` is `true` for a missing
key) therefore read **unset ⇒ enabled**. The client never saw
`session_base.yaml`.

The two rules are exact opposites, so the disagreement was guaranteed for any
group the user had not explicitly toggled. The motivating session's override
was `{"tools": {"workflows": [], "agent_catalog": []}}` — which is why those two
rendered correctly (explicit `[]`) while Fleet Management and Canvas, both
absent, rendered ticked.

A second-order effect: because the group's prefill baseline was "enabled", the
re-enable branch in `tools-group.component.ts:411` could never produce a delta.
Turning the group on was unreachable from the UI; only toggling it **off then
on** worked, by writing an explicit list that beat the base `[]` at merge.

## The fix

Server-authoritative, with a correct client fallback so the UI is never wrong.

1. **`src/core/session_tool_overrides.py`** — `session_tool_group_enablement()`,
   the positive reading of the disable markers, in the module both boundaries
   already share.
2. **`orchestrator/main.py`** — `GET /api/persistent/threads/{id}/tool-groups`
   (owner-gated), returning `{thread_id, source, tool_groups}`. Backed by
   `_merged_session_tool_groups`, which runs the *same* `resolve_config`
   layering as attach minus everything that provably cannot reach `tools.*`
   (the skip ledger is in the docstring and pinned by
   `test_lean_resolve_matches_full_resolve_markers`). Deliberately **not** a
   field on `GET /api/persistent/threads/{id}`: that endpoint is hot and this
   answer costs a resolve.
3. **Cockpit** — `liveConfig()` now merges a tool-group defaults layer
   *underneath* `threadOverride()`. One seam fixes render and diff together:
   `desiredState` and `tools-group.component.ts` needed no changes, and the dead
   re-enable path started working. `SESSION_TOOL_GROUP_BASE_ENABLED` mirrors
   `session_base.yaml` for when the endpoint is unavailable (older orchestrator,
   request failure), pinned by `tests/test_session_tool_group_mirror.py`.
4. **`config/session_base.yaml`** — dropped `browse_website` /
   `download_from_website`, gone from `TOOL_REGISTRY` since the direct
   `browser_*` tools replaced them. They made `load_tools` raise `ValueError`
   for the *whole* batch on every session start, dropping into the per-tool
   fallback at `persistent_session.py:1350` that swallows failures at DEBUG.

### Two traps worth remembering

**The legacy path answers the opposite.** When experts are off
(`_is_experts_db_enabled` / `_user_experts_enabled` false) the agent takes the
`config_name` + `config_override` fallback, where `persistent_session` *appends*
the canonical lists whenever no explicit `[]` marker is present — so an unset
group is **enabled** there. Canvas is asymmetric even then: its branch is
strip-only with no append. The endpoint models both and reports which via
`source`. A resolve *error* is a third state: it **refuses** the attach
(`main.py:3159-3167`, `:19040-19044`), so there is no agent answer and the
endpoint returns `tool_groups: null` rather than guessing.

**The load-order race.** `lastApplied` anchors from `desiredState({})` right
after the thread fetch. If the tool-group answer landed *after* that anchor,
`liveConfig()` would shift under a stale baseline and the next change would
dispatch a `config.update` disabling three groups the user never touched.
`loadThread` therefore `forkJoin`s both requests and sets the server answer
before prefill and before the anchor. This is the same class of trap the
original design recorded for the single-fetch case (see
`docs/done/2026-07-16-live-session-settings.md`), now widened to two requests.

## Verification

- `tests/test_session_tool_groups_endpoint.py` — 20 tests: the resolved-path
  regression, expert/project/request layering, the skip ledger, both legacy
  asymmetries, the error state, and the auth gate.
- `tests/test_session_tool_group_mirror.py` — extended to pin
  `SESSION_TOOL_GROUP_BASE_ENABLED` against `session_base.yaml`.
- `tests/test_config_tool_names_are_registered.py` — every session-base tool
  name must exist in `TOOL_REGISTRY`, pinning the class of bug behind #4.
- Cockpit: the race regression asserts no `config.update` fires while the
  baseline is unanchored, and fails if the ordering in `loadThread` is reverted.
- **Live gate, dev, 2026-07-28** (session `b1758f38`, Playwright against
  `cockpit.srw.works` with `WebSocket.send` instrumented): Canvas ticked, the
  other three unticked; header reads "Select all"; **zero** WebSocket frames
  across an open → close → reopen cycle; exactly one `/tool-groups` request. The
  deployed `_merged_session_tool_groups` returns
  `{orchestrator: false, agent_catalog: false, workflows: false, canvas: true}`
  for that thread.

## Follow-ups (not done)

- ~~`browse_website` / `download_from_website` still in `worker_base.yaml` and
  the four expert configs~~ — **FIXED 2026-07-29**, see
  `docs/done/stale_tool_names_degrade_every_worker_job_tool_load.md`. The test
  now covers every base and expert config in merged form, not just the session
  base. It also surfaced a separate defect:
  `docs/done/expert_prompts_instruct_a_removed_browser_tool.md` (also fixed,
  2026-08-08, with a lint so the class cannot recur).
- `docs/issues/session_tool_group_enablement_is_computed_in_two_places.md` —
  `_session_tool_group_disabled_markers` was deliberately **not** rewritten in
  terms of `session_tool_group_enablement` (it sits on the live attach path);
  the agreement test carries the drift protection instead.
- `docs/issues/settings_pane_shows_fallback_tool_policy_without_saying_so.md` —
  `source: "error"` has no UI; the pane silently renders base defaults.
- `docs/issues/settings_pane_never_refetches_a_threads_config.md` —
  `prefilledThread` never re-fetches, so an out-of-band policy change shows
  stale until the session is switched. Pre-existing for `threadOverride`.
- `docs/tests/session_tool_groups_legacy_and_error_paths_verification.md` —
  runbook for the two `source` values that were unit-tested but never live-run;
  needs an isolated namespace, since the kill switches are deployment-wide.
