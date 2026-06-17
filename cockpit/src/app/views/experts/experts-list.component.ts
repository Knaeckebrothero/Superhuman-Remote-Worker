import {Component, computed, inject, OnInit, signal} from '@angular/core';
import {Router} from '@angular/router';
import {TranslocoPipe, TranslocoService} from '@jsverse/transloco';
import {ApiService} from '../../core/services/api.service';
import type {Expert, ExpertCreateRequest} from '../../core/models/api.model';
import {AppButtonComponent} from '../../ui/button';
import {AppIconButtonComponent} from '../../ui/icon-button';
import {AppBadgeComponent} from '../../ui/badge';
import {AppChipComponent} from '../../ui/chip';
import {AppIconComponent} from '../../ui/icon';
import {AppSpinnerComponent} from '../../ui/spinner';
import {AppDialogComponent} from '../../ui/dialog';

export type ExpertTypeFilter = 'all' | 'worker' | 'session';

/** Pure list filter — exported for unit tests. */
export function filterExperts(rows: Expert[], f: ExpertTypeFilter): Expert[] {
  return f === 'all' ? rows : rows.filter((r) => r.expert_type === f);
}

/** Bundled (disk) experts are read-only; absence of a source means bundled. */
export function isBundled(e: Expert): boolean {
  return (e.source ?? 'bundled') === 'bundled';
}

@Component({
  selector: 'app-experts-list',
  standalone: true,
  imports: [
    TranslocoPipe,
    AppButtonComponent,
    AppIconButtonComponent,
    AppBadgeComponent,
    AppChipComponent,
    AppIconComponent,
    AppSpinnerComponent,
    AppDialogComponent,
  ],
  template: `
    <div class="experts">
      <header class="head">
        <h1>{{ 'experts.title' | transloco }}</h1>
        <div class="head-actions">
          <app-button variant="secondary" (clicked)="fileInput.click()">
            {{ 'experts.import' | transloco }}
          </app-button>
          <input #fileInput type="file" accept="application/json,.json" hidden (change)="onImport($event)" />
          <app-button variant="primary" (clicked)="newExpert()">
            {{ 'experts.new' | transloco }}
          </app-button>
        </div>
      </header>

      <div class="filters">
        @for (f of filters; track f.value) {
          <app-chip [selected]="typeFilter() === f.value" (clicked)="typeFilter.set(f.value)">
            {{ f.key | transloco }}
          </app-chip>
        }
      </div>

      @if (loading()) {
        <app-spinner />
      } @else if (filtered().length === 0) {
        <p class="empty">{{ 'experts.empty' | transloco }}</p>
      } @else {
        <table class="grid">
          <thead>
            <tr>
              <th>{{ 'experts.colName' | transloco }}</th>
              <th>{{ 'experts.colType' | transloco }}</th>
              <th>{{ 'experts.colSource' | transloco }}</th>
              <th class="actions-col">{{ 'experts.colActions' | transloco }}</th>
            </tr>
          </thead>
          <tbody>
            @for (e of filtered(); track e.id) {
              <tr>
                <td class="name-cell">
                  <app-icon size="sm" [style.color]="e.color">{{ e.icon || 'smart_toy' }}</app-icon>
                  <span>{{ e.display_name }}</span>
                </td>
                <td>{{ e.expert_type || '—' }}</td>
                <td>
                  <app-badge [tone]="bundled(e) ? 'neutral' : 'info'">{{ e.source || 'bundled' }}</app-badge>
                  @if (bundled(e)) {
                    <app-badge tone="neutral">{{ 'experts.bundledReadonly' | transloco }}</app-badge>
                  }
                </td>
                <td class="actions-col">
                  @if (!bundled(e)) {
                    <app-icon-button [ariaLabel]="'experts.edit' | transloco" variant="ghost" (clicked)="edit(e)">
                      edit
                    </app-icon-button>
                  }
                  <app-icon-button [ariaLabel]="'experts.duplicate' | transloco" variant="ghost" (clicked)="duplicate(e)">
                    content_copy
                  </app-icon-button>
                  <app-icon-button [ariaLabel]="'experts.export' | transloco" variant="ghost" (clicked)="exportExpert(e)">
                    download
                  </app-icon-button>
                  @if (!bundled(e)) {
                    <app-icon-button [ariaLabel]="'experts.delete' | transloco" variant="danger" (clicked)="askDelete(e)">
                      delete
                    </app-icon-button>
                  }
                </td>
              </tr>
            }
          </tbody>
        </table>
      }

      @if (successMessage()) {
        <div class="banner ok">{{ successMessage() }}</div>
      }
      @if (errorMessage()) {
        <div class="banner err">{{ errorMessage() }}</div>
      }

      <app-dialog
        [open]="confirmOpen()"
        [title]="'experts.confirmDeleteTitle' | transloco"
        (closed)="confirmOpen.set(false)"
      >
        <p>{{ 'experts.confirmDeleteBody' | transloco }} <strong>{{ pendingDelete()?.display_name }}</strong>?</p>
        <div appDialogActions>
          <app-button variant="secondary" (clicked)="confirmOpen.set(false)">
            {{ 'experts.cancel' | transloco }}
          </app-button>
          <app-button variant="danger" (clicked)="confirmDelete()">
            {{ 'experts.delete' | transloco }}
          </app-button>
        </div>
      </app-dialog>
    </div>
  `,
  styles: [
    `
      :host {
        display: block;
        height: 100%;
        overflow-y: auto;
      }
      .experts {
        padding: 1rem 1.5rem;
        max-width: 1100px;
        margin: 0 auto;
      }
      .head {
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: 1rem;
      }
      .head h1 {
        margin: 0;
      }
      .head-actions {
        display: flex;
        gap: 0.5rem;
      }
      .filters {
        display: flex;
        gap: 0.5rem;
        margin-bottom: 1rem;
      }
      .grid {
        width: 100%;
        border-collapse: collapse;
      }
      .grid th,
      .grid td {
        text-align: left;
        padding: 0.5rem;
        border-bottom: 1px solid var(--border-color);
        color: var(--text-primary);
      }
      .name-cell {
        display: flex;
        align-items: center;
        gap: 0.5rem;
      }
      .actions-col {
        text-align: right;
        white-space: nowrap;
      }
      .empty {
        color: var(--text-muted);
        padding: 2rem 0;
        text-align: center;
      }
      .banner {
        margin-top: 1rem;
        padding: 0.5rem 0.75rem;
        border-radius: 6px;
      }
      .banner.ok {
        background: var(--success-tint);
        color: var(--success);
      }
      .banner.err {
        background: var(--danger-tint);
        color: var(--danger);
      }
    `,
  ],
})
export class ExpertsListComponent implements OnInit {
  private api = inject(ApiService);
  private router = inject(Router);
  private transloco = inject(TranslocoService);

