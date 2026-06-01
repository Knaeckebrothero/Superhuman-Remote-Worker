import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  OnInit,
  signal,
} from '@angular/core';
import {TranslocoService} from '@jsverse/transloco';
import {SidebarToggleComponent} from '../../../shell/sidebar-toggle/sidebar-toggle.component';
import {
  AdminConfigService,
  ConfigCatalogEntry,
  ConfigOverride,
} from '../../../core/services/admin-config.service';
import {AppSelectComponent} from '../../../ui/select';
import {AppTextareaComponent} from '../../../ui/textarea';
import {AppButtonComponent} from '../../../ui/button';
import {AppFormFieldComponent} from '../../../ui/form-field';
import {AppBadgeComponent} from '../../../ui/badge';
import {AppToastService} from '../../../ui/toast/toast.service';

/** Model families that can carry a family-specific override (v1). */
const FAMILIES = ['gemma', 'gpt_5', 'gpt_oss', 'minimax', 'codex', 'codex_spark'];

@Component({
  selector: 'app-admin-config',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    SidebarToggleComponent,
    AppSelectComponent,
    AppTextareaComponent,
    AppButtonComponent,
    AppFormFieldComponent,
    AppBadgeComponent,
  ],
  template: `
    <div class="admin-page">
      <div class="page-header">
        <app-sidebar-toggle />
        <h1 class="page-title">Configuration Overrides</h1>
      </div>
      <p class="page-desc">
        Override the bundled config (prompts, instructions, settings, guardrails)
        from the database. Saved edits apply to <strong>future</strong> jobs at
        dispatch — no redeploy. Clearing an override falls back to the shipped
        default.
      </p>

      <section class="admin-section">
        <h2 class="section-title">Pick a config key</h2>
        <div class="picker-row">
          <app-form-field label="Model family">
            <app-select [value]="familyValue()" (changed)="onFamilyChange($event)">
              <option value="_">Global (all families)</option>
              @for (f of families; track f) {
                <option [value]="f">{{ f }}</option>
              }
            </app-select>
          </app-form-field>
          <app-form-field label="Config key">
            <app-select [value]="keyValue()" (changed)="onKeyChange($event)">
              <option value="">— select a config key —</option>
              @for (e of admin.catalog(); track e.name) {
                <option [value]="e.name">{{ e.title }}</option>
              }
            </app-select>
          </app-form-field>
        </div>
      </section>

      @if (selectedEntry(); as entry) {
        <section class="admin-section">
          <div class="entry-head">
            <h2 class="section-title">{{ entry.title }}</h2>
            @if (hasOverride()) {
              <app-badge tone="info" size="xs">override active</app-badge>
            }
          </div>
          <p class="section-desc">{{ entry.description }}</p>

          <div class="editor-grid">
            <app-form-field
              [label]="isStructured() ? 'Bundled default (JSON, read-only)' : 'Bundled default (read-only)'"
            >
              <app-textarea [value]="bundledContent()" [disabled]="true" [rows]="14" />
            </app-form-field>
            <app-form-field
              [label]="isStructured() ? 'Override (JSON)' : 'Override'"
              [hint]="isStructured()
                ? 'Edit as JSON — validated server-side against the catalog.'
                : (hasOverride()
                  ? 'Editing the active override.'
                  : 'No override yet — saving creates one.')"
            >
              <app-textarea
                [value]="overrideContent()"
                (valueChange)="overrideContent.set($event)"
                [rows]="14"
              />
            </app-form-field>
          </div>

          <div class="actions">
            <app-button variant="primary" [loading]="saving()" (clicked)="save()">
              Save override
            </app-button>
            <app-button
              variant="ghost"
              [disabled]="!hasOverride() || saving()"
              (clicked)="resetToBundled()"
            >
              Reset to bundled
            </app-button>
          </div>
        </section>
      }
    </div>
  `,
  styles: [`
    :host {
      display: block;
      height: 100%;
      overflow: auto;
    }
    .admin-page {
      padding: 32px;
      max-width: 1000px;
      margin: 0 auto;
      color: var(--text-primary);
    }
    .page-header {
      display: flex;
      align-items: center;
      gap: 12px;
      margin-bottom: 8px;
    }
    .page-title {
      font-size: 24px;
      font-weight: 700;
      margin: 0;
      color: var(--text-primary);
    }
    .page-desc {
      font-size: 13px;
      color: var(--text-muted);
      margin: 0 0 32px 0;
    }
    .admin-section {
      background: var(--panel-bg);
      border: 1px solid var(--border-color);
      border-radius: var(--radius-lg);
      padding: 24px;
      margin-bottom: 24px;
    }
    .section-title {
      font-size: 18px;
      font-weight: 600;
      margin: 0 0 4px 0;
      color: var(--text-primary);
    }
    .section-desc {
      font-size: 13px;
      color: var(--text-muted);
      margin-bottom: 20px;
    }
    .picker-row {
      display: flex;
      flex-wrap: wrap;
      gap: 16px;
    }
    .picker-row app-form-field {
      flex: 1 1 220px;
    }
    .entry-head {
      display: flex;
      align-items: center;
      gap: 10px;
    }
    .editor-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 16px;
    }
    @media (max-width: 720px) {
      .editor-grid {
        grid-template-columns: 1fr;
      }
    }
    .actions {
      display: flex;
      gap: 12px;
      margin-top: 16px;
    }
  `],
})
export class AdminConfigComponent implements OnInit {
  readonly admin = inject(AdminConfigService);
  private readonly toast = inject(AppToastService);
  private readonly transloco = inject(TranslocoService);

