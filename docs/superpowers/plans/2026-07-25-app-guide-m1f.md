# App Guide M1f — Reliable Text Guide Closure Plan

**Goal:** Close M1 of `docs/features/app_guide_skill.md`: every user-facing
persistent session tier can retrieve the same current, immutable App Guide;
operators have an explicit emergency disable with a visible degraded signal;
the remaining core references are current; and automated plus live evidence
covers routing, compaction, honest gaps, fresh sessions, and resumed sessions.

**Architecture:** Keep the existing three-layer managed skill. M1f does not add
the Phase 2 runtime capability plane, source-repository fallback, screenshots,
or help cards. It finishes the static text-guide contract, adds a narrowly
named negative break-glass control, and validates the actual persistent-session
delivery path. Detailed content stays one hop below the compact `SKILL.md`
router.

**Feature design:** `docs/features/app_guide_skill.md`

## Status

Final verification in progress. M1a–M1e are shipped and work packages 1–3 are
implemented. A first complete live-model run and all six fresh/resumed k3d
delivery cells ran 2026-07-25; their failures and observations drove a final
routing/prerequisite hardening pass. The clean final source/image now has a
recorded identity, but its deployed current-arm evaluation and final-digest
fresh/resumed confirmation still gate M1. The focused suites pass, while the
full combined content command still exposes an unrelated concurrent
Canvas/Office renderer-coverage gap.

## Non-negotiable boundaries

- The guide remains enabled by default and independent of DB-authored skills
  and experts.
- The emergency control is negative and operator-only:
  `APP_GUIDE_BREAK_GLASS_DISABLED`. It is not a user preference, Cockpit
  toggle, grant, or ordinary feature flag.
- Disabling or failing to load the managed guide never revives a frozen,
  owner, project, global, or workspace copy with the reserved `app-guide`
  name.
- Health output exposes a bounded reason code, never a local path, exception,
  skill contents, secret, or private deployment detail.
- The App Guide may explain current build-level behavior but cannot infer
  deployment, grant, attachment, or session readiness. Those distinctions
  remain Phase 2.
- Model evaluations live outside `config/skills/app-guide/` so the runtime
  skill cannot see its held-out questions or expected answers.
- A skipped live-model or k3d run is not a pass and does not close M1.
- Existing user work and unrelated dirty files are never staged with M1f.

## Work package 1 — finish the original-reference audit

**Files**

- Modify: `config/skills/app-guide/references/jobs.md`
- Modify: `config/skills/app-guide/references/experts.md`
- Modify: `config/skills/app-guide/references/memory-and-knowledge.md`
- Modify only when required by the audit:
  `config/skills/app-guide/references/overview.md`,
  `config/skills/app-guide/references/sessions.md`,
  `config/skills/app-guide/references/files-and-integrations.md`, and
  `config/skills/app-guide/SKILL.md`
- Modify: `tests/test_app_guide_content.py`
- Modify: `tests/test_product_help_tool.py`
- Modify: `docs/features/app_guide_skill.md`

**Implementation**

- [x] Compare the Jobs guide to the current job lifecycle, review states,
  workspace choices, Fleet tool actionability, and Cockpit labels. Remove
  historical queue/review/branch claims that are no longer true.
- [x] Compare the Experts guide to bundled versus DB-authored experts,
  persistent versus worker eligibility, selection defaults, project scope, and
  current create/edit/delete constraints.
- [x] Compare the Memory/Knowledge guide to persistent context compaction,
  Memory Light, project knowledge, external OKF knowledge bases, agent tools,
  and the current distinction between automatic recall and explicit search.
- [x] Sweep the remaining broad references for duplicated or conflicting
  claims exposed by those three audits. Route detail to existing focused
  topics instead of copying it.
- [x] Add content metadata where a touched reference lacks it, without
  inventing Phase 2 capability state.
- [x] Add focused contract assertions for consequential safety and
  actionability claims. Prefer assertions tied to canonical Python constants or
  registries where one already exists; do not create a second inventory solely
  to test prose.

**Automated evidence**

```bash
.venv/bin/pytest \
  tests/test_app_guide_content.py \
  tests/test_product_help_tool.py \
  tests/test_bundled_skills.py -q
```

Implementation-time evidence (2026-07-25):

- `tests/test_product_help_tool.py` plus `tests/test_bundled_skills.py`:
  **16 passed**.
- `tests/test_app_guide_content.py`, excluding the unrelated
  `test_canvas_and_direct_browser_tool_inventories_have_guide_coverage`:
  **16 passed, 1 deselected**.
