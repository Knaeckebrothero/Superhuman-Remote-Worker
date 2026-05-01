import {ChangeDetectionStrategy, Component, computed, inject, OnInit, signal} from '@angular/core';
import {SidebarToggleComponent} from '../../../shell/sidebar-toggle/sidebar-toggle.component';
import {AdminModelsService} from '../../../core/services/admin-models.service';
import {AdminProvidersService} from '../../../core/services/admin-providers.service';
import {
  CATALOG_ROLES,
  CatalogModel,
  CatalogModelTestResult,
  CatalogProviderKind,
  CatalogRole,
  LlmEndpointDiscoveredModel,
} from '../../../core/models/api.model';
import {AppButtonComponent} from '../../../ui/button';
import {AppInputComponent} from '../../../ui/input';
import {AppSelectComponent} from '../../../ui/select';
import {AppCheckboxComponent} from '../../../ui/checkbox';
import {AppFormFieldComponent} from '../../../ui/form-field';
import {AppBadgeComponent} from '../../../ui/badge';

interface ProviderOption {
  kind: CatalogProviderKind;
  ref: string;
  label: string;
  available: boolean;
}

/**
 * Well-known label for the seeded codex-proxy llm_endpoints row. The
 * orchestrator's _seed_codex_proxy_endpoint inserts a row with this label
 * when CODEX_PROXY_URL is set; the frontend uses it to detect when the
 * codex source needs the special "subscription" affordance (status banner,
 * deep link to OAuth login).
 */
const CODEX_PROXY_LABEL = 'codex-proxy';

/** Maps the discover endpoint's capability hint onto a catalog role. */
function hintToRole(hint: string | null | undefined): CatalogRole {
  switch (hint) {
    case 'auxiliary':
    case 'embedding':
    case 'vision':
    case 'whisper':
    case 'tts':
      return hint;
    default:
      return 'chat';
  }
}