  readonly families = FAMILIES;

  readonly familyValue = signal<string>('_'); // '_' = global in the picker
  readonly selectedEntry = signal<ConfigCatalogEntry | null>(null);
  readonly bundledContent = signal<string>('');
  readonly overrideContent = signal<string>('');
  readonly saving = signal(false);

  /** The override's family value: null for the global ("_") option. */
  readonly selectedFamily = computed<string | null>(() =>
    this.familyValue() === '_' ? null : this.familyValue(),
  );

  readonly keyValue = computed(() => this.selectedEntry()?.name ?? '');

  /** Structured kinds (settings, guardrails) edit a JSON value, not text. */
  readonly isStructured = computed(() => {
    const k = this.selectedEntry()?.kind;
    return k === 'settings' || k === 'guardrails';
  });

  readonly existingOverride = computed<ConfigOverride | null>(() => {
    const entry = this.selectedEntry();
    if (!entry) return null;
    const fam = this.selectedFamily();
    return (
      this.admin
        .overrides()
        .find((o) => o.family === fam && o.kind === entry.kind && o.name === entry.name) ??
      null
    );
  });

  readonly hasOverride = computed(() => this.existingOverride() !== null);

  ngOnInit(): void {
    this.admin.loadCatalog();
    this.admin.loadOverrides();
  }

  onFamilyChange(value: string | null): void {
    this.familyValue.set(value || '_');
    this.refreshSelection();
  }

  onKeyChange(name: string | null): void {
    this.selectedEntry.set(
      this.admin.catalog().find((e) => e.name === name) ?? null,
    );
    this.refreshSelection();
  }

  save(): void {
    const entry = this.selectedEntry();
    if (!entry) return;

    if (this.isStructured()) {
      let parsed: unknown;
      try {
        parsed = JSON.parse(this.overrideContent());
      } catch {
        this.toast.danger(this.transloco.translate('admin.config.messages.invalidJson'));
        return;
      }
      this.saving.set(true);
      this.admin
        .createOverride({
          family: this.selectedFamily(),
          kind: entry.kind,
          name: entry.name,
          value_json: parsed,
        })
        .subscribe({
          next: () => {
            this.saving.set(false);
            this.toast.success(this.transloco.translate('admin.config.messages.saved'));
          },
          error: () => {
            this.saving.set(false);
            this.toast.danger(this.transloco.translate('admin.config.messages.saveFailed'));
          },
        });
      return;
    }

    if (!this.overrideContent().trim()) {
      this.toast.danger(this.transloco.translate('admin.config.messages.saveEmpty'));
      return;
    }
    this.saving.set(true);
    this.admin
      .createOverride({
        family: this.selectedFamily(),
        kind: entry.kind,
        name: entry.name,
        content: this.overrideContent(),
      })
      .subscribe({
        next: () => {
          this.saving.set(false);
          this.toast.success(this.transloco.translate('admin.config.messages.saved'));
        },
        error: () => {
          this.saving.set(false);
          this.toast.danger(this.transloco.translate('admin.config.messages.saveFailed'));
        },
      });
  }

  resetToBundled(): void {
    const existing = this.existingOverride();
    if (!existing) return;
    this.saving.set(true);
    this.admin.deleteOverride(existing.id).subscribe({
      next: () => {
        this.saving.set(false);
        this.overrideContent.set('');
        this.toast.success(this.transloco.translate('admin.config.messages.removed'));
      },
      error: () => {
        this.saving.set(false);
        this.toast.danger(this.transloco.translate('admin.config.messages.removeFailed'));
      },
    });
  }

  /** Re-seed the editor + bundled reference for the current (family, entry). */
  private refreshSelection(): void {
    const entry = this.selectedEntry();
    if (!entry) {
      this.bundledContent.set('');
      this.overrideContent.set('');
      return;
    }
    const structured = this.isStructured();
    if (!structured) {
      this.overrideContent.set(this.existingOverride()?.content ?? '');
    }
    this.admin.getBundled(this.selectedFamily(), entry.kind, entry.name).subscribe({
      next: (b) => {
        if (structured) {
          const bundledStr = JSON.stringify(b.content ?? null, null, 2);
          this.bundledContent.set(bundledStr);
          const existing = this.existingOverride();
          const hasVal =
            existing != null &&
            existing.value_json !== undefined &&
            existing.value_json !== null;
          this.overrideContent.set(
            hasVal ? JSON.stringify(existing!.value_json, null, 2) : bundledStr,
          );
        } else {
          this.bundledContent.set(typeof b.content === 'string' ? b.content : '');
        }
      },
      error: () => this.bundledContent.set(''),
    });
  }
}