- The unfiltered union is deliberately not recorded as green: it reports one
  Canvas contract failure because the current `CanvasRenderer` contains the
  concurrently introduced `office` value but that separate feature's guide
  coverage set has not yet classified it. Work package 5 must rerun the
  unfiltered command.

## Work package 2 — operator break-glass and degraded health

**Files**

- Modify: `src/core/skill_resolution.py`
- Modify: `src/tools/product_help.py`
- Modify: `src/api/persistent_app.py`
- Modify: `helm/values.yaml`
- Modify: `helm/templates/configmap.yaml`
- Modify: `.env.example`
- Modify: `tests/test_skill_resolution.py`
- Modify: `tests/test_product_help_tool.py`
- Modify: `tests/test_persistent_session.py`
- Modify or create a focused persistent-app health test
- Modify: `helm/ci/test-values.yaml` only if the chart fixture requires an
  explicit value

**Runtime contract**

- [x] Add one shared truthy parser/policy seam for
  `APP_GUIDE_BREAK_GLASS_DISABLED`; default is false.
- [x] When disabled, remove any same-name untrusted entry as today, do not add
  the managed bundle, and do not instantiate `read_product_guide`.
- [x] Preserve the ordinary Canvas companion and unrelated skill behavior.
- [x] Report App Guide state from the persistent agent health surface with one
  of:
  - `ready`;
  - `disabled` / `operator_break_glass`; or
  - `unavailable` / a bounded bundle or reader reason.
- [x] Return HTTP 200 for liveness while representing the overall health as
  degraded when the guide is disabled or unavailable. Readiness for the chat
  runtime remains independent.
- [x] Expose the negative Helm value under `agent`, render it into the shared
  ConfigMap inherited by provisioned agents, and document it as emergency
  rollback only.
- [x] Prove that re-enabling and rebinding a session restores the current
  digest-stamped bundle rather than stale or mutable bytes.

**Automated evidence**

```bash
.venv/bin/pytest \
  tests/test_skill_resolution.py \
  tests/test_product_help_tool.py \
  tests/test_persistent_session.py \
  tests/test_persistent_app.py -q

helm lint helm/ -f helm/ci/test-values.yaml
helm lint helm/ -f helm/ci/customer-external-values.yaml
```

Implementation-time evidence (2026-07-25):

- Focused Python union: **401 passed**, with four pre-existing warnings.
- Ruff check and format check pass for all affected Python files.
- Both Helm lint profiles pass (the existing optional icon recommendation is
  informational).
- Rendered shared ConfigMap contains
  `APP_GUIDE_BREAK_GLASS_DISABLED: "false"` by default and `"true"` under the
  explicit Helm override.

## Work package 3 — compaction, routing, and honest-gap evaluations

**Files**

- Create: `eval/app_guide/cases.yaml`
- Create: `eval/app_guide/run.py`
- Create: `eval/app_guide/README.md` only if required to operate the standalone
  evaluation harness; runtime skill folders must not gain auxiliary docs
- Create: `tests/test_app_guide_eval_harness.py`
- Create: `tests/test_app_guide_compaction.py`
- Modify: `config/skills/app-guide/SKILL.md` only when an observed evaluation
  failure requires a general routing/grounding correction

**Corpus contract**

- [x] Include balanced broad product questions, focused workflow questions,
  availability questions, and paraphrases.
- [x] Include near-miss negatives for repository/codebase onboarding,
  application code questions, generic productivity advice, and similarly named
  non-SRW concepts.
- [x] Include at least one genuine off-document question whose correct result
  is an explicit guide gap, not an answer reconstructed from model priors.
- [x] Store expected trigger decision, expected topic ID or negative decision,
  required facts, forbidden claims, and criticality per case.
- [x] Validate corpus schema, unique IDs, class balance, known topic IDs, and
  required negative/off-document coverage in ordinary CI.

**Execution contract**

- [x] The standalone runner uses a fresh context per case, records tool
  trajectory separately from answer scoring, and supports comparing the
  current skill to a no-skill or previous-skill arm.
- [x] Treat a product-positive case as a trigger success only when the model
  calls `read_product_guide`; prose that happens to be correct from priors is
  not a routing pass.
- [x] Treat a near-miss as a pass only when it does not call the product reader.
- [x] Score critical forbidden claims at zero tolerance. Keep ordinary wording
  quality separate from grounding and trajectory.
- [x] Add a deterministic compaction test proving that after old tool results
  are compacted the managed catalog description is still injected and the
  current reader/topic can be called again; unrelated references remain
  unloaded until requested.

**Automated evidence**