@Component({
  selector: 'app-admin-models',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    SidebarToggleComponent,
    AppButtonComponent,
    AppInputComponent,
    AppSelectComponent,
    AppCheckboxComponent,
    AppFormFieldComponent,
    AppBadgeComponent,
  ],
  template: `
    <div class="admin-page">
      <div class="admin-container">
        <div class="page-header">
          <app-sidebar-toggle />
          <h1 class="page-title">Models Catalog</h1>
        </div>
        <p class="page-desc">
          Curate the LLM offerings available in builder, sessions, and jobs.
          Each model anchors to a configured provider (system API key) or a
          system endpoint.
        </p>

        <!-- Models list -->
        <section class="admin-section">
          <h2 class="section-title">Catalog rows</h2>

          @if (models.loading()) {
            <p class="muted">Loading…</p>
          } @else if (models.models().length === 0) {
            <p class="empty-state">
              Add a model from a configured provider to make it available
              in builder, sessions, and jobs.
            </p>
          } @else {
            @for (group of groupedModels(); track group.key) {
              <div class="provider-group">
                <h3 class="group-title">{{ group.label }}</h3>
                <div class="model-table">
                  <div class="model-header">
                    <span class="col-display">Display label</span>
                    <span class="col-id">Model ID</span>
                    <span class="col-role">Role</span>
                    <span class="col-family">Family</span>
                    <span class="col-enabled">Enabled</span>
                    <span class="col-actions"></span>
                  </div>
                  @for (m of group.rows; track m.id) {
                    <div class="model-row">
                      <span class="col-display">{{ m.display_label }}</span>
                      <span class="col-id mono">{{ m.model_id }}</span>
                      <span class="col-role">{{ m.role }}</span>
                      <span class="col-family">{{ m.family }}</span>
                      <span class="col-enabled">
                        <app-checkbox
                          size="sm"
                          [checked]="m.enabled"
                          [ariaLabel]="'Enabled: ' + m.display_label"
                          (changed)="toggleEnabled(m, $event)"
                        />
                      </span>
                      <span class="col-actions">
                        <app-button
                          variant="secondary"
                          size="sm"
                          [loading]="testing() === m.id"
                          [disabled]="testing() === m.id"
                          (clicked)="testRow(m.id)"
                        >
                          {{ testing() === m.id ? 'Testing…' : 'Test' }}
                        </app-button>
                        <app-button
                          variant="danger"
                          size="sm"
                          (clicked)="deleteRow(m)"
                        >
                          Delete
                        </app-button>
                        @if (testResults()[m.id]; as result) {
                          <app-badge
                            [tone]="result.ok ? 'success' : 'danger'"
                            size="xs"
                          >
                            {{ result.ok ? 'OK' : (result.error || 'failed') }}
                          </app-badge>
                        }
                      </span>
                    </div>
                  }
                </div>
              </div>
            }
          }
        </section>

        <!-- Add model form -->
        <section class="admin-section">
          <h2 class="section-title">Add model</h2>

          <div class="create-form">
            <div class="form-row two-col">
              <app-form-field label="Provider">
                <app-select
                  [value]="formProviderKey()"
                  [disabled]="creating()"
                  (changed)="onProviderKeyChange($event)"
                >
                  @for (p of providerOptions(); track p.kind + ':' + p.ref) {
                    <option
                      [value]="p.kind + ':' + p.ref"
                      [disabled]="!p.available"
                    >
                      {{ p.label }}{{ p.available ? '' : ' (not configured)' }}
                    </option>
                  }
                </app-select>
              </app-form-field>
              <app-form-field label="Role">
                <app-select
                  [value]="formRole()"
                  [disabled]="creating()"
                  (changed)="onRoleChange($event)"
                >
                  @for (r of roles; track r) {
                    <option [value]="r">{{ r }}</option>
                  }
                </app-select>
              </app-form-field>
            </div>

            @if (selectedEndpointRef(); as endpointRef) {
              <div class="discover-pane">
                @if (selectedIsCodex()) {
                  @if (providers.codexAvailability(); as codex) {
                    <div
                      class="codex-status"
                      [class.ok]="codex.available"
                      [class.warn]="!codex.available"
                    >
                      @if (codex.available) {
                        <span>
                          ✓ Codex proxy active —
                          {{ codex.account_count }} subscription{{ codex.account_count === 1 ? '' : 's' }} logged in
                        </span>
                      } @else {
                        <span>
                          ⚠ No active codex subscription. Log in via Settings → Codex
                          before testing models, otherwise dispatched calls will 401.
                        </span>
                      }
                    </div>
                  }
                }
                <div class="discover-actions">
                  <app-button
                    variant="secondary"
                    size="sm"
                    [loading]="discovering()"
                    [disabled]="discovering()"
                    (clicked)="discoverFromEndpoint(endpointRef)"
                  >
                    {{ discovering() ? 'Discovering…' : 'Discover available models' }}
                  </app-button>
                  @if (discoverError()) {
                    <app-badge tone="danger" size="xs">{{ discoverError() }}</app-badge>
                  } @else if (discoveredModels().length > 0) {
                    <span class="muted">
                      Click a model to autofill ID and label:
                    </span>
                  }
                </div>
                @if (discoveredModels().length > 0) {
                  <div class="discover-list">
                    @for (m of discoveredModels(); track m.id) {
                      <button
                        type="button"
                        class="discover-chip"
                        (click)="applyDiscoveredModel(m)"
                      >
                        <span class="mono">{{ m.id }}</span>
                        <span class="discover-cap">{{ m.capability_hint }}</span>
                      </button>
                    }
                  </div>
                }
              </div>
            }

            <div class="form-row two-col">
              <app-form-field label="Model ID">
                <app-input
                  [value]="formModelId()"
                  placeholder="e.g. claude-opus-4-7"
                  [disabled]="creating()"
                  (changed)="formModelId.set($event)"
                />
              </app-form-field>
              <app-form-field label="Display label">
                <app-input
                  [value]="formDisplayLabel()"
                  placeholder="Auto-suggested from ID"
                  [disabled]="creating()"
                  (changed)="formDisplayLabel.set($event)"
                />
              </app-form-field>
            </div>

            <div class="form-row two-col">
              <app-form-field label="Family">
                <app-select
                  [value]="formFamily()"
                  [disabled]="creating()"
                  (changed)="onFamilyChange($event)"
                >
                  @for (f of models.families(); track f) {
                    <option [value]="f">{{ f }}</option>
                  }
                </app-select>
              </app-form-field>
              <app-form-field label="Context window (optional)">
                <app-input
                  type="number"
                  [value]="formContextWindowText()"
                  [disabled]="creating()"
                  (changed)="onContextWindowChange($event)"
                />
              </app-form-field>
            </div>

            @if (formError()) {
              <p class="form-error">{{ formError() }}</p>
            }

            <div class="form-row">
              <app-button
                variant="primary"
                size="md"
                [loading]="creating()"
                [disabled]="!canSubmit() || creating()"
                (clicked)="submit()"
              >
                {{ creating() ? 'Adding…' : 'Add model' }}
              </app-button>
            </div>
          </div>
        </section>
      </div>
    </div>
  `,
  styles: [`
    :host {
      display: block;
      height: 100%;
      overflow: auto;
    }
    .admin-page { padding: 24px; }
    .admin-container { max-width: 1100px; margin: 0 auto; }
    .page-header { display: flex; align-items: center; gap: 12px; }
    .page-title {
      font-size: 22px;
      font-weight: 600;
      margin: 0;
      color: var(--text-primary);
    }
    .page-desc { color: var(--text-muted); margin: 8px 0 24px 0; }
    .admin-section { margin-bottom: 32px; }
    .section-title {
      font-size: 16px;
      font-weight: 600;
      margin: 0 0 12px 0;
      color: var(--text-primary);
    }
    .provider-group { margin-bottom: 18px; }
    .group-title {
      font-size: 13px;
      font-weight: 600;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.4px;
      margin: 12px 0 6px 0;
    }
    .model-table {
      display: flex;
      flex-direction: column;
      gap: 4px;
    }
    .model-header,
    .model-row {
      display: grid;
      grid-template-columns: 1.4fr 2fr 90px 100px 70px 260px;
      gap: 8px;
      align-items: center;
      padding: 8px 12px;
      font-size: 13px;
    }
    .model-header {
      font-weight: 600;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.3px;
      font-size: 11px;
    }
    .model-row {
      background: var(--surface-0);
      border-radius: 6px;
      color: var(--text-primary);
    }
    .mono { font-family: ui-monospace, monospace; font-size: 12px; }
    .empty-state {
      padding: 24px;
      text-align: center;
      color: var(--text-muted);
      background: var(--surface-0);
      border-radius: 6px;
    }
    .muted { color: var(--text-muted); }
    .col-actions {
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
    }
    .create-form {
      padding: 16px;
      background: var(--surface-0);
      border-radius: 8px;
    }
    .form-row {
      display: flex;
      gap: 12px;
      margin-bottom: 12px;
    }
    .form-row.two-col > * { flex: 1; }
    .form-error {
      color: var(--danger);
      font-size: 12px;
      margin: 4px 0 8px 0;
    }
    .discover-pane {
      margin-bottom: 12px;
      padding: 10px 12px;
      background: var(--app-bg);
      border: 1px dashed var(--border-color);
      border-radius: 6px;
    }
    .discover-actions {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }
    .discover-list {
      margin-top: 10px;
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }
    .discover-chip {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 4px 10px;
      background: var(--surface-0);
      border: 1px solid var(--border-color);
      border-radius: 14px;
      font-size: 12px;
      color: var(--text-primary);
      cursor: pointer;
      font-family: inherit;
    }
    .discover-chip:hover {
      border-color: var(--accent-color);
      color: var(--accent-color);
    }
    .discover-cap {
      font-size: 10px;
      text-transform: uppercase;
      letter-spacing: 0.4px;
      color: var(--text-muted);
    }
    .codex-status {
      margin-bottom: 10px;
      padding: 6px 10px;
      border-radius: 6px;
      font-size: 12px;
      line-height: 1.4;
    }
    .codex-status.ok {
      background: var(--success-tint);
      color: var(--success);
    }
    .codex-status.warn {
      background: var(--danger-tint);
      color: var(--danger);
    }
  `],
})
export class AdminModelsComponent implements OnInit {
  readonly models = inject(AdminModelsService);
  readonly providers = inject(AdminProvidersService);

