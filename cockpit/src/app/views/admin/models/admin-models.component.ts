import {ChangeDetectionStrategy, Component, computed, DestroyRef, effect, ElementRef, inject, OnInit, signal, viewChild} from '@angular/core';
import {takeUntilDestroyed} from '@angular/core/rxjs-interop';
import {AdminModelsService} from '../../../core/services/admin-models.service';
import {AdminProvidersService} from '../../../core/services/admin-providers.service';
import {AdminLlmCoordinatorService} from '../llm/admin-llm-coordinator.service';
import {
  CATALOG_CAPABILITIES,
  CatalogCapability,
  CatalogModel,
  CatalogModelTestResult,
  CatalogProviderKind,
  LlmEndpointDiscoveredModel,
} from '../../../core/models/api.model';
import {TTS_VOICE_CUSTOM, voicesForModelId} from '../../../core/models/tts-voices';
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

/**
 * Build the `capabilities[]` pre-fill from the discovery hint array.
 * Filters to known capability values and de-duplicates while preserving
 * the order the orchestrator emitted (chat-capable rows already include
 * `auxiliary`; multimodal families include `vision`).
 */
function hintsToCapabilities(
  hints: readonly string[] | null | undefined,
): CatalogCapability[] {
  const known: CatalogCapability[] = [
    'chat',
    'auxiliary',
    'embedding',
    'vision',
    'whisper',
    'tts',
  ];
  const isKnown = (v: string): v is CatalogCapability =>
    known.includes(v as CatalogCapability);
  const filtered = (hints ?? []).filter(isKnown);
  return filtered.length > 0 ? [...new Set(filtered)] : ['chat', 'auxiliary'];
}

