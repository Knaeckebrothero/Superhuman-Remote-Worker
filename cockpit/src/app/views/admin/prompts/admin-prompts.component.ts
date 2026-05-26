import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  OnInit,
  signal,
} from '@angular/core';
import {SidebarToggleComponent} from '../../../shell/sidebar-toggle/sidebar-toggle.component';
import {
  AdminPromptsService,
  PromptCatalogEntry,
  PromptOverride,
} from '../../../core/services/admin-prompts.service';
import {AppSelectComponent} from '../../../ui/select';
import {AppTextareaComponent} from '../../../ui/textarea';
import {AppButtonComponent} from '../../../ui/button';
import {AppFormFieldComponent} from '../../../ui/form-field';
import {AppBadgeComponent} from '../../../ui/badge';
import {AppToastService} from '../../../ui/toast/toast.service';

/** Model families that can carry a family-specific override (v1). */
const FAMILIES = ['gemma', 'gpt_5', 'gpt_oss', 'minimax', 'codex', 'codex_spark'];

@Component({
  selector: 'app-admin-prompts',
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
        <h1 class="page-title">Prompt Overrides</h1>
      </div>
      <p class="page-desc">
        Override the bundled prompt files from the database. Saved edits apply to
        <strong>future</strong> jobs at dispatch — no redeploy. Clearing an override
        falls back to the shipped default.
      </p>

      <section class="admin-section">
        <h2 class="section-title">Pick a prompt</h2>
        <div class="picker-row">
          <app-form-field label="Model family">
            <app-select [value]="familyValue()" (changed)="onFamilyChange($event)">
              <option value="_">Global (all families)</option>
              @for (f of families; track f) {
                <option [value]="f">{{ f }}</option>
              }
            </app-select>
          </app-form-field>
          <app-form-field label="Prompt">
            <app-select [value]="keyValue()" (changed)="onKeyChange($event)">
              <option value="">— select a prompt —</option>
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
            <app-form-field label="Bundled default (read-only)">
              <app-textarea [value]="bundledContent()" [disabled]="true" [rows]="14" />
            </app-form-field>
            <app-form-field
              label="Override"
              [hint]="hasOverride()
                ? 'Editing the active override.'
                : 'No override yet — saving creates one.'"
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
export class AdminPromptsComponent implements OnInit {
  readonly admin = inject(AdminPromptsService);
  private readonly toast = inject(AppToastService);

  readonly families = FAMILIES;

  readonly familyValue = signal<string>('_'); // '_' = global in the picker
  readonly selectedEntry = signal<PromptCatalogEntry | null>(null);
  readonly bundledContent = signal<string>('');
  readonly overrideContent = signal<string>('');
  readonly saving = signal(false);

  /** The override's family value: null for the global ("_") option. */
  readonly selectedFamily = computed<string | null>(() =>
    this.familyValue() === '_' ? null : this.familyValue(),
  );

  readonly keyValue = computed(() => this.selectedEntry()?.name ?? '');

  readonly existingOverride = computed<PromptOverride | null>(() => {
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
    if (!entry || !this.overrideContent().trim()) {
      this.toast.danger('Nothing to save — pick a prompt and enter content.');
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
          this.toast.success('Override saved — applies to future jobs.');
        },
        error: () => {
          this.saving.set(false);
          this.toast.danger('Failed to save override.');
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
        this.toast.success('Override removed — back to the bundled default.');
      },
      error: () => {
        this.saving.set(false);
        this.toast.danger('Failed to remove override.');
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
    this.overrideContent.set(this.existingOverride()?.content ?? '');
    this.admin.getBundled(this.selectedFamily(), entry.kind, entry.name).subscribe({
      next: (b) => this.bundledContent.set(b.content),
      error: () => this.bundledContent.set(''),
    });
  }
}