  readonly roles: CatalogRole[] = CATALOG_ROLES;
  readonly creating = signal(false);
  readonly testing = signal<string | null>(null);
  readonly testResults = signal<Record<string, CatalogModelTestResult>>({});
  readonly formError = signal<string>('');

  // Form state — signals so OnPush picks up updates from primitive callbacks.
  readonly formProviderKey = signal('');
  readonly formRole = signal<CatalogRole>('chat');
  readonly formModelId = signal('');
  readonly formDisplayLabel = signal('');
  readonly formFamily = signal('default');
  readonly formContextWindow = signal<number | null>(null);

  // Mirror for the number input — keeps an empty string when null so the
  // input renders blank instead of "0".
  readonly formContextWindowText = computed(() => {
    const v = this.formContextWindow();
    return v == null ? '' : String(v);
  });

  // Discover-from-endpoint quick-fill state. Resets when the form provider
  // changes (any non-endpoint provider clears the list).
  readonly discovering = signal(false);
  readonly discoveredModels = signal<LlmEndpointDiscoveredModel[]>([]);
  readonly discoverError = signal<string>('');

  readonly selectedEndpointRef = computed<string | null>(() => {
    const key = this.formProviderKey();
    if (!key.startsWith('endpoint:')) return null;
    return key.slice('endpoint:'.length);
  });

