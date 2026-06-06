import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  OnInit,
  signal,
  untracked,
} from '@angular/core';
import {forkJoin, Observable} from 'rxjs';
import {TranslocoService} from '@jsverse/transloco';
import {SidebarToggleComponent} from '../../../shell/sidebar-toggle/sidebar-toggle.component';
import {
  AdminConfigService,
  ConfigCatalogEntry,
  ConfigOverride,
} from '../../../core/services/admin-config.service';
import {AppSelectComponent} from '../../../ui/select';
import {AppTextareaComponent} from '../../../ui/textarea';
import {AppInputComponent} from '../../../ui/input';
import {AppSwitchComponent} from '../../../ui/switch';
import {AppButtonComponent} from '../../../ui/button';
import {AppFormFieldComponent} from '../../../ui/form-field';
import {AppBadgeComponent} from '../../../ui/badge';
import {AppToastService} from '../../../ui/toast/toast.service';
import {AgentLoopDiagramComponent} from './agent-loop-diagram.component';

/**
 * Model families that can carry a family-specific override (v1).
 *
 * Values MUST equal family_of()'s output (hyphen form): the picked value is
 * stored verbatim in config_overrides.family and matched against
 * family_of(model) at dispatch (migration 0021). The earlier underscore forms
 * (gpt_5, gpt_oss, codex_spark) never resolved against the hyphenated family
 * key — corrected here, and minimax-m3 added.
 */
const FAMILIES = ['gemma', 'gpt-5', 'gpt-oss', 'deepseek', 'minimax', 'minimax-m3', 'codex', 'codex-spark'];

