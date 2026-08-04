---
tags:
  - issue
  - follow-up
  - orchestrator
  - cockpit
  - config-resolution
  - tooling
related:
  - "[[tool_configuration_defects_and_fix_roadmap]]"
  - "[[tool_configuration_live_gates_2026-08-03]]"
  - "[[tool_config_policy_vs_membership]]"
  - "[[registered_tools_no_config_can_grant]]"
  - "[[session_create_tool_toggles_cannot_enable_a_group]]"
  - "[[session_uploads_never_extract_archives]]"
  - "[[resume_job_grant_recheck_fails_open]]"
  - "[[job_mode_reasoning_pick_silently_reset]]"
  - "[[duplicate_expert_bypasses_user_experts_kill_switch]]"
---

# Tool configuration — findings carried out of the implementation run

**Filed:** 2026-08-03.
**Why this exists:** the tool-configuration work shipped as eight tasks on
`develop` (roughly `1664f2d0..d1cb216d`, **not pushed**), each one
implement → adversarial review → fix → scoped re-review, then a whole-branch
review, a three-blocker fix wave, and three rounds of live gates. Along the way
about forty findings were triaged as *minor, deferred* and eight rulings were
settled at real cost. All of it lived only in a **git-ignored** scratch ledger
that the process deletes on completion. This document is that content, made
durable.

Register: [[tool_configuration_defects_and_fix_roadmap]].
Gate evidence: [[tool_configuration_live_gates_2026-08-03]].

**None of the items below is a merge blocker.** The four blockers the whole-branch
review raised are closed — three by the fix wave, the fourth by the gates. What
remains here is one security ticket (filed separately, pre-existing), a handful of
wrong facts baked into code, and a long tail of coverage and polish.

**Read section 1 before shipping this branch anywhere with hand-authored
experts.** It contains the one genuinely owed action, and it is a read-only query.

---

## 1. Owed before this reaches a deployment with real data

**1.1 Run the `jsonb_typeof` scan on any target database.** Task 5's normaliser
turned malformed `tools` values into a hard failure where they were previously
ignored in silence: `core: null`, `core: "str"`, `core: 5` and `core: {}` all
resolved to `[]` before and now raise `ToolPolicyError`. The identity proof
covers *code* — every `tools` write site in `src/` and `orchestrator/` writes
lists — and it cannot cover *stored* data.

Measured so far: k3d has **0** non-array values, but also **0** `tools` keys of
any kind across 41 rows with non-empty fragments, so it is non-vacuous and
uninformative. Remote dev is measured for the `experts` table only, via
`GET /api/experts/{id}/export`, which returns the **raw unmerged** fragment
(unlike `get_expert`, whose `_format_expert_detail` renders the merged view where
`deep_merge` has already erased a stored `null` — the likeliest bad shape is
structurally invisible there): 11 experts, 87 raw declarations, **0 non-list**.
But only 2 of the 11 are DB rows and neither carries a `tools` key at all, so
remote dev has **zero operator-authored tool declarations**. Two bounds, both
real: the enumeration is *visibility*-scoped rather than table-scoped and the
token's admin status could not be confirmed, so a privately-owned expert of
another user may be excluded; and **production is unmeasured**.

A non-zero non-array count is a row that will now fail resolution where it
previously degraded silently. See section 6 for what "fail" means per call site.

**1.2 `project_experts.config_override` cannot be measured at all.** No route
exposes it — `/api/projects/{id}/experts` reads the project's *Gitea repo*
`experts/` directory, not the link table (verified, not assumed) — and
`query_table`'s allowlist excludes both `experts` and `project_experts`. Adding
those two tables to the allowlist, read-only, is the fleet-tooling fix; its
absence is the whole reason 1.1 needed a bespoke instrument.

**1.3 Three issue docs, filed 2026-08-03 out of this run:**

- [[resume_job_grant_recheck_fails_open]] — pre-existing, security-shaped, and
  made reachable from stored data by this work.
- [[job_mode_reasoning_pick_silently_reset]] — the probable job-mode twin of the
  session defect fixed in this series.
