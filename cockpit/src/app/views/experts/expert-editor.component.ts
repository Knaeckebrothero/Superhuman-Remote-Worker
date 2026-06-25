import {Component, computed, effect, inject, OnInit, signal, viewChild} from '@angular/core';
import {FormsModule} from '@angular/forms';
import {ActivatedRoute, Router} from '@angular/router';
import {TranslocoPipe, TranslocoService} from '@jsverse/transloco';
import {ApiService} from '../../core/services/api.service';
import {ModelService} from '../../core/services/model.service';
import type {ExpertCreateRequest, ExpertUpdateRequest, GrantCatalog} from '../../core/models/api.model';
import {AppButtonComponent} from '../../ui/button';
import {AppInputComponent} from '../../ui/input';
import {AppTextareaComponent} from '../../ui/textarea';
import {AppSelectComponent} from '../../ui/select';
import {AppFormFieldComponent} from '../../ui/form-field';
import {AppIconComponent} from '../../ui/icon';
import {SidebarToggleComponent} from '../../shell/sidebar-toggle/sidebar-toggle.component';
import {ExecutionGroupComponent} from '../agent-settings/execution-group.component';
import {ToolsGroupComponent} from '../agent-settings/tools-group.component';
import {AdvancedAccordionComponent} from '../agent-settings/advanced-accordion.component';
import {deepMergeConfig} from '../agent-settings/config-merge';
import {assembleExpertConfig, splitExpertConfig} from './expert-config';
import {isModelAllowed} from '../agent-settings/capability-gates';

/** Derive a valid expert slug (^[a-z][a-z0-9_-]*$) from a display name. */
export function slugify(s: string): string {
  const base = s
    .normalize('NFKD')
    .replace(/[^\x20-\x7E]/g, '')
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
  if (!base) return 'expert';
  return /^[a-z]/.test(base) ? base : `e-${base}`;
}

export interface ParsedConfig {
  config?: Record<string, unknown>;
  error?: string;
}

/** Parse the raw config-fragment textarea into an object (or an error string). */
export function parseConfigText(text: string): ParsedConfig {
  const t = text.trim();
  if (!t) return {config: {}};
  let parsed: unknown;
  try {
    parsed = JSON.parse(t);
  } catch (e) {
    return {error: (e as Error).message};
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    return {error: 'Config must be a JSON object'};
  }
  return {config: parsed as Record<string, unknown>};
}

/** Prompt segments the editor can author (one family-agnostic version each). */
export interface PromptFields {
  persona: string;
  instructions: string;
  strategic: string;
  tactical: string;
  summarization: string;
}

/**
 * Assemble the `prompts` save payload from the editor fields. Only non-empty
 * segments are emitted — an empty field inherits the framework default, and
 * because the API replaces the `prompts` column wholesale, a cleared field drops
 * its override. Strategic/tactical are worker-only (session experts don't run the
 * phase loop), so they are excluded in session mode.
 */
export function buildPromptsPayload(
  fields: PromptFields,
  mode: 'job' | 'session',
): Record<string, string> {
  const out: Record<string, string> = {};
  const add = (k: keyof PromptFields): void => {
    if (fields[k].trim()) out[k] = fields[k];
  };
  add('persona');
  add('instructions');
  add('summarization');
  if (mode === 'job') {
    add('strategic');
    add('tactical');
  }
  return out;
}

interface EditorForm {
  name: string;
  display_name: string;
  description: string;
  icon: string;
  color: string;
  tags: string;
  expert_type: 'worker' | 'session';
  persona: string;
  instructions: string;
  strategic: string;
  tactical: string;
  summarization: string;
  strategicModel: string;
  tacticalModel: string;
  sessionModel: string;
  configText: string;
}

