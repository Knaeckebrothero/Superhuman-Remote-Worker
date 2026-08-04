---
tags:
  - test
  - tools
  - experts
  - live-gate
related:
  - "[[tool_configuration_defects_and_fix_roadmap]]"
  - "[[tool_configuration_deferred_findings]]"
  - "[[tool_config_policy_vs_membership]]"
  - "[[catalog_authoring_live_gate_2026-08-03]]"
---

# Live gate — the expert editor's resolved toolset read (`68ac7bde`)

Run 2026-08-04 against **k3d** (`k3d-srw` / namespace `srw`), as the real
non-admin user `catalog-gate@k3d.local` (`230ce19a`) over the MCP internal-header
auth path, because `_enforce_save_grants` and the grant gate return early for
admins and gating as an admin would exercise the permissive path.

The specific risk driving this gate: the editor sends
`config_name: "worker_base"`, and **job create deliberately does not** — it
excludes `worker_base` from the id it forwards and sends `config_name: null`
(`job-create.component.ts:1328`). So the editor's payload shape had never been
put to the endpoint.

## Server-side checks

| # | Check | Result |
|---|---|---|
| 1 | Create path, `config_name: "worker_base"`, no fragment | **200** · 26 categories · 8 on / 8 off / 10 unavailable · **64 tools** |
| 1b | Same for `session_base` | **200** · 26 categories · 7 on / 9 off / 10 unavailable · **60 tools** |
| 2 | Is the explicit base name equivalent to the endpoint's own default? | **Yes — `categories` byte-identical** to `config_name: null`. The editor's spelling is not a second code path |
| 3 | Does the base actually change the answer? | **Yes.** Same 26-category *set*, different states and contents: 64 vs 60 tools, 8 vs 7 on. Live confirmation that the BASE is the mechanism, not `expert_type` |
| 4 | Does the fragment layer take effect? (`{tools: {browser_direct: false}}`) | **Yes.** `browser_direct` `on → off`, 64 → 55 tools. `base ⊕ fragment` is real, not decorative |
| 5 | Grant gating for this non-admin | `shell` and `delegation` → `unavailable`, `settable=false`, reason *"requires the … capability grant"*. `catalog_authoring` → settable (this user holds it, granted during the 08-03 gate), `off` because `session_base` declares it closed |
| 6 | Malformed fragment (`tools.research: "not-a-policy"`) — the editor has a raw JSON textarea | **422**, *"This configuration cannot be resolved, so its toolset cannot be predicted."* No 500. `previewToolGroups` maps it to `null`, so the pane shows the labelled fallback |

Check 4 is the one that mattered. If a fragment could not move the answer, the
whole `base ⊕ fragment` design would be decoration and the editor would be
showing the base while claiming to describe the expert.

## `browser_direct` is on for a user with no `browser` grant — and that is correct

Worth writing down, because it looks like a gating hole and is not.
`browser` is `{"default": True, "restrict_only": True}`
(`src/core/capability_grants.py:50`), so **absence of a row means granted**, and
the PDP rule reads `if not grants.get("browser", True)`. Unlike
`catalog_authoring` / `shell_tools` / `delegation`, which default `False`.

Cross-checked that the client agrees rather than assuming it: `GET
/api/users/me/capabilities` for this user returns all 12 keys with defaults
filled — `browser=True`, `shell_tools=False`, `delegation=False`,
`catalog_authoring=True`, `datasource_tools=True`. So the editor's
`isCategoryBlocked` (`g[grantKey] !== true`) greys exactly what the server
refuses, and nothing more.

**This downgrades a known gap.** `CAT_TO_GRANT`'s missing seven
`datasource_tools` categories ([[agent_authored_catalog_entries]], coverage note)
only mislead a user who is *explicitly restricted*, because the grant defaults
`True` and the served record says so. Still worth completing; less urgent than
recorded.

## The change is live in the k3d cockpit

`live_update` in the Tiltfile syncs `cockpit/src/` into the running `ng serve`
container, so there is no image rebuild and the pod age does not move — which
looks like a stale deploy and is not. Verified directly rather than inferred:
`/app/src/app/views/experts/expert-editor.component.ts` inside
`srw-cockpit-7747986b9-f7wv5` carries `[resolved]="toolPreview()"` at :328 and
three references to `expertToolPreviewRequest`, and the pod log shows
*"Application bundle generation complete"* at 11:25:42Z with no errors.

## Not covered

- **The rendered pane.** No visual confirmation that the answer paints into the
  rows. The Chrome extension was not connected, and a Playwright drive stops at
  the Keycloak login — entering credentials is not something this harness does.
  What stands in for it: `ng build` succeeds (the template compiler is the only
  thing that resolves an Angular binding — see below), `tools-group.render.spec.ts`
  covers rendering *given* an answer, and the wiring is the same three bindings
  two shipped surfaces already use. **The one-step manual check:** open an expert,
  confirm the Tools card shows a prediction banner and ~26 rows rather than the
  static 12, and that Shell reads *"requires the shell_tools capability grant"*.
- **A worker→session type switch on the create form** re-reading the base. The
  code path is `onTypeChange`; the server behaviour it depends on is check 3.

## Harness note for whoever re-runs this

`tsc --noEmit` and all 1661 cockpit tests passed against
`[resolvedToolset]="toolPreview()"` on `app-tools-group`, whose input is named
`resolved` — `AgentSettingsComponent` takes the wrapper name and forwards the
inner one. **Only `ng build` caught it.** An Angular template binding is resolved
by the template compiler and by nothing else, so for any cockpit change "tests
pass" is not evidence the template binds. The guard spec now pins the per-host
input names by hand and says why.