- [[duplicate_expert_bypasses_user_experts_kill_switch]] — the one expert write
  route that still skips the combined save gate.

---

## 2. Wrong facts baked into code and comments

These are the highest-value items in the list, because a comment is what the next
reader trusts.

1. **The memory arithmetic in the shipped upload comment is wrong**
   (`orchestrator/services/thread_uploads.py`, the cap rationale block above
   `MAX_ZIP_ENTRY_UNCOMPRESSED_BYTES`). It reasons "~6GB decompressed plus ~2GB
   still-referenced compressed payloads, ~24GB with
   `MAX_CONCURRENT_VIRTUAL_UPLOADS=4`". 6 + 2 = 8, and 8 × 4 = **32GB**, not 24.
   The caps it justifies are correct; the number is not. Deferred as cosmetic at
   the time and never swept up.
2. **`request_workspace_upgrade`'s `gate:` string is incomplete**
   (`src/tools/registry.py`). It names the runtime injection but not that the
   tool is *also* gated on `fleet_management_enabled` and stripped when fleet
   management is off. A reader takes the gate string as the complete condition.
3. **A stale docstring names a symbol this series deleted.**
   `tests/test_config_tool_names_are_registered.py:85-90` tells the reader that
   `SESSION_TOOL_GROUP_BASE_ENABLED` "has to move with" the base. Task 8 deleted
   that cockpit mirror; it now exists only inside
   `tests/test_session_tool_group_mirror.py::RETIRED_MIRRORS` as a
   must-not-reappear guard.
4. **`gate:` now carries two shapes** — a terse fact on most entries, a
   multi-sentence rationale with commit SHAs on `get_stuck_jobs` and
   `steer_worker_job`. That is a softer version of the field overload this design
   set out to remove. The long strings are load-bearing (see section 9.6), so the
   fix is a second field, not a truncation.
5. **A test comment mis-attributes its own precedent.** The
   `Object.defineProperty` note in the model-group spec cites an external
   precedent; the trick is already used ~line 316 of the same file.

## 3. Dead code and unused exports

1. **`_validated_session_fleet_tools_override`** (`orchestrator/main.py:4050`) is
   dead in production — Task 7 replaced every caller with the generic validator.
   Only `tests/test_session_config_plumbing.py` still exercises it, which is what
   keeps it looking alive.
2. **`MEASURED_ORIGINS`** (`src/core/tool_report.py:91`) is exported and tested
   and has **zero production call sites**. It documents the right check
   (`origin in MEASURED_ORIGINS`, not `origin == "agent"`) but nothing performs
   it, so the trap it warns about is still open in any new consumer.
3. **`CONFIG_LAYERS`** (`src/core/tool_report.py:104`) is exported and unused.

## 4. Test-coverage holes

Each is a missing pin, not a defect. The first is the most serious of the three
handoff items this series dropped.

1. **Any endpoint test using a bare `AsyncMock` db gets a free admin bypass.**
   `await db.get_user()` returns an `AsyncMock`, `.get("is_admin")` is truthy,
   `_resolve_runner_grants` returns `None` = bypass. **All 20 tests in
   `tests/test_session_tool_groups_endpoint.py` silently exercise the admin path**,
   so their grant enforcement is untested. Round 2's live gate covers the
   behaviour for `shell` specifically, which is why this stayed deferred — but the
   pattern will bite the next endpoint test written this way.
2. **Two session write call sites are pinned only at the endpoint, and only just.**
   Before Task 7's fix round, mutations re-narrowing `create_thread` and
   `_apply_thread_config_update` — restoring the primary defect verbatim — passed
   605 and 573 tests with **zero** failures. Endpoint-level tests now exist for
   both; six of the nine mutation kills are single-test, which is thin even though
   each failing assertion is substantive.
3. **No test pins "config must not narrow a non-empty measurement."** A
   narrowing fix-up inside `compose_tool_view` would fail zero tests.
