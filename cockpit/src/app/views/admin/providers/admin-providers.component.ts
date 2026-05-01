import {ChangeDetectionStrategy, Component, computed, inject, OnInit, signal} from '@angular/core';
import {RouterLink} from '@angular/router';
import {TranslocoPipe, TranslocoService} from '@jsverse/transloco';
import {SidebarToggleComponent} from '../../../shell/sidebar-toggle/sidebar-toggle.component';
import {
  AdminProvidersService,
  DEFAULT_MODEL_KINDS,
  DefaultModelKind,
} from '../../../core/services/admin-providers.service';
import {ModelService} from '../../../core/services/model.service';
import {
  ApiKeyProvider,
  LlmEndpointTestResult,
} from '../../../core/models/api.model';
import {AppButtonComponent} from '../../../ui/button';
import {AppInputComponent} from '../../../ui/input';
import {AppSelectComponent} from '../../../ui/select';
import {AppCheckboxComponent} from '../../../ui/checkbox';
import {AppBadgeComponent} from '../../../ui/badge';
import {AppFormFieldComponent} from '../../../ui/form-field';

/** Providers the admin can seed/rotate — matches VALID_SYSTEM_API_KEY_PROVIDERS. */
const SYSTEM_PROVIDERS: {value: Exclude<ApiKeyProvider, 'codex'>; label: string}[] = [
  {value: 'openai', label: 'OpenAI'},
  {value: 'anthropic', label: 'Anthropic'},
  {value: 'google', label: 'Google'},
  {value: 'groq', label: 'Groq'},
  {value: 'openrouter', label: 'OpenRouter'},
  {value: 'vision', label: 'Vision'},
];

type SystemProviderValue = (typeof SYSTEM_PROVIDERS)[number]['value'];