@Component({
  selector: 'app-expert-editor',
  standalone: true,
  imports: [
    FormsModule,
    TranslocoPipe,
    AppButtonComponent,
    AppInputComponent,
    AppTextareaComponent,
    AppSelectComponent,
    AppFormFieldComponent,
    AppIconComponent,
    SidebarToggleComponent,
    ExecutionGroupComponent,
    ToolsGroupComponent,
    AdvancedAccordionComponent,
  ],
  template: `
    <div class="editor">
      <header class="head">
        <!-- Full-page route (no page shell), so carry the mobile sidebar toggle
             here too — otherwise the off-canvas nav is unreachable on mobile.
             Renders nothing on desktop (sidebar always expanded). -->
        <app-sidebar-toggle />
        <h1>{{ (isEdit() ? 'experts.edit' : 'experts.new') | transloco }}</h1>
      </header>

      <section class="card">
        <app-form-field label="Display name" [required]="true">
          <app-input [value]="form.display_name" (valueChange)="onDisplayName($event)" />
        </app-form-field>
        <app-form-field label="Name (slug)" [required]="true" hint="lowercase; immutable after create">
          <app-input [value]="form.name" [disabled]="isEdit()" (valueChange)="onNameEdit($event)" />
        </app-form-field>
        <app-form-field label="Type" [required]="true">
          <app-select [value]="form.expert_type" [disabled]="isEdit()" (valueChange)="onTypeChange($event)">
            <option value="worker">worker</option>
            <option value="session">session</option>
          </app-select>
        </app-form-field>
        <app-form-field label="Description">
          <app-input [value]="form.description" (valueChange)="form.description = $event" />
        </app-form-field>
        <div class="row">
          <app-form-field label="Icon (Material Symbol)">
            <app-input [value]="form.icon" (valueChange)="form.icon = $event" placeholder="smart_toy" />
          </app-form-field>
          <app-icon size="xl" [style.color]="form.color">{{ form.icon || 'smart_toy' }}</app-icon>
          <app-form-field label="Color (hex)">
            <app-input [value]="form.color" (valueChange)="form.color = $event" placeholder="#6B7280" />
          </app-form-field>
        </div>
        <app-form-field label="Tags (comma separated)">
          <app-input [value]="form.tags" (valueChange)="form.tags = $event" />
        </app-form-field>
      </section>

      <section class="card">
        <h2>Persona</h2>
        <app-textarea
          [value]="form.persona"
          [rows]="8"
          (valueChange)="form.persona = $event"
          placeholder="The expert's persona / system style…"
        />
        <h2>Instructions (optional)</h2>
        <app-textarea [value]="form.instructions" [rows]="5" (valueChange)="form.instructions = $event" />
      </section>

      <section class="card">
        <h2>Phase prompts (advanced)</h2>
        <p class="hint">
          Override the workflow prompts. Leave a field empty to inherit the
          framework default. Custom strategic/tactical text is treated as
          untrusted and stays subordinate to system rules &amp; safety.
        </p>
        @if (mode() === 'job') {
          <h2>Strategic (planning)</h2>
          <app-textarea
            [value]="form.strategic"
            [rows]="6"
            (valueChange)="form.strategic = $event"
            placeholder="Inherit framework default…"
          />
          <h2>Tactical (execution)</h2>
          <app-textarea
            [value]="form.tactical"
            [rows]="6"
            (valueChange)="form.tactical = $event"
            placeholder="Inherit framework default…"
          />
        }
        <h2>Summarization</h2>
        <app-textarea
          [value]="form.summarization"
          [rows]="5"
          (valueChange)="form.summarization = $event"
          placeholder="Inherit framework default…"
        />
      </section>

      <!-- Structured config: reuses the launch-flow groups. [config] = base ⊕ fragment
           drives "default: X" displays; getOverrides() + the model selects feed the
           saved config (see save()). -->
      <section class="card">
        <h2>Execution</h2>
        <app-execution-group
          [config]="baseForGroups()"
          [mode]="mode()"
          [disabled]="false"
          [showProjectMemory]="false"
          [gatedCapabilities]="gatedCapabilities()"
          [catalog]="catalog()"
        />
      </section>

      <section class="card">
        <h2>Model</h2>
        @if (mode() === 'job') {
          <label class="ml">Strategic model
            <select class="model-select" [disabled]="isModelGated()" [ngModel]="form.strategicModel" (ngModelChange)="form.strategicModel = $event">
              <option [ngValue]="''">(base default)</option>
              @for (g of models(); track g.group) {
                <optgroup [label]="g.group">
                  @for (m of g.models; track m) { <option [ngValue]="m" [disabled]="!modelAllowed(m)">{{ m }}</option> }
                </optgroup>
              }
            </select>
          </label>
          <label class="ml">Tactical model
            <select class="model-select" [disabled]="isModelGated()" [ngModel]="form.tacticalModel" (ngModelChange)="form.tacticalModel = $event">
              <option [ngValue]="''">(base default)</option>
              @for (g of models(); track g.group) {
                <optgroup [label]="g.group">
                  @for (m of g.models; track m) { <option [ngValue]="m" [disabled]="!modelAllowed(m)">{{ m }}</option> }
                </optgroup>
              }
            </select>
          </label>
        } @else {
          <label class="ml">Model
            <select class="model-select" [disabled]="isModelGated()" [ngModel]="form.sessionModel" (ngModelChange)="form.sessionModel = $event">
              <option [ngValue]="''">(base default)</option>
              @for (g of models(); track g.group) {
                <optgroup [label]="g.group">
                  @for (m of g.models; track m) { <option [ngValue]="m" [disabled]="!modelAllowed(m)">{{ m }}</option> }
                </optgroup>
              }
            </select>
          </label>
        }
        @if (isModelGated()) {
          <small class="lock-hint">🔒 {{ 'grants.locked.model_selection' | transloco }}</small>
        }
      </section>

      <section class="card">
        <h2>Tools</h2>
        <app-tools-group
          [config]="baseForGroups()"
          [mode]="mode()"
          [disabled]="false"
          [defaultsTools]="defaultsTools()"
          [gatedCapabilities]="gatedCapabilities()"
        />
      </section>

      <section class="card">
        <h2>Advanced</h2>
        <app-advanced-accordion
          [config]="baseForGroups()"
          [mode]="mode()"
          [disabled]="false"
          [settingsMatrix]="settingsMatrix()"
          [strategicModelOverride]="form.strategicModel || null"
          [tacticalModelOverride]="form.tacticalModel || null"
          [sessionModelOverride]="form.sessionModel || null"
        />
      </section>

      <section class="card">
        <h2>Advanced (other keys) — JSON</h2>
        <p class="hint">
          Config keys not covered by the controls above (e.g. <code>instruction_files</code>).
          Credential sections are rejected on save.
        </p>
        <app-textarea
          [value]="form.configText"
          [rows]="6"
          (valueChange)="form.configText = $event"
          placeholder='{ "instruction_files": [] }'
        />
      </section>

      @if (errorMessage()) {
        <div class="banner err">{{ errorMessage() }}</div>
      }

      <footer class="actions">
        <app-button variant="secondary" (clicked)="cancel()">{{ 'experts.cancel' | transloco }}</app-button>
        <app-button variant="primary" [loading]="saving()" (clicked)="save()">
          {{ 'experts.save' | transloco }}
        </app-button>
      </footer>
    </div>
  `,
  styles: [
    `
      :host {
        display: block;
        height: 100%;
        overflow-y: auto;
      }
      .editor {
        padding: 1rem 1.5rem;
      }
      .head {
        display: flex;
        flex-direction: column;
        align-items: flex-start;
        gap: 0.5rem;
        margin-bottom: 1rem;
      }
      .head h1 {
        margin: 0;
      }
      .card {
        background: var(--panel-bg);
        border: 1px solid var(--border-color);
        color: var(--text-primary);
        padding: 1rem;
        border-radius: var(--radius-surface, 8px);
        margin-bottom: 1rem;
        display: flex;
        flex-direction: column;
        gap: 0.75rem;
      }
      .card h2 {
        margin: 0;
        font-size: 0.95rem;
        color: var(--text-primary);
      }
      .row {
        display: flex;
        gap: 1rem;
        align-items: flex-end;
      }
      .ml {
        display: flex;
        flex-direction: column;
        gap: 0.25rem;
        font-size: 0.85rem;
        color: var(--text-secondary);
      }
      .model-select {
        padding: 7px 10px;
        border: 1px solid var(--border-color);
        border-radius: var(--radius-control, 6px);
        background: var(--surface-0, var(--panel-bg));
        color: var(--text-primary);
        font: inherit;
      }
      .model-select:disabled {
        opacity: 0.55;
        cursor: not-allowed;
      }
      .lock-hint {
        display: block;
        margin-top: 0.35rem;
        color: var(--text-muted);
        font-size: 0.8rem;
      }
      .hint {
        margin: 0;
        color: var(--text-muted);
        font-size: 0.85rem;
      }
      .actions {
        display: flex;
        justify-content: flex-end;
        gap: 0.5rem;
      }
      .banner.err {
        background: var(--danger-tint);
        color: var(--danger);
        padding: 0.5rem 0.75rem;
        border-radius: 6px;
        margin-bottom: 1rem;
      }
    `,
  ],
})
export class ExpertEditorComponent implements OnInit {
  private api = inject(ApiService);
  private router = inject(Router);
  private route = inject(ActivatedRoute);
  private transloco = inject(TranslocoService);
  private modelService = inject(ModelService);