4. ~~**The grant-map test parametrises over `GRANT_GATED_CATEGORIES`, not the
   catalog**, so a category added to the catalog without a grant mapping is not
   caught.~~ **CLOSED 2026-08-03** by
   `test_no_gate_the_pdp_enforces_is_missing_from_the_map`, which walks every bool
   grant in `CATALOG`, asks the real PDP whether it denies any category, and
   requires a map entry *and* a reason string when it does. Mutation-tested by
   deleting the new entry. Predicted correctly here: adding `catalog_authoring`
   hit exactly this hole, and the omission was invisible until the inverse
   assertion existed. Note the consequence this item understates — the denial
   still happens, so the failure is a *missing explanation*: the pane shows a bare
   "off" where it should name the grant. The client-side twin `CAT_TO_GRANT`
   (cockpit) has the same shape of hole and is **still open**: the seven
   `datasource_tools` categories have never been listed there.
5. **A schema test proves branch *types*, not acceptance.**
   `test_each_schema_block_accepts_the_five_forms` inspects the `oneOf` branches
   rather than validating the five forms through a validator.
6. **The legacy-shim test asserts against the vocabulary, not the runtime
   literals its docstring names** — a hand transcription of exactly the kind this
   design exists to kill.
7. **`defaultsTools` is the one retired cockpit symbol with no reappearance
   guard** in `RETIRED_MIRRORS`, i.e. the parallel-list species could grow that
   head back silently.
8. **One vacuous assertion.** "renders nothing when there is no reset to report"
   is trivially true — with no model picked the whole field-row is absent, so it
   never proves the notice is correctly absent *while the field shows*.

## 5. Latent, currently unreachable

1. **`expand_category_true` does not filter `placeholder: True` entries.** No
   category currently mixes placeholders with real tools, so `true` cannot expand
   to a placeholder today. Registering one changes that with no test failing.
2. **The legacy `coding` alias is honoured by the loader and rejected by the new
   `additionalProperties: false`.** A config still spelling `tools.coding` loads
   through one path and fails schema validation on the other.
3. **`_classify_code_granted_categories` mutates shared module-level dicts in
   place** (`src/tools/registry.py:201`, called at import). Import-order
   dependent; harmless today because nothing imports the registry twice with
   different expectations.
4. **`config/schema.json`'s `tools.required: ["workspace","core"]` is wrong for
   `$extends` leaves** — pre-existing, not introduced here.
   `config/experts/centurion/config.yaml` fails it today and still does.
5. **Ordering will shift for any config migrated to a category-level form.**
   `true` and `except` emit **sorted** lists; an explicit list keeps author order.
   Tool-menu ordering is a prompt-surface fact, so a migration is not
   byte-neutral even when the *set* is identical.
6. **A capability grant keyed on a category can be bypassed by naming its tools
   under a different category, because the PDP reads keys and `load_tools`
   resolves names.** `capability_grants.evaluate` checks
   `tools.get("catalog_authoring")`, but `load_tools` groups by each name's
   *registry* category — so a stored fragment spelling
   `tools.agent_catalog: [set_expert_bundle]` would bind the write while the
   grant check never fires. Introduced 2026-08-03 by
   [[agent_authored_catalog_entries]]; the same shape applies to any future
   category-keyed grant, which is why it belongs here rather than in that doc.

   **Unreachable through any write boundary** — `validate_tool_override_fragment`
   rejects the foreign name with a 400 (live-verified), so the only way in is a
   row written before 08-03 or by a path that skips validation.

   **Exposed population measured on dev, 2026-08-03: zero.** All 11 experts
   surveyed — 9 bundled (in-repo, and a full-history grep of `config/` for the
   six names returns no commits) plus both DB rows, `Assistant` and
   `General Worker`, neither of which declares `agent_catalog` or `workflows` at
   all. Still unmeasured: prod's database, and project-scoped experts stored in
   a project's Gitea repo — the same instrument gap
   [[registered_tools_no_config_can_grant]] records.

   Fold this into the `jsonb_typeof` scan owed in §1: while reading stored
   `tools` fragments, also grep them for the six `*_bundle` names under any key
   other than `catalog_authoring`. The durable fix, if a row ever turns up, is to
   make the PDP evaluate the *resolved* toolset by registry category rather than
   the fragment's keys.