@Component({
  selector: 'app-admin-providers',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    SidebarToggleComponent,
    TranslocoPipe,
    AppButtonComponent,
    AppInputComponent,
    AppSelectComponent,
    AppCheckboxComponent,
    AppBadgeComponent,
    AppFormFieldComponent,
  ],
  template: `
    <div class="admin-page">
      <div class="admin-container">
        <div class="page-header">
          <app-sidebar-toggle />
          <h1 class="page-title">{{ 'admin.providers.title' | transloco }}</h1>
        </div>
        <p class="page-desc">{{ 'admin.providers.desc' | transloco }}</p>

        <!-- Provider Keys -->
        <section class="admin-section">
          <h2 class="section-title">{{ 'admin.providers.keys.title' | transloco }}</h2>
          <p class="section-desc">{{ 'admin.providers.keys.desc' | transloco }}</p>

          @if (admin.systemApiKeys().length > 0) {
            <div class="key-table">
              <div class="key-header">
                <span class="col-provider">{{ 'admin.providers.keys.colProvider' | transloco }}</span>
                <span class="col-prefix">{{ 'admin.providers.keys.colKey' | transloco }}</span>
                <span class="col-label">{{ 'admin.providers.keys.colLabel' | transloco }}</span>
                <span class="col-source">{{ 'admin.providers.keys.colSource' | transloco }}</span>
                <span class="col-updated">{{ 'admin.providers.keys.colUpdated' | transloco }}</span>
                <span class="col-action"></span>
              </div>
              @for (key of admin.systemApiKeys(); track key.id) {
                <div class="key-row">
                  <span class="col-provider">{{ providerLabel(key.provider) }}</span>
                  <span class="col-prefix mono">{{ key.key_prefix }}...</span>
                  <span class="col-label">{{ key.label || '-' }}</span>
                  <span class="col-source">
                    @if (key.seeded_from) {
                      <app-badge
                        tone="neutral"
                        size="xs"
                        [title]="key.seeded_from"
                      >
                        {{ 'admin.providers.keys.seededBadge' | transloco }}
                      </app-badge>
                    } @else {
                      <span class="muted">{{ 'admin.providers.keys.manualBadge' | transloco }}</span>
                    }
                  </span>
                  <span class="col-updated">{{ formatDate(key.updated_at) }}</span>
                  <span class="col-action">
                    <app-button
                      variant="danger"
                      size="sm"
                      (clicked)="deleteKey(key.provider)"
                    >
                      {{ 'common.delete' | transloco }}
                    </app-button>
                  </span>
                </div>
              }
            </div>
          } @else {
            <p class="empty-state">{{ 'admin.providers.keys.empty' | transloco }}</p>
          }

          <div class="create-form">
            <h3 class="form-title">{{ 'admin.providers.keys.addTitle' | transloco }}</h3>
            <div class="form-row two-col">
              <app-form-field>
                <app-select
                  [value]="keyProvider()"
                  [disabled]="savingKey()"
                  (changed)="onKeyProviderChange($event)"
                >
                  @for (p of providers; track p.value) {
                    <option [value]="p.value">{{ p.label }}</option>
                  }
                </app-select>
              </app-form-field>
              <app-form-field>
                <app-input
                  [value]="keyLabel()"
                  [placeholder]="'admin.providers.keys.labelPlaceholder' | transloco"
                  [disabled]="savingKey()"
                  (changed)="keyLabel.set($event)"
                />
              </app-form-field>
            </div>
            <div class="form-row">
              <app-form-field>
                <app-input
                  type="password"
                  [value]="keyValue()"
                  [placeholder]="'admin.providers.keys.keyPlaceholder' | transloco"
                  [disabled]="savingKey()"
                  (changed)="keyValue.set($event)"
                />
              </app-form-field>
            </div>
            <app-button
              variant="primary"
              size="md"
              [loading]="savingKey()"
              [disabled]="savingKey() || !keyValue().trim()"
              (clicked)="saveKey()"
            >
              {{ savingKey() ? ('common.saving' | transloco) : ('admin.providers.keys.saveButton' | transloco) }}
            </app-button>
          </div>
        </section>

        <!-- System Endpoints -->
        <section class="admin-section section-spacer">
          <h2 class="section-title">{{ 'admin.providers.endpoints.title' | transloco }}</h2>
          <p class="section-desc">{{ 'admin.providers.endpoints.desc' | transloco }}</p>

          @if (admin.systemEndpoints().length > 0) {
            @for (endpoint of admin.systemEndpoints(); track endpoint.id) {
              <div class="endpoint-card">
                <div class="endpoint-head">
                  <div class="endpoint-title">
                    <strong>{{ endpoint.label }}</strong>
                    @if (isCodexEndpoint(endpoint.label)) {
                      <app-badge tone="info" size="xs" [uppercase]="true" shape="pill">
                        codex subscription
                      </app-badge>
                    }
                    <span class="endpoint-url mono">{{ endpoint.base_url }}</span>
                    @if (endpoint.key_prefix) {
                      <span class="endpoint-key mono">key {{ endpoint.key_prefix }}...</span>
                    } @else {
                      <span class="endpoint-key muted">{{ 'admin.providers.endpoints.noKey' | transloco }}</span>
                    }
                  </div>
                  <div class="endpoint-actions">
                    <app-button
                      variant="secondary"
                      size="sm"
                      [loading]="testingEndpointId() === endpoint.id"
                      [disabled]="testingEndpointId() === endpoint.id"
                      (clicked)="testEndpoint(endpoint.id)"
                    >
                      {{ testingEndpointId() === endpoint.id
                          ? ('admin.providers.endpoints.testing' | transloco)
                          : ('admin.providers.endpoints.testButton' | transloco) }}
                    </app-button>
                    <app-button
                      variant="danger"
                      size="sm"
                      (clicked)="deleteEndpoint(endpoint.id)"
                    >
                      {{ 'common.delete' | transloco }}
                    </app-button>
                  </div>
                </div>

                @if (testResults()[endpoint.id]; as result) {
                  <div
                    class="test-result"
                    [class.ok]="result.ok"
                    [class.err]="!result.ok"
                  >
                    @if (result.ok) {
                      {{ 'admin.providers.endpoints.testOk' | transloco: {status: result.status} }}
                    } @else {
                      {{ 'admin.providers.endpoints.testFail' | transloco:
                        {status: result.status ?? '-',
                         error: result.error ?? ''} }}
                    }
                  </div>
                }

                <p class="catalog-hint">
                  {{ 'admin.providers.endpoints.catalogHint' | transloco }}
                  <a routerLink="/admin/models" class="catalog-hint-link">
                    {{ 'admin.providers.endpoints.catalogHintLink' | transloco }}
                  </a>
                  @if (isCodexEndpoint(endpoint.label)) {
                    <span class="codex-aside">
                      ·
                      <a routerLink="/settings" class="catalog-hint-link">
                        Manage subscriptions
                      </a>
                    </span>
                  }
                </p>
              </div>
            }
          } @else {
            <p class="empty-state">{{ 'admin.providers.endpoints.empty' | transloco }}</p>
          }

          <div class="create-form">
            <h3 class="form-title">{{ 'admin.providers.endpoints.addTitle' | transloco }}</h3>
            <div class="form-row two-col">
              <app-form-field>
                <app-input
                  [value]="newEndpointLabel()"
                  [placeholder]="'admin.providers.endpoints.labelPlaceholder' | transloco"
                  [disabled]="creatingEndpoint()"
                  (changed)="newEndpointLabel.set($event)"
                />
              </app-form-field>
              <app-form-field>
                <app-input
                  [value]="newEndpointBaseUrl()"
                  [placeholder]="'admin.providers.endpoints.baseUrlPlaceholder' | transloco"
                  [disabled]="creatingEndpoint()"
                  (changed)="newEndpointBaseUrl.set($event)"
                />
              </app-form-field>
            </div>
            <div class="form-row">
              <app-form-field>
                <app-input
                  type="password"
                  [value]="newEndpointApiKey()"
                  [placeholder]="'admin.providers.endpoints.apiKeyPlaceholder' | transloco"
                  [disabled]="creatingEndpoint()"
                  (changed)="newEndpointApiKey.set($event)"
                />
              </app-form-field>
            </div>
            <app-checkbox
              size="sm"
              [checked]="newEndpointAllowInsecure()"
              [disabled]="creatingEndpoint()"
              (changed)="newEndpointAllowInsecure.set($event)"
            >
              {{ 'admin.providers.endpoints.allowInsecure' | transloco }}
            </app-checkbox>
            @if (endpointFormError()) {
              <p class="form-error">{{ endpointFormError() }}</p>
            }
            <app-button
              variant="primary"
              size="md"
              [loading]="creatingEndpoint()"
              [disabled]="creatingEndpoint() || !newEndpointLabel().trim() || !newEndpointBaseUrl().trim()"
              (clicked)="createEndpoint()"
            >
              {{ creatingEndpoint()
                  ? ('common.saving' | transloco)
                  : ('admin.providers.endpoints.createButton' | transloco) }}
            </app-button>
          </div>
        </section>

        <!-- Defaults -->
        <section class="admin-section section-spacer">
          <h2 class="section-title">{{ 'admin.providers.defaults.title' | transloco }}</h2>
          <p class="section-desc">{{ 'admin.providers.defaults.desc' | transloco }}</p>

          @if (catalogEmpty()) {
            <p class="empty-state">{{ 'admin.providers.defaults.emptyCatalog' | transloco }}</p>
          } @else {
            <div class="defaults-form">
              @for (kind of defaultKinds; track kind) {
                <app-form-field
                  [label]="('admin.providers.defaults.kind.' + kind) | transloco"
                >
                  <app-select
                    [value]="admin.defaults()[kind] ?? ''"
                    (changed)="setDefault(kind, $event ?? '')"
                  >
                    <option value="">{{ 'admin.providers.defaults.unset' | transloco }}</option>
                    @if (kind === 'embedding') {
                      @for (m of modelService.embeddingModels(); track m.id) {
                        <option [value]="m.id">{{ m.label }}</option>
                      }
                    } @else if (kind === 'vision') {
                      @for (m of modelService.visionModels(); track m.id) {
                        <option [value]="m.id">{{ m.label }}</option>
                      }
                    } @else if (kind === 'whisper') {
                      @for (m of modelService.whisperModels(); track m.id) {
                        <option [value]="m.id">{{ m.label }}</option>
                      }
                    } @else if (kind === 'tts') {
                      @for (m of modelService.ttsModels(); track m.id) {
                        <option [value]="m.id">{{ m.label }}</option>
                      }
                    } @else {
                      <!-- chat-slot kinds (builder/browser/citation/auxiliary) -->
                      @for (group of modelService.models(); track group.group) {
                        <optgroup [label]="group.group">
                          @for (model of group.models; track model) {
                            <option [value]="model">{{ model }}</option>
                          }
                        </optgroup>
                      }
                      @if (modelService.auxiliaryModels().length > 0) {
                        <optgroup [label]="'admin.providers.defaults.auxiliaryGroup' | transloco">
                          @for (m of modelService.auxiliaryModels(); track m.id) {
                            <option [value]="m.id">{{ m.label }}</option>
                          }
                        </optgroup>
                      }
                    }
                  </app-select>
                </app-form-field>
              }
            </div>
          }
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
    .admin-page {
      padding: 32px;
      max-width: 900px;
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
      border-radius: 12px;
      padding: 24px;
    }
    .section-spacer { margin-top: 24px; }
    .section-title {
      font-size: 18px;
      font-weight: 600;
      margin-bottom: 4px;
      color: var(--text-primary);
    }
    .section-desc {
      font-size: 13px;
      color: var(--text-muted);
      margin-bottom: 20px;
    }
    .key-table {
      margin-bottom: 20px;
      border: 1px solid var(--border-color);
      border-radius: 8px;
      overflow: hidden;
    }
    .key-header,
    .key-row {
      display: grid;
      grid-template-columns: 1.3fr 1fr 1fr 0.9fr 0.9fr 90px;
      padding: 10px 14px;
      gap: 8px;
      align-items: center;
      font-size: 13px;
    }
    .key-header {
      background: var(--surface-0);
      font-weight: 600;
      font-size: 12px;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }
    .key-row {
      border-top: 1px solid var(--border-color);
    }
    .mono {
      font-family: 'JetBrains Mono', 'Fira Code', monospace;
      font-size: 12px;
      color: var(--text-muted);
    }
    .muted { color: var(--text-muted); }
    .test-result {
      margin: 8px 0;
      padding: 8px 12px;
      border-radius: 6px;
      font-size: 12px;
    }
    .test-result.ok {
      background: var(--success-tint);
      color: var(--success);
    }
    .test-result.err {
      background: var(--danger-tint);
      color: var(--danger);
    }
    .empty-state {
      font-size: 13px;
      color: var(--text-muted);
      text-align: center;
      padding: 18px 12px;
    }
    .endpoint-card {
      border: 1px solid var(--border-color);
      border-radius: 8px;
      padding: 14px;
      margin-bottom: 12px;
    }
    .endpoint-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: flex-start;
    }
    .endpoint-title {
      display: flex;
      flex-direction: column;
      gap: 2px;
      font-size: 13px;
    }
    .endpoint-url,
    .endpoint-key {
      font-size: 11px;
    }
    .endpoint-actions {
      display: flex;
      gap: 6px;
      align-items: center;
    }
    .catalog-hint {
      margin-top: 12px;
      padding-top: 12px;
      border-top: 1px dashed var(--border-color);
      font-size: 12px;
      color: var(--text-muted);
    }
    .catalog-hint-link {
      color: var(--accent-color);
      text-decoration: none;
      margin-left: 4px;
    }
    .catalog-hint-link:hover { text-decoration: underline; }
    .codex-aside { margin-left: 6px; }
    .create-form {
      margin-top: 12px;
      padding-top: 12px;
      border-top: 1px dashed var(--border-color);
    }
    .defaults-form {
      display: flex;
      flex-direction: column;
      gap: 12px;
    }
    .form-row {
      display: flex;
      gap: 8px;
      margin-bottom: 8px;
    }
    .form-row.two-col > * { flex: 1; }
    .form-title {
      font-size: 14px;
      font-weight: 600;
      margin: 0 0 12px 0;
      color: var(--text-primary);
    }
    .form-error {
      color: var(--danger);
      font-size: 12px;
      margin: 4px 0 8px 0;
    }
  `],
})
export class AdminProvidersComponent implements OnInit {
  readonly admin = inject(AdminProvidersService);
  readonly modelService = inject(ModelService);
  private readonly transloco = inject(TranslocoService);

