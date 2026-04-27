# Cockpit Folder Restructure

## Problem

The cockpit grew organically from a debug-grid prototype into a dual-shell app (mobile-first sidebar pages + an expert grid layout). Three top-level folders have collected the resulting overflow with no clear principle separating them:

- `shared/components/` — 16 components: feature widgets, embedded sub-forms, app-shell banners, and a routing helper, all mixed together.
- `simple/pages/` — 14 routed pages. Named "simple" because there used to be a "complex/desktop" sibling — that sibling is now gone, so the name communicates nothing.
- `shared/pages/` — exactly one page (`project-list`), orphaned from the rest.
- `layout/sidebar/` — the main sidebar component on its own; its toggle button lives in `simple/layout/sidebar-toggle/` instead.

There is no good answer to "where does a new feature X go?" — the worst test for any project layout. New components default to `shared/components/` because that's the path of least resistance, which has made the dumping-ground problem self-reinforcing.

A redesign of the UI is planned next. Restructuring the file tree before that redesign means the redesign happens against a coherent foundation rather than dragging the mess forward.

## The Structural Insight

The cockpit already has two presentation modes for the same underlying components:

1. **The simple shell** — a sidebar with routed pages. Mobile-default; also the desktop default today.
2. **The debug grid** — an expert layout where the user composes a screen out of grid panels, picking from a registered set of components.

Both modes render the *same components*. Look at `app.ts:219-345`:

```ts
this.registry.register({ type: 'job-list',     component: JobListComponent });
this.registry.register({ type: 'agent-chat',   component: ChatHistoryComponent });
this.registry.register({ type: 'todo-list',    component: TodoListComponent });
this.registry.register({ type: 'project-list', component: ProjectListPageComponent });
this.registry.register({ type: 'job-review',   component: JobReviewComponent });
// …17 more
```

`JobListComponent` is *both* the routed page at `/jobs` *and* the grid panel for type `'job-list'`. `ProjectListPageComponent` is *both* the routed page at `/projects` *and* the registered grid type `'project-list'`. The dual-mount pattern already works in production — it's just hidden under folder names that imply the components are different things.

The restructure makes the dual-mount pattern the **organizing principle**: every screen-owning component lives in one tree, and is mountable as a route, as a grid panel, or both.

## Long-Term Direction (Context, Not Scope)

The eventual end state, beyond this restructure:

- The grid view (`/debug` today, eventually a non-debug expert mode) becomes a first-class user choice — power users opt into "I'll compose my own layout."
- The simple shell remains the default for everyone else and the only experience on mobile.
- Both views consume the same set of mountable components. Anything new ships once and works in both.

This restructure is a prerequisite for that direction but does not implement it. The expert-mode toggle, grid persistence, layout sharing, etc. are all later work.

## Goals

1. One folder for screen-owning components, regardless of how they're mounted.
2. App-shell chrome separated from screen content.
3. Cross-cutting infra (services, guards, models) untouched.
4. Debug-only widgets that have no user-facing meaning stay in `debug/`.
5. Five mental-model questions, five folder answers — no "where does this go?" ambiguity.
6. No code logic changes. Pure file moves + import path updates.

## Non-Goals

- UI redesign or component-level rewrites — that's the *next* feature.
- Splitting `core/services/` (36 files) into sub-buckets. Marginal value, defer.
- Introducing `index.ts` barrel files at folder boundaries — keeps imports concrete.
- Visual regression test scaffolding — explicitly skipped per user direction (the redesign will obsolete any baselines we capture now).
- Implementing the expert-mode toggle or any runtime behavior change.

## Target Structure

