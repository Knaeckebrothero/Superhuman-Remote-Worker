import {Component, computed, effect, inject, input, output, signal} from '@angular/core';
import {TranslocoPipe} from '@jsverse/transloco';
import {AppIconComponent} from '../../ui/icon';
import {AppSpinnerComponent} from '../../ui/spinner';
import {ApiService} from '../../core/services/api.service';
import {Datasource, DatasourceIndexStatus, DatasourceType} from '../../core/models/api.model';

/** A user's explicit picker selection, tagged with the datasource-set identity
 *  it was made against (so a stale tag falls back to the default). */
export type DatasourceSelection = {key: string; ids: Set<string>} | null;

export function isRepositoryDatasource(type: DatasourceType | string): boolean {
  return (type || '').toString().toLowerCase() === 'repository';
}

/** Stable identity of a datasource set (order-independent). */
export function datasourceSetKey(datasources: {id: string}[]): string {
  return datasources
    .map(d => d.id)
    .sort()
    .join(',');
}

/** Active selection: the user's tagged choice, or the default when the
 *  selection is untouched (null) or stale (made against a different datasource
 *  set). The default is all ids — except when `defaultIds` is provided (live
 *  mode: the session's currently ATTACHED set, possibly empty). */
export function activeDatasourceIds(
  datasources: {id: string}[],
  selection: DatasourceSelection,
  defaultIds?: Set<string> | null,
): Set<string> {
  if (selection && selection.key === datasourceSetKey(datasources)) {
    return selection.ids;
  }
  if (defaultIds) {
    return new Set(datasources.filter(d => defaultIds.has(d.id)).map(d => d.id));
  }
  return new Set(datasources.map(d => d.id));
}

/** Selected datasource IDs to submit: the active set, minus clone-based
 *  repository sources disabled by a lite backend and stale ids. Centrally
 *  indexed `kb` datasources remain available on every tier. */
export function selectedDatasourceIds(
  datasources: Datasource[],
  selection: DatasourceSelection,
  isLiteBackend: boolean,
  defaultIds?: Set<string> | null,
): string[] {
  const active = activeDatasourceIds(datasources, selection, defaultIds);
  return datasources
    .filter(d => active.has(d.id) && !(isLiteBackend && isRepositoryDatasource(d.type)))
    .map(d => d.id);
}

/** True when every selectable datasource (i.e. not lite-excluded and not
 *  locked) is selected. */
export function allDatasourcesSelected(
  datasources: Datasource[],
  selection: DatasourceSelection,
  isLiteBackend: boolean,
  defaultIds?: Set<string> | null,
  lockedIds?: string[],
): boolean {
  const locked = new Set(lockedIds ?? []);
  const selectable = datasources.filter(
    d => !(isLiteBackend && isRepositoryDatasource(d.type)) && !locked.has(d.id),
  );
  if (selectable.length === 0) return false;
  const active = activeDatasourceIds(datasources, selection, defaultIds);
  return selectable.every(d => active.has(d.id));
}

/**
 * Datasource checkbox list. Hidden entirely when no datasources are available.
 */