  private execGroup = viewChild(ExecutionGroupComponent);
  private toolsGroup = viewChild(ToolsGroupComponent);
  private advancedGroup = viewChild(AdvancedAccordionComponent);

  editingId = signal<string | null>(null);
  saving = signal(false);
  errorMessage = signal('');
  private slugTouched = false;

  // Async-loaded context.
  frameworkDefaults = signal<Record<string, unknown>>({});
  defaultsTools = signal<Record<string, string[]>>({});
  settingsMatrix = signal<Record<string, Record<string, unknown>>>({});
  /** The expert's stored config fragment (the save baseline). {} on create. */
  rawFragment = signal<Record<string, unknown>>({});
  private prefillDone = false;

  readonly models = this.modelService.models;

  // Capability grants → editor control-greying. undefined = loading; null = admin
  // (unrestricted, no gating). A resolved record gates per the deny-default PDP.
  capabilities = signal<Record<string, unknown> | null | undefined>(undefined);
  catalog = signal<GrantCatalog>({});
  /** What the group components consume; null ⇒ no gating (admin / loading). */
  gatedCapabilities = computed(() => this.capabilities() ?? null);
  /** True only when a model_selection restriction is in force. */
  isModelGated = computed(() => {
    const g = this.capabilities();
    return g != null && Array.isArray((g as Record<string, unknown>)['model_selection']);
  });
  modelAllowed = (id: string): boolean => isModelAllowed(this.capabilities() ?? null, id);