6. **The 100-entry upload cap is shared with SFTP**, which has no per-write
   subprocess cost — the cap was sized against `rclone rcat`'s one-subprocess-per-key
   behaviour. Deliberately "one number, not two"; worth revisiting if the SFTP
   path ever becomes the common one.
7. **`_ensure_remote_dir` is un-memoised** (`src/core/backends/remote.py:716`), so
   a large extraction issues on the order of 10k redundant SFTP stats.
8. **Duplicate or normalising archive entry names are last-write-wins.** Two zip
   members differing only by a path form that normalises to the same target
   silently collapse to one file.

## 6. Narrow robustness and error reporting

1. **`ToolPolicyError` does not degrade uniformly, and the map is worth keeping.**
   Re-read from source per call site after the first version of this table got 4
   of 8 rows wrong, every error understating risk: **3 of 8 hard-fail** (session
   create, `agent_create_thread`, expert-default-set — all 500), **3 degrade
   silently** (`_dispatch_job_to_agent` logs and continues with
   `resolved_config=None`; `/tool-groups` returns `source: "error"`; attach falls
   back to `config_name` and loses the expert), **1 fails soft to HTTP 200**
   (`_resume_job_on_agent` returns `False`, which `resume_job` reads as
   "queue for auto-dispatch"), and **1 fails open** — the subject of
   [[resume_job_grant_recheck_fails_open]].
2. **`normalize_tool_policy` sits inside a `try` that loses the filename.**
   `orchestrator/services/config_resolver.py:110` — on raise, `bundled_leaf` is
   left un-normalised and the error does not say which config file was bad.
3. **`set_application_expert_default` reaches the global exception handler**, so a
   `ToolPolicyError` message is swallowed entirely rather than shown to the
   caller.
4. **`payload.get("attached")` sits outside the JSON `try/except`** on the
   toolset-measurement path, so a malformed payload can 500 instead of degrading.
5. **`source: "error"` plus a live measurement fabricates a disagreement.** When
   the config half errors and the agent half succeeds, the composed view reports a
   config-vs-agent conflict that does not exist.
6. **`reason` is populated on `state: "on"` rows**, which the contract describes
   as the unavailable-only field. Harmless, and the locked-on note now depends on
   it — so this is a contract-wording item, not a code one.
7. **The category set is not fixed at 25.** Unclassified categories are unioned
   in, so the row count is data-dependent; anything asserting 25 is asserting
   today's registry.
8. **`mcp` reports `settable: true`** though it is datasource-derived and a user
   toggle cannot change it.
9. **`create_automation` validates the tools fragment before the membership
   check**, so an unauthorised connector selection is reported second.
10. **Normalising before the PDP turns one loud refusal into a silent pass.** A
    `datasource_tools`-denied `sql: true` used to 422; it now normalises to `[]`
    first and passes. Loud → silent, **not** fail-open (the category is
    code-granted, so nothing is granted either way), but the diagnostic is worse.
11. **`_legacy_session_tool_policy` coerces Task 5's `{"only": [...]}` form to
    `[]`** (`orchestrator/main.py:1987`, `list(value) if isinstance(value,
    (list, tuple)) else []`). On an experts-disabled deployment a policy-form
    declaration therefore reads as *off*. Second of the three dropped handoff
    items; unreachable while the shipped configs use lists, reachable the moment
    one migrates.
12. **The upload refusal path can peak ~400MB** against the 300MB the comment
    implies, because the sidecar is written after the budget check.

## 7. UX and product

1. **The reasoning-reset notice is suppressed exactly when the field disappears.**
   If the new family has no selectable reasoning, field and notice vanish
   together, so the one case with no visible evidence also has no notice.
2. **`pinReasoning` dismisses on `mousedown`/`keydown`**, so merely *looking* at
   the select clears the notice and pins the default.
3. **There is no submit-time echo.** A silently reset level is discovered after
   creation, not before.
4. **An N-member archive floods the cockpit attached-files hint.** Extraction is
   correct; the hint enumerates every member.
5. **Toggling "share memories" on a project can surface a tools-policy 400** with
   no apparent connection to what the user clicked, because
   `toggleProjectMemory` re-submits the whole stored `default_config_override`.
   Only reachable for a project whose stored override is already invalid.