```
src/app/
├── core/                        # cross-cutting infra (UNCHANGED IN PURPOSE)
│   ├── guards/                  # auth.guard, admin.guard
│   ├── interceptors/            # auth.interceptor
│   ├── markdown/                # citation-extension
│   ├── models/                  # 7 domain models
│   ├── routing/
│   │   └── message-redirect/    # ← from shared/components/  (pure router redirect, no UI)
│   └── services/                # 36 services + specs (unchanged)
│
├── ui/                          # design-system primitives (UNCHANGED — 21 components)
│   ├── badge/  button/  card/  checkbox/  chip/  dialog/  form-field/
│   ├── icon/  icon-button/  input/  menu/  radio-group/  select/
│   ├── spinner/  switch/  tab-bar/  tab-nav/  textarea/
│   └── theme-toggle/  toast/  tooltip/
│
├── shell/                       # app-frame chrome only
│   ├── sidebar/                 # ← from layout/sidebar/
│   ├── sidebar-toggle/          # ← from simple/layout/sidebar-toggle/
│   ├── notification-bell/       # ← from shared/components/
│   └── empty-catalog-banner/    # ← from shared/components/
│
├── views/                       # ★ self-contained screen-owning components
│   │                            #   Mountable as a route, as a grid panel, or both.
│   │
│   │ ── route + grid (current) ───────────
│   ├── shell/                   # /                   (was simple/pages/shell/)
│   ├── sessions/                # /sessions
│   ├── session-create/          # /sessions/new
│   ├── chat/                    # /sessions/:id
│   ├── jobs/                    # /jobs               + grid 'job-list' (page is the list)
│   ├── create/                  # /create             + grid 'job-create'
│   ├── inbox/                   # /inbox              + grid 'action-center'
│   ├── projects/                # /projects           + grid 'project-list' (was shared/pages/)
│   ├── project-detail/          # /projects/:id
│   ├── datasources/             # /datasources        + grid 'datasource-list'
│   ├── settings/                # /settings
│   ├── sudo/                    # /sudo
│   ├── admin/
│   │   ├── providers/           # /admin/providers
│   │   ├── models/              # /admin/models
│   │   └── users/               # /admin/users
│   │
│   │ ── grid-only (registered, no route) ──
│   ├── todos/                   # grid 'todo-list'
│   ├── statistics/              # grid 'statistics'
│   ├── agents/                  # grid 'agent-list'
│   ├── workspace-browser/       # grid 'workspace-browser'
│   ├── chat-history/            # grid 'agent-chat'
│   ├── instruction-builder/     # grid 'instruction-builder'
│   ├── config-editor/           # grid 'config-editor'
│   ├── job-review/              # grid 'job-review'
│   │
│   │ ── embedded views (composed inside other views) ──
│   ├── persistent-chat/         # used by views/chat/
│   ├── agent-settings/          # used by views/create/ + views/session-create/
│   └── agent-steps/             # used by views/jobs/ + views/chat/
│
├── debug/                       # grid host + DEBUG-ONLY widgets (UNCHANGED INTERNALLY)
│   ├── pages/debug.component.ts # /debug — grid host page
│   ├── layout/                  # split-panel, panel-header, component-host (grid scaffolding)
│   ├── components/              # widgets only meaningful inside debug:
│   │   ├── db-table/            # raw PostgreSQL viewer
│   │   ├── agent-activity/      # internal activity stream
│   │   ├── request-viewer/      # raw API request log
│   │   ├── graph-timeline/      # LangGraph internals
│   │   ├── memory-panel/        # raw memory store inspector
│   │   ├── layout-picker/       # grid meta-tool
│   │   ├── menu/                # debug menu
│   │   └── placeholders/        # demo widgets
│   ├── services/                # graph, layout, request services
│   └── *.model.ts               # graph, layout, request, layout-preset
│
├── app.config.ts
├── app.config.server.ts
├── app.routes.ts
├── app.routes.server.ts
└── app.ts
```

Folders deleted entirely after the moves: `simple/`, `shared/`, `layout/`, `core/components/` (currently empty).

## The One Rule