  form: EditorForm = {
    name: '',
    display_name: '',
    description: '',
    icon: 'smart_toy',
    color: '#6B7280',
    tags: '',
    expert_type: 'worker',
    persona: '',
    instructions: '',
    strategic: '',
    tactical: '',
    summarization: '',
    strategicModel: '',
    tacticalModel: '',
    sessionModel: '',
    configText: '',
  };

  isEdit = computed(() => this.editingId() !== null);
  mode = computed<'job' | 'session'>(() => (this.form.expert_type === 'session' ? 'session' : 'job'));
  /** Base config for the structured controls' "default: X" displays: the type
   *  base merged with the expert's own fragment. */
  baseForGroups = computed(() => deepMergeConfig(this.frameworkDefaults(), this.rawFragment()));

  constructor() {
    // Prefill the structured controls once the fragment is loaded AND the
    // view-children exist (defeats the ViewChild-null race on fast responses).
    effect(() => {
      const frag = this.rawFragment();
      const ready = this.execGroup() && this.toolsGroup() && this.advancedGroup();
      if (!this.prefillDone && ready && Object.keys(frag).length) {
        this.prefillDone = true;
        this.applyPrefill(frag);
      }
    });
  }

  ngOnInit(): void {
    this.modelService.load();
    // Author's capabilities → grey controls they lack grants for. Fail-open on
    // error (null = no gating); the save-time 422 remains the backstop.
    this.api.getMyCapabilities().subscribe((c) => {
      this.capabilities.set(c ? c.grants : null);
      this.catalog.set(c?.catalog ?? {});
    });
    // Type base (worker + session both use `defaults`): drives default displays
    // + the tools-group baseline.
    this.api.getExpertDetail('defaults').subscribe((d) => {
      this.frameworkDefaults.set((d?.config as Record<string, unknown>) ?? {});
      this.defaultsTools.set(d?.defaults_tools ?? {});
      this.settingsMatrix.set(d?.settings_matrix ?? {});
    });

    const id = this.route.snapshot.paramMap.get('id');
    if (!id) return;
    this.editingId.set(id);
    // Prefill from the export bundle = the RAW fragment (never the merged result).
    this.api.exportExpert(id).subscribe((bundle) => {
      const b = (bundle ?? {}) as Record<string, unknown>;
      this.form.name = (b['name'] as string) ?? '';
      this.form.display_name = (b['display_name'] as string) ?? '';
      this.form.description = (b['description'] as string) ?? '';
      this.form.icon = (b['icon'] as string) ?? 'smart_toy';
      this.form.color = (b['color'] as string) ?? '#6B7280';
      this.form.tags = Array.isArray(b['tags']) ? (b['tags'] as string[]).join(', ') : '';
      this.form.expert_type = b['expert_type'] === 'session' ? 'session' : 'worker';
      const prompts = (b['prompts'] ?? {}) as Record<string, unknown>;
      this.form.persona = (prompts['persona'] as string) ?? '';
      this.form.instructions = (prompts['instructions'] as string) ?? '';
      this.form.strategic = (prompts['strategic'] as string) ?? '';
      this.form.tactical = (prompts['tactical'] as string) ?? '';
      this.form.summarization = (prompts['summarization'] as string) ?? '';
      const cfg = (b['config'] ?? {}) as Record<string, unknown>;
      // Raw flap shows only the keys the structured controls don't own.
      const {rawRemainderText} = splitExpertConfig(cfg);
      this.form.configText = rawRemainderText;
      this.rawFragment.set(cfg); // triggers the prefill effect
    });
  }