6. **`product_help` and `session_task` read `unavailable` on the creation forms**
   while being unconditional floors on the created session. Correct per the
   prediction contract (a forecast cannot see runtime injection) and newly
   *visible* because the forms now render all 25 rows. The live pane handles this
   correctly via the locked-on path.
7. ~~**Job create has no grant gating at all**~~ — **FIXED 2026-08-04
   (`44c268d9`).** Measured 2026-08-03 rather than inferred:
   `views/create/job-create.component.ts:277` mounted
   `<app-agent-settings mode="job">` with **no `gatedCapabilities`, no
   `readsResolvedToolset` and no `resolved`** binding, so it rendered the six
   static `JOB_TOOL_CATEGORIES` rows and showed a user without `shell_tools` a
   plain tickable **Shell** box. It now reads the preview endpoint with
   `expert_type: 'worker'` and renders what the server returns.

   Two notes worth keeping from the fix. First, the server-side twin was NOT the
   `expert_type` hardcoding, despite that being the obvious suspect: measured,
   `resolve_config` returns byte-identical `tools` for `worker` and `session` on
   both bases, because that argument selects prompt leaves. What mattered was the
   base default — `session_base` vs `worker_base`. A test named for `expert_type`
   passed with it reverted, which is how that was found.

   Second, removing the `resolvedToolset` binding passed all fifteen pre-existing
   job-create tests. That absence is why the defect shipped, and both creation
   forms are now pinned to pass the read in
   `cockpit/src/app/views/agent-settings/toolset-surfaces-read-the-resolved-toolset.spec.ts`
   (moved out of `views/create` when the expert editor joined it — see 8).

8. ~~**The expert editor still shows a static toolset.**~~ **FIXED 2026-08-04
   (`68ac7bde`).** It now reads the preview endpoint and passes `resolved` +
   `readsResolvedToolset`, so all four toolset surfaces answer from one
   server-side computation. Two things from doing it are worth keeping:

   It asks **base ⊕ fragment, never `expert_id`**. On create there is no id, so
   the answer would be the bare base while the saved expert gets something else.
   On edit, layering the fragment over the stored row cannot express a key the
   author **deleted** — `tools.shell` removed in the editor still resolves from
   the row underneath, and the pane would keep showing a category the expert is
   about to lose. `expertToolPreviewRequest` is an exported pure function so that
   decision is directly testable rather than buried in a subscribe.

   And a limit of source-scan guards, found by shipping into it: the first
   version bound `[resolvedToolset]` on `app-tools-group`, whose input is named
   `resolved` (`AgentSettingsComponent` takes the former and forwards the
   latter). `tsc --noEmit` passed, all 1661 cockpit tests passed, and **only
   `ng build` rejected it.** A scan cannot tell a binding from a typo, and
   neither can the type checker: an Angular template binding is only resolved by
   the template compiler. The spec now records that and pins the per-host input
   names by hand. Generalises past this register — any cockpit change whose
   evidence is "tests pass" has not tested its templates.

   By contrast the New Session form is now largely honest, which is worth
   recording because the two are usually lumped together:
   `views/session-create/session-create.component.ts:173,180` **does** pass
   `readsResolvedToolset=true` and `gatedCapabilities`, and the preview endpoint
   applies the grant gate server-side. Verified on k3d for a non-admin: without
   the grant, `catalog_authoring` came back
   `{state: "unavailable", settable: false, reason: "requires the
   catalog_authoring capability grant", decided_by: "grant"}`, and `shell` the
   same. What that form still cannot see is runtime injection and datasource
   attachment — and it says so, carrying `origin: "prediction"` with
   `prediction_reason: "no agent exists for an unsaved session"`.
7. **The live pane's 25 rows are 7 on / 10 off / 8 unavailable** on a stock
   session — worth knowing before reading a screenshot as a defect.
8. **`rowState`'s `on` short-circuit bypasses the client-side grant belt** for a
   server-settable-and-on row. Deliberate generalisation (the write path is
   unaffected and unsettable keys are still never emitted), untested
   combination. Plus a style inconsistency between `[class.disabled]` and
   `isRowLocked`.