```bash
.venv/bin/pytest \
  tests/test_app_guide_eval_harness.py \
  tests/test_app_guide_compaction.py \
  tests/test_persistent_graph.py \
  tests/test_persistent_session.py -q
```

The live-model command and selected model are recorded by the runner. The
result artifact must include per-case trajectory and critical-failure counts,
but no raw secrets or private session content.

Implementation-time evidence (2026-07-25):

- The versioned corpus contains **30** synthetic cases: 17 product positives
  and 13 near-miss negatives, including two broad questions, two availability
  questions, paraphrases, all four required near-miss classes, and one honest
  off-document case.
- Harness/compaction unit tests: **12 passed**.
- The full work-package regression command above: **230 passed**, with three
  pre-existing warnings.
- Ruff check/format and `git diff --check` pass for the work-package files.
- A supported in-memory credential handoff from an authorized synthetic session
  allowed the first complete current-arm run without persisting a key or base
  URL. It scored 18/30 with no provider errors or critical forbidden claims,
  correctly failing the release gate and exposing routing/grounding weaknesses.
- A later candidate-snapshot run scored 29/30, but it is diagnostic rather
  than release evidence. The complete final deployed current arm must still
  pass against the exact final bundle.

## Work package 4 — fresh/resumed k3d acceptance matrix

**Preflight**

- [x] Confirm `kubectl config current-context` is the intended local k3d
  context, the cluster responds, and Tilt or the selected image deployment path
  is current. The host endpoint was unavailable, so the responding direct k3d
  API endpoint and image-import path were recorded instead.
- [x] Record full source revision and relevant deployed image IDs. Do not
  silently treat a dirty live-mounted tree as a committed image.
- [x] Use synthetic users/projects/questions only. Never print datasource
  credentials or private environment values.

**Matrix**

| Session | DB skills | DB experts | Workspace | Required observation |
|---|---:|---:|---|---|
| Fresh | off | off | None | Managed index and focused topic load without workspace files |
| Fresh | off | off | Virtual | Same guide digest; unrelated reference remains on demand |
| Fresh | off | off | Container | Same guide digest; mutable same-name workspace content is ignored |
| Resumed pre-M1f thread | off | off | None | Rebind exposes the current guide and reader |
| Resumed pre-M1f thread | off | off | Virtual | Current digest replaces frozen/stale catalog bytes |
| Resumed pre-M1f thread | off | off | Container | Current guide remains authoritative across workspace persistence |

Checkpoint result: all six cells passed against deployed guide digest
`7d16da6338f4e6bc1a50b3c1ab20e6da3a2d0405f6815998ecc506d82d744dc0`.
The later routing hardening produced final digest `20974f2e...`, so the matrix
remains open until live paths confirm that exact digest.

**Behavioral probes**

- [ ] Ask the core broad question and the Email folder-allowlist question in at
  least one fresh and one resumed session; confirm the reader trajectory and
  grounded answer.
- [x] Force a context compaction, ask a second product question, and confirm
  the guide is retrieved again from the current bundle.
- [x] Enable the break-glass value in the local test deployment, confirm the
  guide/tool disappear and health reports `operator_break_glass`, then disable
  it and confirm a rebind restores the guide.
- [x] Ask the held-out off-document question and confirm the answer names the
  guide gap rather than inventing UI or support.

**Evidence**

- [x] Create `docs/tests/app_guide_m1_verification.md` with date, source revision,
  deployment identity, matrix results, commands, bounded output excerpts, and
  any accepted warnings.
- Do not mark the matrix complete from unit mocks, direct function calls, or a
  session created against different source bytes.

## Work package 5 — close M1

- [ ] Run the union of focused Python tests, Ruff checks, both Helm lint
  profiles, and any affected Cockpit checks.
- [ ] Run `git diff --check` and validate the staged patch independently from
  unrelated dirty work.
- [ ] Update `docs/features/app_guide_skill.md` with shipped M1f behavior and
  links to the evaluation/live evidence.
- [ ] Check the remaining Phase 1 boxes only when their evidence above is
  present.
- [ ] Mark M1 complete only when the Phase 1 exit gate passes for both a fresh
  chart-default session and a pre-upgrade resumed session.

## Recommended commit boundaries

1. `docs(app-guide): define M1 closure plan`
2. `docs(app-guide): finish core reference audit`
3. `feat(app-guide): add break-glass health contract`
4. `test(app-guide): add routing and compaction evaluations`
5. `docs(app-guide): record M1 live verification`

Commit boundaries may be combined when a test and its implementation are
inseparable, but documentation planning remains the first commit and live
verification remains the final gate.