@Component({
  selector: 'app-admin-models',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AppButtonComponent,
    AppInputComponent,
    AppSelectComponent,
    AppCheckboxComponent,
    AppFormFieldComponent,
    AppBadgeComponent,
  ],
  template: `
    <div class="admin-models">
      <p class="section-intro">
        Curate the LLM offerings available in sessions and jobs.
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
              in sessions and jobs.
            </p>
          } @else {
            @for (group of groupedModels(); track group.key) {
              <div class="provider-group">
                <h3 class="group-title">{{ group.label }}</h3>
                <div class="model-table">
                  <div class="model-header">
                    <span class="col-display">Display label</span>
                    <span class="col-id">Model ID</span>
                    <span class="col-capability">Capabilities</span>
                    <span class="col-family">Family</span>
                    <span class="col-enabled">Enabled</span>
                    <span class="col-actions"></span>
                  </div>
                  @for (m of group.rows; track m.id) {
                    <div class="model-row">
                      <span class="col-display">{{ m.display_label }}</span>
                      <span class="col-id mono">{{ m.model_id }}</span>
                      <span class="col-capability">
                        <span class="cap-badges">
                          @for (cap of rowCapabilities(m); track cap) {
                            <app-badge tone="neutral" size="xs">{{ cap }}</app-badge>
                          }
                        </span>
                      </span>
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
              <app-form-field label="Capabilities">
                <fieldset class="cap-fieldset" [disabled]="creating()">
                  @for (c of capabilities; track c) {
                    <label class="cap-checkbox">
                      <app-checkbox
                        size="sm"
                        [checked]="formCapabilities().includes(c)"
                        [ariaLabel]="c"
                        (changed)="toggleFormCapability(c, $event)"
                      />
                      <span>{{ c }}</span>
                    </label>
                  }
                </fieldset>
              </app-form-field>
            </div>

            @if (selectedEndpointRef(); as endpointRef) {
              <div class="discover-pane" #discoverPane>
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
                        <span class="discover-cap">
                          {{ m.capability_hints.join(', ') }}
                        </span>
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
                  (changed)="onModelIdChange($event)"
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

            @if (formCapabilities().includes('tts')) {
              <div class="form-row">
                <app-form-field label="Voice (optional)">
                  @if (ttsVoiceOptions().length > 0) {
                    <app-select
                      [value]="formVoiceCustom() ? CUSTOM_VOICE : formVoice()"
                      [disabled]="creating()"
                      (changed)="onVoiceSelect($event)"
                    >
                      <option value="">(backend default)</option>
                      @for (v of ttsVoiceOptions(); track v) {
                        <option [value]="v">{{ v }}</option>
                      }
                      <option [value]="CUSTOM_VOICE">Custom…</option>
                    </app-select>
                  } @else {
                    <app-input
                      [value]="formVoice()"
                      placeholder="e.g. af_heart, alloy — depends on the TTS backend"
                      [disabled]="creating()"
                      (changed)="formVoice.set($event)"
                    />
                  }
                </app-form-field>
              </div>
              @if (ttsVoiceOptions().length > 0 && formVoiceCustom()) {
                <div class="form-row">
                  <app-form-field label="Custom voice">
                    <app-input
                      [value]="formVoice()"
                      placeholder="exact voice id (must match the backend)"
                      [disabled]="creating()"
                      (changed)="formVoice.set($event)"
                    />
                  </app-form-field>
                </div>
              }
            }

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
  `,
  styles: [`
    :host {
      display: block;
    }
    .admin-models {
      display: block;
    }
    .section-intro {
      font-size: 13px;
      color: var(--text-muted);
      margin: 0 0 16px 0;
    }
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
      grid-template-columns: 1.4fr 2fr 160px 100px 70px 260px;
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
      border-radius: var(--radius-control);
      color: var(--text-primary);
    }
    .mono { font-family: ui-monospace, monospace; font-size: 12px; }
    .empty-state {
      padding: 24px;
      text-align: center;
      color: var(--text-muted);
      background: var(--surface-0);
      border-radius: var(--radius-surface);
    }
    .muted { color: var(--text-muted); }
    .col-actions {
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
    }
    .cap-badges {
      display: flex;
      flex-wrap: wrap;
      gap: 4px;
    }
    .cap-fieldset {
      display: flex;
      flex-wrap: wrap;
      gap: 8px 14px;
      padding: 6px 8px;
      border: 1px solid var(--border-color);
      border-radius: var(--radius-control);
      background: var(--surface-0);
    }
    .cap-fieldset[disabled] { opacity: 0.6; pointer-events: none; }
    .cap-checkbox {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      font-size: 12px;
      color: var(--text-primary);
      cursor: pointer;
      user-select: none;
    }
    .create-form {
      padding: 16px;
      background: var(--surface-0);
      border-radius: var(--radius-surface);
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
      border-radius: var(--radius-surface);
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
      border-radius: var(--radius-pill);
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
      border-radius: var(--radius-control);
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
    @media (max-width: 720px) {
      /* The catalog table is a 6-column grid
         (1.4fr 2fr 160px 100px 70px 260px = 590px of fixed columns alone), so
         at phone widths Family / Enabled / Test / Delete fall off the right
         edge and the host's overflow:auto quietly hides them. Collapse each
         row into a stacked card so every field and control stays on-screen.
         Desktop (>720px) keeps the table grid untouched. */
      .model-header {
        display: none;
      }
      /* Two-column card: title + enabled-toggle share row 1 (toggle pinned
         top-right, the conventional spot); the model-id becomes a muted
         subtitle so an id identical to the display label reads as a faint echo
         rather than a duplicate; capabilities + family share row 3 (tags left,
         family right); Test/Delete span the bottom. */
      .model-row {
        grid-template-columns: 1fr auto;
        align-items: start;
        column-gap: 8px;
        row-gap: 6px;
        padding: 12px 14px;
      }
      .col-display {
        grid-column: 1;
        grid-row: 1;
        font-size: 15px;
        font-weight: 600;
        min-width: 0;
        overflow-wrap: anywhere;
      }
      .col-enabled {
        grid-column: 2;
        grid-row: 1;
        justify-self: end;
        display: flex;
        align-items: center;
        gap: 6px;
        white-space: nowrap;
      }
      .col-enabled::before {
        content: 'Enabled';
        font-size: 12px;
        color: var(--text-muted);
      }
      .col-id {
        grid-column: 1 / -1;
        grid-row: 2;
        font-size: 11px;
        opacity: 0.6;
      }
      .col-capability {
        grid-column: 1;
        grid-row: 3;
      }
      .col-family {
        grid-column: 2;
        grid-row: 3;
        justify-self: end;
        align-self: start;
        font-size: 12px;
        white-space: nowrap;
      }
      .col-family::before {
        content: 'Family: ';
        color: var(--text-muted);
      }
      .col-actions {
        grid-column: 1 / -1;
        grid-row: 4;
        margin-top: 2px;
      }
      /* Stack the cramped two-up form rows (two ~137px fields side-by-side). */
      .form-row.two-col {
        flex-direction: column;
      }
    }
  `],
})
export class AdminModelsComponent implements OnInit {
  readonly models = inject(AdminModelsService);
  readonly providers = inject(AdminProvidersService);
  private readonly coordinator = inject(AdminLlmCoordinatorService);
  private readonly destroyRef = inject(DestroyRef);