@Component({
  selector: 'app-datasources-group',
  standalone: true,
  imports: [TranslocoPipe, AppIconComponent, AppSpinnerComponent],
  template: `
    @if (!loading() && datasources().length > 0) {
      <div class="settings-group">
        <div class="group-header">
          <span class="group-label">{{ 'agentSettings.datasources.group' | transloco }}</span>
          <button
            type="button"
            class="select-all-btn"
            (click)="toggleAll()"
            [disabled]="disabled() || selectableDatasources().length === 0"
          >{{ (allSelected() ? 'agentSettings.common.deselectAll' : 'agentSettings.common.selectAll') | transloco }}</button>
        </div>
        <div class="ds-picker">
          @for (ds of datasources(); track ds.id) {
            <label
              class="ds-option"
              [class.selected]="isChecked(ds)"
              [class.ds-disabled]="isLiteExcluded(ds) || isLocked(ds)"
            >
              <input
                type="checkbox"
                [checked]="isChecked(ds)"
                (change)="toggle(ds.id)"
                [disabled]="disabled() || isLiteExcluded(ds) || isLocked(ds)"
              >
              <app-icon size="md" class="ds-type-icon" [class]="'ds-type-' + ds.type">{{ getTypeIcon(ds.type) }}</app-icon>
              <span class="ds-info">
                <span class="ds-name">{{ ds.name }}</span>
                @if (isLiteExcluded(ds)) {
                  <span class="ds-desc">Requires a sandbox or VM workspace</span>
                } @else if (isLocked(ds)) {
                  <span class="ds-desc">{{ 'agentSettings.datasources.lockedLive' | transloco }}</span>
                } @else if (ds.description) {
                  <span class="ds-desc">{{ ds.description }}</span>
                }
                @if (isNotReady(ds)) {
                  <span class="ds-indexing">
                    {{ 'agentSettings.datasources.notReady' | transloco }}
                  </span>
                }
              </span>
              <span class="ds-type-badge">
                {{ 'datasources.filter.' + ds.type | transloco }}
              </span>
              @if (ds.is_global) {
                <span class="ds-type-badge" [class.ds-rw-badge]="ds.read_only === false">
                  {{ (ds.read_only === false
                    ? 'datasources.table.badgeRw'
                    : 'datasources.table.badgeRo') | transloco }}
                </span>
              }
            </label>
          }
        </div>
      </div>
    } @else if (loading()) {
      <div class="settings-group">
        <div class="group-header">
          <span class="group-label">{{ 'agentSettings.datasources.group' | transloco }}</span>
        </div>
        <div class="ds-loading">
          <app-spinner size="sm" />

          {{ 'agentSettings.datasources.loading' | transloco }}
        </div>
      </div>
    }
  `,
  styles: [`
    .settings-group {
      margin-bottom: 20px;
    }
    .group-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 12px;
      padding-bottom: 6px;
      border-bottom: 1px solid var(--border-color, var(--surface-0));
    }
    .group-label {
      font-size: 11px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      color: var(--text-muted, var(--text-muted));
    }
    .select-all-btn {
      flex-shrink: 0;
      background: none;
      border: none;
      padding: 0;
      cursor: pointer;
      font-family: inherit;
      font-size: 11px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      color: var(--accent-color, var(--accent-color));
    }
    .select-all-btn:hover:not(:disabled) {
      text-decoration: underline;
    }
    .select-all-btn:disabled {
      opacity: 0.5;
      cursor: not-allowed;
    }
    .ds-picker {
      display: flex;
      flex-direction: column;
      gap: 4px;
    }
    .ds-option {
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 8px 10px;
      border-radius: var(--radius-control);
      cursor: pointer;
      transition: background 0.15s;
    }
    .ds-option:hover {
      background: rgba(255, 255, 255, 0.03);
    }
    .ds-option.selected {
      background: color-mix(in srgb, var(--accent-color) 20%, transparent);
    }
    .ds-option.ds-disabled {
      opacity: 0.5;
      cursor: not-allowed;
    }
    .ds-option input[type="checkbox"] {
      accent-color: var(--accent-color, var(--accent-color));
      flex-shrink: 0;
    }
    .ds-type-icon {
      color: var(--text-muted, var(--text-muted));
      flex-shrink: 0;
    }
    .ds-type-postgresql { color: var(--info); }
    .ds-type-neo4j { color: var(--success); }
    .ds-type-mongodb { color: var(--alert); }
    .ds-type-webdav { color: var(--info); }
    .ds-type-kb { color: var(--accent-color); }
    .ds-info {
      display: flex;
      flex-direction: column;
      gap: 1px;
      flex: 1;
      min-width: 0;
    }
    .ds-name {
      font-size: 13px;
      font-weight: 500;
      color: var(--text-primary, var(--text-primary));
    }
    .ds-desc {
      font-size: 11px;
      color: var(--text-muted, var(--text-muted));
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .ds-indexing {
      font-size: 11px;
      color: var(--warning, var(--text-muted));
      white-space: normal;
    }
    .ds-type-badge {
      font-size: 10px;
      font-weight: 500;
      text-transform: uppercase;
      letter-spacing: 0.3px;
      padding: 2px 6px;
      border-radius: var(--radius-tag);
      background: rgba(255, 255, 255, 0.06);
      color: var(--text-muted, var(--text-muted));
      flex-shrink: 0;
    }
    /* Public read-write chip — warning tone so attachers see write access. */
    .ds-rw-badge {
      color: var(--warning, var(--text-primary));
      background: var(--warning-tint, rgba(255, 255, 255, 0.06));
    }
    .ds-loading {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 12px;
      color: var(--text-muted, var(--text-muted));
      padding: 8px 0;
    }
  `],
})
export class DatasourcesGroupComponent {
  // Optional so the picker still renders in bare unit tests without an injector.
  private readonly api = inject(ApiService, {optional: true});

  datasources = input<Datasource[]>([]);
  loading = input(false);
  disabled = input(false);
  /**
   * When a lite workspace backend (virtual/none) is selected, clone-based
   * repository datasources are unavailable. `kb` repositories are indexed by
   * the orchestrator, so they remain selectable.
   */
  isLiteBackend = input(false);
  /**
   * Default selection when the user hasn't touched the picker (live mode:
   * the session's currently attached set — possibly empty). Null keeps the
   * create-flow default of "everything eligible selected".
   */
  initialSelectedIds = input<string[] | null>(null);
  /**
   * Entries rendered but frozen at their current state (live mode: kb-type
   * datasources — their knowledge bindings only rewire on attach, so live
   * changes are out of scope; live_session_settings.md Slice B).
   */
  lockedIds = input<string[]>([]);

