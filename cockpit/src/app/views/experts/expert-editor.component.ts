import {Component, computed, inject, OnInit, signal} from '@angular/core';
import {ActivatedRoute, Router} from '@angular/router';
import {TranslocoPipe, TranslocoService} from '@jsverse/transloco';
import {ApiService} from '../../core/services/api.service';
import type {ExpertCreateRequest, ExpertUpdateRequest} from '../../core/models/api.model';
import {AppButtonComponent} from '../../ui/button';
import {AppInputComponent} from '../../ui/input';
import {AppTextareaComponent} from '../../ui/textarea';
import {AppSelectComponent} from '../../ui/select';
import {AppFormFieldComponent} from '../../ui/form-field';
import {AppIconComponent} from '../../ui/icon';

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
  configText: string;
}

@Component({
  selector: 'app-expert-editor',
  standalone: true,
  imports: [
    TranslocoPipe,
    AppButtonComponent,
    AppInputComponent,
    AppTextareaComponent,
    AppSelectComponent,
    AppFormFieldComponent,
    AppIconComponent,
  ],
  template: `
    <div class="editor">
      <header class="head">
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
        <h2>Config fragment (JSON)</h2>
        <p class="hint">
          Advanced — merged over the {{ form.expert_type }} base. Credential sections are rejected on save.
        </p>
        <app-textarea
          [value]="form.configText"
          [rows]="10"
          (valueChange)="form.configText = $event"
          placeholder='{ "llm": { "model": "gemma-4-moe" }, "tools": { "shell": false } }'
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
        max-width: 760px;
        margin: 0 auto;
      }
      .head {
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

  editingId = signal<string | null>(null);
  saving = signal(false);
  errorMessage = signal('');
  private slugTouched = false;

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
    configText: '',
  };

  isEdit = computed(() => this.editingId() !== null);

  ngOnInit(): void {
    const id = this.route.snapshot.paramMap.get('id');
    if (!id) return;
    this.editingId.set(id);
    // Prefill from the export bundle = the RAW fragment (never the merged
    // result), so re-saving doesn't bake the type defaults into the fragment.
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
      const cfg = (b['config'] ?? {}) as Record<string, unknown>;
      this.form.configText = Object.keys(cfg).length ? JSON.stringify(cfg, null, 2) : '';
    });
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
    if (v === 'worker' || v === 'session') {
      this.form.expert_type = v;
    }
  }

  save(): void {
    this.errorMessage.set('');
    const parsed = parseConfigText(this.form.configText);
    if (parsed.error) {
      this.errorMessage.set(`Config: ${parsed.error}`);
      return;
    }
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
      config: parsed.config ?? {},
      prompts: {persona: this.form.persona, instructions: this.form.instructions},
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
