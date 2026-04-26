import {Component, computed, inject, OnInit, signal} from '@angular/core';
import {FormsModule} from '@angular/forms';
import {SidebarToggleComponent} from '../../../simple/layout/sidebar-toggle/sidebar-toggle.component';
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
  imports: [FormsModule, SidebarToggleComponent],
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
                        <input
                          type="checkbox"
                          [checked]="m.enabled"
                          (change)="toggleEnabled(m, $event)"
                        />
                      </span>
                      <span class="col-actions">
                        <button
                          class="link-btn"
                          (click)="testRow(m.id)"
                          [disabled]="testing() === m.id"
                        >
                          {{ testing() === m.id ? 'Testing…' : 'Test' }}
                        </button>
                        <button class="revoke-btn" (click)="deleteRow(m)">
                          Delete
                        </button>
                        @if (testResults()[m.id]; as result) {
                          <span
                            class="test-result"
                            [class.ok]="result.ok"
                            [class.fail]="!result.ok"
                          >
                            {{ result.ok ? 'OK' : (result.error || 'failed') }}
                          </span>
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
              <div>
                <label class="field-label">Provider</label>
                <select
                  class="form-input"
                  [(ngModel)]="formProviderKey"
                  (ngModelChange)="onProviderChange()"
                  [disabled]="creating()"
                >
                  @for (p of providerOptions(); track p.kind + ':' + p.ref) {
                    <option
                      [value]="p.kind + ':' + p.ref"
                      [disabled]="!p.available"
                    >
                      {{ p.label }}{{ p.available ? '' : ' (not configured)' }}
                    </option>
                  }
                </select>
              </div>
              <div>
                <label class="field-label">Role</label>
                <select class="form-input" [(ngModel)]="formRole" [disabled]="creating()">
                  @for (r of roles; track r) {
                    <option [value]="r">{{ r }}</option>
                  }
                </select>
              </div>
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
                  <button
                    class="link-btn"
                    (click)="discoverFromEndpoint(endpointRef)"
                    [disabled]="discovering()"
                  >
                    {{ discovering() ? 'Discovering…' : 'Discover available models' }}
                  </button>
                  @if (discoverError()) {
                    <span class="test-result fail">{{ discoverError() }}</span>
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
              <div>
                <label class="field-label">Model ID</label>
                <input
                  type="text"
                  class="form-input mono"
                  placeholder="e.g. claude-opus-4-7"
                  [(ngModel)]="formModelId"
                  [disabled]="creating()"
                />
              </div>
              <div>
                <label class="field-label">Display label</label>
                <input
                  type="text"
                  class="form-input"
                  placeholder="Auto-suggested from ID"
                  [(ngModel)]="formDisplayLabel"
                  [disabled]="creating()"
                />
              </div>
            </div>

            <div class="form-row two-col">
              <div>
                <label class="field-label">Family</label>
                <select class="form-input" [(ngModel)]="formFamily" [disabled]="creating()">
                  @for (f of models.families(); track f) {
                    <option [value]="f">{{ f }}</option>
                  }
                </select>
              </div>
              <div>
                <label class="field-label">Context window (optional)</label>
                <input
                  type="number"
                  class="form-input"
                  [(ngModel)]="formContextWindow"
                  [disabled]="creating()"
                />
              </div>
            </div>

            @if (formError()) {
              <p class="form-error">{{ formError() }}</p>
            }

            <div class="form-row">
              <button
                class="create-btn"
                (click)="submit()"
                [disabled]="!canSubmit() || creating()"
              >
                {{ creating() ? 'Adding…' : 'Add model' }}
              </button>
            </div>
          </div>
        </section>
      </div>
    </div>
  `,
  styles: [`
    .admin-page { padding: 24px; }
    .admin-container { max-width: 1100px; margin: 0 auto; }
    .page-header { display: flex; align-items: center; gap: 12px; }
    .page-title { font-size: 22px; font-weight: 600; margin: 0; }
    .page-desc { color: var(--text-muted, #6c7086); margin: 8px 0 24px 0; }
    .admin-section { margin-bottom: 32px; }
    .section-title { font-size: 16px; font-weight: 600; margin: 0 0 12px 0; }
    .provider-group { margin-bottom: 18px; }
    .group-title {
      font-size: 13px;
      font-weight: 600;
      color: var(--text-muted, #6c7086);
      text-transform: uppercase;
      letter-spacing: 0.4px;
      margin: 12px 0 6px 0;
    }
    .model-table {
      display: flex;
      flex-direction: column;
      gap: 4px;
    }
    .model-header, .model-row {
      display: grid;
      grid-template-columns: 1.4fr 2fr 90px 100px 70px 220px;
      gap: 8px;
      align-items: center;
      padding: 8px 12px;
      font-size: 13px;
    }
    .model-header {
      font-weight: 600;
      color: var(--text-muted, #6c7086);
      text-transform: uppercase;
      letter-spacing: 0.3px;
      font-size: 11px;
    }
    .model-row {
      background: var(--surface-0, #313244);
      border-radius: 6px;
    }
    .mono { font-family: ui-monospace, monospace; font-size: 12px; }
    .empty-state {
      padding: 24px;
      text-align: center;
      color: var(--text-muted, #6c7086);
      background: var(--surface-0, #313244);
      border-radius: 6px;
    }
    .muted { color: var(--text-muted, #6c7086); }
    .col-actions { display: flex; gap: 8px; align-items: center; }
    .link-btn {
      background: none;
      border: 1px solid var(--border-color, #313244);
      border-radius: 6px;
      padding: 4px 10px;
      font-size: 12px;
      color: var(--accent, #89b4fa);
      cursor: pointer;
    }
    .link-btn:disabled { opacity: 0.5; cursor: not-allowed; }
    .revoke-btn {
      background: none;
      border: 1px solid var(--red, #f38ba8);
      color: var(--red, #f38ba8);
      border-radius: 6px;
      padding: 4px 10px;
      font-size: 12px;
      cursor: pointer;
    }
    .test-result { font-size: 11px; }
    .test-result.ok { color: var(--green, #a6e3a1); }
    .test-result.fail { color: var(--red, #f38ba8); }
    .create-form {
      padding: 16px;
      background: var(--surface-0, #313244);
      border-radius: 8px;
    }
    .form-row {
      display: flex;
      gap: 12px;
      margin-bottom: 12px;
    }
    .form-row.two-col > * { flex: 1; }
    .field-label {
      display: block;
      font-size: 12px;
      font-weight: 500;
      color: var(--text-muted, #6c7086);
      margin-bottom: 4px;
    }
    .form-input {
      width: 100%;
      padding: 8px 12px;
      background: var(--timeline-bg, #11111b);
      border: 1px solid var(--border-color, #313244);
      border-radius: 6px;
      color: var(--text-primary, #cdd6f4);
      font-family: inherit;
      font-size: 13px;
      box-sizing: border-box;
    }
    .form-error {
      color: var(--red, #f38ba8);
      font-size: 12px;
      margin: 4px 0 8px 0;
    }
    .create-btn {
      padding: 8px 16px;
      background: var(--accent, #89b4fa);
      border: none;
      border-radius: 6px;
      color: var(--timeline-bg, #11111b);
      font-size: 13px;
      font-weight: 600;
      cursor: pointer;
    }
    .create-btn:disabled { opacity: 0.5; cursor: not-allowed; }
    .discover-pane {
      margin-bottom: 12px;
      padding: 10px 12px;
      background: var(--timeline-bg, #11111b);
      border: 1px dashed var(--border-color, #313244);
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
      background: var(--surface-0, #313244);
      border: 1px solid var(--border-color, #313244);
      border-radius: 14px;
      font-size: 12px;
      color: var(--text-primary, #cdd6f4);
      cursor: pointer;
    }
    .discover-chip:hover {
      border-color: var(--accent, #89b4fa);
      color: var(--accent, #89b4fa);
    }
    .discover-cap {
      font-size: 10px;
      text-transform: uppercase;
      letter-spacing: 0.4px;
      color: var(--text-muted, #6c7086);
    }
    .codex-status {
      margin-bottom: 10px;
      padding: 6px 10px;
      border-radius: 6px;
      font-size: 12px;
      line-height: 1.4;
    }
    .codex-status.ok {
      background: rgba(166, 227, 161, 0.12);
      color: var(--green, #a6e3a1);
    }
    .codex-status.warn {
      background: rgba(243, 139, 168, 0.12);
      color: var(--red, #f38ba8);
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

  // Form state
  formProviderKey = '';
  formRole: CatalogRole = 'chat';
  formModelId = '';
  formDisplayLabel = '';
  formFamily = 'default';
  formContextWindow: number | null = null;

  // Discover-from-endpoint quick-fill state. Resets when the form provider
  // changes (any non-endpoint provider clears the list).
  readonly discovering = signal(false);
  readonly discoveredModels = signal<LlmEndpointDiscoveredModel[]>([]);
  readonly discoverError = signal<string>('');

  selectedEndpointRef(): string | null {
    if (!this.formProviderKey.startsWith('endpoint:')) return null;
    return this.formProviderKey.slice('endpoint:'.length);
  }

  /** Provider dropdown options sourced from the keys + endpoints lists.
   * Non-LLM providers (`tavily`, `vision`) are filtered out — they live in
   * `system_api_keys` for env injection but can't anchor a catalog row.
   * The seeded `codex-proxy` endpoint is rendered as a "subscription"
   * source so admins recognise it as separate from a generic vLLM/Ollama
   * endpoint. */
  readonly providerOptions = computed<ProviderOption[]>(() => {
    const opts: ProviderOption[] = [];
    for (const key of this.providers.systemApiKeys()) {
      if (key.provider === 'tavily' || key.provider === 'vision') continue;
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
    // Stash availability so the template can read it without re-calling.
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
      !!this.formProviderKey &&
      !!this.formModelId.trim() &&
      !!this.formDisplayLabel.trim() &&
      !!this.formFamily.trim()
    );
  }

  submit(): void {
    this.formError.set('');
    const [kind, ref] = this.formProviderKey.split(':') as [CatalogProviderKind, string];
    if (!kind || !ref) {
      this.formError.set('Pick a provider.');
      return;
    }
    this.creating.set(true);
    this.models
      .createModel({
        provider_kind: kind,
        provider_ref: ref,
        model_id: this.formModelId.trim(),
        display_label: this.formDisplayLabel.trim(),
        role: this.formRole,
        family: this.formFamily.trim(),
        context_window: this.formContextWindow ?? null,
      })
      .subscribe({
        next: () => {
          this.formModelId = '';
          this.formDisplayLabel = '';
          this.formContextWindow = null;
          this.creating.set(false);
        },
        error: (err) => {
          this.formError.set(err?.error?.detail ?? 'Failed to add model.');
          this.creating.set(false);
        },
      });
  }

  toggleEnabled(model: CatalogModel, ev: Event): void {
    const checked = (ev.target as HTMLInputElement).checked;
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
    this.formModelId = m.id;
    if (!this.formDisplayLabel.trim()) {
      this.formDisplayLabel = m.id;
    }
    this.formRole = hintToRole(m.capability_hint);
    if (m.family) this.formFamily = m.family;
    if (m.context_window) this.formContextWindow = m.context_window;
  }

  onProviderChange(): void {
    // Clear the discover list whenever the provider changes — a system
    // (API key) provider has no /v1/models endpoint to probe.
    this.discoveredModels.set([]);
    this.discoverError.set('');
  }
}