  private readonly discoverPaneRef = viewChild<ElementRef<HTMLElement>>('discoverPane');

  readonly capabilities: CatalogCapability[] = CATALOG_CAPABILITIES;
  readonly creating = signal(false);
  readonly testing = signal<string | null>(null);
  readonly testResults = signal<Record<string, CatalogModelTestResult>>({});
  readonly formError = signal<string>('');

  // Form state — signals so OnPush picks up updates from primitive callbacks.
  readonly formProviderKey = signal('');
  // Default to ['chat', 'auxiliary'] — chat-capable LLMs always serve
  // auxiliary unless the operator explicitly unchecks it.
  readonly formCapabilities = signal<CatalogCapability[]>(['chat', 'auxiliary']);
  readonly formModelId = signal('');
  readonly formDisplayLabel = signal('');
  readonly formFamily = signal('default');
  readonly formContextWindow = signal<number | null>(null);
  // Optional TTS voice (e.g. Kokoro af_heart, OpenAI alloy) — persisted into
  // the catalog row's params_json and read by the TTS service. Only sent when
  // the tts capability is selected.
  readonly formVoice = signal('');
  // True when the operator picked "Custom…" (or the backend is unrecognized);
  // then formVoice is a free-text voice id rather than a catalog selection.
  readonly formVoiceCustom = signal(false);
  protected readonly CUSTOM_VOICE = TTS_VOICE_CUSTOM;

  // Mirror for the number input — keeps an empty string when null so the
  // input renders blank instead of "0".
  readonly formContextWindowText = computed(() => {
    const v = this.formContextWindow();
    return v == null ? '' : String(v);
  });

  /** Voices for the current model's backend (Kokoro/OpenAI); empty when the
   *  backend isn't recognized → the form falls back to a free-text field. */
  readonly ttsVoiceOptions = computed(() => voicesForModelId(this.formModelId()));

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
    for (const ep of this.providers.systemEndpoints()) {
      const isCodex = ep.label === CODEX_PROXY_LABEL;
      opts.push({
        kind: 'endpoint',
        ref: ep.id,
        label: isCodex
          ? `${ep.label} (codex subscription)`
          : `${ep.label} (endpoint)`,
        // Every system endpoint can anchor a catalog row — including the
        // seeded codex-proxy even without an active subscription (admins may
        // seed rows ahead of OAuth login; the runtime status banner below
        // tells them when login is needed).
        available: true,
      });
    }
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

  constructor() {
    // Native <select> displays the first option when no value matches, which
    // misleads the operator into thinking a provider is selected when the
    // signal is still empty — leaving the discover pane hidden and the Add
    // button disabled. When exactly one available provider exists, lock the
    // form to it.
    effect(() => {
      const options = this.providerOptions();
      if (this.formProviderKey()) return;
      const available = options.filter((o) => o.available);
      if (available.length !== 1) return;
      const only = available[0];
      this.onProviderKeyChange(`${only.kind}:${only.ref}`);
    });
  }

  ngOnInit(): void {
    this.models.loadModels();
    this.models.loadFamilies();
    this.providers.loadSystemApiKeys();
    this.providers.loadSystemEndpoints();
    this.providers.loadCodexAvailability();

    this.coordinator.discoverEndpoint$
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe((endpointId) => this.preselectAndDiscover(endpointId));
  }

