---
tags:
  - test
  - verification
  - sessions
  - orchestrator
related:
  - "[[session_tool_group_checkbox_disagrees_with_the_agent]]"
---

# Session tool-groups — legacy and error path live verification

**Status: NOT RUN.** The `resolved` path was live-gated on dev 2026-07-28
(session `b1758f38`: correct checkboxes, zero spurious `config.update`, one
endpoint call). The other two `source` values are covered by
`tests/test_session_tool_groups_endpoint.py` but have **never executed against a
real cluster**. Design:
`docs/done/session_tool_group_checkbox_disagrees_with_the_agent.md`.

**Why it was skipped:** the legacy path is selected by
`_is_experts_db_enabled()` / `_user_experts_enabled()`. Both are deployment-wide
on the shared dev cluster, so flipping either would change tool availability for
every concurrently running session. This runbook therefore needs an isolated
namespace or a quiet window — it is not a "just try it on dev" check.

## Why these paths deserve a live gate at all

The legacy path returns the **opposite** answer to the resolved path for a group
the override doesn't name: `persistent_session` appends the canonical lists when
no explicit `[]` marker is present, so an unset group is ENABLED there. A wrong
answer here is the original bug in mirror image — the UI would show Fleet
Management off while the agent actually holds all 13 tools. Unit tests assert
the endpoint models this; only a live run proves the endpoint and the agent
still agree in a real deployment.

## Preconditions

- An isolated namespace (or a dev window with no other active sessions).
- A session whose `config_override` omits at least one tool group, plus one that
  explicitly pins a group to `[]`, so both branches are exercised.
- `kubectl --context main -n <ns>`; browser access to the cockpit for that env.

## Cases

### 1. Legacy path — unset group reads ENABLED

1. Set `EXPERTS_DB_ENABLED=false` on the orchestrator deployment (or set the
   `user_experts` system setting false), and wait for rollout.
2. `GET /api/persistent/threads/{id}/tool-groups` ⇒ `source: "legacy"` and all
   four groups `true` for a stock session.
3. Open Settings→Tools: all four render **ticked**.
4. Send a message so the agent re-derives its toolset, then confirm from the
   agent pod that the 13 `orchestrator` tools are actually bound:
   ```
   kubectl -n <ns> logs srw-agent-j-XXXX -c agent | grep "Loaded .* tools"
   ```
   The endpoint's booleans and the bound toolset must match. **This is the
   assertion that matters** — everything above is setup.

### 2. Legacy path — canvas asymmetry

`canvas` is strip-only on the legacy path (no append), so it follows the base
YAML rather than defaulting on. With a thread whose `config_name` resolves to a
base that declares no canvas group (e.g. `worker_base`), expect
`canvas: false` while `orchestrator: true`.

### 3. Legacy path — explicit `[]` still disables

A thread whose `config_override` pins `tools.workflows: []` must report
`workflows: false` while the unset groups stay `true`.

### 4. Error path — no guess

Force the resolve to fail (e.g. point the expert lookup at a broken DB, or
temporarily raise inside `_merged_session_tool_groups`). Expect:

- `source: "error"`, `tool_groups: null`, HTTP 200 (not a 5xx — the endpoint is
  not a second failure surface);
- the pane falls back to `SESSION_TOOL_GROUP_BASE_ENABLED` and still renders;
- no exception escapes to the client.

Note this state means the session would be **refused at attach**
(`main.py:3159-3167`, `:19040-19044`), so a session that reports `error` here is
not a session that runs with defaults — it is one that cannot start.

### 5. Restore

Revert `EXPERTS_DB_ENABLED` / `user_experts`, confirm a stock session returns to
`source: "resolved"` with `{orchestrator: false, agent_catalog: false,
workflows: false, canvas: true}`.

## Recording the result

Update the Status block above, and the Verification section of
`docs/done/session_tool_group_checkbox_disagrees_with_the_agent.md`.
