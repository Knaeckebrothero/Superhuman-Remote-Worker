import {Component, computed, input, output, signal} from '@angular/core';
import {TranslocoPipe} from '@jsverse/transloco';
import {AppIconComponent} from '../../ui/icon';
import {AppSpinnerComponent} from '../../ui/spinner';
import {Datasource, DatasourceType} from '../../core/models/api.model';

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

/** Active selection: the user's tagged choice, or all ids when the selection is
 *  untouched (null) or stale (made against a different datasource set). */
export function activeDatasourceIds(
  datasources: {id: string}[],
  selection: DatasourceSelection,
): Set<string> {
  if (selection && selection.key === datasourceSetKey(datasources)) {
    return selection.ids;
  }
  return new Set(datasources.map(d => d.id));
}

/** Selected datasource IDs to submit: the active set, minus repository sources
 *  disabled by a lite backend and any ids not in the current datasource set. */
export function selectedDatasourceIds(
  datasources: Datasource[],
  selection: DatasourceSelection,
  isLiteBackend: boolean,
): string[] {
  const active = activeDatasourceIds(datasources, selection);
  return datasources
    .filter(d => active.has(d.id) && !(isLiteBackend && isRepositoryDatasource(d.type)))
    .map(d => d.id);
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
        <div class="group-label">{{ 'agentSettings.datasources.group' | transloco }}</div>
        <div class="ds-picker">
          @for (ds of datasources(); track ds.id) {
            <label
              class="ds-option"
              [class.selected]="isChecked(ds)"
              [class.ds-disabled]="isLiteExcluded(ds)"
            >
              <input
                type="checkbox"
                [checked]="isChecked(ds)"
                (change)="toggle(ds.id)"
                [disabled]="disabled() || isLiteExcluded(ds)"
              >
              <app-icon size="md" class="ds-type-icon" [class]="'ds-type-' + ds.type">{{ getTypeIcon(ds.type) }}</app-icon>
              <span class="ds-info">
                <span class="ds-name">{{ ds.name }}</span>
                @if (isLiteExcluded(ds)) {
                  <span class="ds-desc">Requires a sandbox or VM workspace</span>
                } @else if (ds.description) {
                  <span class="ds-desc">{{ ds.description }}</span>
                }
              </span>
              <span class="ds-type-badge">{{ ds.type }}</span>
            </label>
          }
        </div>
      </div>
    } @else if (loading()) {
      <div class="settings-group">
        <div class="group-label">{{ 'agentSettings.datasources.group' | transloco }}</div>
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
    .group-label {
      font-size: 11px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      color: var(--text-muted, var(--text-muted));
      margin-bottom: 12px;
      padding-bottom: 6px;
      border-bottom: 1px solid var(--border-color, var(--surface-0));
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
  datasources = input<Datasource[]>([]);
  loading = input(false);
  disabled = input(false);
  /**
   * When a lite workspace backend (virtual/none) is selected, repository
   * datasources can't be cloned — they are shown disabled and excluded from
   * the emitted selection.
   */
  isLiteBackend = input(false);

  change = output<void>();

  // The user's explicit selection, tagged with the datasource-set identity it
  // was made against. Null — or a stale tag (e.g. after switching project) —
  // means "default: all eligible selected". The picker is the source of truth;
  // explicit-only resolution attaches exactly what's checked.
  private readonly selection = signal<{key: string; ids: Set<string>} | null>(null);

  readonly modifiedCount = computed(() => this.getSelectedIds().length);

  /** A repository datasource can't be used under a lite backend. */
  isLiteExcluded(ds: Datasource): boolean {
    return this.isLiteBackend() && isRepositoryDatasource(ds.type);
  }

  isChecked(ds: Datasource): boolean {
    return (
      !this.isLiteExcluded(ds) &&
      activeDatasourceIds(this.datasources(), this.selection()).has(ds.id)
    );
  }

  toggle(id: string): void {
    const next = new Set(activeDatasourceIds(this.datasources(), this.selection()));
    if (next.has(id)) {
      next.delete(id);
    } else {
      next.add(id);
    }
    this.selection.set({key: datasourceSetKey(this.datasources()), ids: next});
    this.change.emit();
  }

  getTypeIcon(type: DatasourceType | string): string {
    const icons: Record<string, string> = {
      postgresql: 'database',
      neo4j: 'hub',
      mongodb: 'eco',
      webdav: 'cloud',
    };
    return icons[type] || 'storage';
  }

  /**
   * Selected datasource IDs, excluding repository sources disabled by a lite
   * backend and any ids not in the current datasource set.
   */
  getSelectedIds(): string[] {
    return selectedDatasourceIds(
      this.datasources(),
      this.selection(),
      this.isLiteBackend(),
    );
  }

  resetAll(): void {
    this.selection.set(null);
  }
}