  readonly providers = SYSTEM_PROVIDERS;
  readonly defaultKinds: DefaultModelKind[] = DEFAULT_MODEL_KINDS;

  readonly catalogEmpty = computed(() => this.modelService.models().length === 0);

  // API key form
  readonly keyProvider = signal<SystemProviderValue>('openai');
  readonly keyValue = signal('');
  readonly keyLabel = signal('');
  readonly savingKey = signal(false);

  // Endpoint form
  readonly newEndpointLabel = signal('');
  readonly newEndpointBaseUrl = signal('');
  readonly newEndpointApiKey = signal('');
  readonly newEndpointAllowInsecure = signal(false);
  readonly creatingEndpoint = signal(false);
  readonly endpointFormError = signal<string>('');
  readonly testingEndpointId = signal<string | null>(null);
  readonly testResults = signal<Record<string, LlmEndpointTestResult>>({});

  ngOnInit(): void {
    this.admin.loadSystemApiKeys();
    this.admin.loadSystemEndpoints();
    this.admin.loadDefaults();
    this.modelService.load();
  }

  providerLabel(p: string): string {
    return SYSTEM_PROVIDERS.find((x) => x.value === p)?.label ?? p;
  }

  isCodexEndpoint(label: string | null | undefined): boolean {
    return label === 'codex-proxy';
  }

