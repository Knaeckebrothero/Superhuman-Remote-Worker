# Capability-Grants UI (Slice-2 fast-follow) Implementation Plan

> ## ✅ STATUS: COMPLETE — executed inline on `develop` (uncommitted) + live-verified on k3d, 2026-06-18
>
> All 8 tasks executed. **70 cockpit specs** (4 new pure-gate + 66 existing, no regression) green;
> AOT `ng build` completes (only pre-existing budget warnings); `tsc --noEmit` 0 errors. **Live on
> k3d cockpit** (`test`/`test` = admin): the Admin → Grants panel renders all 8 catalog rows with the
> correct control type each, and **set (PUT) + revoke (DELETE)** round-trip through the real API → DB →
> append-only audit (verified, left clean); the expert editor renders with the admin-bypass (no
> gating) confirmed (autonomy shows `full`, models enabled, 0 lock hints). The per-task `- [ ]`
> checkboxes below are the historical plan of record — this banner is the as-built record.
>
> **As-built notes / deviations:**
> 1. **Admin panel uses literal English strings** (not Transloco) to match the existing admin-section
>    convention (`AdminUsersComponent` etc.); only the editor lock hints use the new `grants.*` i18n keys.
> 2. **`AdminGrantsService` is Observable-based** (`.subscribe`), matching `AdminUsersService`, rather
>    than the `firstValueFrom`/async draft in this plan.
> 3. **`browser_direct` is included** in the tools-group gate map (correct + free: the resolved-grants
>    record carries `browser`'s effective value, so it only blocks when explicitly denied) — slightly
>    exceeds the "defer browser greying" scope without risk.
> 4. **`vm_workspace` greying** is left to the accordion's pre-existing `canUseVm` (already grant-aware
>    via the backend dual-read) — not re-wired.
> 5. **Not driven live:** the non-admin greying *visual* (needs a non-admin Keycloak session; `test` is
>    admin). The gate logic (4 vitest) + the `[disabled]`/filtered-`@for` bindings (AOT) cover it.
> 6. **Dev-test mechanics:** cockpit at `https://localhost` (traefik), Keycloak `test`/`test`; the
>    `markLoaded … classList` console error is a pre-existing index.html splash-shim race, benign.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Commits are the user's responsibility.** This user handles all commits/pushes. Every "Checkpoint" means *stop, confirm tests green, leave changes staged* — do NOT run `git commit`/`git push`.

**Goal:** Add the two deferred Slice-2 UI pieces — an **Admin → Grants** management panel (set/revoke capability grants per user/project/global) and **editor control-greying** (disable expert-editor controls the author lacks grants for, with a lock hint) — on top of the already-shipped enforcement backend.

**Architecture:** Pure gate logic in a tiny testable `capability-gates.ts`; an `AdminGrantsService` (signals) mirroring `AdminUsersService` over the live `/api/admin/grants` CRUD; a new `AdminGrantsComponent` at `/admin/grants`; and an opt-in `gatedCapabilities` input threaded into the three agent-settings group components (default empty ⇒ launch flow unaffected), fed by `GET /api/users/me/capabilities` in the expert editor.

**Tech Stack:** Angular 21 (standalone components, signals, Transloco i18n), vitest (pure logic + component specs), Playwright (live browser). Backend endpoints already ship (Slice-2): `GET /api/users/me/capabilities`, `GET/PUT/DELETE /api/admin/grants/...`.

---

## Context & resolved design questions