  /** Seed the structured controls + model selects from the stored fragment. */
  private applyPrefill(frag: Record<string, unknown>): void {
    const llm = (frag['llm'] ?? {}) as Record<string, unknown>;
    const strat = (llm['strategic'] ?? {}) as Record<string, unknown>;
    const tact = (llm['tactical'] ?? {}) as Record<string, unknown>;
    const baseModel = (llm['model'] as string) ?? '';
    this.form.strategicModel = (strat['model'] as string) ?? baseModel ?? '';
    this.form.tacticalModel = (tact['model'] as string) ?? baseModel ?? '';
    this.form.sessionModel = baseModel;

    this.toolsGroup()?.prefillFromConfig(frag);
    this.advancedGroup()?.prefillFromConfig(frag);

    const exec = this.execGroup();
    exec?.resetAll();
    const autonomy = frag['autonomy'] as string | undefined;
    if (autonomy) exec?.autonomy.set(autonomy);
    const scholar = (frag['scholar'] as Record<string, unknown>)?.['enabled'] as boolean | undefined;
    if (scholar !== undefined) exec?.scholar.set(scholar);
    const verification = frag['verification'] as Record<string, unknown> | undefined;
    if (verification?.['enabled'] !== undefined) exec?.critic.set(verification['enabled'] as boolean);
    if (verification?.['max_rounds'] !== undefined) {
      exec?.criticRounds.set(verification['max_rounds'] as number);
    }
    const interactive = frag['interactive'] as Record<string, unknown> | undefined;
    const pm = interactive?.['permission_mode'] as string | undefined;
    if (pm) exec?.permissionMode.set(pm);
  }