## 8. Cross-task findings, visible only with all eight tasks in view

1. **A narrowed *closed* group is silently widened.**
   `src/api/persistent_session.py::_load_tools_for_backend` re-appends the full
   canonical membership of the four closed groups (`orchestrator` 14,
   `agent_catalog` 5, `workflows` 7, `canvas` 3), so a config granting 1 of the 14
   yields 14 bound. Not UI-reachable — the cockpit only ever sends `true`/`false`
   for these — so it is a **config-authoring hazard**, and it makes Task 7's proof
   narrower than stated. Round 3's gate saw it directly: a `config.update` that
   asked only for shell added 30 tools, 27 of them these groups.
2. **The parallel-list species was demoted, not eliminated.** Five cockpit lists
   were deleted and one added (`AUXILIARY_TOOL_CATEGORIES`, deliberate, tested,
   cosmetic-only). The **agent-side** lists in
   `src/api/persistent_session.py:1470-1520` survive, and they are what makes
   item 1 and the whole locked-on class possible.
3. **The expert write boundary was a three-task seam failure** where each task was
   locally correct: Task 4 classified, Task 7 wired eight boundaries and did not
   count `/api/experts` as one, Task 8 then drove UI traffic through it. Fixed in
   the fix wave; the lesson is that "every write boundary" needs an enumeration
   someone owns, not a per-task list.
4. **Six of the ten `explicit` marks are provisional.** The design's preferred
   disposition for the `*_bundle` trio is its own admin-scoped category; marking
   them `explicit` was the cheap move that held the grant delta at zero.

## 9. Settled during the run — do not re-open without new evidence

Recorded because each cost real investigation and none is obvious from the code.

1. **`{"only": [...]}` must return its list AS WRITTEN**, never intersected with
   the `true` expansion. An intersection silently strips `steer_worker_job` and
   `get_stuck_jobs` from centurion. Pinned on both sides — config
   (`test_explicit_tools_stay_nameable`) and resolver
   (`TestOnlyIsNeverIntersected`, including centurion's real on-disk declaration
   round-tripping byte-identically).
2. **`shell` accepts `{only: [...]}`, a bare list, `false` and `[]`, and refuses
   `true`, `{except: [...]}` and `{except: []}`.** The rationale is
   **auto-tracking**, not the mode-alias pair: `{except: [srw_cloud_status]}`
   returns exactly what `true` returns, recomputed from the registry every
   resolution, so forbidding one while blessing the other forbade nothing. Only
   `only` fails to auto-track. Settled on the *third* attempt; both superseded
   rationales are recorded in-code so a fourth derivation is not needed.
   Consequence: `config/experts/bughunter`'s `shell` cannot migrate to any
   category-level form.
3. **`ToolsConfig` fields stay hand-written.** Runtime derivation from
   `get_categories()` is **impossible**, not merely undesirable:
   `src/tools/registry` → `spawn_subagent` → `src/core/loader`, so the import only
   runs one way (verified). Instead `get_all_tool_names`' 23-name tuple was
   *deleted* and derived from the dataclass — one of the four drifting lists
   removed outright rather than tested.
4. **`mcp: true` stays the `"*"` sentinel.** `get_tools_by_category("mcp")` is
   process-local — `register_mcp_tools` mutates the global registry per
   job/session and is never called in the orchestrator, where resolution happens
   — so a registry-derived expansion would be `[]` there and session-dependent in
   every agent. Pinned by a test that injects a fake mcp entry. `mcp {except:}`
   is refused.
5. **`only`/bare lists are deliberately not membership-checked (`except` is).** A
   membership check at the last seam would break every MCP-attached session,
   because the datasource layer writes `mcp: ["*"]` and `"*"` is in no category.
   Cross-category defence belongs at the request boundary, which is where Task 7
   put it.
