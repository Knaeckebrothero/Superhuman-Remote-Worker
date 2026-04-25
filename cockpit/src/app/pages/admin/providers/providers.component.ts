import {Component, computed, inject, OnInit, signal} from '@angular/core';
import {FormsModule} from '@angular/forms';
import {RouterLink} from '@angular/router';
import {TranslocoPipe, TranslocoService} from '@jsverse/transloco';
import {SidebarToggleComponent} from '../../../simple/layout/sidebar-toggle/sidebar-toggle.component';
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

/** Providers the admin can seed/rotate — matches VALID_SYSTEM_API_KEY_PROVIDERS. */
const SYSTEM_PROVIDERS: {value: Exclude<ApiKeyProvider, 'codex'>; label: string}[] = [
  {value: 'openai', label: 'OpenAI'},
  {value: 'anthropic', label: 'Anthropic'},
  {value: 'google', label: 'Google'},
  {value: 'groq', label: 'Groq'},
  {value: 'openrouter', label: 'OpenRouter'},
  {value: 'tavily', label: 'Tavily (Web Search)'},
  {value: 'vision', label: 'Vision'},
];

type SystemProviderValue = (typeof SYSTEM_PROVIDERS)[number]['value'];

@Component({
  selector: 'app-admin-providers',
  standalone: true,
  imports: [FormsModule, RouterLink, SidebarToggleComponent, TranslocoPipe],
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
                      <span class="badge" title="{{ key.seeded_from }}">
                        {{ 'admin.providers.keys.seededBadge' | transloco }}
                      </span>
                    } @else {
                      <span class="muted">{{ 'admin.providers.keys.manualBadge' | transloco }}</span>
                    }
                  </span>
                  <span class="col-updated">{{ formatDate(key.updated_at) }}</span>
                  <span class="col-action">
                    <button class="revoke-btn" (click)="deleteKey(key.provider)">
                      {{ 'common.delete' | transloco }}
                    </button>
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
              <select class="form-input" [(ngModel)]="keyProvider" [disabled]="savingKey()">
                @for (p of providers; track p.value) {
                  <option [value]="p.value">{{ p.label }}</option>
                }
              </select>
              <input
                type="text"
                class="form-input"
                [placeholder]="'admin.providers.keys.labelPlaceholder' | transloco"
                [(ngModel)]="keyLabel"
                [disabled]="savingKey()"
              />
            </div>
            <div class="form-row">
              <input
                type="password"
                class="form-input"
                [placeholder]="'admin.providers.keys.keyPlaceholder' | transloco"
                [(ngModel)]="keyValue"
                [disabled]="savingKey()"
              />
            </div>
            <button
              class="create-btn"
              (click)="saveKey()"
              [disabled]="savingKey() || !keyValue.trim()"
            >
              {{ savingKey() ? ('common.saving' | transloco) : ('admin.providers.keys.saveButton' | transloco) }}
            </button>
          </div>
        </section>

        <!-- System Endpoints -->
        <section class="admin-section" style="margin-top: 24px;">
          <h2 class="section-title">{{ 'admin.providers.endpoints.title' | transloco }}</h2>
          <p class="section-desc">{{ 'admin.providers.endpoints.desc' | transloco }}</p>

          @if (admin.systemEndpoints().length > 0) {
            @for (endpoint of admin.systemEndpoints(); track endpoint.id) {
              <div class="endpoint-card">
                <div class="endpoint-head">
                  <div class="endpoint-title">
                    <strong>{{ endpoint.label }}</strong>
                    <span class="endpoint-url mono">{{ endpoint.base_url }}</span>
                    @if (endpoint.key_prefix) {
                      <span class="endpoint-key mono">key {{ endpoint.key_prefix }}...</span>
                    } @else {
                      <span class="endpoint-key muted">{{ 'admin.providers.endpoints.noKey' | transloco }}</span>
                    }
                  </div>
                  <div class="endpoint-actions">
                    <button
                      class="test-btn"
                      (click)="testEndpoint(endpoint.id)"
                      [disabled]="testingEndpointId() === endpoint.id"
                    >
                      {{ testingEndpointId() === endpoint.id
                          ? ('admin.providers.endpoints.testing' | transloco)
                          : ('admin.providers.endpoints.testButton' | transloco) }}
                    </button>
                    <button class="revoke-btn" (click)="deleteEndpoint(endpoint.id)">
                      {{ 'common.delete' | transloco }}
                    </button>
                  </div>
                </div>

                @if (testResults()[endpoint.id]) {
                  <div
                    class="test-result"
                    [class.ok]="testResults()[endpoint.id]!.ok"
                    [class.err]="!testResults()[endpoint.id]!.ok"
                  >
                    @if (testResults()[endpoint.id]!.ok) {
                      {{ 'admin.providers.endpoints.testOk' | transloco: {status: testResults()[endpoint.id]!.status} }}
                    } @else {
                      {{ 'admin.providers.endpoints.testFail' | transloco:
                        {status: testResults()[endpoint.id]!.status ?? '-',
                         error: testResults()[endpoint.id]!.error ?? ''} }}
                    }
                  </div>
                }

                <p class="catalog-hint">
                  {{ 'admin.providers.endpoints.catalogHint' | transloco }}
                  <a routerLink="/admin/models" class="catalog-hint-link">
                    {{ 'admin.providers.endpoints.catalogHintLink' | transloco }}
                  </a>
                </p>
              </div>
            }
          } @else {
            <p class="empty-state">{{ 'admin.providers.endpoints.empty' | transloco }}</p>
          }

          <div class="create-form">
            <h3 class="form-title">{{ 'admin.providers.endpoints.addTitle' | transloco }}</h3>
            <div class="form-row two-col">
              <input
                type="text"
                class="form-input"
                [placeholder]="'admin.providers.endpoints.labelPlaceholder' | transloco"
                [(ngModel)]="newEndpointLabel"
                [disabled]="creatingEndpoint()"
              />
              <input
                type="text"
                class="form-input"
                [placeholder]="'admin.providers.endpoints.baseUrlPlaceholder' | transloco"
                [(ngModel)]="newEndpointBaseUrl"
                [disabled]="creatingEndpoint()"
              />
            </div>
            <div class="form-row">
              <input
                type="password"
                class="form-input"
                [placeholder]="'admin.providers.endpoints.apiKeyPlaceholder' | transloco"
                [(ngModel)]="newEndpointApiKey"
                [disabled]="creatingEndpoint()"
              />
            </div>
            <label class="inline-checkbox">
              <input
                type="checkbox"
                [(ngModel)]="newEndpointAllowInsecure"
                [disabled]="creatingEndpoint()"
              />
              <span>{{ 'admin.providers.endpoints.allowInsecure' | transloco }}</span>
            </label>
            @if (endpointFormError()) {
              <p class="form-error">{{ endpointFormError() }}</p>
            }
            <button
              class="create-btn"
              (click)="createEndpoint()"
              [disabled]="creatingEndpoint() || !newEndpointLabel.trim() || !newEndpointBaseUrl.trim()"
            >
              {{ creatingEndpoint()
                  ? ('common.saving' | transloco)
                  : ('admin.providers.endpoints.createButton' | transloco) }}
            </button>
          </div>
        </section>

        <!-- Defaults -->
        <section class="admin-section" style="margin-top: 24px;">
          <h2 class="section-title">{{ 'admin.providers.defaults.title' | transloco }}</h2>
          <p class="section-desc">{{ 'admin.providers.defaults.desc' | transloco }}</p>

          @if (catalogEmpty()) {
            <p class="empty-state">{{ 'admin.providers.defaults.emptyCatalog' | transloco }}</p>
          } @else {
            <div class="create-form" style="border-top: none; padding-top: 0;">
              @for (kind of defaultKinds; track kind) {
                <div class="form-row">
                  <label class="field-label">
                    {{ ('admin.providers.defaults.kind.' + kind) | transloco }}
                  </label>
                  <select
                    class="form-input"
                    [ngModel]="admin.defaults()[kind] ?? ''"
                    (ngModelChange)="setDefault(kind, $event)"
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
                  </select>
                </div>
              }
            </div>
          }
        </section>
      </div>
    </div>
  `,
  styles: [`
    .admin-page {
      padding: 32px;
      max-width: 900px;
      margin: 0 auto;
      color: var(--text-primary, #cdd6f4);
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
      color: var(--text-primary, #cdd6f4);
    }
    .page-desc {
      font-size: 13px;
      color: var(--text-muted, #6c7086);
      margin: 0 0 32px 0;
    }
    .admin-section {
      background: var(--panel-bg, #181825);
      border: 1px solid var(--border-color, #313244);
      border-radius: 12px;
      padding: 24px;
    }
    .section-title {
      font-size: 18px;
      font-weight: 600;
      margin-bottom: 4px;
      color: var(--text-primary, #cdd6f4);
    }
    .section-desc {
      font-size: 13px;
      color: var(--text-muted, #6c7086);
      margin-bottom: 20px;
    }
    .key-table {
      margin-bottom: 20px;
      border: 1px solid var(--border-color, #313244);
      border-radius: 8px;
      overflow: hidden;
    }
    .key-header, .key-row {
      display: grid;
      grid-template-columns: 1.3fr 1fr 1fr 0.9fr 0.9fr 80px;
      padding: 10px 14px;
      gap: 8px;
      align-items: center;
      font-size: 13px;
    }
    .key-header {
      background: var(--surface-0, #313244);
      font-weight: 600;
      font-size: 12px;
      color: var(--text-muted, #6c7086);
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }
    .key-row {
      border-top: 1px solid var(--border-color, #313244);
    }
    .mono {
      font-family: 'JetBrains Mono', 'Fira Code', monospace;
      font-size: 12px;
      color: var(--text-muted, #6c7086);
    }
    .muted {
      color: var(--text-muted, #6c7086);
    }
    .badge {
      display: inline-block;
      padding: 2px 6px;
      background: var(--surface-0, #313244);
      border-radius: 4px;
      font-size: 11px;
      color: var(--text-muted, #6c7086);
      letter-spacing: 0.3px;
    }
    .revoke-btn {
      padding: 4px 10px;
      background: transparent;
      border: 1px solid var(--red, #f38ba8);
      border-radius: 6px;
      color: var(--red, #f38ba8);
      font-size: 12px;
      cursor: pointer;
    }
    .revoke-btn:hover {
      background: var(--red, #f38ba8);
      color: var(--timeline-bg, #11111b);
    }
    .test-btn {
      padding: 4px 10px;
      background: transparent;
      border: 1px solid var(--border-color, #313244);
      border-radius: 6px;
      color: var(--text-primary, #cdd6f4);
      font-size: 12px;
      cursor: pointer;
      margin-right: 6px;
    }
    .test-btn:disabled { opacity: 0.5; cursor: wait; }
    .test-result {
      margin: 8px 0;
      padding: 8px 12px;
      border-radius: 6px;
      font-size: 12px;
    }
    .test-result.ok {
      background: rgba(166, 227, 161, 0.12);
      color: var(--green, #a6e3a1);
    }
    .test-result.err {
      background: rgba(243, 139, 168, 0.12);
      color: var(--red, #f38ba8);
    }
    .empty-state {
      font-size: 13px;
      color: var(--text-muted, #6c7086);
      text-align: center;
      padding: 18px 12px;
    }
    .endpoint-card {
      border: 1px solid var(--border-color, #313244);
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
    .endpoint-url, .endpoint-key {
      font-size: 11px;
    }
    .catalog-hint {
      margin-top: 12px;
      padding-top: 12px;
      border-top: 1px dashed var(--border-color, #313244);
      font-size: 12px;
      color: var(--text-muted, #6c7086);
    }
    .catalog-hint-link {
      color: var(--blue, #89b4fa);
      text-decoration: none;
      margin-left: 4px;
    }
    .catalog-hint-link:hover { text-decoration: underline; }
    .create-form {
      margin-top: 12px;
      padding-top: 12px;
      border-top: 1px dashed var(--border-color, #313244);
    }
    .form-row {
      display: flex;
      gap: 8px;
      margin-bottom: 8px;
    }
    .form-row.two-col > * { flex: 1; }
    .form-row.three-col > * { flex: 1; }
    .form-title {
      font-size: 14px;
      font-weight: 600;
      margin: 0 0 12px 0;
    }
    .field-label {
      display: block;
      font-size: 12px;
      font-weight: 500;
      color: var(--text-muted, #6c7086);
      margin-bottom: 4px;
    }
    .form-input {
      flex: 1;
      padding: 8px 12px;
      background: var(--surface-0, #313244);
      border: 1px solid var(--border-color, #313244);
      border-radius: 6px;
      color: var(--text-primary, #cdd6f4);
      font-family: inherit;
      font-size: 13px;
    }
    .form-input:disabled { opacity: 0.5; }
    .inline-checkbox {
      display: flex;
      align-items: center;
      gap: 6px;
      font-size: 12px;
      color: var(--text-muted, #6c7086);
      margin: 6px 0 8px 0;
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
  keyProvider: SystemProviderValue = 'openai';
  keyValue = '';
  keyLabel = '';
  readonly savingKey = signal(false);

  // Endpoint form
  newEndpointLabel = '';
  newEndpointBaseUrl = '';
  newEndpointApiKey = '';
  newEndpointAllowInsecure = false;
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

  formatDate(iso: string | null): string {
    if (!iso) return '-';
    return new Date(iso).toLocaleDateString(this.transloco.getActiveLang(), {
      month: 'short',
      day: 'numeric',
    });
  }

  // ── Keys ────────────────────────────────────────────────────────

  saveKey(): void {
    if (!this.keyValue.trim()) return;
    this.savingKey.set(true);
    this.admin
      .setSystemApiKey(this.keyProvider, {
        api_key: this.keyValue.trim(),
        label: this.keyLabel.trim() || null,
      })
      .subscribe({
        next: () => {
          this.keyValue = '';
          this.keyLabel = '';
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
    const label = this.newEndpointLabel.trim();
    const baseUrl = this.newEndpointBaseUrl.trim();
    if (!label || !baseUrl) return;

    if (!/^https?:\/\//i.test(baseUrl)) {
      this.endpointFormError.set(
        this.transloco.translate('admin.providers.endpoints.errorUrlScheme'),
      );
      return;
    }
    if (baseUrl.toLowerCase().startsWith('http://') && !this.newEndpointAllowInsecure) {
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
        api_key: this.newEndpointApiKey.trim() || null,
        allow_insecure: this.newEndpointAllowInsecure,
      })
      .subscribe({
        next: () => {
          this.newEndpointLabel = '';
          this.newEndpointBaseUrl = '';
          this.newEndpointApiKey = '';
          this.newEndpointAllowInsecure = false;
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