> A `views/<name>/` component is a self-contained screen. It is mountable two ways:
> 1. From `app.routes.ts` as a route target (sidebar pages — the simple shell).
> 2. From `app.ts`'s `registerComponents()` as a grid panel type (debug grid view).
>
> Most views support both. Some only support grid (no route yet). Some only support route (no useful grid use). Some are embedded inside other views. All four are still "views" — same folder, same shape, different mount points.

## Mental Model: Five Answers

| Adding… | Goes in |
|---------|---------|
| Design-system primitive (button variant, new control) | `ui/` |
| App-shell chrome (sidebar, header, banner, toggle) | `shell/` |
| Anything with a user-facing screen | `views/` |
| Cross-cutting service / guard / model / routing helper | `core/` |
| Debug-only widget (raw DB viewer, request log) | `debug/` |

The current tree has no good answer for half these cases. The new tree has one obvious answer for each.

## Naming Choice: `views/` vs `shared/`

The user's mental model called it a "shared directory." This document uses `views/` instead because:

- Angular convention loads "shared" with module-system baggage that doesn't apply to standalone components.
- "View" precisely names what these are: a self-contained surface that owns a screen experience.
- Keeps `shared/` available if we ever need it for actual cross-cutting utilities (unlikely under this layout, but free to reserve).

If `shared/` is preferred for clarity, the rename is mechanical. The structure underneath is what matters.

## Component Inventory and Move Map

### `shared/components/` (16 items)

Re-categorized by what they actually are:

| Component | Move to | Why |
|-----------|---------|-----|
| `agent-list` | `views/agents/` | Registered grid panel `'agent-list'`. View. |
| `agent-settings/` (7 files) | `views/agent-settings/` | Embedded in create + session-create. View (just embedded today). |
| `agent-steps` | `views/agent-steps/` | Embedded in jobs + chat. View. |
| `chat-history` | `views/chat-history/` | Registered grid panel `'agent-chat'`. View. |
| `config-editor` | `views/config-editor/` | Registered grid panel `'config-editor'`. View. |
| `datasource-list` | `views/datasources/` | Registered grid panel `'datasource-list'` AND `/datasources` route. Already serves as both. |
| `empty-catalog-banner` | `shell/empty-catalog-banner/` | App-shell banner rendered globally by `app.ts`. Chrome. |
| `instruction-builder` | `views/instruction-builder/` | Registered grid panel. View. |
| `job-create` | `views/create/` | Registered grid panel `'job-create'` AND `/create` route. Already both. |
| `job-list` | `views/jobs/` | Registered grid panel `'job-list'` AND `/jobs` route via the page wrapper. View. |
| `job-review` | `views/job-review/` | Registered grid panel `'job-review'`. View. |
| `message-redirect` | `core/routing/message-redirect/` | Pure router-redirect helper. No UI. Routing infra. |
| `notification-bell` | `shell/notification-bell/` | Lives in the sidebar. Chrome. |
| `persistent-chat` | `views/persistent-chat/` | Embedded in chat page. View (embedded). |
| `statistics` | `views/statistics/` | Registered grid panel. View. |
| `todo-list` | `views/todos/` | Registered grid panel `'todo-list'`. View. |
| `workspace-browser` | `views/workspace-browser/` | Registered grid panel. View. |

### `simple/pages/` (14 routed pages)

All move to `views/`, with the same name. No content changes.

| From | To |
|------|-----|
| `simple/pages/shell/` | `views/shell/` |
| `simple/pages/sessions/` | `views/sessions/` |
| `simple/pages/session-create/` | `views/session-create/` |
| `simple/pages/chat/` | `views/chat/` |
| `simple/pages/jobs/` | `views/jobs/` (merge with job-list) |
| `simple/pages/create/` | `views/create/` (merge with job-create) |
| `simple/pages/inbox/` | `views/inbox/` |
| `simple/pages/project-detail/` | `views/project-detail/` |
| `simple/pages/datasources/` | `views/datasources/` (merge with datasource-list) |
| `simple/pages/settings/` | `views/settings/` |
| `simple/pages/sudo/` | `views/sudo/` |
| `simple/pages/admin-providers/` | `views/admin/providers/` |
| `simple/pages/admin-models/` | `views/admin/models/` |
| `simple/pages/admin-users/` | `views/admin/users/` |