  /** Provider dropdown options sourced from the keys + endpoints lists.
   * Non-LLM providers (`vision`) are filtered out — they live in
   * `system_api_keys` for env injection but can't anchor a catalog row.
   * The seeded `codex-proxy` endpoint is rendered as a "subscription"
   * source so admins recognise it as separate from a generic vLLM/Ollama
   * endpoint. */
  readonly providerOptions = computed<ProviderOption[]>(() => {
    const opts: ProviderOption[] = [];
    for (const key of this.providers.systemApiKeys()) {
      if (key.provider === 'vision') continue;
      opts.push({
        kind: 'system',
        ref: key.provider,
        label: key.provider,
        available: true,
      });
    }
    const codex = this.providers.codexAvailability();
    for (const ep of this.providers.systemEndpoints()) {
      const isCodex = ep.label === CODEX_PROXY_LABEL;
      opts.push({
        kind: 'endpoint',
        ref: ep.id,
        label: isCodex
          ? `${ep.label} (codex subscription)`
          : `${ep.label} (endpoint)`,
        // Codex proxy is "available" for catalog authoring even without an
        // active subscription — admins may seed catalog rows ahead of OAuth
        // login. The runtime status banner below tells them when login is
        // needed.
        available: isCodex ? true : true,
      });
    }
    void codex;
    return opts;
  });

  /** True when the form provider is the seeded codex-proxy endpoint. */
  readonly selectedIsCodex = computed(() => {
    const ref = this.selectedEndpointRef();
    if (!ref) return false;
    const ep = this.providers.systemEndpoints().find((e) => e.id === ref);
    return ep?.label === CODEX_PROXY_LABEL;
  });

  readonly groupedModels = computed(() => {
    const groups = new Map<string, {key: string; label: string; rows: CatalogModel[]}>();
    for (const m of this.models.models()) {
      const key = `${m.provider_kind}:${m.provider_ref}`;
      const label = m.provider_kind === 'endpoint'
        ? this.endpointLabel(m.provider_ref)
        : m.provider_ref;
      if (!groups.has(key)) {
        groups.set(key, {key, label, rows: []});
      }
      groups.get(key)!.rows.push(m);
    }
    return Array.from(groups.values()).sort((a, b) => a.label.localeCompare(b.label));
  });