  filters: {value: ExpertTypeFilter; key: string}[] = [
    {value: 'all', key: 'experts.filterAll'},
    {value: 'worker', key: 'experts.filterWorker'},
    {value: 'session', key: 'experts.filterSession'},
  ];
  typeFilter = signal<ExpertTypeFilter>('all');
  rows = signal<Expert[]>([]);
  loading = signal(true);
  successMessage = signal('');
  errorMessage = signal('');
  confirmOpen = signal(false);
  pendingDelete = signal<Expert | null>(null);

  filtered = computed(() => filterExperts(this.rows(), this.typeFilter()));
  bundled = isBundled;

  ngOnInit(): void {
    this.refresh();
  }

  refresh(): void {
    this.loading.set(true);
    this.api.getExperts().subscribe((rows) => {
      this.rows.set(rows);
      this.loading.set(false);
    });
  }

  newExpert(): void {
    this.router.navigate(['/experts/new']);
  }

  onImport(ev: Event): void {
    const input = ev.target as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;
    file.text().then((text) => {
      input.value = '';
      let body: ExpertCreateRequest;
      try {
        body = JSON.parse(text) as ExpertCreateRequest;
      } catch {
        this.errorMessage.set(this.transloco.translate('experts.invalidJson'));
        return;
      }
      this.api.importExpert(body).subscribe({
        next: () => {
          this.successMessage.set(this.transloco.translate('experts.imported'));
          this.refresh();
        },
        error: (err) => this.errorMessage.set(this.detail(err) ?? 'Import failed'),
      });
    });
  }

  edit(e: Expert): void {
    this.router.navigate(['/experts', e.id, 'edit']);
  }

  duplicate(e: Expert): void {
    this.api.duplicateExpert(e.id).subscribe({
      next: () => {
        this.successMessage.set(this.transloco.translate('experts.duplicated'));
        this.refresh();
      },
      error: (err) => this.errorMessage.set(this.detail(err) ?? 'Duplicate failed'),
    });
  }

  exportExpert(e: Expert): void {
    this.api.exportExpert(e.id).subscribe((bundle) => {
      const blob = new Blob([JSON.stringify(bundle, null, 2)], {type: 'application/json'});
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `${e.display_name || e.id}.expert.json`;
      a.click();
      URL.revokeObjectURL(url);
    });
  }

  askDelete(e: Expert): void {
    this.pendingDelete.set(e);
    this.confirmOpen.set(true);
  }

  confirmDelete(): void {
    const e = this.pendingDelete();
    if (!e) return;
    this.api.deleteExpert(e.id).subscribe({
      next: () => {
        this.confirmOpen.set(false);
        this.successMessage.set(this.transloco.translate('experts.deleted'));
        this.refresh();
      },
      error: (err) => {
        this.confirmOpen.set(false);
        const d = err?.error?.detail;
        this.errorMessage.set(
          d && typeof d === 'object' && Array.isArray(d.blockers)
            ? this.transloco.translate('experts.inUse', {count: d.blockers.length})
            : (typeof d === 'string' ? d : 'Delete failed'),
        );
      },
    });
  }

  private detail(err: unknown): string | null {
    const d = (err as {error?: {detail?: unknown}})?.error?.detail;
    return typeof d === 'string' ? d : null;
  }
}
