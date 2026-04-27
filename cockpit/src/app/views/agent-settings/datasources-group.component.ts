import {Component, computed, input, output, signal} from '@angular/core';
import {TranslocoPipe} from '@jsverse/transloco';
import {AppIconComponent} from '../../ui/icon';
import {AppSpinnerComponent} from '../../ui/spinner';
import {Datasource, DatasourceType} from '../../core/models/api.model';

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
              [class.selected]="selectedIds().has(ds.id)"
            >
              <input
                type="checkbox"
                [checked]="selectedIds().has(ds.id)"
                (change)="toggle(ds.id)"
                [disabled]="disabled()"
              >
              <app-icon size="md" class="ds-type-icon" [class]="'ds-type-' + ds.type">{{ getTypeIcon(ds.type) }}</app-icon>
              <span class="ds-info">
                <span class="ds-name">{{ ds.name }}</span>
                @if (ds.description) {
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
      color: var(--text-muted, #6c7086);
      margin-bottom: 12px;
      padding-bottom: 6px;
      border-bottom: 1px solid var(--border-color, #313244);
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
      border-radius: 6px;
      cursor: pointer;
      transition: background 0.15s;
    }
    .ds-option:hover {
      background: rgba(255, 255, 255, 0.03);
    }
    .ds-option.selected {
      background: rgba(203, 166, 247, 0.06);
    }
    .ds-option input[type="checkbox"] {
      accent-color: var(--accent-color, #cba6f7);
      flex-shrink: 0;
    }
    .ds-type-icon {
      color: var(--text-muted, #6c7086);
      flex-shrink: 0;
    }
    .ds-type-postgresql { color: var(--info); }
    .ds-type-neo4j { color: var(--success); }
    .ds-type-mongodb { color: var(--alert); }
    .ds-type-webdav { color: #89dceb; }
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
      color: var(--text-primary, #cdd6f4);
    }
    .ds-desc {
      font-size: 11px;
      color: var(--text-muted, #6c7086);
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
      border-radius: 4px;
      background: rgba(255, 255, 255, 0.06);
      color: var(--text-muted, #6c7086);
      flex-shrink: 0;
    }
    .ds-loading {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 12px;
      color: var(--text-muted, #6c7086);
      padding: 8px 0;
    }
  `],
})
export class DatasourcesGroupComponent {
  datasources = input<Datasource[]>([]);
  loading = input(false);
  disabled = input(false);

  change = output<void>();

  readonly selectedIds = signal<Set<string>>(new Set());

  readonly modifiedCount = computed(() => this.selectedIds().size);

  toggle(id: string): void {
    this.selectedIds.update(current => {
      const next = new Set(current);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
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

  /** Return selected datasource IDs. */
  getSelectedIds(): string[] {
    return Array.from(this.selectedIds());
  }

  resetAll(): void {
    this.selectedIds.set(new Set());
  }
}