  ngOnInit(): void {
    this.models.loadModels();
    this.models.loadFamilies();
    this.providers.loadSystemApiKeys();
    this.providers.loadSystemEndpoints();
    this.providers.loadCodexAvailability();
  }

  endpointLabel(refId: string): string {
    const ep = this.providers.systemEndpoints().find((e) => e.id === refId);
    return ep ? `${ep.label} (endpoint)` : `endpoint:${refId}`;
  }

  canSubmit(): boolean {
    return (
      !!this.formProviderKey() &&
      !!this.formModelId().trim() &&
      !!this.formDisplayLabel().trim() &&
      !!this.formFamily().trim()
    );
  }

  onProviderKeyChange(value: string | null): void {
    this.formProviderKey.set(value ?? '');
    this.discoveredModels.set([]);
    this.discoverError.set('');
    // Provider change invalidates any prior model-specific autofill — wiping
    // the form fields prevents a stale model_id from being submitted under a
    // different provider.
    this.formModelId.set('');
    this.formDisplayLabel.set('');
    this.formFamily.set('default');
    this.formContextWindow.set(null);
    this.formError.set('');
  }

  onRoleChange(value: string | null): void {
    if (value) this.formRole.set(value as CatalogRole);
  }

  onFamilyChange(value: string | null): void {
    if (value !== null) this.formFamily.set(value);
  }

  onContextWindowChange(text: string): void {
    if (text === '' || text == null) {
      this.formContextWindow.set(null);
      return;
    }
    const n = Number(text);
    this.formContextWindow.set(Number.isFinite(n) ? n : null);
  }

  submit(): void {
    this.formError.set('');
    const [kind, ref] = this.formProviderKey().split(':') as [CatalogProviderKind, string];
    if (!kind || !ref) {
      this.formError.set('Pick a provider.');
      return;
    }
    this.creating.set(true);
    this.models
      .createModel({
        provider_kind: kind,
        provider_ref: ref,
        model_id: this.formModelId().trim(),
        display_label: this.formDisplayLabel().trim(),
        role: this.formRole(),
        family: this.formFamily().trim(),
        context_window: this.formContextWindow() ?? null,
      })
      .subscribe({
        next: () => {
          this.formModelId.set('');
          this.formDisplayLabel.set('');
          this.formContextWindow.set(null);
          this.creating.set(false);
        },
        error: (err) => {
          this.formError.set(err?.error?.detail ?? 'Failed to add model.');
          this.creating.set(false);
        },
      });
  }

  toggleEnabled(model: CatalogModel, checked: boolean): void {
    this.models.updateModel(model.id, {enabled: checked}).subscribe();
  }

  deleteRow(model: CatalogModel): void {
    if (!confirm(`Delete "${model.display_label}" from the catalog?`)) return;
    this.models.deleteModel(model.id).subscribe((res) => {
      if (res.warning) alert(res.warning);
    });
  }

  testRow(id: string): void {
    this.testing.set(id);
    this.models.testModel(id).subscribe({
      next: (result) => {
        this.testResults.update((curr) => ({...curr, [id]: result}));
        this.testing.set(null);
      },
      error: () => this.testing.set(null),
    });
  }

  discoverFromEndpoint(endpointId: string): void {
    this.discovering.set(true);
    this.discoverError.set('');
    this.discoveredModels.set([]);
    this.providers.discoverSystemEndpointModels(endpointId).subscribe({
      next: (result) => {
        this.discovering.set(false);
        if (!result.ok) {
          this.discoverError.set(result.error || 'Discovery failed.');
          return;
        }
        this.discoveredModels.set(result.models);
      },
      error: (err) => {
        this.discovering.set(false);
        this.discoverError.set(err?.error?.detail ?? 'Discovery failed.');
      },
    });
  }

  applyDiscoveredModel(m: LlmEndpointDiscoveredModel): void {
    this.formModelId.set(m.id);
    this.formDisplayLabel.set(m.id);
    this.formRole.set(hintToRole(m.capability_hint));
    this.formFamily.set(m.family || 'default');
    this.formContextWindow.set(m.context_window ?? null);
  }
}