- **Backend is done + live-verified** (commit `a7ad2be0`). This slice is UI-only — no orchestrator changes. The catalog (`src/core/capability_grants.py` `CATALOG`) is delivered by both endpoints, so the UI reads it dynamically (never hardcodes the 8 keys).
- **Spec:** `docs/features/global_expert_management.md` Slice 3 — "greyed ungated controls fed by `/api/users/me/capabilities`, grant-fed model picker, admin-users grant toggle." This plan realizes exactly that, scoped to the two deferred pieces.
- **Catalog shape (delivered by the API):** `{ "<key>": { type: 'bool'|'enum'|'list', default: any, restrict_only: true, order?: string[] } }`. 8 keys: `vm_workspace, shell_tools, delegation, datasource_tools, browser, model_selection, autonomy_ceiling, permission_mode`.
- **`/api/users/me/capabilities` →** `{ is_admin: boolean, grants: Record<key, value>|null, catalog }`. `grants===null` ⇒ admin (unrestricted). For a non-admin, `grants` is the fully-resolved effective set (every catalog key present).
- **`GET /api/admin/grants?scope_kind=&scope_id=` →** `{ grants: [{ key, value_json, granted_by, updated_at }], catalog }` — only the **explicitly-set** rows for that one scope (NOT resolved/inherited). Absence of a key ⇒ inherits.
- **v1 greying scope (deliberate):** gate the **high-signal deny-default** controls — `shell_tools`, `delegation` (tools-group toggles), `autonomy_ceiling`, `permission_mode` (execution-group selects, by **filtering options** above the ceiling), and `model_selection` (the editor's inline model `<select>`s, only when restricted). **Already handled / deferred:** `vm_workspace` greying is already done by the accordion's existing `canUseVm` computed (reads `user.can_use_vm`, which the backend dual-reads against the grant) — leave it; `browser` + `datasource_tools` are allow-by-default and low-signal — defer (note in the lock-legend). This keeps the blast radius on the shared launch-flow groups small.
- **Launch-flow safety:** the new group input `gatedCapabilities` defaults to an **empty set ⇒ no gating**, so job/session create render byte-identically. Only the expert editor passes a non-empty value.
- **Admin-panel value semantics (tri-state):** restrict-only deny-default means a grant row is "set or inherit". The control offers **Inherit (default: X)** → `DELETE`, vs an explicit value → `PUT`. This avoids confusing "off vs unset".

## File Structure

**New files:**
- `cockpit/src/app/views/agent-settings/capability-gates.ts` — pure: `hasGrant`, `allowedEnumOptions`, `isModelAllowed`, `gateReason`. No Angular imports.
- `cockpit/src/app/views/agent-settings/capability-gates.spec.ts` — vitest for the above.
- `cockpit/src/app/core/services/admin-grants.service.ts` — admin CRUD over `/api/admin/grants` (signals).
- `cockpit/src/app/views/admin/grants/admin-grants.component.ts` — the Admin → Grants panel.

**Modified files:**
- `cockpit/src/app/core/models/api.model.ts` — `GrantCatalogEntry`, `UserCapabilities`, `Grant`, `GrantListResponse`.
- `cockpit/src/app/core/services/api.service.ts` — `getMyCapabilities()`.
- `cockpit/src/app/app.routes.ts` — `/admin/grants` route.
- `cockpit/src/app/shell/sidebar/sidebar.component.ts` — admin grants nav link.
- `cockpit/src/app/views/experts/expert-editor.component.ts` — fetch capabilities, gate inline model selects, pass `gatedCapabilities` to groups.
- `cockpit/src/app/views/agent-settings/tools-group.component.ts` — opt-in per-toggle gating (shell/delegation).
- `cockpit/src/app/views/agent-settings/execution-group.component.ts` — opt-in enum-option filtering (autonomy/permission).
- `cockpit/src/assets/i18n/en.json` — `grants` namespace + editor lock hints + nav string.

## Test commands
- Pure + component specs: `cd cockpit && npx vitest run src/app/views/agent-settings/capability-gates.spec.ts src/app/views/admin/grants/ src/app/views/experts/`
- Build (AOT + budget): `cd cockpit && npm install --no-save @monaco-editor/loader && npx ng build`
- Live: Playwright vs dev cockpit (login `test`/`test`, an admin in dev).

---

### Task 1: Models

**Files:** Modify `cockpit/src/app/core/models/api.model.ts`.

- [ ] **Step 1: Add the grant types** (append near the `User`/`Expert` interfaces):

```typescript
/** One entry of the capability catalog (delivered by both grant endpoints). */
export interface GrantCatalogEntry {
  type: 'bool' | 'enum' | 'list';
  default: unknown;
  restrict_only: boolean;
  order?: string[];
}

export type GrantCatalog = Record<string, GrantCatalogEntry>;

/** GET /api/users/me/capabilities */
export interface UserCapabilities {
  is_admin: boolean;
  grants: Record<string, unknown> | null; // null ⇒ admin (unrestricted)
  catalog: GrantCatalog;
}

/** One explicitly-set grant row (GET /api/admin/grants). */
export interface Grant {
  key: string;
  value_json: unknown;
  granted_by: string | null;
  updated_at: string;
}

/** GET /api/admin/grants?scope_kind=&scope_id= */
export interface GrantListResponse {
  grants: Grant[];
  catalog: GrantCatalog;
}
```

- [ ] **Step 2: Typecheck.** Run: `cd cockpit && npx tsc --noEmit -p tsconfig.json`  Expected: no new errors.
- [ ] **Step 3: Checkpoint.**

---

### Task 2: ApiService.getMyCapabilities() + AdminGrantsService

**Files:** Modify `cockpit/src/app/core/services/api.service.ts`; create `cockpit/src/app/core/services/admin-grants.service.ts`.

- [ ] **Step 1: Add `getMyCapabilities()` to `ApiService`** (mirror the existing `getAgents()` catchError shape):

```typescript
  /** Current user's resolved capabilities + the catalog (drives editor greying). */
  getMyCapabilities(): Observable<UserCapabilities | null> {
    return this.http
      .get<UserCapabilities>(`${this.baseUrl}/users/me/capabilities`)
      .pipe(catchError(() => of(null)));
  }
```
Add `UserCapabilities` to the existing `api.model` import block at the top of the file.

- [ ] **Step 2: Create `AdminGrantsService`** (mirror `AdminUsersService` — signals + load/set/delete):

```typescript
import { Injectable, inject, signal } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { environment } from '../../../environments/environment';
import type { Grant, GrantCatalog, GrantListResponse } from '../models/api.model';

export type ScopeKind = 'user' | 'project' | 'global';

@Injectable({ providedIn: 'root' })
export class AdminGrantsService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.apiUrl;

  readonly grants = signal<Grant[]>([]);
  readonly catalog = signal<GrantCatalog>({});
  readonly loading = signal(false);

  async load(scopeKind: ScopeKind, scopeId: string | null): Promise<void> {
    this.loading.set(true);
    let params = new HttpParams().set('scope_kind', scopeKind);
    if (scopeKind !== 'global' && scopeId) params = params.set('scope_id', scopeId);
    try {
      const res = await firstValueFrom(
        this.http.get<GrantListResponse>(`${this.baseUrl}/admin/grants`, { params }),
      );
      this.grants.set(res?.grants ?? []);
      this.catalog.set(res?.catalog ?? {});
    } finally {
      this.loading.set(false);
    }
  }

  setGrant(
    scopeKind: ScopeKind, scopeId: string | null, key: string,
    valueJson: unknown, reason?: string,
  ): Promise<unknown> {
    const sid = scopeKind === 'global' ? 'global' : scopeId;
    return firstValueFrom(
      this.http.put(`${this.baseUrl}/admin/grants/${scopeKind}/${sid}/${key}`,
        { value_json: valueJson, reason: reason ?? null }),
    );
  }

  deleteGrant(scopeKind: ScopeKind, scopeId: string | null, key: string): Promise<unknown> {
    const sid = scopeKind === 'global' ? 'global' : scopeId;
    return firstValueFrom(
      this.http.delete(`${this.baseUrl}/admin/grants/${scopeKind}/${sid}/${key}`),
    );
  }
}
```
(The backend ignores the path `scope_id` for `global` — passing the literal `"global"` is safe and keeps the route shape.)

- [ ] **Step 3: Typecheck + checkpoint.** Run: `cd cockpit && npx tsc --noEmit -p tsconfig.json`

---

### Task 3: [TDD] Pure capability-gate helpers

**Files:** Create `cockpit/src/app/views/agent-settings/capability-gates.ts`; test `capability-gates.spec.ts`.

- [ ] **Step 1: Write the failing spec:**

```typescript
import { describe, it, expect } from 'vitest';
import { hasGrant, allowedEnumOptions, isModelAllowed } from './capability-gates';

const CAT = {
  shell_tools: { type: 'bool', default: false, restrict_only: true },
  autonomy_ceiling: { type: 'enum', default: 'review', restrict_only: true,
    order: ['dependent', 'guided', 'partial', 'review', 'full'] },
  model_selection: { type: 'list', default: null, restrict_only: true },
} as const;

describe('capability-gates', () => {
  it('hasGrant: admin (null grants) is always granted', () => {
    expect(hasGrant(null, 'shell_tools')).toBe(true);
  });
  it('hasGrant: bool key reads the resolved value', () => {
    expect(hasGrant({ shell_tools: true }, 'shell_tools')).toBe(true);
    expect(hasGrant({ shell_tools: false }, 'shell_tools')).toBe(false);
    expect(hasGrant({}, 'shell_tools')).toBe(false); // absent ⇒ denied
  });
  it('allowedEnumOptions: filters options above the ceiling', () => {
    const all = CAT.autonomy_ceiling.order as string[];
    expect(allowedEnumOptions(null, 'autonomy_ceiling', all, CAT)).toEqual(all); // admin: all
    expect(allowedEnumOptions({ autonomy_ceiling: 'review' }, 'autonomy_ceiling', all, CAT))
      .toEqual(['dependent', 'guided', 'partial', 'review']); // 'full' dropped
  });
  it('isModelAllowed: null selection ⇒ all allowed', () => {
    expect(isModelAllowed(null, 'gpt-x')).toBe(true);
    expect(isModelAllowed({ model_selection: null }, 'gpt-x')).toBe(true);
    expect(isModelAllowed({ model_selection: ['a', 'b'] }, 'a')).toBe(true);
    expect(isModelAllowed({ model_selection: ['a', 'b'] }, 'c')).toBe(false);
  });
});
```

- [ ] **Step 2: Run to verify failure.** Run: `cd cockpit && npx vitest run src/app/views/agent-settings/capability-gates.spec.ts`  Expected: FAIL (module not found).

- [ ] **Step 3: Implement `capability-gates.ts`:**

```typescript
/** Pure capability-gate helpers for editor control-greying. `grants === null`
 * means the caller is an admin (unrestricted) — every gate is open. No Angular
 * imports so it is unit-testable in isolation. Mirrors the backend PDP semantics
 * (src/core/capability_grants.py): restrict-only, deny-by-default for bools. */
import type { GrantCatalog } from '../../core/models/api.model';

type Grants = Record<string, unknown> | null;

/** True if a bool capability is granted (admin ⇒ always; absent ⇒ denied). */
export function hasGrant(grants: Grants, key: string): boolean {
  if (grants === null) return true;
  return grants[key] === true;
}

/** Enum options at or below the granted ceiling (admin ⇒ all). */
export function allowedEnumOptions(
  grants: Grants, key: string, all: string[], catalog: GrantCatalog,
): string[] {
  if (grants === null) return all;
  const order = catalog[key]?.order ?? all;
  const ceiling = grants[key];
  if (typeof ceiling !== 'string' || !order.includes(ceiling)) return all;
  const max = order.indexOf(ceiling);
  return all.filter((o) => !order.includes(o) || order.indexOf(o) <= max);
}

/** True if `model` is permitted by the model_selection grant (null/admin ⇒ all). */
export function isModelAllowed(grants: Grants, model: string): boolean {
  if (grants === null) return true;
  const sel = grants['model_selection'];
  if (sel == null) return true;
  return Array.isArray(sel) ? sel.includes(model) : true;
}

/** i18n key for the lock hint on a gated control ('' ⇒ not gated). */
export function gateReason(grants: Grants, key: string): string {
  return hasGrant(grants, key) ? '' : `grants.locked.${key}`;
}
```

- [ ] **Step 4: Run to verify pass.** Expected: PASS.
- [ ] **Step 5: Checkpoint.**

---

### Task 4: Admin → Grants panel + route + sidebar + i18n

**Files:** Create `cockpit/src/app/views/admin/grants/admin-grants.component.ts`; modify `app.routes.ts`, `shell/sidebar/sidebar.component.ts`, `assets/i18n/en.json`.

- [ ] **Step 1: Create `AdminGrantsComponent`** — scope selector (user/project/global) + a row per catalog key with a tri-state control. Mirror `AdminUsersComponent`'s section/grid/signal style. Key shape:

```typescript
import { Component, computed, inject, OnInit, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { TranslocoPipe } from '@jsverse/transloco';
import { AdminGrantsService, ScopeKind } from '../../../core/services/admin-grants.service';
import { AdminUsersService } from '../../../core/services/admin-users.service';
import { ApiService } from '../../../core/services/api.service';
import type { GrantCatalogEntry } from '../../../core/models/api.model';

const INHERIT = '__inherit__';

@Component({
  selector: 'app-admin-grants',
  standalone: true,
  imports: [FormsModule, TranslocoPipe],
  template: `
    <section class="admin-section">
      <header class="head">
        <h1>{{ 'grants.title' | transloco }}</h1>
        <p class="desc">{{ 'grants.description' | transloco }}</p>
      </header>

      <div class="scope-bar">
        <label>{{ 'grants.scope' | transloco }}
          <select [(ngModel)]="scopeKind" (ngModelChange)="onScopeKindChange()">
            <option value="user">{{ 'grants.scopeUser' | transloco }}</option>
            <option value="project">{{ 'grants.scopeProject' | transloco }}</option>
            <option value="global">{{ 'grants.scopeGlobal' | transloco }}</option>
          </select>
        </label>
        @if (scopeKind() === 'user') {
          <label>{{ 'grants.user' | transloco }}
            <select [(ngModel)]="scopeId" (ngModelChange)="reload()">
              <option [ngValue]="null">—</option>
              @for (u of users.users(); track u.id) {
                <option [ngValue]="u.id">{{ u.display_name }} ({{ u.email }})</option>
              }
            </select>
          </label>
        }
        @if (scopeKind() === 'project') {
          <label>{{ 'grants.project' | transloco }}
            <select [(ngModel)]="scopeId" (ngModelChange)="reload()">
              <option [ngValue]="null">—</option>
              @for (p of projects(); track p.id) {
                <option [ngValue]="p.id">{{ p.name }}</option>
              }
            </select>
          </label>
        }
        <label class="reason">{{ 'grants.reason' | transloco }}
          <input type="text" [(ngModel)]="reason" placeholder="optional audit note" />
        </label>
      </div>

      @if (scopeReady()) {
        <table class="grid">
          <thead><tr>
            <th>{{ 'grants.colKey' | transloco }}</th>
            <th>{{ 'grants.colDefault' | transloco }}</th>
            <th>{{ 'grants.colValue' | transloco }}</th>
          </tr></thead>
          <tbody>
            @for (k of catalogKeys(); track k) {
              <tr>
                <td><code>{{ k }}</code></td>
                <td>{{ fmt(svc.catalog()[k].default) }}</td>
                <td>{{ renderControl(k) }}</td>
              </tr>
            }
          </tbody>
        </table>
      } @else {
        <p class="empty">{{ 'grants.pickScope' | transloco }}</p>
      }

      @if (error()) { <div class="banner err">{{ error() }}</div> }
    </section>
  `,
  styles: [`/* reuse admin-users section/grid tokens: --panel-bg, --border-color, --text-primary */`],
})
export class AdminGrantsComponent implements OnInit {
  readonly svc = inject(AdminGrantsService);
  readonly users = inject(AdminUsersService);
  private readonly api = inject(ApiService);

  scopeKind = signal<ScopeKind>('user');
  scopeId = signal<string | null>(null);
  reason = signal('');
  projects = signal<{ id: string; name: string }[]>([]);
  error = signal('');

  catalogKeys = computed(() => Object.keys(this.svc.catalog()));
  scopeReady = computed(() => this.scopeKind() === 'global' || !!this.scopeId());

  ngOnInit(): void {
    this.users.loadUsers();
    this.api.getProjects().subscribe((ps) =>
      this.projects.set((ps ?? []).map((p) => ({ id: p.id, name: p.name }))));
    this.reload();
  }

  onScopeKindChange(): void { this.scopeId.set(null); this.reload(); }

  reload(): void {
    if (!this.scopeReady()) return;
    this.svc.load(this.scopeKind(), this.scopeId()).catch((e) => this.error.set(String(e)));
  }

  /** current explicitly-set value for a key, or the INHERIT sentinel. */
  currentValue(key: string): string {
    const row = this.svc.grants().find((g) => g.key === key);
    if (!row) return INHERIT;
    return String(row.value_json);
  }

  // renderControl is implemented in the template via a child <select>; see Step 1b.
  renderControl(key: string): string { return key; } // placeholder removed in 1b
  fmt(v: unknown): string { return v === null ? 'all' : String(v); }
}
```

- [ ] **Step 1b: Replace the value cell with a real tri-state control.** Swap the `<td>{{ renderControl(k) }}</td>` for an inline control bound per type. Add this template fragment + handler (boolean shown; enum uses `spec.order`, list uses a text input):

```html
<td>
  @if (spec(k).type === 'bool') {
    <select [ngModel]="currentValue(k)" (ngModelChange)="apply(k, $event)">
      <option value="__inherit__">{{ 'grants.inherit' | transloco }} ({{ fmt(spec(k).default) }})</option>
      <option value="true">{{ 'grants.allow' | transloco }}</option>
      <option value="false">{{ 'grants.deny' | transloco }}</option>
    </select>
  } @else if (spec(k).type === 'enum') {
    <select [ngModel]="currentValue(k)" (ngModelChange)="apply(k, $event)">
      <option value="__inherit__">{{ 'grants.inherit' | transloco }} ({{ fmt(spec(k).default) }})</option>
      @for (o of spec(k).order; track o) { <option [value]="o">{{ o }}</option> }
    </select>
  } @else {
    <input type="text" [ngModel]="listText(k)" (blur)="applyList(k, $any($event.target).value)"
           placeholder="model ids, comma-separated — empty = all" />
  }
</td>
```

```typescript
  spec(key: string): GrantCatalogEntry { return this.svc.catalog()[key]; }
  listText(key: string): string {
    const row = this.svc.grants().find((g) => g.key === key);
    return Array.isArray(row?.value_json) ? (row!.value_json as string[]).join(', ') : '';
  }
  async apply(key: string, raw: string): Promise<void> {
    this.error.set('');
    try {
      if (raw === INHERIT) { await this.svc.deleteGrant(this.scopeKind(), this.scopeId(), key); }
      else {
        const t = this.spec(key).type;
        const v: unknown = t === 'bool' ? raw === 'true' : raw;
        await this.svc.setGrant(this.scopeKind(), this.scopeId(), key, v, this.reason() || undefined);
      }
      await this.svc.load(this.scopeKind(), this.scopeId());
    } catch (e) { this.error.set(String(e)); }
  }
  async applyList(key: string, raw: string): Promise<void> {
    this.error.set('');
    const ids = raw.split(',').map((s) => s.trim()).filter(Boolean);
    try {
      if (ids.length === 0) await this.svc.deleteGrant(this.scopeKind(), this.scopeId(), key);
      else await this.svc.setGrant(this.scopeKind(), this.scopeId(), key, ids, this.reason() || undefined);
      await this.svc.load(this.scopeKind(), this.scopeId());
    } catch (e) { this.error.set(String(e)); }
  }
```
Delete the `renderControl` placeholder. (If `ApiService.listProjects` doesn't exist, drop the project-scope `<select>` population to a no-op for v1 and surface a free-text project-id input instead — verify the method name in Task 4 Step 2.)

- [ ] **Step 2:** (`ApiService.getProjects(userId?): Observable<Project[]>` is confirmed to exist — Task 4 Step 1 uses it. `Project` is already exported from `api.model`.)

- [ ] **Step 3: Register the route** in `cockpit/src/app/app.routes.ts` (verified: admin entries use **eager** `component:` imports + `canActivate: [authGuard, adminGuard]`, both imported at the top). Add the import near the other admin imports:
```typescript
import {AdminGrantsComponent} from './views/admin/grants/admin-grants.component';
```
and the route after the `admin/config` line:
```typescript
  { path: 'admin/grants', component: AdminGrantsComponent, canActivate: [authGuard, adminGuard] },
```

- [ ] **Step 4: Add the sidebar link** in `cockpit/src/app/shell/sidebar/sidebar.component.ts`, inside the existing `@if (userService.currentUser()?.is_admin) { … }` admin block, mirroring the `admin/users` link:

```html
<a class="nav-link" routerLink="/admin/grants" routerLinkActive="active">
  <app-icon size="md" class="nav-icon">verified_user</app-icon>
  {{ 'nav.adminGrants' | transloco }}
</a>
```

- [ ] **Step 5: Add i18n** (Task 7 holds the full block; add at least `grants.*` + `nav.adminGrants` now so the panel renders).
- [ ] **Step 6: Checkpoint.** Run: `cd cockpit && npx tsc --noEmit -p tsconfig.json`

---

### Task 5: Editor — fetch capabilities + gate inline model selects

**Files:** Modify `cockpit/src/app/views/experts/expert-editor.component.ts`.

- [ ] **Step 1: Fetch capabilities on init + expose helpers.** Add to the class:

```typescript
  capabilities = signal<Record<string, unknown> | null | undefined>(undefined); // undefined=loading
  // grants===null ⇒ admin (no gating). undefined ⇒ not loaded yet ⇒ no gating.
  gatedCapabilities = computed(() => this.capabilities() ?? null);
  isModelGated = computed(() => {
    const g = this.capabilities();
    return g != null && Array.isArray((g as Record<string, unknown>)['model_selection']);
  });
```
In `ngOnInit`, alongside the existing `modelService.load()` / defaults fetch:
```typescript
    this.api.getMyCapabilities().subscribe((c) =>
      this.capabilities.set(c ? c.grants : null));
```
Import `getMyCapabilities` is already on `ApiService` (Task 2).

- [ ] **Step 2: Gate the three inline model `<select>`s.** For each (`strategicModel`, `tacticalModel`, `sessionModel`), add `[disabled]` + a lock hint. Example (strategic):

```html
<select class="model-select" [ngModel]="form.strategicModel" [disabled]="isModelGated()" ...>
  @for (m of modelService.models(); track m.id) {
    <option [value]="m.id" [disabled]="!modelAllowed(m.id)">{{ m.id }}</option>
  }
</select>
@if (isModelGated()) { <small class="lock-hint">{{ 'grants.locked.model_selection' | transloco }}</small> }
```
Add the helper: `modelAllowed = (id: string) => isModelAllowed(this.capabilities() ?? null, id);` (import `isModelAllowed` from `../agent-settings/capability-gates`).

- [ ] **Step 3: Thread `gatedCapabilities` into the groups.** On the three group bindings, add the input (Task 6 defines it):

```html
<app-execution-group ... [gatedCapabilities]="gatedCapabilities()" [catalog]="catalog()" />
<app-tools-group ... [gatedCapabilities]="gatedCapabilities()" />
<app-advanced-accordion ... [gatedCapabilities]="gatedCapabilities()" [catalog]="catalog()" />
```
Store the catalog from the capabilities call too: `catalog = signal<GrantCatalog>({})` and set it in the subscribe (`this.catalog.set(c?.catalog ?? {})`).

- [ ] **Step 4: Typecheck + checkpoint.** Run: `cd cockpit && npx tsc --noEmit -p tsconfig.json`

---

### Task 6: Group components — opt-in per-control gating

**Files:** Modify `tools-group.component.ts`, `execution-group.component.ts`. (advanced-accordion already gates VM via `canUseVm`; its other controls are deferred — accept the input but only use it if trivial.)

**Default empty ⇒ no gating** (launch flow unchanged).

- [ ] **Step 1: tools-group — add inputs + gate shell/delegation toggles.** Add:

```typescript
  gatedCapabilities = input<Record<string, unknown> | null>(null); // null ⇒ no gating
```
Add a helper mapping the two relevant categories to catalog keys and computing blocked state:
```typescript
  private readonly CAT_TO_GRANT: Record<string, string> = { shell: 'shell_tools', delegation: 'delegation' };
  isCategoryBlocked(catKey: string): boolean {
    const g = this.gatedCapabilities();
    if (g === null) return false;            // no gating context (launch flow / admin)
    const grantKey = this.CAT_TO_GRANT[catKey];
    return !!grantKey && g[grantKey] !== true; // deny-default
  }
```
In the category `@for` template, extend the checkbox disable + add a lock class/hint:
```html
<label class="tool-toggle" [class.disabled]="disabled() || isCategoryBlocked(cat.key)">
  <input type="checkbox" [disabled]="disabled() || isCategoryBlocked(cat.key)" ... />
  ...
  @if (isCategoryBlocked(cat.key)) { <span class="lock" title="{{ 'grants.lockedShort' | transloco }}">🔒</span> }
</label>
```
**Important:** gating only *disables*; it must not change emitted overrides (a blocked category keeps whatever the config already had — never auto-toggle, to avoid corrupting an admin-authored fragment opened by a lesser user).

- [ ] **Step 2: execution-group — add input + filter enum options.** Add the same `gatedCapabilities = input<Record<string, unknown> | null>(null);` plus `catalog = input<GrantCatalog>({});`. For the autonomy select (job) and permission-mode select (session), filter the rendered options through `allowedEnumOptions`:

```typescript
  import { allowedEnumOptions } from './capability-gates';
  autonomyOptions = computed(() =>
    allowedEnumOptions(this.gatedCapabilities(), 'autonomy_ceiling',
      ['dependent','guided','partial','review','full'], this.catalog()));
  permissionOptions = computed(() =>
    allowedEnumOptions(this.gatedCapabilities(), 'permission_mode',
      ['supervised','auto_accept','autonomous'], this.catalog()));
```
Render the selects from these computed lists instead of hardcoded `@for`/options. Keep the currently-selected value visible even if it exceeds the ceiling (an admin-authored expert may pin a higher value) — append it if missing so the control never blanks:
```typescript
  private withCurrent(opts: string[], current: string | null): string[] {
    return current && !opts.includes(current) ? [...opts, current] : opts;
  }
```

- [ ] **Step 3: Typecheck.** Run: `cd cockpit && npx tsc --noEmit -p tsconfig.json`
- [ ] **Step 4: Confirm launch flow is unaffected.** Grep the launch-flow host (`agent-settings.component.ts`) — it must NOT pass `gatedCapabilities`, so the input stays `null` ⇒ no gating. Run: `cd cockpit && grep -n "gatedCapabilities" src/app/views/agent-settings/agent-settings.component.ts` (expect: no matches).
- [ ] **Step 5: Checkpoint.**

---

### Task 7: i18n strings

**Files:** Modify `cockpit/src/assets/i18n/en.json`.

- [ ] **Step 1: Add a `grants` namespace + `nav.adminGrants`:**

```json
"grants": {
  "title": "Capability Grants",
  "description": "Set or revoke per-user, per-project, or global capability grants. Deny-by-default: a capability not granted is blocked at save and dispatch.",
  "scope": "Scope", "scopeUser": "User", "scopeProject": "Project", "scopeGlobal": "Global",
  "user": "User", "project": "Project", "reason": "Reason",
  "pickScope": "Pick a scope to view its grants.",
  "colKey": "Capability", "colDefault": "Default", "colValue": "Grant",
  "inherit": "Inherit", "allow": "Allow", "deny": "Deny",
  "lockedShort": "Requires a capability grant",
  "locked": {
    "vm_workspace": "VM workspaces require the vm_workspace grant.",
    "shell_tools": "Shell tools require the shell_tools grant.",
    "delegation": "Delegation requires the delegation grant.",
    "datasource_tools": "Datasource tools require the datasource_tools grant.",
    "browser": "Browser tools require the browser grant.",
    "model_selection": "Your administrator limits which models you may select.",
    "autonomy_ceiling": "Autonomy above your ceiling requires a grant.",
    "permission_mode": "This permission mode requires a grant."
  }
}
```
And add `"adminGrants": "Admin · Grants"` to the existing `nav` namespace.

- [ ] **Step 2: Validate JSON.** Run: `cd cockpit && node -e "JSON.parse(require('fs').readFileSync('src/assets/i18n/en.json','utf8')); console.log('ok')"`  Expected: `ok`
- [ ] **Step 3: Checkpoint.**

---

### Task 8: Verification (unit + build + live)

**Files:** none (verification).

- [ ] **Step 1: Unit.** Run: `cd cockpit && npx vitest run src/app/views/agent-settings/capability-gates.spec.ts`  Expected: PASS. Then the existing editor/agent-settings specs to confirm no regression: `npx vitest run src/app/views/experts/ src/app/views/agent-settings/`.

- [ ] **Step 2: Build + budget.** Run: `cd cockpit && npm install --no-save @monaco-editor/loader && npx ng build`  Expected: build OK, no new `anyComponentStyle` (32 kB) budget error. If the admin-grants styles trip the budget, move shared admin styles to a class on the global stylesheet.

- [ ] **Step 3: Live (Playwright vs dev cockpit, admin user).**
  1. Navigate `/admin/grants`. Scope=User → pick the test user. The 8 catalog rows render with defaults; the grandfathered `shell_tools`/`delegation` show their set value, others show **Inherit**.
  2. Set `vm_workspace` → **Allow**; reload the row → it persists. Set it back to **Inherit** → the row clears (DELETE). (Cross-check a `capability_grant_audit` row was written.)
  3. Set `autonomy_ceiling` → `guided`; set `model_selection` → `gpt-4o, claude-3` then clear it.
  4. As a **non-admin** user (no shell/delegation grant) open `/experts/new`: the **Shell** and **Delegation** tool toggles are disabled with a 🔒; the **Autonomy** dropdown omits options above the ceiling; if a model restriction is set, the model selects are disabled with the lock hint. Admin user sees no gating.
  5. Confirm a non-admin can still **save** a within-grants expert (the editor greying and the save-time 422 agree).

- [ ] **Step 4: Final checkpoint.** Report results; leave staged for the user.

---

## Self-Review

**Spec coverage:** "admin-users grant toggle" → Task 4 (Admin → Grants panel, all scopes); "greyed ungated controls fed by `/api/users/me/capabilities`" → Tasks 5–6; "grant-fed model picker" → Task 5 Step 2 (per-option + select disable). Deferred-by-design (noted): `browser`/`datasource_tools` greying, launch-flow greying, a richer model multiselect.

**Type consistency:** `UserCapabilities.grants: Record<string,unknown>|null`; `hasGrant(grants, key)`, `allowedEnumOptions(grants, key, all, catalog)`, `isModelAllowed(grants, model)` — identical signatures across Tasks 3/5/6. `gatedCapabilities = input<Record<string,unknown>|null>(null)` on both groups; `null ⇒ no gating` everywhere. `ScopeKind = 'user'|'project'|'global'` shared by service + component.

**Risk / blast radius:** the only shared-component edits are tools-group + execution-group, both behind a `null`-default input → launch flow byte-identical (Task 6 Step 4 asserts it). Greying never mutates the fragment (disable-only) so opening an admin-authored expert as a lesser user can't silently strip pinned values — it just can't *edit* the gated ones; save-time 422 remains the backstop.
