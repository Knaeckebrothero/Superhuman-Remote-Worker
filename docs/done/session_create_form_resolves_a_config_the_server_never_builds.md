---
tags:
  - issue
  - cockpit
  - orchestrator
  - config-resolution
  - sessions
related:
  - "[[session_tool_group_checkbox_disagrees_with_the_agent]]"
  - "[[session_tool_group_enablement_is_computed_in_two_places]]"
  - "[[no_workspace_agent_mode]]"
  - "[[session_permission_mode_grant_denied_ready_timeout]]"
---

# The New Session form resolves a config the server will never build, so every create 400s on the lite-backend rule

**Status:** ✅ RESOLVED 2026-07-29 — commit `6b727734` on `develop`, k3d-verified
end to end (pending push + deploy; the dev cluster stays broken until then).

**Reported:** every attempt to create a session from the New Session form on the
dev cluster failed with `The request couldn't be completed.` — including with
the model selector left alone, which is what ruled out the model as a cause.

**Severity:** high — the form was unusable on dev for any configuration. The
landing draft chat (`/`) was unaffected and remained the only working path.

**Component:** `orchestrator/main.py` (`_load_expert_detail`, `get_expert`,
`_resolve_session_account_defaults`, `create_thread`),
`cockpit/src/app/views/session-create`, `cockpit/src/app/views/agent-settings`,
`cockpit/src/app/core/services/error-message.service.ts`.

## Summary

The server rejected the request with a precise, correct message:

> Lite session backends cannot attach clone-based repository connectors; OKF
> Knowledge Base connectors remain available

The client and the server disagreed about the session's effective workspace
tier, and three independent defects conspired to make that disagreement both
unavoidable and illegible.

### 1. The form resolved a different config than `create_thread` would

`GET /api/experts/{id}` returned the expert fragment merged onto the framework
base *only*. The form therefore read `workspace.backend` as `session_base.yaml`'s
`sandbox`.

`create_thread` layers `_resolve_session_account_defaults` above that base and
below the expert — the `base_defaults` slot in `resolve_config` — and that layer
sets `workspace.backend` **unconditionally**, defaulting to
`SESSION_DEFAULT_WORKSPACE_BACKEND = "virtual"` when the owner has no
`settings.persistent_agent.workspace_backend`.

So the form believed `sandbox` (not lite) while the server resolved `virtual`
(lite). The consequences cascade:

- `datasources-group.component.ts` drops clone-based repository connectors from
  the submitted set only when `isLiteBackend`. Believing it was on `sandbox`, it
  dropped nothing.
- The picker preselects **all** eligible connectors when `initialSelectedIds` is
  null, which create mode always passes.
- All three datasources on dev are type `repository`.
- `create_thread` materializes the resolved backend into `config_override`
  *before* calling `_authorize_thread_datasource_ids`, so the lite check ran
  against `virtual` and refused.

No form submission could succeed.

**Why it surfaced when it did, not months earlier.** Every previously successful
form-created session with datasources had explicitly picked `vm` or `sandbox` in
Advanced → Backend — visible in `threads.metadata.config_override.workspace.backend`.
Leaving the tier selector untouched is what exposes the mismatch, and the form
*displayed* "Container" the whole time.

### 2. The displayed default could not be chosen

Every settings control renders `override() ?? resolved()`, so the value on screen
may be one the user picked or one merely inherited — indistinguishable to them.
Two things made the displayed value unpinnable:

- A native `<select>` fires no `change` event when the option already showing is
  re-picked. With the form displaying "Container" there was literally no way to
  pin it.
- Fourteen handlers in `execution-group` and `model-group` went further and
  *collapsed a chosen value back to inherit* whenever it equalled the resolved
  one (`value === this.resolvedX() ? null : value`).

Combined with defect 1, the user saw "Container", could not select it, and
silently got `virtual`.

### 3. A rejected config destroyed the form, and said nothing useful

Both session create paths (`session-create.component.ts`,
`sessions-page.component.ts`) handed their body to a `/sessions/_creating` route
and unmounted. `chat-page` created the thread from `history.state.createBody`,
and on failure toasted and navigated to `/sessions`. Every selection was gone —
nothing was left to correct, which is the entire point of a validation error.

And the toast was useless anyway: `ErrorMessageService` resolved
`errors.http.<status>` *before* `error.detail`, and `errors.http.4xx` is defined
for every 4xx. The generic "The request couldn't be completed." always won and
the server's explanation sat unreachable one step below it.

## Resolution

**Serve the account layer to the read path** rather than teach the client a
second copy of the layering. Patching the client's `?? 'sandbox'` fallback to
`'virtual'` would have fixed today's symptom and relocated the disagreement to
anyone who sets a non-default account preference.

