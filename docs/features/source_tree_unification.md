---
tags:
  - feature
  - architecture
  - refactor
  - orchestrator
  - agent
  - open-source
aliases:
  - flat source tree
  - unified tree
  - monorepo restructure
  - orchestrator/src merge
related:
  - "[[agent_open_source_split]]"
  - "[[orchestrator_main_py_monolith]]"
  - "[[go_rewrite]]"
  - "[[cockpit_folder_restructure]]"
  - "[[2026-06-09-release-package-and-licensing]]"
  - "[[2026-06-09-roadmap-priorities]]"
---

# Source Tree Unification — Risk/Benefit Assessment

**Date:** 2026-06-15 (assessment) / 2026-07-04 (decision + plan)
**Status:** **DECIDED — executing next.** The original June-15 assessment is
preserved below unchanged; the decision, re-measured facts, target layout, and
migration plan are in the sections that follow it (from
[Decision & Plan](#decision--plan-2026-07-04) onward).

## The Proposal

Collapse the current two-tree split — agent in `src/` + `agent.py`, orchestrator
in `orchestrator/` — into a single flat source tree:

- Thin entry files at the top: `orchestrator.py`, the worker-agent entry, and
  the persistent-session-agent entry (today: `agent.py` driving `src/graph.py`
  and `src/persistent_graph.py`).
- Shared packages and utilities below, importable by every entry.

Stated motivation: the split has always made code reuse awkward, which removed
the natural home for shared utilities and likely contributed to
`orchestrator/main.py` growing into a 20k-line monolith. Framed as a precursor
to a possible Go rewrite (see [[go_rewrite]]) and to open-sourcing the core (see
[[agent_open_source_split]] and [[2026-06-09-release-package-and-licensing]]).

Stated disadvantages (from the author): images would carry more Python than
needed, and Dockerfiles would get more complex because you'd have to pick and
choose packages per image instead of copying one directory.

## Verdict (BLUF)

The merge is a good idea whose payoff is **almost entirely conditional on the
open-source release happening**. The release is the event that makes the repo
layout permanent (public import paths become API for forks) and is therefore the
moment when paying the churn cost is cheapest. Done standalone, mid-pilot, it
scores near zero on every roadmap decision rule and is mostly cost. Done as the
first commit of release prep, it is mostly benefit.

Two corollaries:

- The small **`shared/` extraction** (~a day) is positive-EV regardless of the
  release and removes most of the day-to-day pain. It can be done independently
  and early.
- The Go rewrite neither justifies the restructure nor benefits from it at
  *start* time — only at *port* time, and only if its own trigger conditions
  ever fire. Keep the two decisions decoupled.

## Verified Current State

Grounding facts from a pass over the tree (2026-06-15):

- **The shared surface is tiny.** Only two agent modules are imported by the
  orchestrator: `src.core.model_registry` (~316 lines; used by `main.py`,
  `services/builder_config.py`, `services/llm_endpoint_probe.py`,
  `services/capability_credentials.py`) and `src.utils.ssh_key` (~199 lines;
  used by `main.py`). Five import sites, ~515 lines total.
- **The dependency is one-directional.** `grep` for `from orchestrator` /
  `import orchestrator` inside `src/` and `agent.py` returns nothing. The agent
  never imports orchestrator code. This is the property the OSS split depends on.
- **There is already duplication papering over the gap.** `orchestrator/utils/
  db_url.py` is a hand-mirror of `src/utils/db_url.py`. Its comment claims it is
  "kept duplicated so the orchestrator container image doesn't need to bundle the
  agent `src/` tree" — but `docker/Dockerfile.orchestrator` already does
  `COPY src/ ./src/`. The justification is already void; the duplication is pure
  tax.
- **The orchestrator image already bundles all of `src/`.** So "images carry
  more Python than needed" is today's reality, not a new cost the merge
  introduces.
- **Requirements are already split** into two files (agent ~84 lines,
  orchestrator ~44 lines), each image installing its own. This is the real lever
  keeping the agent image from inheriting the k8s client and friends.
- **The monolith is still a monolith, and growing.** `orchestrator/main.py` is
  ~20.5k lines (the [[orchestrator_main_py_monolith]] issue measured 19,032 on
  2026-05-18). The `APIRouter` split has barely started: `routers/sessions.py`
  and `routers/automations.py` exist, but `main.py` has only 5 `include_router`
  calls and still carries the bulk of the endpoints inline.

## Benefits

1. **Ends the duplication-with-a-false-alibi.** Shared code gets one correct
   home; the mirrored `db_url.py` and its now-false comment go away.
2. **Gives shared code a legitimate place to live.** Today, anything needed by
   both sides has no correct location, so it gets duplicated or absorbed into
   `main.py`. This removes that excuse — but see the limits below; it removes the
   excuse, not the monolith.
3. **Layout finality before open-sourcing.** Once the AGPL core is public, import
   paths and repo shape are effectively API for forks and contributors. A
   pre-release move costs ~a week of internal churn; a post-release move imposes
   the same churn on every downstream user and makes the project read as
   unstable. This is the single strongest argument, and it pins the *when*.
4. **Go-rewrite optionality, for free.** A unified tree with thin entries and an
   enforced `entries → components → shared` import direction is structurally
   `cmd/` + `internal/`. If the rewrite ever activates, the port maps 1:1. You
   get this without committing to Go.
5. **Builds get simpler, not harder — if you let them.** Both Dockerfiles
   converge to the same shape: `COPY` the tree + install their own requirements +
   their own `CMD`. The author's "more complex, pick and choose" worry only
   materializes if you *also* try to minimize image contents (see Risk 4); the
   default flat copy is actually simpler than today.

## Risks

1. **Churn radius.** Essentially every import across ~230 Python files changes.
   In-flight branches (workspace-reaper, the uncommitted headscale fix) become
   conflict bombs. The fallout list is long but mechanical: Tiltfile live-sync
   paths, CI workflows, helm, test imports, CLAUDE.md, and the `get_project_root`
   pyproject-marker hack. Fully mitigable by timing (after branches land) and by
   doing it as a single mechanical move-commit with zero logic changes — but only
   if a genuinely quiet window exists.
2. **Boundary erosion (the sneaky one).** The crude two-tree split makes sideways
   imports physically awkward. One tree turns `orchestrator → agent-internals`
   into a one-line temptation, and a year of that quietly destroys the property
   the OSS thesis rests on: that a third party can drive the agent with a
   ~300-line orchestrator over HTTP. **If the merge happens without an
   import-linter rule in CI from day one, this risk is near-certain over time.**
3. **It won't fix the monolith.** The 20k `main.py` needs its own incremental
   router/dispatch extraction (the plan in [[orchestrator_main_py_monolith]] is
   sound). The service layer already exists and was *available* before the merge;
   the split being absent is habit, not a missing home. If this restructure is
   mentally booked as "the monolith fix," the budget gets spent and the monolith
   survives.
4. **Dependency creep.** One tree gravitates toward one requirements file. The
   moment the dep sets merge, the agent image inherits the orchestrator's deps.
   The two-requirements split must survive the merge *deliberately* — it is the
   thing actually keeping images lean (torch already dominates size; everything
   else is noise next to it).
5. **Opportunity cost.** Against the [[2026-06-09-roadmap-priorities]] decision
   rules (pilot proximity, trust, deployment friction, product clarity), a
   standalone restructure scores ~zero. It is parking-lot work unless attached to
   the release milestone. Solo-dev weeks are the scarcest input in the whole
   equation.
6. **Strategy-reversal exposure (low).** If the posture ever reverts from "AGPL
   everything" to open-agent / closed-orchestrator, a unified tree makes
   re-drawing that separation harder. Current direction makes this unlikely, but
   it is a near-one-way door.

## Engaging the Author's Stated Disadvantages

- *"Images carry more Python than needed."* Already true today — the orchestrator
  bundles all of `src/`. It is size-noise next to the torch wheel, and under an
  all-AGPL release it is not a licensing leak either. Net-neutral.
- *"Dockerfiles get more complex — pick and choose packages."* This is the real
  tension, but it is optional. Copying the whole tree keeps the Dockerfile simple
  (the status quo) at the cost of fat images. Selectively copying subpackages
  buys lean images at the cost of the complexity feared. Since torch dominates
  image size, the lean-image optimization isn't worth the complexity — so the
  disadvantage is a hypothetical you can decline, not a cost the restructure
  forces on you.

## The Independent Win: `shared/` Extraction

Regardless of the release decision or the full move, extracting the ~515 shared
lines (`model_registry`, `ssh_key`, `db_url`) into a small shared package is
positive-EV on its own:

- removes the mirrored `db_url.py` and its false comment;
- gives the genuinely-shared code one home;
- is ~a day of low-risk work with a small blast radius (5 import sites);
- does not pre-commit to the full flat layout.

This is the recommended first concrete step *if* any action is taken.

## Sequencing Options (if/when this is acted on)

1. **Pre-release hygiene (recommended).** Extract `shared/` now; do the full
   unified-tree move as the first commit of public-release prep, after
   workspace-reaper and the headscale fix land. Layout is final before it becomes
   public API; churn is paid once, in the cheapest window.
2. **Full move now.** Restructure on `develop` soon and absorb the conflict pain
   on in-flight branches plus Tilt/CI/helm churn mid-pilot. Hard to justify
   against the roadmap rules.
3. **Minimal only, defer layout.** Extract `shared/`, stop the duplication, and
   revisit the full layout only if the Go rewrite actually activates per
   [[go_rewrite]]'s trigger conditions.

## Non-Goals / Out of Scope

- Not a fix for `orchestrator/main.py` — that is the separate router/dispatch
  extraction in [[orchestrator_main_py_monolith]].
- Not a commitment to the Go rewrite — that has its own independent trigger
  conditions and is not a dependency in either direction.
- Not a change to the agent↔orchestrator HTTP boundary, which stays exactly as
  documented in [[agent_open_source_split]].

## Open Questions

- If the full move happens, what enforces the import direction? (Candidate:
  `import-linter` contract in CI, added in the same PR as the move.)
- Same binary vs. selective copy for images — accept fat images for Dockerfile
  simplicity, or pay complexity for lean ones? (Lean recommended *against* given
  torch dominance.)
- Does the move land before or after the partial `APIRouter` split is finished?
  (Finishing first means fewer files churned by the move.)

---

# Decision & Plan (2026-07-04)

> Revised the same day after a 13-agent recon pass (8 codebase censuses, 4
> web-research briefs, 1 adversarial review of this plan; ~1.25M tokens, all
> file:line claims below verified against the tree at `9fbb50bb`). The
> revision corrected two structural errors in the first draft: (1) the six
> steps **cannot land as six pushes** — CI has no editable install mid-sequence
> and `develop.yml` auto-deploys every push, so steps gate locally and the PR
> lands as one push; (2) "homelab uptake stays behind the tag-pin" was false —
> CI bumps image tags on every develop push, so deploy safety needs explicit
> handling (see Deploy Safety).

## Drift Check (2026-07-30)

> Re-measured at `773ad46e` — 758 commits, +178k/−20k lines across 523 `.py`
> files since the recon baseline `9fbb50bb`. **All nine Decisions survive**;
> the *computed facts* beneath them are stale. Consequence adopted: this doc
> freezes decisions and methods, while facts (manifest, hazard inventories,
> counts, baseline) are **regenerated by script at execution time** — step 0
> gains that census script. Numbers elsewhere in this doc illustrate scale;
> census output is the execution input.

Decision-level re-verification (2026-07-30):
- **Checkpoint safety holds** — `src/core/state.py` has a zero diff since the
  recon's empirical serde verification (still a TypedDict of primitives +
  BaseMessages).
- **No new top-level app** — the canvas gateway is
  `orchestrator/canvas_gateway.py` (added 07-13) served from the *orchestrator
  image*; the target layout is unchanged. Its deployment command line is a new
  consumer (below).
- **Layering intact** — the orchestrator now imports `src.tools.*` (zero sites
  at baseline), but the pulled modules (`registry`, `mcp/sdk`,
  `knowledge/{gardener,chunker}`) have clean import blocks — no
  langchain/langgraph — so the shared-stays-langgraph-free invariant survives;
  they simply join the manifest under the ≥2-apps rule.

Fact deltas (regenerate via census, do not hand-patch the sections below):
- **Shared surface**: +44 cross-tree import sites; ~11 modules absent from the
  manifest — `services/knowledge_store` (7 sites), `core/product_capabilities`,
  `core/datasource_setup`, `core/datasource_catalog`, `core/backends/rclone`,
  `core/session_tool_overrides`, `core/runtime_provenance`, `tools/registry`,
  `tools/mcp/sdk`, `tools/knowledge/{gardener,chunker}` (+ transitive
  `core/chunk_planner`). The MCP graft **grew**: both `Dockerfile.mcp{,.dev}`
  now also graft `orchestrator/security/anti_framing.py` (+ its package
  `__init__`, `Dockerfile.mcp:64-68`) → `anti_framing` joins shared alongside
  `formatters`.
- **Hazards**: deferred `from main import` sites 33 → 41; total rewrite
  surface now exceeds the recorded ~2,400. Two new runtime-path entries added
  to § Runtime Path Fixes: `src/core/skill_resolution.py:204` and the
  deliberate `sys.path` surgery in `src/tools/mcp/sdk.py`.
- **Consumers**: NEW `helm/templates/canvas-gateway/deployment.yaml:120-121`
  runs `uvicorn canvas_gateway:app` → becomes `orchestrator.canvas_gateway:app`
  (added to § Consumer Checklist). `develop.yml`, `main.yml`,
  `db-migrations.yml`, and the Tiltfile all changed since their cited line
  numbers — re-derive the CI filter list, the deploy-safety all-green gate
  list, and § Tilt During the Migration against current files at step 0.
- **Baseline** (§ Baseline) stale — tests grew by ~180 files; re-record.

Drift rate, for scheduling honesty: ~44 new cross-tree sites and 8 new
circular-import workarounds accrued in 26 days. The census script makes delay
*survivable*, not free — the mechanical surface grows roughly 10–15% per
month of feature work on the old layout.

## Why Now

The June-15 verdict ("defer to release prep") rested on facts that have eroded:

| Fact | 2026-06-15 (assessment) | 2026-07-04 (re-measured) |
|---|---|---|
| Shared surface | 2 modules, 5 import sites, ~515 lines | **12 modules, 47 import sites** (`model_registry`, `ssh_key`, `capability_grants`, `skill_format`, `skill_resolution`, `expert_resolution`, `loader`, `transport_resolution`, `backends.factory`, `embedding_service`, `knowledge_graph`, `neo4j_db`) |
| `db_url.py` duplication | Mirrored, identical | Mirrored **and diverged** — `src/` copy gained the checkpointer resolvers (2026-06-25); orchestrator copy stale since 2026-05-06 |
| `main.py` | ~20.5k lines | **25.4k lines**, still 6 `include_router` calls |
| Quiet window | Never available (in-flight branches) | **Available** — clean tree, develop 1 docs-commit ahead, no stashes; running loops compound a *separate* project repo |

The boundary-erosion risk (Risk 2) is no longer hypothetical: the orchestrator
already reaches into agent internals (`backends.factory`, `embedding_service`,
…) — the two-tree split isn't preventing coupling, only making it ugly and
breeding stale duplicates. The shared surface grew ~10× in three weeks; every
week of delay makes the move strictly bigger. The flatten is also the declared
precondition for the broader hygiene wave (abstractions, dedup, monolith
split): hygiene done on the old layout churns twice.

## Decisions Taken

1. **Layout: src-layout with real top-level packages.** `src/` is the sys.path
   root (not a package — delete today's `src/__init__.py`). New root
   `pyproject.toml` + editable install kills every `sys.path.insert` hack
   (today: `agent.py:37`, `orchestrator/init.py:46-49`,
   `orchestrator/mcp/run.py:27`, `orchestrator/main.py:24634/25133`,
   `tests/conftest.py:19-20`, plus ~65 per-test-file inserts).
2. **Scope: all Python apps move** — agent, orchestrator, MCP server
   (`orchestrator/mcp/`), and `vm/controller`. `config/`, `tests/`, `scripts/`,
   `helm/`, `docker/` stay where they are; `tests/` and `eval/` get import
   rewrites only (recon found `eval/memory/*.py` imports `from src.` — in
   scope, missed by the first draft).
3. **Flatten first, hygiene after.** The `main.py` router split happens
   *post-move*. This move is **zero-logic-change** — `git mv` + mechanical
   import/string rewrites only — with a short **declared exception list**
   (§ Runtime Path Fixes: `__file__`-hop-count corrections that are
   provably-required consequences of the deeper tree).
4. **Requirements files survive separately.** Root `requirements.txt` **stays
   at repo root** (it is the agent set AND the default dev-venv set; keeping it
   avoids touching 8 dependent files). `orchestrator/requirements.txt` and
   `orchestrator/mcp/requirements.txt` move with their packages. Never merged.
   The pyproject declares `dependencies = []` with a pointer comment (PyPA
   "abstract vs concrete deps" pattern); all installs use
   `pip install -r <reqs>` + `pip install --no-deps -e .`.
5. **import-linter ships in the same PR** — CI-blocking, `ignore_imports = []`
   from day one (§ import-linter below has the concrete config).
6. **Single-push landing.** Steps 1–5 are commits within one PR; each gates on
   *local* pytest+ruff+AST-equivalence, but only the completed state reaches
   `origin/develop`. Mid-sequence commits are CI-broken by construction (no
   editable install in CI until the PR's workflow edits land) — accepted
   bisect cost, mitigated by the per-step commit structure.
7. **Images use the editable install, not `PYTHONPATH`.**
   `pip install --no-deps -e .` in the final stage with source at `/app` —
   required because Tilt `live_update` syncs source files and the runtime must
   import from the synced tree (a site-packages copy would silently ignore
   syncs). `ENV PYTHONSAFEPATH=1` added to all Python images and CI test
   invocations (kills the script-dir/cwd shadowing failure class).
8. **No checkpoint compat shim.** Recon verified checkpointed state embeds no
   `src.*` module paths (§ Checkpoint Compatibility) — a `sys.modules['src']`
   alias would create dual-module bugs and mask the import-linter. Instead:
   pre-flight blob inventory + a k3d pause→deploy→resume acceptance gate.
9. **ruff stays configless.** No `[tool.ruff]` in the new pyproject — adding
   config (or `requires-python` changing the inferred target-version) risks
   finding-drift/reformat churn conflated with the move. Only the CI path
   arguments change. Re-run ruff 0.14.10 before/after step 1 to confirm zero
   drift.

## Target Layout

```
src/                 # sys.path root — NO __init__.py (delete the current src/__init__.py)
  agent/             # was src/*  (core/, tools/, api/, database/, services/, utils/, graph.py, …)
    __main__.py      # was root agent.py (root file deleted — a root shim would shadow the package)
    agent.py         # was src/agent.py → imports become `from agent.agent import UniversalAgent`
  orchestrator/      # was orchestrator/*  (main.py, services/, database/, routers/, seed/, …)
    requirements.txt # moves with the package
  mcp_server/        # was orchestrator/mcp/   (NOT `mcp` — fastmcp pulls PyPI `mcp`, which
    __main__.py      #   orchestrator/mcp/oauth_bridge.py:32 imports absolutely — confirmed collision)
    requirements.txt # replaces run.py + the stale __main__.py
  vm_controller/     # was vm/controller/ — gains __init__.py + __main__.py (was ENTRYPOINT controller.py);
                     #   its flat sibling import `from headscale_client import` (controller.py:38) → relative;
                     #   its in-tree Dockerfile relocates to docker/Dockerfile.vm-controller (repo-root context)
  shared/            # the manifest below — NEW, EMPTY __init__.py files (do not inherit re-export chains)
requirements.txt     # STAYS at root (agent + dev-venv set)
init.py              # stays a root entry; `from src.init import` → `from agent.init import`
pyproject.toml       # NEW — packaging + pytest + import-linter config (NOT ruff, per Decision 9)
```

**pyproject sketch:** `[build-system] requires = ["setuptools>=77"]`,
`build-backend = "setuptools.build_meta"`; `[project]` with `requires-python`
and `dependencies = []` + pointer comment; `[tool.setuptools.packages.find]
where = ["src"]`, `namespaces = false`, exclude tests;
`[tool.pytest.ini_options] testpaths = ["tests"]`, `asyncio_mode = "strict"`
(declares today's emergent behavior; **import-mode stays default `prepend`** —
`importlib` mode breaks `tests/_fs_backend.py`-style helper imports and is a
separate follow-up); `[tool.importlinter]` per § import-linter. Add
`src/*.egg-info/` and `build/` to `.gitignore`.

**Import style:** `from agent.core.loader import …`, `from
orchestrator.services.completion import …`, `from shared.model_registry import
…`.

**Entry points — no root scripts sharing package names.** A root `agent.py`
next to package `agent/` shadows the package (`sys.path[0]` wins), so entries
become module mains:

- Agent: `python -m agent --port 8001 --loop` (Dockerfile CMD + provisioner
  command strings + docs). The `__main__.py` conversion drops `agent.py:37`'s
  sys.path hack (carrying it over would put `agent/` itself on sys.path and
  recreate flat-import dual identity).
- Orchestrator: `uvicorn orchestrator.main:app` — unchanged **as the dev
  command** (which is in fact broken *today* from repo root:
  `ModuleNotFoundError: No module named 'database'`; the move fixes it). The
  **image** CMD is `uvicorn main:app` on a flattened `COPY orchestrator/ ./`
  (`docker/Dockerfile.orchestrator:61,85`) and must change to the qualified
  form with a package-layout COPY — image and repo layout stop disagreeing.
- MCP: `python -m mcp_server` (real `__main__.py`; today's
  `orchestrator/mcp/__main__.py` is stale — docstring still says
  `cockpit.mcp`).
- VM controller: `python -m vm_controller`.
- The orchestrator's ~190 internal flat imports (`from services.x`, `from
  utils.db_url`, …) get rewritten to `orchestrator.`-qualified — see Rewrite
  Mechanics for the rules and the deferred-import constraint.

## The `shared/` Manifest

**Rule (amended by recon):** a module moves to `shared/` iff **≥2 apps import
it — from ANY tree, not just `src/`** — plus its transitive closure computed on
*module files* with **new, empty `__init__.py` files**. The second clause
matters: computing the closure through today's package `__init__.py`
re-exports would drag ~3,292 LOC of agent-only code (`document_processor.py`,
`document_models.py`, `utils/config.py`, `postgres_db.py` via
`src/utils/__init__.py:5-18` and `src/database/__init__.py:34`) that no other
app actually uses. Those stay in `agent/`.

**The manifest** (AST-computed closure over top-level *and* lazy in-function
imports; 21 modules, ~10.4k LOC — no closure bombs: nothing touches `tools/`,
`graph.py`, `context.py`, `workspace.py`, paramiko, playwright, or langgraph):

- **Move as-is** (no real intra-src deps): `model_registry` (401 LOC),
  `capability_grants` (181), `skill_format` (126), `transport_resolution`
  (109), `ssh_key` (199), `embedding_service` (226), `db_url` (72),
  `neo4j_db` (203).
- **Small closure**: `knowledge_graph` (725, +`neo4j_db`);
  `backends/factory` (70, +`workspace_backend` ABC 522 + the four lite
  backends `scratch`/`rclone`/`virtual`/`object_store` ~1,108 — orchestrator
  only wants the `LITE_BACKENDS` frozenset, but the rule forces the set in;
  all stdlib; acceptable).
- **The loader cluster** (~6.7k LOC): `loader` (4,696), `expert_resolution`
  (204), `skill_resolution` (72), plus `src/llm/{__init__,exceptions,key_ring,
  reasoning_chat}` (1,452) — dragged via `skill_resolution.py:14 →
  expert_resolution.py:135 (lazy deep_merge) → loader.py:17-18 (top-level
  model_registry + ReasoningChatOpenAI)`. This is "the LLM client factory in
  shared/" — a known smell, but it mirrors today's reality
  (`config_resolver.py:21` imports loader top-level; the orchestrator image
  already ships it). **Do not split `loader.py` in this PR**; flagged for
  post-flatten hygiene. `src/llm/response_guards.py` and
  `session_components.py` are NOT in the closure — they stay agent-side.
- **From the orchestrator tree**: `orchestrator/services/formatters.py` —
  imported by both orchestrator and the MCP server (today via a triple-fallback
  in `mcp/server.py:20-28` ending in an `importlib.import_module` string, plus
  a physical two-file graft in `docker/Dockerfile.mcp:50-51`). Moving it to
  `shared/` is what makes the mutual-independence contract pass with zero
  exceptions, and it deletes the Dockerfile graft + the Tiltfile negation
  dance. It imports only stdlib — clean move. (This was the first draft's
  biggest gap: its "12 modules" counted only orchestrator↔agent.)

**Dependency facts** (verified, so the move documents rather than discovers):
zero new orchestrator-image deps — every third-party import of every closure
member is already in `orchestrator/requirements.txt`, transitively satisfied,
or lazily guarded exactly as today (`neo4j` stays optional per the
graceful-degradation guard at `main.py:24630`; `jinja2` is only reached by
agent-side callers). `shared/` must stay **langgraph-free** —
`tests/test_lazy_imports.py` regression-guards this property today; keep the
guard pointed at the new paths.

The diverged `orchestrator/utils/db_url.py` mirror is deleted;
`shared/db_url.py` (the `src/` superset copy) becomes the single source.

## Rewrite Mechanics

**Tool:** LibCST `rename.RenameCommand` (version-pinned in a checked-in codemod
script), one invocation per rename pair. It is scope-aware (QualifiedNameProvider,
handles aliases, multiline parenthesized from-imports, TYPE_CHECKING blocks;
structurally cannot mangle comments/docstrings) — but not bug-free, so ship a
small before/after fixture suite covering this repo's actual import styles and
run it first. `ast-grep`/grep for enumeration and residue sweeps only; plain
`sed` only for non-Python files. Bowler is archived, `pasta` is dead, rope is
IDE-oriented — don't build on them.

**Commit sequence per step** (git-history preservation):
- **(A) pure `git mv` commit** — verified pure via
  `git diff -M100% --name-status <base>..HEAD | grep -v '^R100'` → empty.
  Exact-rename detection is then deterministic (blame/`--follow` guaranteed,
  GitHub renders renames).
- **(B) import-rewrite commit** (LibCST).
- **(C) string-literal + non-Python sweep commit** (human-reviewed diff).
- Final PR commit adds **`.git-blame-ignore-revs`** listing the B/C SHAs (not
  A — blame follows whole-file renames natively); GitHub honors the file
  natively. Document the one-time
  `git config blame.ignoreRevsFile .git-blame-ignore-revs` for devs.

**Zero-logic-change is a checked invariant, not a claim:** ship
`scripts/verify_ast_equiv.py` (~60 lines; Black `--safe` precedent) — for each
moved file, apply *only* the rename map to the old AST's Import/ImportFrom
nodes and assert `ast.dump`-equality with the new AST. Run per step and in CI
on the PR. Supplementary gates: `python -m compileall -q src/`, ruff
(F401/F821), residue greps, pytest, `lint-imports`.

**Rewrite dictionaries** (imports AND quoted dotted paths in string literals —
mock.patch targets, `sys.modules` keys, `importlib` args; a naive import-only
rewrite leaves 300+ broken `patch()` calls that fail at test time):
1. `src.` → `agent.` — ~213 src-internal + ~834 test import sites + ~444 test
   string literals; `src.agent` → `agent.agent` (20 sites, all tests).
2. Orchestrator flat → qualified, first segment ∈ {auth, database, routers,
   security, seed, services, utils, main, init, graph_routes, logging_config,
   uploads} → `orchestrator.<segment>` — ~190 internal + ~405 test import
   sites + ~198 flat patch strings (`"main.` → `"orchestrator.main.` etc.).
   Applies **only inside** `src/orchestrator/**` and `tests/**`.
3. `orchestrator.mcp` → `mcp_server`; `vm.controller` → `vm_controller`.

Total budget: **~2,400 sites**, not the first draft's "~250 files of imports".

**Named hazards, each with its handling:**
- The 33 deferred `from main import …` sites (routers/, auth/bff.py,
  security/auth.py, 3 services) are **deliberate circular-import
  workarounds** — re-target them *in place*, never hoist to module level (a
  hoist breaks startup).
- The 7 `try/except ImportError` dual-mode import fallbacks
  (`main.py:38-51`, `database/postgres.py:8192-8200`,
  `seed/llm_config.py:90-98`, `mcp/server.py` ×3, `mcp/__main__.py:15-18`)
  **collapse to the single qualified form** — leaving them would let a stale
  except-arm silently mask rewrite mistakes. Correctness requirement, not
  cleanup.
- `routers/__init__.py` + `auth/__init__.py` self-reference their own
  submodules by flat absolute name → rewrite to **relative** imports
  (move-invariant).
- Three modules are importable as `init` (root, orchestrator's, src's);
  `from init import _seed_admin_mcp_token` at `main.py:5579` and — unguarded —
  `security/auth.py:359` → qualify as `orchestrator.init`.
- The one true dynamic import in prod code:
  `mcp/server.py:28 importlib.import_module("orchestrator.services.formatters")`
  — dies with the fallback collapse (formatters moves to shared/). After the
  codemod, `grep -rn 'import_module('` must show no cross-app strings.
- **Logging namespace tuples** hardcode module-path prefixes: `agent.py:59`
  `app_namespaces=("src", "orchestrator")`, `src/init.py:56`, root
  `init.py:64`, `orchestrator/main.py:54-65` (flat names). Missed = DEBUG log
  filtering silently stops applying — rewrite to `("agent", "shared",
  "orchestrator", …)`.
- File-path module loaders: `tests/test_managers_{todo,plan,memory}.py`
  (spec_from_file_location on `src/core/…`), `scripts/check_endpoint_auth.py:28`
  (hardcodes `orchestrator/main.py`, consumed by
  `tests/test_endpoint_inventory.py`).
- `tests/test_lazy_imports.py` asserts on `src.core` lazy-import behavior by
  name; `tests/test_mcp.py`'s preamble deliberately imports the LOCAL package
  as `mcp` and pre-mocks fastmcp — both need **manual** (non-mechanical)
  fix-up when their trees move.
- String-sweep false positives exist — e.g. `test_workspace_backends.py:940`
  `backend.copy("src.txt", …)` — hence the human-reviewed diff for commit C.
- The 5 test files that today import the same services module under BOTH
  names (flat + qualified) currently get two distinct module objects; after
  unification the copies merge, which can *expose* previously-masked patch
  leakage between tests — if new failures appear there, suspect this, not the
  rewrite.
- **Post-mv cleanup in the same step:** purge `__pycache__` husks (untracked
  husks left at old paths become PEP-420 namespace packages that shadow the
  new tree), `*.egg-info/`, `build/`; delete `src/__init__.py`; check
  `git status --ignored` for leftover dirs matching package names. Devs must
  re-run `pip install --no-deps -e .` (or recreate venvs).

## Migration Steps

All steps are commits in ONE PR (Decision 6). "Green" per step = **local**:
`pytest tests/ -q --continue-on-collection-errors` matches the baseline
profile + `ruff check`/`format --check` + `verify_ast_equiv.py`. Tilt is
**untrusted** until step 5 (see § Tilt During the Migration).

0. **Prep (separate small commits, can push before the PR).** Push pending
   develop commits (tree must be clean AND pushed before the mv commits); add
   `scripts/verify_ast_equiv.py` + the codemod script + LibCST fixtures + the
   **census script** (`scripts/flatten_census.py` — regenerates the shared/
   manifest closure, the from-main / dual-mode / `__file__` / `sys.path` /
   string-literal inventories, and a consumer-surface sweep over `docker/
   helm/ .github/ Tiltfile scripts/`; see § Drift Check — doc numbers are
   scale illustrations, census output is the execution input); run the
   checkpoint pre-flight inventory (§ Checkpoint Compatibility); install
   `requirements-dev.txt` into `.venv` so the local gate equals CI; re-record
   the baseline (§ Baseline).
1. **`pyproject.toml` + agent move.** `git mv src/* → src/agent/`; root
   `agent.py` → `src/agent/__main__.py` (delete root file, drop its sys.path
   hack); delete `src/__init__.py`; dictionaries 1 (+ `eval/`); agent-side
   path-depth fixes (§ Runtime Path Fixes); Dockerfile.agent{,.dev} COPY/CMD +
   `COPY pyproject.toml` replacing the `touch` marker hack; CI install lines
   gain `pip install --no-deps -e .`; develop.yml agent/test filters.
2. **Orchestrator + MCP moves together.** `git mv` both trees
   (`orchestrator/mcp → src/mcp_server` first, then `orchestrator/* →
   src/orchestrator/` — the directory must fully dissolve: a leftover
   `orchestrator/` shell containing only `mcp/` is a namespace-package
   near-miss); dictionary 2 + 3; collapse the 7 dual-mode fallbacks; fix the
   two self-referencing `__init__.py`s; rewrite `tests/conftest.py` (delete
   the sys.path inserts at :19-20 and the `import main` pin block at :45-59;
   keep all env setdefaults verbatim); orchestrator-side path-depth fixes;
   Dockerfile.orchestrator{,.dev} package-layout COPY + CMD
   `uvicorn orchestrator.main:app`; Dockerfile.mcp{,.dev} package COPY + CMD
   `python -m mcp_server`; helm `llm-seed-job.yaml:59` →
   `python -m orchestrator.seed.llm_config` (chart+image are deploy-coupled);
   db-migrations.yml globs/working-directory, `scripts/schema-snapshot.sh`
   OUT_DIR/runner, `.squawk.toml` excluded_paths.
3. **VM controller move.** `vm/controller/ → src/vm_controller/` + package
   files; sibling import → relative; Dockerfile →
   `docker/Dockerfile.vm-controller` (repo-root context), ENTRYPOINT
   `python -m vm_controller`; both workflows' build context/file keys.
4. **`shared/` extraction.** Move the manifest (incl. `formatters.py`); new
   empty `__init__.py`s; delete the `db_url.py` mirror; rewrite both sides'
   imports; simplify Dockerfile.mcp (drop the formatters graft) + Tiltfile mcp
   block (drop the negations); delete the two runtime sys.path inserts +
   silent `except` shells around KB/embedding imports in `main.py:24630-24644`
   and `:25128-25139` (they'd mask a botched move as silent sparse-only KB
   search).
5. **Plumbing + enforcement.** import-linter into pyproject + both workflows'
   lint jobs; Tiltfile full rewrite (sync paths, narrowed agent
   `only=['src/agent/','src/shared/',…]` — a naive `src/` watch turns every
   edit into the ~50s agent rebuild + ConfigMap fan-out + Reloader bounce);
   ruff CI paths → `src/ tests/` (also develop.yml's auto-format `git add`
   line); remaining `-r` install paths; **provisioner command strings**
   (`agent_provisioner.py:241,1046-1059`, `persistent_provisioner.py:182,
   515-524` + operator-guidance strings in `main.py:16011,16202`) →
   `python -m agent`; README/CLAUDE.md/AGENTS.md/docker-compose comments;
   `.dockerignore`/`.gitignore`; the ~25 docs mentioning `python agent.py`
   (single sed sweep, low risk).
6. **Verification + land.** Full local suites (pytest profile == baseline,
   cockpit vitest 785/785, ruff green, `lint-imports` clean,
   `verify_ast_equiv` clean, residue greps clean); local builds of **all 7
   images** (agent/orchestrator/mcp prod+dev, vm-controller); Tilt
   `tilt trigger` full rebuilds, then the complete k3d smoke path (login →
   session → job → git.localhost → cloud.localhost) + the checkpoint resume
   gate; then the single push, with deploy safety per the next section.

## import-linter (concrete)

`import-linter>=2.13` (grimp-based, Rust core — trivial at our 287 files), one
layers contract expresses everything:

```toml
[tool.importlinter]
root_packages = ["agent", "orchestrator", "mcp_server", "vm_controller", "shared"]

[[tool.importlinter.contracts]]
name = "App boundaries: apps mutually independent, shared at the bottom"
type = "layers"
layers = [
    "agent | orchestrator | mcp_server | vm_controller",
    "shared",
]
```

`|` = independent siblings (direct AND indirect chains); higher may import
lower; `shared` imports none of them. After step 4 both directions start at
**zero violations, no grandfathering** — this is the OSS-split property
([[agent_open_source_split]]) made mechanical.

Operational requirements (from research):
- CI wiring: in both workflows' lint jobs, `pip install --no-deps -e .` (root
  packages must be importable) then `lint-imports --verbose`.
- **Keep `ignore_imports = []`**; the default
  `unmatched_ignore_imports_alerting = error` makes any future exception list
  self-shrinking. Any addition requires an issue link in a comment.
- **`__init__.py` everywhere** under `src/<pkg>/` — a missing one makes those
  modules vanish from grimp's graph and the contract passes *vacuously*. Cheap
  CI guard: `find src -type d -not -path '*/__pycache__*' ! -exec test -e
  '{}/__init__.py' \; -print` must be empty (except `src/` itself).
- `root_packages` is a closed list — CI one-liner diffing `ls src/` against it
  catches a future sixth package going unpoliced.
- **TYPE_CHECKING imports stay counted** (do not set
  `exclude_type_checking_imports`) — type-only coupling still defeats the
  split-repo property; recon found only 2 such imports, so this costs nothing.
- The linter is static: `importlib.import_module("<string>")` is invisible.
  Belt-and-braces CI grep: no
  `import_module(["'](agent|orchestrator|mcp_server|vm_controller)\.` outside
  the named app's own tree. (The only prod offender dies in step 4.)

## Runtime Path Fixes (declared zero-logic-change exceptions)

`__file__`-relative computations whose hop count changes when trees gain a
directory level. These are the ONLY intentional behavior-relevant edits; each
is whitelisted here so the AST-equivalence review can except them. Most fail
*silently* (config-not-found → defaults, template → None), which is why they
are enumerated rather than discovered.

Agent side (step 1):
- `src/utils/config.py:15` — a SECOND `get_project_root` (pure 3-parent depth,
  no marker walk). Post-move returns `src/`; live consumer:
  `src/services/memory/ingestion.py:109-111` loads the memory-verdict prompt
  through it, and memory failures are historically silent. Fix: +1 parent or
  delegate to the loader's marker version.
- `src/agent.py:2097` (`config/agents` templates, 2→3 hops);
  `src/api/orchestrator_client.py:1368,1373` (critic verification template,
  3→4 hops — silent None on miss); `src/init.py:35` (dies with the sys.path
  hack, fine).
- (drift 07-30) `src/core/skill_resolution.py:204` — `parents[2] / "config" /
  "skills"` fallback. Hop-sensitive AND a manifest member: at its final
  `src/shared/` home the depth is coincidentally unchanged (parents[2] = root
  again), but at step 1's interim `src/agent/core/` it silently resolves to
  `src/config/skills`. Fix once in step 1 by delegating to the marker-walk
  `get_project_root` (depth-immune) instead of chasing hop counts across two
  moves.
- (drift 07-30) `src/tools/mcp/sdk.py` — *deliberate* `sys.path` surgery to
  un-shadow the third-party `mcp` distribution; now also imported lazily by
  `main.py:17854`. Do NOT blind-rewrite: once nothing in the tree is named
  top-level `mcp` (Decision 1 named our package `mcp_server`), the shadowing
  it guards against may no longer exist — census flags it for manual review;
  the workaround may shrink or vanish.
- `src/core/loader.py:228`'s 4-parent fallback is *wrong today* and becomes
  correct at `src/agent/core/` (it was authored at that depth pre-cutover —
  git 37e9a3a2) — leave at 4. The marker walk (`.git` OR `pyproject.toml`,
  :214-228) is unaffected in-repo by the new root pyproject (`.git` matches at
  the same level); in images, replace every `RUN touch /app/pyproject.toml`
  hack with `COPY pyproject.toml ./` (4 Dockerfiles) — and never ship
  site-packages-only installs, or the marker walk finds nothing and every
  config lookup silently breaks.

Orchestrator side (step 2):
- `services/workspace.py:43` (3→4, WORKSPACE_PATH fallback — k8s sets the env,
  bare-metal dev breaks); `uploads.py:47` + `init.py:1464` (uploads dir,
  2→3); `init.py:857` (`helm/values.yaml` for system-model seeding — would
  silently seed nothing); `main.py:19005-19018` `_get_config_dir` (first
  candidate becomes `src/config`; `/app/config` saves images, bare-metal
  expert-scan returns empty); `services/docker_provisioner.py:88` (3→4,
  dev-compose auto-detect).
- Verification: `grep -rn '\.parent\.parent' src/` reviewed line-by-line
  against new depths; assert both `get_project_root` implementations resolve
  to a directory containing `config/` in dev, test, and image contexts.

## Checkpoint Compatibility

Verified against installed langgraph-checkpoint 3.0.1 source + an empirical
serde round-trip: `JsonPlusSerializer` embeds module paths only for
non-primitive Python types (pydantic models, dataclasses, enums, …);
**our checkpointed state has none of ours** — `UniversalAgentState` is a
TypedDict of primitives/dicts plus `BaseMessage`s (which embed
`langchain_core.*`, unaffected); todos/freeze_data/errors are dict literals;
`pickle_fallback=False`; zero `import pickle` anywhere. Existing paused jobs'
blobs remain loadable byte-for-byte after the rename.

But: if this were wrong, the failure is **silent** (the ext hook swallows
reconstruction errors → objects degrade to `None`), so ship positive
verification, not error monitoring:
1. **Pre-flight** (step 0): read-only script over the dev/homelab checkpoint
   DB loading every blob with a recording ext hook, inventorying all
   `(module, class)` pairs — expected: only `langchain_core.*`/`langgraph.*`/
   stdlib; any `src.*` hit is investigated before merge.
2. **Resume gate** (step 6, k3d, `CHECKPOINTER_BACKEND=postgres`): pause a job
   mid-tactical-phase → deploy renamed images → resume → assert the
   checkpoint-restore log path (`agent.py:3105-3116`), not cold start
   (`:3230`); repeat once via the pending_review + feedback path.
3. **No `sys.modules['src']` alias shim** — nothing to remap, and a shim would
   create dual-module/isinstance bugs and mask the import-linter. If a stray
   blob ever surfaces, langgraph 3.x's `JsonPlusSerializer(__unpack_ext_hook__)`
   is the surgical remap tool; or just cancel the affected job.

## Deploy Safety & Rollout

The first draft claimed homelab only updates behind tag-pin commits — **wrong**:
CI authors a `deploy: update image tags` commit on every develop push
(develop.yml:1288, values-experimental.yaml), and `deploy-experimental` does
NOT require the build jobs to have succeeded (only tests/audit; each image tag
bump is individually gated). A partial build failure therefore ships a
**mixed-layout cluster**. The mix is fatal in both directions because the
orchestrator injects the agent pod command explicitly
(`python agent.py …` — old orchestrator + new agent image, or new orchestrator
+ old agent image, crashloops every job/session pod).

For the landing push:
- Pre-push: build all 7 images locally (Docker build failures are exactly the
  class local pytest+ruff never exercises).
- Temporarily make `deploy-experimental` require ALL `build-*` results ==
  success (or pause Fleet / disable the deploy job), land, verify GHCR tags
  exist for the sha, then re-enable.
- Optional belt-and-braces: keep a one-line `/app/agent.py` shim **in the
  agent image only** (`from agent.__main__ import main; main()`) for one
  release — at `/app` it does not shadow the package at `/app/src`, and it
  makes the image tolerant of an old orchestrator during rollout.
- Rollback = `git revert` of the full PR → full ~30-min CI cycle → verify all
  tags bumped consistently. There is no partial rollback; that's the price of
  Decision 6 and why step 6's local verification is the real gate.
- k3d during verification: only `tilt trigger` full rebuilds; never manual
  `helm upgrade` under Tilt ([[reference_tilt_helm_upgrade_reverts_image]]).

## Tilt During the Migration

Between steps 1 and 5 Tilt is **untrusted — silently**: syncs land in dead
container paths (`sync('orchestrator/', '/app/')` matches nothing;
`sync('src/', '/app/src/')` delivers files the flat image never imports), and
uvicorn `--reload` even logs a restart while re-importing the stale baked
module — a dev "verifying" through Tilt gets false-positive confirmation.
`live_update` also never deletes files `git mv` removed, so warm containers
carry shadowing husks. Verify steps 1–4 via bare `pytest`/`uvicorn` only;
after step 5's Tiltfile rewrite, `tilt trigger` full rebuilds before trusting
any k3d observation. Also note the mcp block's `ignore='src/'` line flips from
correct to self-destructive when the MCP source moves under `src/` — no blind
sed on the Tiltfile.

## Baseline (recorded 2026-07-04, HEAD 9fbb50bb)

- ruff 0.14.10 (the exact CI pin): check + format-check fully green (604 files).
- `pytest tests/ -q --continue-on-collection-errors` (Python 3.13 venv):
  **7647 passed / 24 skipped / 1 failed / 9 errors, ~374s**. The failure
  (`test_database_phase1` → Postgres not running) and the 9 collection errors
  (`testcontainers` missing in `.venv`; CI installs it) are environmental.
- Cockpit vitest: **785/785** (59 files).
- Post-move acceptance = the exact same profile under the exact same
  invocation. Any new failure name is attributable to the restructure.
  (Fix the `.venv` — install `requirements-dev.txt` — in step 0 so the gate
  sharpens to 0 errors.)

## Acceptance Criteria

- [ ] `verify_ast_equiv.py` green: every moved file AST-identical modulo the
      rename map (plus the whitelisted Runtime Path Fixes)
- [ ] Pure-mv commits verified: `git diff -M100% --name-status | grep -v ^R100`
      empty per move commit
- [ ] Residue greps: `grep -rn "from src\.\|import src" --include="*.py"` → 0;
      `grep -rn '"src\.' src/ tests/` → only whitelisted false positives;
      `grep -rn 'patch("main\.\|patch("services\.\|patch("security\.' tests/`
      → 0; `grep -rn "sys.path.insert" src/ tests/ init.py` → 0;
      `python -c "import main"` and `import src` fail
- [ ] One `db_url.py` in the repo
- [ ] `lint-imports` passes with `ignore_imports = []`; `__init__.py`-coverage
      guard empty
- [ ] pytest matches the baseline profile; cockpit vitest 785/785; ruff green
      with zero finding drift
- [ ] A fresh venv built from README instructions can run pytest, `python
      init.py --help`, and `uvicorn orchestrator.main:app` from repo root
- [ ] All 7 images build locally; `python -m agent --help`,
      `uvicorn orchestrator.main:app`, `python -m mcp_server`,
      `python -m vm_controller` boot
- [ ] Every develop.yml change-detection filter matches ≥1 existing path
      (silent-skip guard); db-migrations.yml gate re-verified by touching a
      migration path post-move
- [ ] `schema-snapshot.sh --check` passes post-move (schema-artifacts-fresh
      gate)
- [ ] Tilt inner loop verified per component with the new sync paths (edit
      signal lands in pod logs per CLAUDE.md)
- [ ] Full k3d smoke-test path green + KB search returns dense results (not
      the silent sparse fallback) + the checkpoint resume gate passes
- [ ] Diff review confirms: moves + rewrites + declared exceptions only

## Consumer Checklist (non-Python surfaces, by step)

Step 1: `docker/Dockerfile.agent{,.dev}` (COPY requirements/src/agent.py, CMD,
touch-marker→COPY pyproject); Tiltfile agent `only=` list; develop.yml agent +
test filters (`agent.py` entry dies); CI `pip install -e .` lines
(main.yml:374, develop.yml:700).
Step 2: `docker/Dockerfile.orchestrator{,.dev}` (flat COPY→package, CMD);
`docker/Dockerfile.mcp{,.dev}` (flatten+graft→package, CMD; graft now includes
`security/anti_framing.py`, mcp:64-68); helm
`canvas-gateway/deployment.yaml:120-121` (`uvicorn canvas_gateway:app` →
`orchestrator.canvas_gateway:app`); helm
`llm-seed-job.yaml:59` + `llm-seed-configmap.yaml` comment; Tiltfile
orchestrator+mcp blocks; develop.yml orchestrator/MCP filters + license/audit
`-r` paths (:358-359, :393-394; main.yml:351-352); db-migrations.yml (paths,
working-directory, `git add` lines, header comment); `scripts/schema-snapshot.sh`
(OUT_DIR, runner, git-add hint; a header-template edit forces regenerating both
`*_current.sql` artifacts in the same commit); `.squawk.toml` excluded_paths.
Step 3: both workflows' vm-controller build context/file keys (main.yml:996,
develop.yml:1275); `docker/Dockerfile.vm-controller` (new home).
Step 5: ruff command sites (main.yml:52,54; develop.yml:67,71,77,81 — incl.
the auto-format `git add`); import-linter CI steps; Tiltfile full pass;
README/CLAUDE.md/AGENTS.md; docker-compose.dev.yaml:13-14 comments;
`.dockerignore` (`!requirements.txt` etc.), `.gitignore` (+egg-info/build),
`.env.example` comments; ~25 docs files mentioning `python agent.py`
(cosmetic sweep).
Unaffected (verified): helm srw-config ConfigMap fan-out (env-only, bundles no
source), workspace-pod/session subprocess call sites, VM golden-image
machinery (VM_TEMPLATE_PATH env; in-VM daemon is image-baked),
`helm-vm-cluster` (image refs only), config/ + prompts/ + templates
(resolved via `get_project_root`, fixed above).
