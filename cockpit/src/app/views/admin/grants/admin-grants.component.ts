import {ChangeDetectionStrategy, Component, computed, inject, OnInit, signal} from '@angular/core';
import {FormsModule} from '@angular/forms';
import {SidebarToggleComponent} from '../../../shell/sidebar-toggle/sidebar-toggle.component';
import {AdminGrantsService, ScopeKind} from '../../../core/services/admin-grants.service';
import {AdminUsersService} from '../../../core/services/admin-users.service';
import {ApiService} from '../../../core/services/api.service';
import {GrantCatalogEntry} from '../../../core/models/api.model';

const INHERIT = '__inherit__';

/**
 * Admin → Grants panel: set/revoke capability grants per user/project/global.
 * Deny-by-default; restrict-only. Reads the catalog dynamically from the API
 * (never hardcodes the keys). Server-side admin-gated.
 */
@Component({
  selector: 'app-admin-grants',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [FormsModule, SidebarToggleComponent],
  template: `
    <div class="admin-page">
      <div class="admin-container">
        <div class="page-header">
          <app-sidebar-toggle />
          <h1 class="page-title">Capability Grants</h1>
        </div>

        <section class="admin-section">
          <p class="desc">
            Set or revoke per-user, per-project, or global capability grants.
            Deny-by-default: a capability that isn't granted is blocked at expert
            save-time and at job/session dispatch.
          </p>

          <div class="scope-bar">
            <label>Scope
              <select [ngModel]="scopeKind()" (ngModelChange)="onScopeKindChange($event)">
                <option value="user">User</option>
                <option value="project">Project</option>
                <option value="global">Global</option>
              </select>
            </label>

            @if (scopeKind() === 'user') {
              <label>User
                <select [ngModel]="scopeId()" (ngModelChange)="onScopeId($event)">
                  <option [ngValue]="null">— select —</option>
                  @for (u of users.users(); track u.id) {
                    <option [ngValue]="u.id">{{ u.display_name }} ({{ u.email }})</option>
                  }
                </select>
              </label>
            }
            @if (scopeKind() === 'project') {
              <label>Project
                <select [ngModel]="scopeId()" (ngModelChange)="onScopeId($event)">
                  <option [ngValue]="null">— select —</option>
                  @for (p of projects(); track p.id) {
                    <option [ngValue]="p.id">{{ p.name }}</option>
                  }
                </select>
              </label>
            }
            <label class="reason">Reason (optional)
              <input type="text" [ngModel]="reason()" (ngModelChange)="reason.set($event)"
                     placeholder="audit note" />
            </label>
          </div>

          @if (scopeReady()) {
            <table class="grid">
              <thead><tr><th>Capability</th><th>Default</th><th>Grant</th></tr></thead>
              <tbody>
                @for (k of catalogKeys(); track k) {
                  <tr>
                    <td><code>{{ k }}</code></td>
                    <td class="muted">{{ fmt(svc.catalog()[k].default) }}</td>
                    <td>
                      @if (svc.catalog()[k].type === 'bool') {
                        <select [ngModel]="currentValue(k)" (ngModelChange)="apply(k, $event)">
                          <option value="__inherit__">Inherit (default: {{ fmt(svc.catalog()[k].default) }})</option>
                          <option value="true">Allow</option>
                          <option value="false">Deny</option>
                        </select>
                      } @else if (svc.catalog()[k].type === 'enum') {
                        <select [ngModel]="currentValue(k)" (ngModelChange)="apply(k, $event)">
                          <option value="__inherit__">Inherit (default: {{ fmt(svc.catalog()[k].default) }})</option>
                          @for (o of svc.catalog()[k].order; track o) {
                            <option [value]="o">{{ o }}</option>
                          }
                        </select>
                      } @else {
                        <input type="text" [ngModel]="listText(k)"
                               (ngModelChange)="listDraft.set($event)"
                               (blur)="applyList(k, listDraft())"
                               placeholder="model ids, comma-separated — empty = all" />
                      }
                    </td>
                  </tr>
                }
              </tbody>
            </table>
          } @else {
            <p class="muted">Pick a scope to view and edit its grants.</p>
          }

          @if (error()) { <div class="banner err">{{ error() }}</div> }
        </section>
      </div>
    </div>
  `,
  styles: [`
    .admin-page { height: 100%; overflow-y: auto; }
    .admin-container { padding: 1rem 1.5rem; max-width: var(--content-max-width); margin: 0 auto; }
    .page-header { display: flex; align-items: center; gap: 0.5rem; margin-bottom: 1rem; }
    .page-title { margin: 0; color: var(--text-primary); }
    .admin-section {
      background: var(--panel-bg); border: 1px solid var(--border-color);
      border-radius: var(--radius-lg); padding: 1.25rem;
    }
    .desc { color: var(--text-muted); margin: 0 0 1rem; }
    .scope-bar { display: flex; flex-wrap: wrap; gap: 1rem; margin-bottom: 1rem; }
    .scope-bar label { display: flex; flex-direction: column; gap: 0.25rem; color: var(--text-muted); font-size: 0.85rem; }
    .scope-bar select, .scope-bar input, .grid select, .grid input {
      padding: 0.35rem 0.5rem; background: var(--surface-0); color: var(--text-primary);
      border: 1px solid var(--border-color); border-radius: var(--radius-surface);
    }
    .reason { flex: 1; min-width: 12rem; }
    .grid { width: 100%; border-collapse: collapse; }
    .grid th, .grid td { text-align: left; padding: 0.5rem; border-bottom: 1px solid var(--border-color); color: var(--text-primary); }
    .grid .muted, .muted { color: var(--text-muted); }
    .banner { margin-top: 1rem; padding: 0.5rem 0.75rem; border-radius: 6px; }
    .banner.err { background: var(--danger-tint); color: var(--danger); }
  `],
})
export class AdminGrantsComponent implements OnInit {
  readonly svc = inject(AdminGrantsService);
  readonly users = inject(AdminUsersService);
  private readonly api = inject(ApiService);