  formatDate(iso: string | null): string {
    if (!iso) return '-';
    return new Date(iso).toLocaleDateString(this.transloco.getActiveLang(), {
      month: 'short',
      day: 'numeric',
    });
  }

  // ── Keys ────────────────────────────────────────────────────────

  onKeyProviderChange(value: string | null): void {
    if (value) this.keyProvider.set(value as SystemProviderValue);
  }

  saveKey(): void {
    const value = this.keyValue().trim();
    if (!value) return;
    this.savingKey.set(true);
    this.admin
      .setSystemApiKey(this.keyProvider(), {
        api_key: value,
        label: this.keyLabel().trim() || null,
      })
      .subscribe({
        next: () => {
          this.keyValue.set('');
          this.keyLabel.set('');
          this.savingKey.set(false);
        },
        error: () => this.savingKey.set(false),
      });
  }

  deleteKey(provider: string): void {
    if (!confirm(this.transloco.translate('admin.providers.keys.confirmDelete'))) return;
    this.admin.deleteSystemApiKey(provider).subscribe();
  }

  // ── Endpoints ───────────────────────────────────────────────────

  createEndpoint(): void {
    const label = this.newEndpointLabel().trim();
    const baseUrl = this.newEndpointBaseUrl().trim();
    if (!label || !baseUrl) return;

    if (!/^https?:\/\//i.test(baseUrl)) {
      this.endpointFormError.set(
        this.transloco.translate('admin.providers.endpoints.errorUrlScheme'),
      );
      return;
    }
    if (baseUrl.toLowerCase().startsWith('http://') && !this.newEndpointAllowInsecure()) {
      this.endpointFormError.set(
        this.transloco.translate('admin.providers.endpoints.errorHttpNeedsOptIn'),
      );
      return;
    }

    this.endpointFormError.set('');
    this.creatingEndpoint.set(true);
    this.admin
      .createSystemEndpoint({
        label,
        base_url: baseUrl,
        api_key: this.newEndpointApiKey().trim() || null,
        allow_insecure: this.newEndpointAllowInsecure(),
      })
      .subscribe({
        next: () => {
          this.newEndpointLabel.set('');
          this.newEndpointBaseUrl.set('');
          this.newEndpointApiKey.set('');
          this.newEndpointAllowInsecure.set(false);
          this.creatingEndpoint.set(false);
        },
        error: (err) => {
          this.endpointFormError.set(err?.error?.detail ?? String(err));
          this.creatingEndpoint.set(false);
        },
      });
  }

  deleteEndpoint(endpointId: string): void {
    if (!confirm(this.transloco.translate('admin.providers.endpoints.confirmDelete'))) return;
    this.admin.deleteSystemEndpoint(endpointId).subscribe();
  }

  testEndpoint(endpointId: string): void {
    this.testingEndpointId.set(endpointId);
    this.admin.testSystemEndpoint(endpointId).subscribe({
      next: (result) => {
        this.testResults.update((r) => ({...r, [endpointId]: result}));
        this.testingEndpointId.set(null);
      },
      error: () => this.testingEndpointId.set(null),
    });
  }

  // ── Defaults ────────────────────────────────────────────────────

  setDefault(kind: DefaultModelKind, model: string): void {
    this.admin.setDefault(kind, model).subscribe();
  }
}
