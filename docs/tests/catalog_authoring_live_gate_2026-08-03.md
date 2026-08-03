---
tags:
  - test
  - live-gate
  - tools
  - grants
related:
  - "[[agent_authored_catalog_entries]]"
  - "[[tool_configuration_live_gates_2026-08-03]]"
---

# Live gate — agent-authored catalogue entries (`catalog_authoring`)

**Cluster:** local k3d, context `k3d-srw`, namespace `srw`. Tilt had already
rolled the orchestrator with the change (pod `srw-orchestrator-5769d854b5-qxznr`).
Confirmed in-pod before testing anything: the grant present in `CATALOG`, 25
registry categories, and `catalog_authoring` holding exactly the six tools.

**Run as a NON-admin, which is the whole point.** `_enforce_save_grants` returns
early for admins (`orchestrator/main.py:5043`), so gating this as the admin
account would have exercised the permissive path and proved nothing. Created
`catalog-gate@k3d.local` (`230ce19a-…`, `is_admin=false`, `is_approved=true`) on
the throwaway cluster for the purpose; the account and its grant row are left in
place so the gate can be re-run.

## Results — 8 checks, all passing

| # | Check | Result |
|---|---|---|
| 1 | Non-admin, no grant: PDP verdict via the **real** DB grant resolution (`resolve_grants_for`), not a mock | `catalog_authoring` resolves `False`; violation raised naming the grant |
| 1b | Same, at the HTTP boundary — `POST /api/experts` with `tools.catalog_authoring: true` | **422** `config exceeds your capability grants: catalog_authoring: tools.catalog_authoring requires the catalog_authoring grant` |
| 2a | Grant held: same create | **200**, `owner_id` = the test user |
| 2a′ | What actually got stored | `true` normalised at the boundary to the canonical six names — the T5 authoring vocabulary resolving to `list[str]`, verified in the row rather than assumed |
| 2b | The six survive `resolve_config` + `ToolsConfig` construction | all six present in `capture["merged_fragment"].tools.catalog_authoring`; 21 tool categories merged |
| 2c | `load_tools` actually instantiates them | **6 of 6 bound**, and both factories reached — `create_catalog_tools` for the expert/skill bundles, `create_workflow_tools` for the automation bundles |
| 3 | Agent-created automation lands switched off | **201**, `enabled=False`, `next_run_at=None`, `owner_id` = the test user |
| 4 | Ownership boundary: non-owner updates an admin-owned expert | **403** `Only the owner may edit this expert` |
| 5 | The old spelling at the real HTTP boundary — `tools.agent_catalog: [set_expert_bundle]` | **400**, and the message names the new home: *"'set_expert_bundle' is in tools.catalog_authoring. A tool list may only name tools of its own category…"* |

Check 2c is the one no unit test could produce: `load_tools` dispatches by
category to a factory, and `catalog_authoring` is the only category whose members
come from two different factories. A mocked `load_tools` — which is what the
`persistent_session` specs use — validates nothing about that branch.

Check 5 closes the half that the 2026-08-03 tool-configuration gates could not:
gate A.3 there proved a category-level `true` could not reach the writes but said
nothing about an explicit name. It is now refused by registry-membership
validation, with no special case, because the name is genuinely foreign to that
category.

## Harness notes for whoever re-runs this

Three dead ends cost time and are worth writing down:

- `resolve_config` returns a **lean** structure (`agent`, `instructions`,
  `model_family`, `prompts`, `resolved_at`). The merged config is in
  `capture["merged_fragment"]`. Reading `tools` off the return value yields `{}`
  and looks exactly like "the feature does not work" — it was my harness, twice.
- Inside the orchestrator pod the import root is `services.…`, not
  `orchestrator.services.…`.
- `load_agent_config_from_dict` requires `agent_id` and `display_name`; the
  merged fragment alone will not load.

## Not covered

- **The agent actually calling the tool in a conversation.** These gates prove
  the tools bind and that the endpoints they call are correctly scoped; they do
  not prove an LLM drives `set_expert_bundle(dry_run=false)` end to end. The
  worthwhile follow-up is one session on dev: grant it, ask for an expert, read
  back the row.
- **The cockpit rendering against a live session.** Covered by mounted tests
  (both mutation-verified) and a clean production build, not by a browser drive.