  onDisplayName(v: string): void {
    this.form.display_name = v;
    if (!this.slugTouched && !this.isEdit()) {
      this.form.name = slugify(v);
    }
  }

  onNameEdit(v: string): void {
    this.slugTouched = true;
    this.form.name = v;
  }

  onTypeChange(v: 'worker' | 'session' | null): void {
    if (v !== 'worker' && v !== 'session') return;
    this.form.expert_type = v;
    // Structured state differs by mode (tool categories, autonomy vs permission
    // mode); reset so a worker→session switch on CREATE doesn't carry stale state.
    this.execGroup()?.resetAll();
    this.toolsGroup()?.resetAll();
    this.advancedGroup()?.resetAll();
    this.form.strategicModel = '';
    this.form.tacticalModel = '';
    this.form.sessionModel = '';
    // Phase-prompt overrides are mode-specific (strategic/tactical are worker-
    // only) — clear them so a worker→session switch on CREATE doesn't carry over.
    this.form.strategic = '';
    this.form.tactical = '';
    this.form.summarization = '';
  }

  /** Assemble the prompts payload for save (see buildPromptsPayload). */
  private buildPrompts(): Record<string, string> {
    return buildPromptsPayload(this.form, this.mode());
  }

  /** Build the model-select config fragment for the current mode. */
  private modelOverride(): Record<string, unknown> {
    const llm: Record<string, unknown> = {};
    if (this.mode() === 'job') {
      if (this.form.strategicModel) llm['strategic'] = {model: this.form.strategicModel};
      if (this.form.tacticalModel) llm['tactical'] = {model: this.form.tacticalModel};
    } else if (this.form.sessionModel) {
      llm['model'] = this.form.sessionModel;
    }
    return Object.keys(llm).length ? {llm} : {};
  }

  save(): void {
    this.errorMessage.set('');
    const parsed = parseConfigText(this.form.configText);
    if (parsed.error) {
      this.errorMessage.set(`Config: ${parsed.error}`);
      return;
    }
    // Merge the structured controls' fragments + the model selects.
    const groupOverrides = [
      this.execGroup()?.getOverrides() ?? {},
      this.toolsGroup()?.getOverrides() ?? {},
      this.advancedGroup()?.getOverrides() ?? {},
      this.modelOverride(),
    ].reduce((acc, frag) => deepMergeConfig(acc, frag), {} as Record<string, unknown>);

    const config = assembleExpertConfig(this.rawFragment(), groupOverrides, parsed.config ?? {});

    const payload: ExpertCreateRequest = {
      name: this.form.name,
      display_name: this.form.display_name,
      expert_type: this.form.expert_type,
      description: this.form.description || null,
      icon: this.form.icon,
      color: this.form.color,
      tags: this.form.tags
        .split(',')
        .map((t) => t.trim())
        .filter(Boolean),
      config,
      prompts: this.buildPrompts(),
    };
    this.saving.set(true);
    const id = this.editingId();
    const obs = id
      ? this.api.updateExpert(id, payload as ExpertUpdateRequest)
      : this.api.createExpert(payload);
    obs.subscribe({
      next: () => this.router.navigate(['/experts']),
      error: (err) => {
        this.saving.set(false);
        const d = (err as {error?: {detail?: unknown}})?.error?.detail;
        this.errorMessage.set(
          typeof d === 'string' ? d : this.transloco.translate('experts.saveFailed'),
        );
      },
    });
  }

  cancel(): void {
    this.router.navigate(['/experts']);
  }
}
