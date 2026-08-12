# Cockpit verification gaps: the root `tsc` invocation is a no-op, and `PersistentChatComponent` cannot be mounted in a spec

**Status:** Open — both confirmed by direct experiment, no fix attempted. Neither is a product bug; both make *verification* weaker than it looks, which is worse than a visible failure.
**Found:** 2026-08-01 → 2026-08-08, during the batch-tool-approval work (`docs/superpowers/specs/2026-08-01-batch-tool-approval-design.md`).
**Severity:** Medium-High. Defect A means any task that "passed typecheck" using the documented-looking command proved nothing. Defect B silently blocks DOM-level testing of the largest, most user-facing component in the cockpit.
**Component:** `cockpit/tsconfig.json` · `cockpit/vitest` setup (`cockpit/src/test-setup.ts`) · `cockpit/src/app/views/persistent-chat/persistent-chat.component.ts`

---

## Defect A — `npx tsc --noEmit -p tsconfig.json` type-checks nothing

The root `cockpit/tsconfig.json` is a **solution-style** config: `files: []` plus project `references`. Without `-b` (build mode), `tsc` resolves zero input files and exits **0 regardless of the code**.

**Proved by experiment, twice, independently:** append `const x: number = "s";` to a real service file, then run

```bash
npx tsc --noEmit -p tsconfig.json     # exit 0 — error not detected
npx tsc --noEmit -p tsconfig.app.json # exit 2 — TS2322 as expected
```

**Use `-p tsconfig.app.json`.** It is the config that actually includes the app sources.

**Consequence:** during this work, several tasks gated on the root-config command and reported "typecheck clean". Those gates were hollow. When the correct config was finally used it immediately caught two real type errors that the hollow gate had waved through (a `decision` union that needed widening in both `persistent-chat.service.ts` and `cache.model.ts`).

**Also verified:** `strictTemplates` is on, so the real config *does* catch template binding errors (a template calling a method that doesn't exist fails the build) — which is why it is worth running.

### How widespread it is (swept 2026-08-09)

This is not a one-off mistake in one plan — it is the repo's habitual instruction:

- **5+ plan/checklist docs** tell the reader to run the no-op form, including at least one not-yet-done plan (`docs/superpowers/plans/2026-06-18-skills-slice-1.md`) and several in `docs/done/` (`2026-06-18-capability-grants-ui.md` has it as a per-task checkpoint five times). Every one of those "typecheck" gates passed vacuously.
- `cockpit/package.json` has **no `typecheck` script** at all — so there is nothing correct to copy, and everyone hand-writes the command.
- **CI has no typecheck step** (`rg "tsc|typecheck" .github/workflows/*.yml` → nothing). The only compile-time safety net is `ng build`, which runs later and is not what the checklists are invoking.

**Suggested fix (in priority order):**
1. Add `"typecheck": "tsc --noEmit -p tsconfig.app.json"` to `cockpit/package.json`, so there is one correct, discoverable command.
2. Add a CI step that runs it — right now nothing type-checks the cockpit on a PR except a full `ng build`.
3. Update the plan docs that recommend the no-op form (or at least the not-yet-executed ones).
4. Optionally make the root config fail loudly rather than silently pass.

**Related but working:** `npm run i18n:check` (parity + hardcoded-string checks) does exist and does work — verified 2026-08-09, `de-DE matches en (2467 keys)`. It is similarly undiscoverable; worth adding to the same CI step and to the frontend checklist, since a missing locale key renders the raw key string to the user.

## Defect B — `PersistentChatComponent` throws `NG0951` under vitest, so it can never be mounted

`PersistentChatComponent`'s constructor calls `this.messagesInner()` — a `viewChild.required` signal query — inside `afterNextRender`. Under this repo's vitest pipeline:

1. Components are **not** run through `ngtsc`, so `viewChild.required` never resolves → `NG0951`, even when the target element is present unconditionally in the template.
2. `afterNextRender` fires **synchronously** inside `fixture.detectChanges()` in this harness, so the crash is unconditional — independent of how complete the DI mocks are, and not avoidable via the template-override trick used by `session-create`.

Two throwaway diagnostics confirmed both points (written, run, deleted — never committed).

**Important correction to a plausible-sounding belief:** this is **not** a codebase-wide "we don't mount components" convention. 12+ cockpit specs *do* mount real components with `TestBed.createComponent` + `fixture.detectChanges()` (session-create, canvas browser/office/popout/live-app renderers, tool-card, notify-user-tool-card, memory-panel, agent-activity, layout-preview, agent-loop-diagram). It is **this component** that cannot be mounted. An existing related note lives in `contact-form.component.spec.ts`, which documented the same ngtsc gap for signal `input()`.

**Consequence:** any logic that must be unit-tested has to live in **exported pure functions** rather than component methods. That pattern is already used deliberately here — `formatPermissionArgs`, `permissionTitleKey`, `permissionApproveKey` in `persistent-chat.component.ts` exist as module-level functions purely so they are testable.

**What it cost:** the batch-approval card's central claim — that the template's `@for` renders **all N** pending calls rather than just the first — could not be unit-tested at all. It was ultimately verified by a live browser gate on k3d instead (see `docs/issues/batch_tool_approval_residuals.md`).

**Suggested fix (either):**
- Wire `@angular/build`'s test runner into `angular.json` so components go through `ngtsc`; or
- Move the `viewChild.required` deref out of the constructor's `afterNextRender` (that constructor is deliberately settled and previously fragile — treat with care).

Until then: keep component logic in exported pure functions, and treat DOM-level claims about this component as requiring a browser gate.

## Reproduce

Defect A:
```bash
cd cockpit
printf '\nconst __probe: number = "s";\n' >> src/app/core/services/persistent-chat.service.ts
npx tsc --noEmit -p tsconfig.json      # exit 0  (wrong)
npx tsc --noEmit -p tsconfig.app.json  # exit 2  (right)
git checkout -- src/app/core/services/persistent-chat.service.ts
```

Defect B: write a spec that does `TestBed.createComponent(PersistentChatComponent)` + `fixture.detectChanges()` → `NG0951`.