  change = output<void>();

  // The user's explicit selection, tagged with the datasource-set identity it
  // was made against. Null — or a stale tag (e.g. after switching project) —
  // means "default: all eligible selected". The picker is the source of truth;
  // explicit-only resolution attaches exactly what's checked.
  private readonly selection = signal<{key: string; ids: Set<string>} | null>(null);

  /** Central index status per KB datasource id (for the still-indexing warning). */
  readonly indexStatuses = signal<Record<string, DatasourceIndexStatus>>({});

  constructor() {
    // Fetch central index status for KB rows so the picker can warn when a
    // selected knowledge base isn't fully indexed yet. Re-runs when the eligible
    // list changes (e.g. project switch); the signal write lands in the async
    // HTTP callback, never synchronously inside the effect.
    effect(() => {
      for (const ds of this.datasources()) {
        if (ds.type === 'kb') this.loadIndexStatus(ds.id);
      }
    });
  }

  readonly modifiedCount = computed(() => this.getSelectedIds().length);

  private defaultIds(): Set<string> | null {
    const init = this.initialSelectedIds();
    return init === null ? null : new Set(init);
  }

  /** Datasources the user can actually toggle (not lite-excluded, not locked). */
  readonly selectableDatasources = computed<Datasource[]>(() =>
    this.datasources().filter(ds => !this.isLiteExcluded(ds) && !this.isLocked(ds))
  );

  /** True when every selectable datasource is currently checked. */
  readonly allSelected = computed(() =>
    allDatasourcesSelected(
      this.datasources(),
      this.selection(),
      this.isLiteBackend(),
      this.defaultIds(),
      this.lockedIds(),
    )
  );

  /** A repository datasource can't be used under a lite backend. */
  isLiteExcluded(ds: Datasource): boolean {
    return this.isLiteBackend() && isRepositoryDatasource(ds.type);
  }

  /** Frozen at its current state — rendered, never toggleable. */
  isLocked(ds: Datasource): boolean {
    return this.lockedIds().includes(ds.id);
  }

  isChecked(ds: Datasource): boolean {
    return (
      !this.isLiteExcluded(ds) &&
      activeDatasourceIds(this.datasources(), this.selection(), this.defaultIds()).has(ds.id)
    );
  }

  toggle(id: string): void {
    if (this.lockedIds().includes(id)) return;
    const next = new Set(
      activeDatasourceIds(this.datasources(), this.selection(), this.defaultIds()),
    );
    if (next.has(id)) {
      next.delete(id);
    } else {
      next.add(id);
    }
    this.selection.set({key: datasourceSetKey(this.datasources()), ids: next});
    this.change.emit();
  }

  /** Check every selectable datasource, or clear them all if already all on.
   *  Locked entries keep their current state either way. */
  toggleAll(): void {
    const selectAll = !this.allSelected();
    const active = activeDatasourceIds(
      this.datasources(), this.selection(), this.defaultIds(),
    );
    const lockedKept = this.datasources()
      .filter(d => this.isLocked(d) && active.has(d.id))
      .map(d => d.id);
    const ids = selectAll
      ? new Set([...this.selectableDatasources().map(d => d.id), ...lockedKept])
      : new Set(lockedKept);
    this.selection.set({key: datasourceSetKey(this.datasources()), ids});
    this.change.emit();
  }

  getTypeIcon(type: DatasourceType | string): string {
    const icons: Record<string, string> = {
      postgresql: 'database',
      neo4j: 'hub',
      mongodb: 'eco',
      webdav: 'cloud',
      kb: 'menu_book',
    };
    return icons[type] || 'storage';
  }

  /**
   * Selected datasource IDs, excluding clone-based repository sources disabled
   * by a lite backend and any ids not in the current datasource set.
   */
  getSelectedIds(): string[] {
    return selectedDatasourceIds(
      this.datasources(),
      this.selection(),
      this.isLiteBackend(),
      this.defaultIds(),
    );
  }

  private loadIndexStatus(id: string): void {
    this.api?.getDatasourceIndexStatus(id).subscribe((status) => {
      if (!status) return;
      this.indexStatuses.update((m) => ({...m, [id]: status}));
    });
  }

  /** True when a KB datasource is in the index but not fully `ready` yet. */
  isNotReady(ds: Datasource): boolean {
    if (ds.type !== 'kb') return false;
    const status = this.indexStatuses()[ds.id]?.status;
    return !!status && status !== 'ready';
  }

  resetAll(): void {
    this.selection.set(null);
  }
}