> **Page-vs-widget overlap.** Three pages today re-render their `shared/components/` counterpart inside a routing wrapper: `jobs-page` wraps `job-list`, `create-page` wraps `job-create`, `datasources-page` wraps `datasource-list`. Under the consolidated `views/` model these collapse: the view itself is what mounts in both contexts. Whether to keep the wrapper or fold it in is a per-component judgement during the move — the default is to fold unless the wrapper adds genuine routing-only behavior.

### `shared/pages/` (1 orphan)

| From | To |
|------|-----|
| `shared/pages/project-list.component.ts` | `views/projects/project-list.component.ts` |

### `layout/` and `simple/layout/`

| From | To |
|------|-----|
| `layout/sidebar/` | `shell/sidebar/` |
| `simple/layout/sidebar-toggle/` | `shell/sidebar-toggle/` |

### `debug/`

Untouched internally. Already coherent: `pages/`, `layout/`, `components/`, `services/`, top-level `*.model.ts` files.

### `core/`

Untouched, except for one addition:

| From | To |
|------|-----|
| `shared/components/message-redirect/` | `core/routing/message-redirect/` |

`core/components/` is currently empty and gets deleted.

## Sequencing

Each step is independently buildable. No half-state where the project doesn't compile.

1. **Shell chrome** — move `layout/sidebar/`, `simple/layout/sidebar-toggle/`, `shared/components/notification-bell/`, `shared/components/empty-catalog-banner/` → `shell/`. Update `app.ts` imports.
2. **Routing helper** — move `shared/components/message-redirect/` → `core/routing/message-redirect/`. Update `app.routes.ts` import.
3. **Grid-only views** — move the 8 registered-but-not-routed components from `shared/components/` → `views/<name>/`. Update `app.ts` `registerComponents()` imports.
4. **Embedded views** — move `agent-settings/`, `agent-steps/`, `persistent-chat/` → `views/`. Update consumer imports.
5. **Page-and-grid pairs** — for `job-list`/`jobs-page`, `job-create`/`create-page`, `datasource-list`/`datasources-page`: consolidate each pair into a single `views/<name>/` folder. Update `app.routes.ts` and `app.ts` imports.
6. **Remaining pages** — move `simple/pages/*` → `views/*`. Nest admin pages under `views/admin/`. Update `app.routes.ts` imports.
7. **Orphan page** — move `shared/pages/project-list.component.ts` → `views/projects/`. Update `app.ts` import.
8. **Cleanup** — delete `simple/`, `shared/`, `layout/`, `core/components/`.
9. **Verify** — `npx tsc --noEmit`, `npm test`, `npx ng build`. Manual smoke: route to each page; mount each registered grid panel in `/debug`.

Total: ~70 files moved, all import paths updated. Single PR. Reviewers see one big move-rename diff plus the import-path edits.

## Risk Register

| Risk | Mitigation |
|------|-----------|
| Import path churn introduces typos that compile-pass but fail at runtime | `tsc --noEmit` after every step + `ng build` at the end. Routes hit each page manually. |
| `ComponentRegistryService` registrations break silently (string-typed `type` keys) | After cleanup, mount each registered type in `/debug` once. List of types in `app.ts:220-344`. |
| A `.spec.ts` file gets left behind | Move spec files alongside their components. `find tests by component name` after the move to verify nothing is orphaned. |
| Page-and-grid pair consolidation (step 5) introduces behavioral changes | Examine each wrapper before folding. If it has logic beyond `<app-thing />`, keep the wrapper at the route entry point and import the inner component from the same folder. |
| Existing `tests/` (vitest) reference old import paths | Run `npm test` after each step. Update spec imports as part of the move. |
| The `core/services/component-registry.service.ts` itself doesn't move, but its consumers do | No-op — the service is consumed only by `app.ts` and `debug/`, and both update naturally. |