  scopeKind = signal<ScopeKind>('user');
  scopeId = signal<string | null>(null);
  reason = signal('');
  listDraft = signal('');
  projects = signal<{id: string; name: string}[]>([]);
  error = signal('');

  catalogKeys = computed(() => Object.keys(this.svc.catalog()));
  scopeReady = computed(() => this.scopeKind() === 'global' || !!this.scopeId());

  ngOnInit(): void {
    this.users.loadUsers();
    this.api.getProjects().subscribe((ps) =>
      this.projects.set((ps ?? []).map((p) => ({id: p.id, name: p.name}))));
    this.reload();
  }

  onScopeKindChange(kind: ScopeKind): void {
    this.scopeKind.set(kind);
    this.scopeId.set(null);
    this.reload();
  }

  onScopeId(id: string | null): void {
    this.scopeId.set(id);
    this.reload();
  }

  reload(): void {
    if (!this.scopeReady()) return;
    this.svc.load(this.scopeKind(), this.scopeId());
  }

  fmt(v: unknown): string {
    return v === null ? 'all' : String(v);
  }

  spec(key: string): GrantCatalogEntry {
    return this.svc.catalog()[key];
  }

  /** The explicitly-set value as a select-bindable string, or the INHERIT sentinel. */
  currentValue(key: string): string {
    const row = this.svc.grants().find((g) => g.key === key);
    return row ? String(row.value_json) : INHERIT;
  }

  listText(key: string): string {
    const row = this.svc.grants().find((g) => g.key === key);
    return Array.isArray(row?.value_json) ? (row!.value_json as string[]).join(', ') : '';
  }

  apply(key: string, raw: string): void {
    this.error.set('');
    const done = {
      next: () => this.svc.load(this.scopeKind(), this.scopeId()),
      error: (e: unknown) => this.error.set(this.detail(e)),
    };
    if (raw === INHERIT) {
      this.svc.deleteGrant(this.scopeKind(), this.scopeId(), key).subscribe(done);
    } else {
      const v: unknown = this.spec(key).type === 'bool' ? raw === 'true' : raw;
      this.svc.setGrant(this.scopeKind(), this.scopeId(), key, v, this.reason() || undefined).subscribe(done);
    }
  }

  applyList(key: string, raw: string): void {
    this.error.set('');
    const ids = raw.split(',').map((s) => s.trim()).filter(Boolean);
    const done = {
      next: () => this.svc.load(this.scopeKind(), this.scopeId()),
      error: (e: unknown) => this.error.set(this.detail(e)),
    };
    if (ids.length === 0) {
      this.svc.deleteGrant(this.scopeKind(), this.scopeId(), key).subscribe(done);
    } else {
      this.svc.setGrant(this.scopeKind(), this.scopeId(), key, ids, this.reason() || undefined).subscribe(done);
    }
  }

  private detail(err: unknown): string {
    const d = (err as {error?: {detail?: unknown}})?.error?.detail;
    return typeof d === 'string' ? d : 'Request failed';
  }
}