6. **`get_stuck_jobs` and `steer_worker_job` are unratified drift, not a
   preserved boundary.** Both were registered 2026-07-29 (`64f51a91`) without
   touching `session_tool_overrides.py`; that vocabulary was edited the next day
   (`ef3ec62b`) for a sibling tool and never back-filled them. `get_stuck_jobs` is
   caller-scoped (`require_approved_user` + `_visibility_kwargs_for_stats`) —
   the same posture as `list_worker_jobs`, which *is* in the vocabulary — and
   `steer_worker_job`'s own docstring calls it non-destructive. The `explicit`
   marks were **kept** because they hold the grant delta at zero, and the gate
   strings rewritten to say the exclusion is behaviour-preserving rather than a
   security boundary.
7. **`product_help` and `session_task` gained `ToolsConfig` fields and this grants
   nothing new**, which is checkable: `load_tools` groups by *registry* metadata,
   so `tools.core: [read_product_guide]` already bound it. Both categories are
   wholly `grant: "code"`, so `true` → `[]`, and no shipped config declares
   either.
8. **`db_overrides` and `user_settings` are dead parameters of `resolve_config`**,
   never passed by any caller (verified by scanning every call site). So
   `config_overrides.value_json` and `users.settings` are **not** exposed to the
   normaliser. What is: `experts.config`, `project_experts.config_override`, and
   the request layer.
9. **The design doc's JSON-Schema fragment did not work as written.**
   `additionalProperties` only sees `properties` in the *same* schema object, not
   inside `oneOf` branches, so it rejected both `{only:}` and `{except:}`. The
   shipped construction hoists `properties` and reduces `oneOf` to bare
   `required`; verified with `Draft202012Validator` over 11 probes.
10. **Do not grant the `kb_*` tools in the bases.** `curator` is the only bundled
    expert overriding `tools.knowledge`, and lists replace rather than merge, so a
    base grant would reach every other expert with no prompt telling them to use
    it. `kb_export`'s omission from curator is drift (it predates the
    vault-corruption bug by four months) but was left alone — nothing in
    curator's prompt surface calls it.

## 10. Closed on the way — do not re-file

- **A code-granted category's `true` is no longer accepted-and-discarded.** At
  every *request* boundary `{"tools": {"sql": true}}` now raises — "this would
  turn nothing on… drop the key; write `false` only if you meant to assert it is
  off" (`src/core/tool_policy.py`, `_is_affirmative`, pinned by
  `tests/test_tool_override_boundary.py`). It still normalises quietly to `[]`
  with a warning at the **config** layer, which is deliberate: `false`/`[]` there
  is a harmless assertion about a category nobody manages.
- The `{tools: {...}}` grant fragment at the live-update boundary once fed the
  PDP the **raw** override; it now models attach exactly, flip last, with the
  ordering documented in-code (`orchestrator/main.py`, `grant_fragment`).
- `preview_tool_groups` had no legacy branch and hardcoded `source: "resolved"`;
  fixed in Task 6's fix round.
- The cockpit thread-switch anchor race and the missing client timeout; fixed in
  Task 6 and re-pinned in Task 8.
- The "shell self-heal" framing that presumed a UI affordance which did not
  exist: the affordance now exists (round 3).
- The k3d cluster blockers (gitea's sqlite→postgres refuse-to-start guard, the
  0070-0078 migration drift against an image pinned at 0069, an ad-hoc pod image
  not in the Deployment spec). All environment, none code; resolved before the
  gates ran. Documented remedy for the gitea half:
  `docs/operations/gitea_sqlite_to_postgres.md`.

## 11. Deliberately dropped

Recorded so nobody wonders where they went. Every one was an accuracy nit about a
*scratch report* that no longer exists: a fix report claiming 90/90 tests where
the real number was 80/80; a report citing `jobs.py:1038` for a quote that is
actually at `:1053`/`:1061`; "three call sites" where the truth was two sites and
three triggers; "five named SHARP EDGE tests" where the sixth was merely unnamed;
and a bundle-headroom figure (55.83 kB) that three later measurements superseded
— the current number is 48.64 kB, in
[[tool_configuration_live_gates_2026-08-03]]. One more was dropped because the
ledger itself had gone stale about it: the accepted-and-inverted `true` on
code-granted categories was closed in its own fix round, and is recorded in
section 10 rather than as an open item.