@Component({
  selector: 'app-admin-config',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    SidebarToggleComponent,
    AppSelectComponent,
    AppTextareaComponent,
    AppInputComponent,
    AppSwitchComponent,
    AppButtonComponent,
    AppFormFieldComponent,
    AppBadgeComponent,
    AgentLoopDiagramComponent,
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
        <h2 class="section-title">How prompts are used</h2>
        <p class="section-desc">
          Where each prompt and instruction file below runs in an agent's loop.
          Workers (jobs) alternate strategic and tactical phase prompts;
          persistent sessions use a single interactive prompt every turn.
        </p>
        <app-agent-loop-diagram />
      </section>

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
              @for (g of groupedCatalog(); track g.label) {
                <optgroup [label]="g.label">
                  @for (e of g.entries; track e.name) {
                    <option [value]="e.name">{{ e.title }}</option>
                  }
                </optgroup>
              }
            </app-select>
          </app-form-field>
        </div>
      </section>

      @if (settingsEntries().length) {
        <section class="admin-section">
          <div class="entry-head">
            <h2 class="section-title">Inference settings</h2>
            <span class="entry-scope">{{ familyLabel() }}</span>
          </div>
          <p class="section-desc">
            LLM sampling + context limits for this model family, layered on the
            bundled settings matrix. Leave a field empty to use the shipped
            default. Saved edits apply to <strong>future</strong> jobs at
            dispatch.
          </p>

          <div class="settings-form">
            @for (e of settingsEntries(); track e.name) {
              <div class="setting-row">
                <div class="setting-label">
                  <span class="setting-title">{{ e.title }}</span>
                  <span class="setting-desc">{{ e.description }}</span>
                </div>
                <div class="setting-control">
                  @if (e.type === 'boolean') {
                    <app-switch
                      [checked]="boolValue(e.name)"
                      (changed)="setBool(e.name, $event)"
                    />
                  } @else {
                    <app-input
                      type="number"
                      [value]="strValue(e.name)"
                      (valueChange)="setStr(e.name, $event)"
                      [placeholder]="bundledDisplay(e.name)"
                      [fullWidth]="false"
                    />
                  }
                  <div class="setting-flags">
                    <span class="setting-default">default: {{ bundledDisplay(e.name) }}</span>
                    @if (settingOverridden(e.name)) {
                      <app-badge tone="info" size="xs">override</app-badge>
                      <button type="button" class="setting-reset" (click)="resetSetting(e.name)">
                        reset
                      </button>
                    }
                  </div>
                </div>
              </div>
            }
          </div>

          <div class="actions">
            <app-button
              variant="primary"
              [loading]="savingSettings()"
              (clicked)="saveSettings()"
            >
              Save settings
            </app-button>
          </div>
        </section>
      }

      @if (selectedEntry(); as entry) {
        <section class="admin-section">
          <div class="entry-head">
            <h2 class="section-title">{{ entry.title }}</h2>
            @if (entry.group) {
              <span class="entry-scope">{{ entry.group }}</span>
            }
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
      flex-wrap: wrap;
      gap: 10px;
    }
    .entry-scope {
      font-size: 11px;
      color: var(--text-muted);
      border: 1px solid var(--border-color);
      border-radius: var(--radius-tag, 6px);
      padding: 2px 8px;
      white-space: nowrap;
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
    .settings-form {
      display: flex;
      flex-direction: column;
    }
    .setting-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 24px;
      padding: 14px 0;
      border-bottom: 1px solid var(--border-color);
    }
    .setting-row:last-child {
      border-bottom: none;
    }
    .setting-label {
      display: flex;
      flex-direction: column;
      gap: 2px;
      min-width: 0;
    }
    .setting-title {
      font-size: 14px;
      font-weight: 500;
      color: var(--text-primary);
    }
    .setting-desc {
      font-size: 12px;
      color: var(--text-muted);
    }
    .setting-control {
      display: flex;
      flex-direction: column;
      align-items: flex-end;
      gap: 6px;
      flex-shrink: 0;
    }
    .setting-control app-input {
      width: 150px;
    }
    .setting-flags {
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .setting-default {
      font-size: 11px;
      color: var(--text-muted);
    }
    .setting-reset {
      font-size: 11px;
      color: var(--accent-color);
      background: none;
      border: none;
      cursor: pointer;
      padding: 0;
      text-decoration: underline;
    }
    @media (max-width: 560px) {
      .setting-row {
        flex-direction: column;
        align-items: stretch;
        gap: 10px;
      }
      .setting-control {
        align-items: stretch;
      }
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

  /**
   * Catalog entries bucketed into <optgroup>s for the picker. Groups (and the
   * entries within them) keep the catalog's order, so the picker reads
   * shared → worker → persistent → guardrails. Entries with no `group` fall
   * into a trailing "Other" bucket. `settings` are excluded — they have their
   * own typed form rather than a per-key text editor.
   */
  readonly groupedCatalog = computed(() => {
    const groups: {label: string; entries: ConfigCatalogEntry[]}[] = [];
    const byLabel = new Map<string, ConfigCatalogEntry[]>();
    for (const e of this.admin.catalog()) {
      if (e.kind === 'settings') continue;
      const label = e.group ?? 'Other';
      let bucket = byLabel.get(label);
      if (!bucket) {
        bucket = [];
        byLabel.set(label, bucket);
        groups.push({label, entries: bucket});
      }
      bucket.push(e);
    }
    return groups;
  });

  /** Settings leaves (temperature, top_p, …) rendered as the typed form. */
  readonly settingsEntries = computed(() =>
    this.admin.catalog().filter((e) => e.kind === 'settings'),
  );

  /** Human label for the family the settings form is scoped to. */
  readonly familyLabel = computed(() =>
    this.familyValue() === '_' ? 'Global (all families)' : this.familyValue(),
  );

  /** Bundled (shipped) value per settings leaf, for the selected family. */
  readonly bundledSettings = signal<Record<string, unknown>>({});

  /** Editable form state per settings leaf: string for numbers, bool for switches. */
  readonly settingsForm = signal<Record<string, string | boolean>>({});

  readonly savingSettings = signal(false);

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

  constructor() {
    // Re-seed the settings form whenever the catalog arrives or the family
    // changes. Reads of bundled/override values happen in the async callback
    // (untracked), so this effect only depends on (family, catalog).
    effect(() => {
      const fam = this.selectedFamily();
      const entries = this.settingsEntries();
      if (entries.length === 0) return;
      untracked(() => this.seedSettingsForm(fam, entries));
    });
  }

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

  // --- Settings form -----------------------------------------------------

  strValue(name: string): string {
    const v = this.settingsForm()[name];
    return v == null ? '' : String(v);
  }

  boolValue(name: string): boolean {
    return Boolean(this.settingsForm()[name]);
  }

  setStr(name: string, value: string): void {
    this.settingsForm.update((f) => ({...f, [name]: value}));
  }

  setBool(name: string, value: boolean): void {
    this.settingsForm.update((f) => ({...f, [name]: value}));
  }

  /** Display string for a leaf's bundled default ("—" when the matrix is silent). */
  bundledDisplay(name: string): string {
    const v = this.bundledSettings()[name];
    return v == null ? '—' : String(v);
  }

  /** True when a DB override row exists for (selected family, settings, name). */
  settingOverridden(name: string): boolean {
    const fam = this.selectedFamily();
    return this.admin
      .overrides()
      .some((o) => o.family === fam && o.kind === 'settings' && o.name === name);
  }

  /** Drop a single field back to its bundled default (committed on Save). */
  resetSetting(name: string): void {
    const entry = this.settingsEntries().find((e) => e.name === name);
    const bundled = this.bundledSettings()[name];
    const value =
      entry?.type === 'boolean'
        ? Boolean(bundled)
        : bundled == null
          ? ''
          : String(bundled);
    this.settingsForm.update((f) => ({...f, [name]: value}));
  }

  /** Fetch bundled defaults for the family and seed the form (override > bundled). */
  seedSettingsForm(family: string | null, entries: ConfigCatalogEntry[]): void {
    this.admin.getBundledSettings(family, entries.map((e) => e.name)).subscribe({
      next: (bundled) => {
        this.bundledSettings.set(bundled);
        const overrides = untracked(() => this.admin.overrides());
        const form: Record<string, string | boolean> = {};
        for (const e of entries) {
          const ov = overrides.find(
            (o) => o.family === family && o.kind === 'settings' && o.name === e.name,
          );
          const eff = ov && ov.value_json != null ? ov.value_json : bundled[e.name];
          form[e.name] =
            e.type === 'boolean' ? Boolean(eff) : eff == null ? '' : String(eff);
        }
        this.settingsForm.set(form);
      },
      error: () => undefined,
    });
  }

  /**
   * Save the settings form for the current family. Diffs each leaf against its
   * bundled default: changed → upsert an override, set-back-to-default →
   * delete the override (keeps the DB delta thin). Validates numbers
   * client-side before writing so out-of-bounds values fail fast.
   */
  saveSettings(): void {
    const entries = this.settingsEntries();
    const fam = this.selectedFamily();
    const form = this.settingsForm();
    const bundled = this.bundledSettings();
    const overrides = this.admin.overrides();
    const ops: Observable<unknown>[] = [];

    for (const e of entries) {
      const existing =
        overrides.find(
          (o) => o.family === fam && o.kind === 'settings' && o.name === e.name,
        ) ?? null;

      let val: number | boolean | null;
      if (e.type === 'boolean') {
        val = Boolean(form[e.name]);
      } else {
        const raw = String(form[e.name] ?? '').trim();
        if (raw === '') {
          val = null; // empty → fall back to bundled default
        } else {
          const n = Number(raw);
          if (!Number.isFinite(n)) {
            this.toast.danger(`${e.title} must be a number.`);
            return;
          }
          if (e.type === 'integer' && !Number.isInteger(n)) {
            this.toast.danger(`${e.title} must be a whole number.`);
            return;
          }
          if (e.min != null && n < e.min) {
            this.toast.danger(`${e.title} must be at least ${e.min}.`);
            return;
          }
          if (e.max != null && n > e.max) {
            this.toast.danger(`${e.title} must be at most ${e.max}.`);
            return;
          }
          val = n;
        }
      }

      const overrideVal =
        existing && existing.value_json != null ? existing.value_json : undefined;
      const sameAsBundled = val === null || val === bundled[e.name];
      if (sameAsBundled) {
        // Reset to default: drop the override row if one exists.
        if (existing) ops.push(this.admin.deleteOverride(existing.id));
      } else if (val !== overrideVal) {
        // Changed from the current (override or bundled) value: upsert.
        ops.push(
          this.admin.createOverride({
            family: fam,
            kind: 'settings',
            name: e.name,
            value_json: val,
          }),
        );
      }
    }

    if (ops.length === 0) {
      this.toast.info('No setting changes to save.');
      return;
    }
    this.savingSettings.set(true);
    forkJoin(ops).subscribe({
      next: () => {
        this.savingSettings.set(false);
        this.toast.success('Inference settings saved.');
        // Refresh override rows so the badges / diff baseline update. The form
        // already holds the saved values, so we don't re-seed (which could read
        // overrides before this reload lands).
        this.admin.loadOverrides();
      },
      error: () => {
        this.savingSettings.set(false);
        this.toast.danger('Failed to save settings.');
      },
    });
  }
}