  /** Cross-tab handoff target — Providers tab fires this when the operator
   * clicks "Discover" on an endpoint card. Resets the form to that
   * endpoint, kicks off the probe, and scrolls the discover pane into
   * view (parent will have already flipped the tab). */
  private preselectAndDiscover(endpointId: string): void {
    this.formProviderKey.set(`endpoint:${endpointId}`);
    this.formModelId.set('');
    this.formDisplayLabel.set('');
    this.formFamily.set('default');
    this.formContextWindow.set(null);
    this.formError.set('');
    this.discoveredModels.set([]);
    this.discoverError.set('');
    this.discoverFromEndpoint(endpointId);
    queueMicrotask(() => {
      this.discoverPaneRef()?.nativeElement.scrollIntoView({
        behavior: 'smooth',
        block: 'center',
      });
    });
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
      !!this.formFamily().trim() &&
      this.formCapabilities().length > 0
    );
  }

  /** Capability set displayed for a catalog row in the table. */
  rowCapabilities(m: CatalogModel): CatalogCapability[] {
    return m.capabilities ?? [];
  }

  /** Toggle one capability checkbox in the create/edit form. Maintains
   * CATALOG_CAPABILITIES order so the array sent to the backend is
   * deterministic regardless of click order. */
  toggleFormCapability(capability: CatalogCapability, checked: boolean): void {
    const current = new Set(this.formCapabilities());
    if (checked) {
      current.add(capability);
    } else {
      current.delete(capability);
    }
    const ordered = this.capabilities.filter((c) => current.has(c));
    this.formCapabilities.set(ordered);
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

  /** Voice dropdown change: a real voice sets formVoice directly; the
   *  "Custom…" sentinel switches to the free-text field. */
  onVoiceSelect(value: string | null): void {
    if (value === this.CUSTOM_VOICE) {
      this.formVoiceCustom.set(true);
      this.formVoice.set('');
    } else {
      this.formVoiceCustom.set(false);
      this.formVoice.set(value ?? '');
    }
  }

  submit(): void {
    this.formError.set('');
    const [kind, ref] = this.formProviderKey().split(':') as [CatalogProviderKind, string];
    if (!kind || !ref) {
      this.formError.set('Pick a provider.');
      return;
    }
    const capabilities = this.formCapabilities();
    if (capabilities.length === 0) {
      this.formError.set('Select at least one capability.');
      return;
    }
    const voice = this.formVoice().trim();
    // Voice rides in the catalog row's params_json; only meaningful for TTS.
    const paramsJson =
      capabilities.includes('tts') && voice ? {voice} : undefined;
    this.creating.set(true);
    this.models
      .createModel({
        provider_kind: kind,
        provider_ref: ref,
        model_id: this.formModelId().trim(),
        display_label: this.formDisplayLabel().trim(),
        capabilities,
        family: this.formFamily().trim(),
        context_window: this.formContextWindow() ?? null,
        params_json: paramsJson,
      })
      .subscribe({
        next: () => {
          this.formModelId.set('');
          this.formDisplayLabel.set('');
          this.formContextWindow.set(null);
          this.formVoice.set('');
          this.formVoiceCustom.set(false);
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
    this.formCapabilities.set(hintsToCapabilities(m.capability_hints));
    // Endpoint discovery doesn't always know the family — fall back to the
    // matcher when the discovered row leaves it empty.
    if (m.family) {
      this.formFamily.set(m.family);
    } else {
      this.detectAndSetFamily(m.id);
    }
    this.formContextWindow.set(m.context_window ?? null);
  }

  /** Invoked when the operator types or pastes a model ID directly (not
   * via the discovery list). Pre-fills the family dropdown from the
   * matcher; admin can still override before save. */
  onModelIdChange(value: string): void {
    this.formModelId.set(value);
    const trimmed = value.trim();
    if (!trimmed) {
      this.formFamily.set('default');
      return;
    }
    this.detectAndSetFamily(trimmed);
  }

  private detectAndSetFamily(modelId: string): void {
    this.models.detectFamily(modelId).subscribe((res) => {
      // Only apply the matcher's suggestion when it actually matched; a
      // 'fallback' result means we'd just be replacing whatever the admin
      // already selected with 'default', which is rarely what they want.
      if (res.source === 'matched') {
        this.formFamily.set(res.family);
      }
    });
  }
}