- `_account_defaults_layer(user_id, expert_type)` returns exactly what
  create/dispatch pass as `base_defaults`: the full session layer for sessions,
  the default-model floor for workers.
- `_load_expert_detail(include_account_defaults=)` inserts it between the
  framework base and the expert fragment, in all three branches (DB expert,
  virtual `defaults`, bundled expert).
- Exposed as `GET /api/experts/{id}?account_defaults=true`, **off by default**.
  The expert editor must keep diffing against the pure framework baseline —
  folding a personal preference into that baseline would let it be saved into a
  shared expert. Both create forms opt in; `project-detail` deliberately does
  not, since its `worker_base` fetch backs a project-scoped fallback.

The worker/session asymmetry is load-bearing and pinned by tests: sessions must
report `virtual`, workers must keep `sandbox`, because job dispatch has no
account workspace layer. Feeding sessions' answer to the job form would make it
lie in the opposite direction.

**Choosing is now pinning.** The fourteen collapse-to-inherit handlers set the
chosen value unconditionally, and `PinOnInteractDirective` commits the displayed
value on `mousedown`/`keydown` — deliberate interaction being the intent signal
the browser withholds. It is a no-op once an override exists, so it never
overwrites a real choice. Only the reset control clears back to inherit. The
model pickers already had an explicit `[ngValue]="null"` "Default · <model>"
sentinel and were correct by construction; they were left alone.

**Create before navigating.** Both session paths POST first and route to the real
thread id only on success, rendering the server's reason in place with every
selection intact. The `_creating` bridge in `chat-page` is retained for history
entries and service-worker-cached bundles predating the change. `api.createJob`
no longer swallows failures into `null`, which had made the job form's `error:`
branch dead code and reduced every rejection to "Failed to create job."

**Structured details win for refusals.** `ErrorMessageService` now prefers a
JSON-body `detail` over the generic per-status line for every 4xx **and 503** —
the status this API uses for deliberate "this deployment can't do that" answers
(readiness gate, "VM provisioning is not available on this deployment"). 500
keeps the generic line, because `create_thread`'s catch-all raises
`HTTPException(500, detail=str(e))`, i.e. raw internal exception text.
Unstructured (string) bodies are ignored so a gateway's HTML error page is never
rendered as the message.

## Verification

Reproduced against the dev orchestrator by curling `srw-orchestrator:8085` from
the `srw-mcp` pod with `X-MCP-User-Id` / `X-MCP-Scope` / `X-Internal-Key`:
minimal body 200, `+datasource_ids=[repository]` 400,
`+config_override.workspace.backend=sandbox` 200. Model and `expert_id` were
ruled out the same way.

k3d, after seeding a temporary repository connector to reproduce dev's data
shape (removed afterwards):

- `experts/session_base?type=session` → `sandbox`; with `&account_defaults=true`
  → `virtual`.
- `experts/worker_base` → `sandbox` either way; its `llm.model` resolves from the
  base placeholder to the account default with the flag.
- UI: the tier reads **Virtual**; the repository connector greys out with
  "Requires a sandbox or VM workspace"; interacting with the tier select marks
  the row modified and reveals the reset button; choosing **Container**
  re-enables the connector; creating persists
  `config_override.workspace.backend=sandbox` with the connector attached.
- The VM tier's 503 rendered as "VM provisioning is not available on this
  deployment" with the form and its input intact.

That last check is what caught the fix's first cut being wrong: the detail
preference was originally scoped to 4xx only, so the VM refusal displayed
"The server encountered an error." The unit tests were green on the wrong rule —
only running it on the cluster exposed it.

Tests: `tests/test_expert_defaults.py::TestAccountDefaultsLayer` (6 cases,
covering the session/worker asymmetry, expert-beats-account precedence, a saved
tier preference beating the platform default, opt-out, and the anonymous
caller), plus cockpit specs for the query param, the pin behaviour, the
create-then-navigate flow, and the error resolution order.

## Residual

- **Not pushed.** Dev remains broken until this is deployed.
- Interacting with any settings dropdown now writes that value into
  `config_override`, so "modified" indicators appear more eagerly than before.
  Intended — it is what makes the displayed default expressible — but it is a
  visible behaviour change across the whole Advanced pane.
- A preflight/dry-run endpoint was considered for pre-submit validation and
  **rejected**: it costs a second round trip for something a recoverable inline
  error already solves. Resolution stays server-side; see the reasoning under
  "Resolution" for why the client must not become a second resolver (MCP,
  the draft path, job dispatch and session re-attach all resolve with no
  frontend in the loop).