## What This Does Not Touch

- No changes to any component's template, styles, or behavior.
- No changes to `core/services/`, `core/models/`, `core/guards/`, `core/interceptors/`.
- No changes to `ui/` primitives.
- No changes to `debug/` internals.
- No changes to routes, route guards, or routed paths (`/sessions`, `/jobs`, etc. all stay identical).
- No changes to `ComponentRegistryService` registration types (string keys stay the same).
- No introduction of barrel `index.ts` files beyond what already exists in `ui/<primitive>/index.ts`.

## Success Criteria

- `npx tsc --noEmit` clean.
- `npm test` green.
- `npx ng build` succeeds.
- Every previously-routed path still loads its expected page.
- Every previously-registered grid panel type still mounts in `/debug`.
- The five-question mental-model table is the only thing a contributor needs to know to place a new component.
- `simple/`, `shared/`, `layout/` directories no longer exist in the tree.

## Resolved Decisions

These were open questions at draft time. All four are now locked.

1. **Page-and-grid wrapper consolidation (step 5).** **Resolved: keep both files in the same `views/<name>/` folder.** Investigation showed the wrappers (`jobs-page`, `create-page`, `datasources-page`) are byte-identical 48-line route-chrome wrappers around the actual widgets (`job-list` 1200 LOC, `job-create`, `datasource-list`). The chrome they add — `<app-sidebar-toggle />` + page padding — is route-only and would be wrong if folded into the widget (the widget also mounts as a grid panel, where sidebar-toggle has no place). The split is genuine routing chrome.
2. **`views/` vs `shared/` naming.** **Resolved: `views/`.**
3. **`agent-settings/` sub-tree.** **Resolved: stays flat.** Move all 7 files into `views/agent-settings/` as a single folder. Split only if it grows.
4. **`core/services/` split.** **Resolved: stays one folder.** All 36 services remain in `core/services/`. Splitting into api/state/infra buckets is deferred — possibly indefinitely. Marginal value, large churn.

## Follow-Up: Page Chrome Unification (Separate Feature)

Investigation during the restructure turned up a wider pattern that this PR explicitly does NOT touch but should be addressed afterward:

**Of the 14 routed pages, 13 render the sidebar-toggle in three inconsistent ways:**

| Pattern | Pages | Approach |
|---------|-------|----------|
| Wrapper file | `jobs`, `create`, `datasources` | Separate `XPageComponent` wraps `XComponent` with shared chrome |
| Inline | `admin-providers`, `admin-models`, `admin-users`, `inbox`, `session-create`, `project-detail`, `settings`, `project-list` | Renders `<app-sidebar-toggle />` directly inside its own template |
| Custom header | `sessions`, `shell` | More elaborate header chrome (split-panel + toggle) |

The three wrapper files in the first row are the proximate cause of question 1 — but the duplication is much wider. New pages have no convention to follow. The follow-up feature should:

1. Extract `shell/page-chrome/` as a single component owning sidebar-toggle + page padding + flex layout.
2. Either inline-wrap each page (`<app-page-chrome><app-jobs /></app-page-chrome>`) or use Angular route-children so the chrome is applied automatically by the router and per-page wrappers vanish entirely.
3. Standardize all 13 pages to one pattern. Delete the three wrapper files.

This is a separate PR. Doing it together with the restructure would mean a 70-file move PR plus 13 simultaneous template edits — much harder to review. Do the move first, settle the folder layout, then unify the chrome against the new structure.

## Status

All open questions resolved. Ready to execute the 9-step sequencing.
