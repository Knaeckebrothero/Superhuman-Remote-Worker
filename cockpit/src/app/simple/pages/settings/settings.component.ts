import {ChangeDetectionStrategy, Component, computed, effect, inject, OnInit, signal} from '@angular/core';
import {Router} from '@angular/router';
import {environment} from '../../../core/environment';
import {McpTokenService} from '../../../core/services/mcp-token.service';
import {UserService} from '../../../core/services/user.service';
import {ApiService} from '../../../core/services/api.service';
import {
  SettingsService,
  MainCloudSettingsResponse,
  MainCloudFormState,
} from '../../../core/services/settings.service';
import {ModelService} from '../../../core/services/model.service';
import {I18nService, SupportedLang} from '../../../core/services/i18n.service';
import {
    ApiKeyProvider,
    CodexStatus,
    CommunicationSettings,
    LlmEndpointTestResult,
    McpTokenCreateResponse,
    Project
} from '../../../core/models/api.model';
import {SidebarToggleComponent} from '../../layout/sidebar-toggle/sidebar-toggle.component';
import {AppThemeToggleComponent} from '../../../ui/theme-toggle';
import {TranslocoPipe, TranslocoService} from '@jsverse/transloco';
import {AppButtonComponent} from '../../../ui/button';
import {AppInputComponent} from '../../../ui/input';
import {AppSelectComponent} from '../../../ui/select';
import {AppCheckboxComponent} from '../../../ui/checkbox';
import {AppFormFieldComponent} from '../../../ui/form-field';
import {AppBadgeComponent} from '../../../ui/badge';
import {AppIconComponent} from '../../../ui/icon';

const PROVIDERS: { value: ApiKeyProvider; label: string }[] = [
  { value: 'openai', label: 'OpenAI' },
  { value: 'anthropic', label: 'Anthropic' },
  { value: 'google', label: 'Google' },
  { value: 'groq', label: 'Groq' },
  { value: 'openrouter', label: 'OpenRouter' },
  { value: 'codex', label: 'Codex' },
  { value: 'tavily', label: 'Tavily (Web Search)' },
  { value: 'vision', label: 'Vision' },
];

const EXPIRY_OPTIONS = [
  { value: '', label: '' },
  { value: '30', label: '' },
  { value: '90', label: '' },
  { value: '365', label: '' },
];

@Component({
  selector: 'app-settings',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    SidebarToggleComponent,
    AppThemeToggleComponent,
    TranslocoPipe,
    AppButtonComponent,
    AppInputComponent,
    AppSelectComponent,
    AppCheckboxComponent,
    AppFormFieldComponent,
    AppBadgeComponent,
    AppIconComponent,
  ],
  template: `
    <div class="settings-page">
      <div class="settings-container">
        <div class="page-header">
          <app-sidebar-toggle />
          <h1 class="page-title">{{ 'settings.title' | transloco }}</h1>
        </div>

        <!-- Appearance Section -->
        <section class="settings-section">
          <h2 class="section-title">{{ 'settings.appearance.title' | transloco }}</h2>
          <p class="section-desc">{{ 'settings.appearance.desc' | transloco }}</p>
          <div class="form-block">
            <app-form-field [label]="'settings.appearance.themeLabel' | transloco">
              <app-theme-toggle [showLabels]="true" [ariaLabel]="'settings.appearance.themeLabel' | transloco" />
            </app-form-field>
          </div>
        </section>

        <!-- Language Section -->
        <section class="settings-section">
          <h2 class="section-title">{{ 'settings.language.title' | transloco }}</h2>
          <p class="section-desc">{{ 'settings.language.desc' | transloco }}</p>
          <div class="form-block">
            <app-form-field [label]="'settings.language.label' | transloco">
              <app-select
                [value]="i18n.activeLang()"
                (changed)="onLanguageChange($any($event))"
              >
                <option value="en">English</option>
                <option value="de-DE">Deutsch</option>
              </app-select>
            </app-form-field>
          </div>
        </section>

        <!-- API Keys Section -->
        <section class="settings-section">
          <h2 class="section-title">{{ 'settings.apiKeys.title' | transloco }}</h2>
          <p class="section-desc">{{ 'settings.apiKeys.desc' | transloco }}</p>

          <!-- Key List -->
          @if (settingsService.apiKeys().length > 0) {
            <div class="key-table">
              <div class="key-header">
                <span class="col-provider">{{ 'settings.apiKeys.colProvider' | transloco }}</span>
                <span class="col-prefix">{{ 'settings.apiKeys.colKey' | transloco }}</span>
                <span class="col-label">{{ 'settings.apiKeys.colLabel' | transloco }}</span>
                <span class="col-updated">{{ 'settings.apiKeys.colUpdated' | transloco }}</span>
                <span class="col-action"></span>
              </div>
              @for (key of settingsService.apiKeys(); track key.id) {
                <div class="key-row">
                  <span class="col-provider">{{ providerLabel(key.provider) }}</span>
                  <span class="col-prefix mono">{{ key.key_prefix }}...</span>
                  <span class="col-label">{{ key.label || '-' }}</span>
                  <span class="col-updated">{{ formatDate(key.updated_at) }}</span>
                  <span class="col-action">
                    <app-button variant="danger" size="sm" (clicked)="deleteApiKey(key.provider)">
                      {{ 'common.delete' | transloco }}
                    </app-button>
                  </span>
                </div>
              }
            </div>
          } @else {
            <p class="empty-state">{{ 'settings.apiKeys.empty' | transloco }}</p>
          }

          <!-- Set Key Form -->
          <div class="create-form">
            <h3 class="form-title">{{ 'settings.apiKeys.addTitle' | transloco }}</h3>
            <div class="form-row two-col">
              <app-select
                [value]="keyProvider()"
                [disabled]="settingKey()"
                (changed)="onKeyProviderChange($event)"
              >
                @for (p of providers; track p.value) {
                  <option [value]="p.value">{{ p.label }}</option>
                }
              </app-select>
              <app-input
                [value]="keyLabel()"
                [placeholder]="'settings.apiKeys.labelPlaceholder' | transloco"
                [disabled]="settingKey()"
                (changed)="keyLabel.set($event)"
              />
            </div>
            <div class="form-row">
              <app-input
                type="password"
                [value]="keyValue()"
                [placeholder]="'settings.apiKeys.keyPlaceholder' | transloco"
                [disabled]="settingKey()"
                (changed)="keyValue.set($event)"
              />
            </div>
            <app-button
              variant="primary"
              size="md"
              [loading]="settingKey()"
              [disabled]="settingKey() || !keyValue().trim()"
              (clicked)="saveApiKey()"
            >
              {{ settingKey() ? ('common.saving' | transloco) : ('settings.apiKeys.saveButton' | transloco) }}
            </app-button>
          </div>
        </section>

        <!-- LLM Endpoints Section -->
        <section class="settings-section section-spacer">
          <h2 class="section-title">{{ 'settings.llmEndpoints.title' | transloco }}</h2>
          <p class="section-desc">{{ 'settings.llmEndpoints.desc' | transloco }}</p>

          @if (settingsService.llmEndpoints().length > 0) {
            @for (endpoint of settingsService.llmEndpoints(); track endpoint.id) {
              <div class="endpoint-card">
                <div class="endpoint-head">
                  <div class="endpoint-title">
                    <strong>{{ endpoint.label }}</strong>
                    <span class="endpoint-url mono">{{ endpoint.base_url }}</span>
                    @if (endpoint.key_prefix) {
                      <span class="endpoint-key mono">key {{ endpoint.key_prefix }}...</span>
                    } @else {
                      <span class="endpoint-key muted">{{ 'settings.llmEndpoints.noKey' | transloco }}</span>
                    }
                  </div>
                  <div class="endpoint-actions">
                    <app-button
                      variant="secondary"
                      size="sm"
                      [loading]="testingEndpointId() === endpoint.id"
                      [disabled]="testingEndpointId() === endpoint.id"
                      (clicked)="testLlmEndpoint(endpoint.id)"
                    >
                      {{ testingEndpointId() === endpoint.id
                          ? ('settings.llmEndpoints.testing' | transloco)
                          : ('settings.llmEndpoints.testButton' | transloco) }}
                    </app-button>
                    <app-button variant="danger" size="sm" (clicked)="deleteLlmEndpoint(endpoint.id)">
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
                      {{ 'settings.llmEndpoints.testOk' | transloco: {status: result.status} }}
                    } @else {
                      {{ 'settings.llmEndpoints.testFail' | transloco:
                        {status: result.status ?? '-',
                         error: result.error ?? ''} }}
                    }
                  </div>
                }

                <p class="catalog-hint">
                  {{ 'settings.llmEndpoints.catalogHint' | transloco }}
                </p>
              </div>
            }
          } @else {
            <p class="empty-state">{{ 'settings.llmEndpoints.empty' | transloco }}</p>
          }

          <!-- Create endpoint form -->
          <div class="create-form">
            <h3 class="form-title">{{ 'settings.llmEndpoints.addTitle' | transloco }}</h3>
            <div class="form-row two-col">
              <app-input
                [value]="newEndpointLabel()"
                [placeholder]="'settings.llmEndpoints.labelPlaceholder' | transloco"
                [disabled]="creatingEndpoint()"
                (changed)="newEndpointLabel.set($event)"
              />
              <app-input
                [value]="newEndpointBaseUrl()"
                [placeholder]="'settings.llmEndpoints.baseUrlPlaceholder' | transloco"
                [disabled]="creatingEndpoint()"
                (changed)="newEndpointBaseUrl.set($event)"
              />
            </div>
            <div class="form-row">
              <app-input
                type="password"
                [value]="newEndpointApiKey()"
                [placeholder]="'settings.llmEndpoints.apiKeyPlaceholder' | transloco"
                [disabled]="creatingEndpoint()"
                (changed)="newEndpointApiKey.set($event)"
              />
            </div>
            <app-checkbox
              size="sm"
              [checked]="newEndpointAllowInsecure()"
              [disabled]="creatingEndpoint()"
              (changed)="newEndpointAllowInsecure.set($event)"
            >
              {{ 'settings.llmEndpoints.allowInsecure' | transloco }}
            </app-checkbox>
            @if (endpointFormError()) {
              <p class="form-error">{{ endpointFormError() }}</p>
            }
            <app-button
              variant="primary"
              size="md"
              [loading]="creatingEndpoint()"
              [disabled]="creatingEndpoint() || !newEndpointLabel().trim() || !newEndpointBaseUrl().trim()"
              (clicked)="createLlmEndpoint()"
            >
              {{ creatingEndpoint()
                  ? ('common.saving' | transloco)
                  : ('settings.llmEndpoints.createButton' | transloco) }}
            </app-button>
          </div>
        </section>

        <!-- Preferences Section -->
        <section class="settings-section section-spacer">
          <h2 class="section-title">{{ 'settings.preferences.title' | transloco }}</h2>
          <p class="section-desc">{{ 'settings.preferences.desc' | transloco }}</p>

          <div class="form-block">
            <div class="form-row two-col">
              <app-form-field [label]="'settings.preferences.defaultModel' | transloco">
                <app-select
                  [value]="prefModel() ?? resolved().default_model ?? ''"
                  (changed)="onPrefChange(prefModel, resolved().default_model, $event)"
                >
                  @for (group of modelService.models(); track group.group) {
                    <optgroup [label]="group.configured ? group.group : group.group + ' ' + ('settings.preferences.noApiKey' | transloco)">
                      @for (model of group.models; track model) {
                        <option [value]="model">{{ model }}{{ !prefModel() && model === resolved().default_model ? ' (' + ('common.default' | transloco) + ')' : '' }}</option>
                      }
                    </optgroup>
                  }
                </app-select>
              </app-form-field>
              <app-form-field [label]="'settings.preferences.auxModel' | transloco">
                <app-select
                  [value]="prefAuxModel() ?? resolved().default_auxiliary_model ?? ''"
                  (changed)="onPrefChange(prefAuxModel, resolved().default_auxiliary_model, $event)"
                >
                  @for (m of modelService.auxiliaryModels(); track m.id) {
                    <option [value]="m.id">{{ m.label }}{{ !prefAuxModel() && m.id === resolved().default_auxiliary_model ? ' (' + ('common.default' | transloco) + ')' : '' }}{{ m.configured ? '' : ' ' + ('common.noKey' | transloco) }}</option>
                  }
                </app-select>
              </app-form-field>
            </div>
            <div class="form-row two-col">
              <app-form-field [label]="'settings.preferences.autonomy' | transloco">
                <app-select
                  [value]="prefAutonomy() ?? resolved().default_autonomy ?? ''"
                  (changed)="onPrefChange(prefAutonomy, resolved().default_autonomy, $event)"
                >
                  <option value="full">{{ 'settings.preferences.autonomyFull' | transloco }}{{ !prefAutonomy() && resolved().default_autonomy === 'full' ? ' (' + ('common.default' | transloco) + ')' : '' }}</option>
                  <option value="review">{{ 'settings.preferences.autonomyReview' | transloco }}{{ !prefAutonomy() && resolved().default_autonomy === 'review' ? ' (' + ('common.default' | transloco) + ')' : '' }}</option>
                  <option value="partial">{{ 'settings.preferences.autonomyPartial' | transloco }}{{ !prefAutonomy() && resolved().default_autonomy === 'partial' ? ' (' + ('common.default' | transloco) + ')' : '' }}</option>
                  <option value="guided">{{ 'settings.preferences.autonomyGuided' | transloco }}{{ !prefAutonomy() && resolved().default_autonomy === 'guided' ? ' (' + ('common.default' | transloco) + ')' : '' }}</option>
                  <option value="dependent">{{ 'settings.preferences.autonomyDependent' | transloco }}{{ !prefAutonomy() && resolved().default_autonomy === 'dependent' ? ' (' + ('common.default' | transloco) + ')' : '' }}</option>
                </app-select>
              </app-form-field>
              <app-form-field [label]="'settings.preferences.reasoning' | transloco">
                <app-select
                  [value]="prefReasoning() ?? resolved().default_reasoning_level ?? ''"
                  (changed)="onPrefChange(prefReasoning, resolved().default_reasoning_level, $event)"
                >
                  <option value="low">{{ 'settings.preferences.reasoningLow' | transloco }}{{ !prefReasoning() && resolved().default_reasoning_level === 'low' ? ' (' + ('common.default' | transloco) + ')' : '' }}</option>
                  <option value="medium">{{ 'settings.preferences.reasoningMedium' | transloco }}{{ !prefReasoning() && resolved().default_reasoning_level === 'medium' ? ' (' + ('common.default' | transloco) + ')' : '' }}</option>
                  <option value="high">{{ 'settings.preferences.reasoningHigh' | transloco }}{{ !prefReasoning() && resolved().default_reasoning_level === 'high' ? ' (' + ('common.default' | transloco) + ')' : '' }}</option>
                </app-select>
              </app-form-field>
            </div>

            <h3 class="subsection-title">{{ 'settings.preferences.helperModels' | transloco }}</h3>
            <div class="form-row two-col">
              <app-form-field [label]="'settings.preferences.visionModel' | transloco" [hint]="'settings.preferences.visionHint' | transloco">
                <app-select
                  [value]="prefVisionModel() ?? resolved().default_vision_model ?? ''"
                  (changed)="onPrefChange(prefVisionModel, resolved().default_vision_model, $event)"
                >
                  @for (m of modelService.visionModels(); track m.id) {
                    <option [value]="m.id">{{ m.label }}{{ !prefVisionModel() && m.id === resolved().default_vision_model ? ' (' + ('common.default' | transloco) + ')' : '' }}{{ m.configured ? '' : ' ' + ('common.noKey' | transloco) }}</option>
                  }
                </app-select>
              </app-form-field>
              <app-form-field [label]="'settings.preferences.whisperModel' | transloco" [hint]="'settings.preferences.whisperHint' | transloco">
                <app-select
                  [value]="prefWhisperModel() ?? resolved().default_whisper_model ?? ''"
                  (changed)="onPrefChange(prefWhisperModel, resolved().default_whisper_model, $event)"
                >
                  @for (m of modelService.whisperModels(); track m.id) {
                    <option [value]="m.id">{{ m.label }}{{ !prefWhisperModel() && m.id === resolved().default_whisper_model ? ' (' + ('common.default' | transloco) + ')' : '' }}{{ m.configured ? '' : ' ' + ('common.noKey' | transloco) }}</option>
                  }
                </app-select>
              </app-form-field>
            </div>
            <div class="form-row two-col">
              <app-form-field [label]="'settings.preferences.embeddingModel' | transloco" [hint]="'settings.preferences.embeddingHint' | transloco">
                <app-select
                  [value]="prefEmbeddingModel() ?? resolved().default_embedding_model ?? ''"
                  (changed)="onPrefChange(prefEmbeddingModel, resolved().default_embedding_model, $event)"
                >
                  @for (m of modelService.embeddingModels(); track m.id) {
                    <option [value]="m.id">{{ m.label }}{{ m.dimensions ? ' (' + m.dimensions + 'd)' : '' }}{{ !prefEmbeddingModel() && m.id === resolved().default_embedding_model ? ' (' + ('common.default' | transloco) + ')' : '' }}{{ m.configured ? '' : ' ' + ('common.noKey' | transloco) }}</option>
                  }
                </app-select>
              </app-form-field>
              <app-form-field [label]="'settings.preferences.embeddingProvider' | transloco" [hint]="'settings.preferences.embeddingProviderHint' | transloco">
                <app-select
                  [value]="prefEmbeddingProvider() ?? resolved().embedding_provider ?? ''"
                  (changed)="onPrefChange(prefEmbeddingProvider, resolved().embedding_provider, $event)"
                >
                  <option value="local">{{ 'settings.preferences.providerLocal' | transloco }}{{ !prefEmbeddingProvider() && resolved().embedding_provider === 'local' ? ' (' + ('common.default' | transloco) + ')' : '' }}</option>
                  <option value="openrouter">{{ 'settings.preferences.providerOpenrouter' | transloco }}{{ !prefEmbeddingProvider() && resolved().embedding_provider === 'openrouter' ? ' (' + ('common.default' | transloco) + ')' : '' }}</option>
                </app-select>
              </app-form-field>
            </div>

            <div class="actions-row">
              <app-button
                variant="primary"
                size="md"
                [loading]="savingPrefs()"
                [disabled]="savingPrefs()"
                (clicked)="savePreferences()"
              >
                {{ savingPrefs() ? ('common.saving' | transloco) : ('settings.preferences.save' | transloco) }}
              </app-button>
              @if (prefsSaved()) {
                <app-badge tone="success" size="sm">{{ 'common.saved' | transloco }}</app-badge>
              }
            </div>
          </div>
        </section>

        <!-- Persistent Agent Section -->
        <section class="settings-section section-spacer">
          <h2 class="section-title">{{ 'settings.persistent.title' | transloco }}</h2>
          <p class="section-desc">{{ 'settings.persistent.desc' | transloco }}</p>

          <div class="form-block">
            <app-form-field
              [label]="'settings.persistent.model' | transloco"
              [hint]="paModel() ? '' : (resolved().persistent_agent?.model ? ('settings.persistent.defaultPrefix' | transloco) + ' ' + (resolved().persistent_agent?.model || '') : '')"
            >
              <app-input
                [value]="paModel() ?? ''"
                [placeholder]="resolved().persistent_agent?.model ?? ('settings.persistent.modelPlaceholder' | transloco)"
                (changed)="onPaModelChange($event)"
              />
            </app-form-field>
            <div class="form-row two-col">
              <app-form-field [label]="'settings.persistent.permissionMode' | transloco">
                <app-select
                  [value]="paPermissionMode() ?? resolved().persistent_agent?.permission_mode ?? ''"
                  (changed)="onPrefChange(paPermissionMode, resolved().persistent_agent?.permission_mode, $event)"
                >
                  <option value="supervised">{{ 'settings.persistent.permissionSupervised' | transloco }}{{ !paPermissionMode() && resolved().persistent_agent?.permission_mode === 'supervised' ? ' (' + ('common.default' | transloco) + ')' : '' }}</option>
                  <option value="auto_accept">{{ 'settings.persistent.permissionAutoAccept' | transloco }}{{ !paPermissionMode() && resolved().persistent_agent?.permission_mode === 'auto_accept' ? ' (' + ('common.default' | transloco) + ')' : '' }}</option>
                  <option value="autonomous">{{ 'settings.persistent.permissionAutonomous' | transloco }}{{ !paPermissionMode() && resolved().persistent_agent?.permission_mode === 'autonomous' ? ' (' + ('common.default' | transloco) + ')' : '' }}</option>
                </app-select>
              </app-form-field>
              <app-form-field [label]="'settings.persistent.config' | transloco">
                <app-select
                  [value]="paConfigName()"
                  (changed)="paConfigName.set($event ?? '')"
                >
                  <option value="">{{ 'settings.persistent.configDefault' | transloco }}</option>
                  <option value="developer">{{ 'settings.persistent.configDeveloper' | transloco }}</option>
                  <option value="scholar">{{ 'settings.persistent.configScholar' | transloco }}</option>
                </app-select>
              </app-form-field>
            </div>
            <app-form-field [label]="'settings.persistent.greeting' | transloco">
              <app-input
                [value]="paGreeting()"
                [placeholder]="'settings.persistent.greetingPlaceholder' | transloco"
                (changed)="paGreeting.set($event)"
              />
            </app-form-field>
            <div class="form-row two-col">
              <app-form-field [label]="'settings.persistent.idleTimeout' | transloco">
                <app-input
                  type="number"
                  [value]="paIdleTimeoutText()"
                  [placeholder]="(resolved().persistent_agent?.idle_timeout_minutes ?? 30) + ''"
                  (changed)="onPaIdleTimeoutChange($event)"
                />
              </app-form-field>
              <app-form-field [label]="'settings.persistent.commandAllowlist' | transloco">
                <app-input
                  [value]="paCommandAllowlist()"
                  [placeholder]="'settings.persistent.commandAllowlistPlaceholder' | transloco"
                  (changed)="paCommandAllowlist.set($event)"
                />
              </app-form-field>
            </div>
            <div class="actions-row">
              <app-button
                variant="primary"
                size="md"
                [loading]="savingPA()"
                [disabled]="savingPA()"
                (clicked)="savePersistentAgent()"
              >
                {{ savingPA() ? ('common.saving' | transloco) : ('settings.persistent.save' | transloco) }}
              </app-button>
              @if (paSaved()) {
                <app-badge tone="success" size="sm">{{ 'common.saved' | transloco }}</app-badge>
              }
            </div>
          </div>
        </section>

        <!-- Communication Preferences Section -->
        <section class="settings-section section-spacer">
          <h2 class="section-title">{{ 'settings.communication.title' | transloco }}</h2>
          <p class="section-desc">{{ 'settings.communication.desc' | transloco }}</p>

          <div class="form-block">
            <app-form-field [label]="'settings.communication.replyDelivery' | transloco">
              <app-select
                [value]="commDelivery()"
                (changed)="commDelivery.set($event ?? 'next_strategic_phase')"
              >
                <option value="next_strategic_phase">{{ 'settings.communication.deliveryNextStrategic' | transloco }}</option>
                <option value="immediate_interrupt">{{ 'settings.communication.deliveryImmediate' | transloco }}</option>
                <option value="llm_triage">{{ 'settings.communication.deliveryLlmTriage' | transloco }}</option>
              </app-select>
            </app-form-field>

            <app-form-field [label]="'settings.communication.channels' | transloco">
              <div class="channel-list">
                <app-checkbox size="sm" [checked]="commChannelEmail()" (changed)="commChannelEmail.set($event)">Email</app-checkbox>
                <app-checkbox size="sm" [checked]="commChannelNtfy()" (changed)="commChannelNtfy.set($event)">Ntfy</app-checkbox>
                <app-checkbox size="sm" [checked]="commChannelSlack()" (changed)="commChannelSlack.set($event)">Slack</app-checkbox>
                <app-checkbox size="sm" [checked]="commChannelDiscord()" (changed)="commChannelDiscord.set($event)">Discord</app-checkbox>
              </div>
            </app-form-field>

            <app-checkbox [checked]="commQuietEnabled()" (changed)="commQuietEnabled.set($event)">
              {{ 'settings.communication.quietHours' | transloco }}
            </app-checkbox>

            @if (commQuietEnabled()) {
              <div class="form-row two-col quiet-hours-row">
                <app-form-field [label]="'settings.communication.start' | transloco">
                  <input
                    type="time"
                    class="time-input"
                    [value]="commQuietStart()"
                    (input)="commQuietStart.set(asInputValue($event))"
                  />
                </app-form-field>
                <app-form-field [label]="'settings.communication.end' | transloco">
                  <input
                    type="time"
                    class="time-input"
                    [value]="commQuietEnd()"
                    (input)="commQuietEnd.set(asInputValue($event))"
                  />
                </app-form-field>
              </div>
              <app-form-field [label]="'settings.communication.timezone' | transloco">
                <app-input
                  [value]="commQuietTimezone()"
                  [placeholder]="'settings.communication.timezonePlaceholder' | transloco"
                  (changed)="commQuietTimezone.set($event)"
                />
              </app-form-field>
            }

            <div class="actions-row">
              <app-button
                variant="primary"
                size="md"
                [loading]="savingComm()"
                [disabled]="savingComm()"
                (clicked)="saveCommunication()"
              >
                {{ savingComm() ? ('common.saving' | transloco) : ('settings.communication.save' | transloco) }}
              </app-button>
              @if (commSaved()) {
                <app-badge tone="success" size="sm">{{ 'common.saved' | transloco }}</app-badge>
              }
            </div>
          </div>
        </section>

        <!-- MCP Tokens Section -->
        <section class="settings-section section-spacer">
          <h2 class="section-title">{{ 'settings.mcp.title' | transloco }}</h2>
          <p class="section-desc">{{ 'settings.mcp.desc' | transloco }}</p>

          <!-- Token List -->
          @if (tokenService.tokens().length > 0) {
            <div class="token-table">
              <div class="token-header">
                <span class="col-name">{{ 'settings.mcp.colName' | transloco }}</span>
                <span class="col-prefix">{{ 'settings.mcp.colToken' | transloco }}</span>
                <span class="col-scope">{{ 'settings.mcp.colScope' | transloco }}</span>
                <span class="col-origin">{{ 'settings.mcp.colOrigin' | transloco }}</span>
                <span class="col-used">{{ 'settings.mcp.colLastUsed' | transloco }}</span>
                <span class="col-expires">{{ 'settings.mcp.colExpires' | transloco }}</span>
                <span class="col-action"></span>
              </div>
              @for (token of activeTokens(); track token.id) {
                <div class="token-row">
                  <span class="col-name">{{ token.name }}</span>
                  <span class="col-prefix mono">{{ token.token_prefix }}...</span>
                  <span class="col-scope">{{ formatScope(token.scope) }}</span>
                  <span class="col-origin">{{ formatOrigin(token.origin) }}</span>
                  <span class="col-used">{{ token.last_used_at ? formatDate(token.last_used_at) : ('common.never' | transloco) }}</span>
                  <span class="col-expires">{{ token.expires_at ? formatDate(token.expires_at) : ('common.never' | transloco) }}</span>
                  <span class="col-action">
                    <app-button variant="danger" size="sm" (clicked)="revokeToken(token.id)">
                      {{ 'settings.mcp.revoke' | transloco }}
                    </app-button>
                  </span>
                </div>
              }
            </div>
          } @else {
            <p class="empty-state">{{ 'settings.mcp.empty' | transloco }}</p>
          }

          <!-- Newly Created Token -->
          @if (newToken(); as nt) {
            <div class="new-token-banner">
              <p class="new-token-warning">{{ 'settings.mcp.copyWarning' | transloco }}</p>
              <div class="new-token-row">
                <input
                  type="text"
                  class="new-token-input"
                  [value]="nt.token"
                  readonly
                  #tokenInput
                />
                <app-button variant="primary" size="md" (clicked)="copyToken(tokenInput)">
                  {{ copied() ? ('common.copied' | transloco) : ('common.copy' | transloco) }}
                </app-button>
              </div>
            </div>
          }

          <!-- Create Token Form -->
          <div class="create-form">
            <h3 class="form-title">{{ 'settings.mcp.createTitle' | transloco }}</h3>
            <div class="form-row">
              <app-input
                [value]="newName()"
                [placeholder]="'settings.mcp.namePlaceholder' | transloco"
                [disabled]="creating()"
                (changed)="newName.set($event)"
              />
            </div>
            <div class="form-row two-col">
              <app-select
                [value]="newScope()"
                [disabled]="creating()"
                (changed)="newScope.set($event ?? 'user')"
              >
                <option value="user">{{ 'settings.mcp.scopeUser' | transloco }}</option>
                @for (p of projects(); track p.id) {
                  <option [value]="'project:' + p.id">{{ 'settings.mcp.scopeProjectPrefix' | transloco }} {{ p.name }}</option>
                }
                @if (userService.currentUser()?.is_admin) {
                  <option value="all">{{ 'settings.mcp.scopeAll' | transloco }}</option>
                }
              </app-select>
              <app-select
                [value]="newExpiryText()"
                [disabled]="creating()"
                (changed)="onNewExpiryChange($event)"
              >
                <option value="">{{ 'settings.mcp.expiryNever' | transloco }}</option>
                <option value="30">{{ 'settings.mcp.expiry30' | transloco }}</option>
                <option value="90">{{ 'settings.mcp.expiry90' | transloco }}</option>
                <option value="365">{{ 'settings.mcp.expiry365' | transloco }}</option>
              </app-select>
            </div>
            <app-button
              variant="primary"
              size="md"
              [loading]="creating()"
              [disabled]="creating() || !newName().trim()"
              (clicked)="createToken()"
            >
              {{ creating() ? ('settings.mcp.creating' | transloco) : ('settings.mcp.create' | transloco) }}
            </app-button>
          </div>

          <!-- Connection Instructions (shown after token creation) -->
          @if (newToken()) {
            <div class="instructions">
              <h3 class="form-title">{{ 'settings.mcp.claudeCodeTitle' | transloco }}</h3>
              <p class="section-desc" [innerHTML]="'settings.mcp.claudeCodeDesc' | transloco"></p>
              <div class="code-block-wrapper">
                <pre class="code-block">{{mcpJsonSnippet()}}</pre>
                <app-button class="code-copy-btn" variant="primary" size="sm" (clicked)="copyText(mcpJsonSnippet())">
                  {{ snippetCopied() ? ('common.copied' | transloco) : ('common.copy' | transloco) }}
                </app-button>
              </div>
            </div>
          }

          <!-- Web UI Connector Instructions -->
          <div class="instructions">
            <h3 class="form-title">{{ 'settings.mcp.webConnectorTitle' | transloco }}</h3>
            <p class="section-desc">{{ 'settings.mcp.webConnectorDesc' | transloco }}</p>
            <div class="connector-url-row">
              <input
                type="text"
                class="readonly-input mono"
                [value]="mcpServerUrl()"
                readonly
                #mcpUrlInput
              />
              <app-button variant="primary" size="md" (clicked)="copyText(mcpUrlInput.value, 'connector')">
                {{ connectorCopied() ? ('common.copied' | transloco) : ('common.copy' | transloco) }}
              </app-button>
            </div>
            <p class="section-hint">{{ 'settings.mcp.webConnectorHint' | transloco }}</p>
          </div>
        </section>

        <!-- Codex Proxy Section (Admin Only) -->
        @if (userService.currentUser()?.is_admin) {
          <section class="settings-section section-spacer">
            <h2 class="section-title">{{ 'settings.codex.title' | transloco }}</h2>
            <p class="section-desc">
              {{ 'settings.codex.desc' | transloco }}
              (<code>codex/*</code>).
            </p>

            <!-- Status -->
            <div class="codex-status-card">
              @if (codexLoading()) {
                <span class="codex-status-text">{{ 'settings.codex.checking' | transloco }}</span>
              } @else {
                <span class="codex-status-dot" [class.connected]="codexStatus().connected"></span>
                <span class="codex-status-text">
                  {{ (codexStatus().connected ? 'settings.codex.connected' : 'settings.codex.notConnected') | transloco }}
                  @if (codexStatus().model_count > 0) {
                    &mdash; {{ codexStatus().model_count }} model(s) available
                  }
                </span>
                <app-button
                  variant="ghost"
                  size="sm"
                  [ariaLabel]="'settings.codex.refreshStatus' | transloco"
                  (clicked)="loadCodexStatus()"
                >
                  <app-icon size="sm">refresh</app-icon>
                </app-button>
              }
            </div>

            <!-- Accounts -->
            @if (codexStatus().accounts.length > 0) {
              <div class="codex-accounts">
                @for (acct of codexStatus().accounts; track acct.name) {
                  <div class="codex-account-row">
                    <span class="mono">{{ acct.name }}</span>
                    <span class="codex-account-status">{{ acct.status }}</span>
                    <app-button variant="danger" size="sm" (clicked)="disconnectCodexAccount(acct.name)">
                      {{ 'settings.codex.disconnect' | transloco }}
                    </app-button>
                  </div>
                }
              </div>
            }

            <!-- Models -->
            @if (codexModels().length > 0) {
              <div class="codex-models">
                <h3 class="form-title">{{ 'settings.codex.availableModels' | transloco }}</h3>
                <div class="codex-model-chips">
                  @for (m of codexModels(); track m) {
                    <span class="codex-model-chip">{{ m }}</span>
                  }
                </div>
              </div>
            }

            <!-- Connect -->
            <div class="create-form">
              <app-button
                variant="primary"
                size="md"
                [loading]="codexConnecting()"
                [disabled]="codexConnecting()"
                (clicked)="connectCodexAccount()"
              >
                {{ (codexConnecting() ? 'settings.codex.waiting' : 'settings.codex.connectAccount') | transloco }}
              </app-button>
              @if (codexConnecting()) {
                <div class="codex-callback-help">
                  <p class="codex-callback-title">{{ 'settings.codex.completeSignIn' | transloco }}</p>
                  <ol class="codex-callback-steps">
                    <li>{{ 'settings.codex.step1' | transloco }}</li>
                    <li>{{ 'settings.codex.step2' | transloco }}</li>
                    <li>{{ 'settings.codex.step3' | transloco }}</li>
                    <li>{{ 'settings.codex.step4' | transloco }}</li>
                  </ol>
                  <div class="codex-callback-input-row">
                    <app-input
                      [value]="codexCallbackUrl()"
                      [placeholder]="'settings.codex.callbackPlaceholder' | transloco"
                      (changed)="codexCallbackUrl.set($event)"
                    />
                    <app-button
                      variant="primary"
                      size="md"
                      [loading]="codexCallbackSubmitting()"
                      [disabled]="codexCallbackSubmitting()"
                      (clicked)="submitCodexCallback()"
                    >
                      {{ (codexCallbackSubmitting() ? 'settings.codex.submitting' : 'settings.codex.completeLogin') | transloco }}
                    </app-button>
                  </div>
                  @if (codexCallbackError()) {
                    <p class="codex-callback-error">{{ codexCallbackError() }}</p>
                  }
                  <p class="codex-callback-hint">
                    {{ 'settings.codex.portForwardHint' | transloco }}
                  </p>
                </div>
              }
            </div>
          </section>

          <!-- Cloud Storage Section (Admin Only, Phase 4) -->
          <section class="settings-section section-spacer">
            <h2 class="section-title">{{ 'settings.cloud.title' | transloco }}</h2>
            <p class="section-desc">
              {{ 'settings.cloud.desc' | transloco }}
            </p>

            @if (cloudLoading()) {
              <p class="section-desc">{{ 'settings.cloud.loading' | transloco }}</p>
            } @else if (cloudSettings(); as s) {
              <!-- Status row -->
              <div class="codex-status-card">
                <span
                  class="codex-status-dot"
                  [class.connected]="s.effective.is_initialized"
                ></span>
                <span class="codex-status-text">
                  {{ 'settings.cloud.active' | transloco }} <strong>{{ s.effective.backend_id }}</strong>
                  @if (s.effective.is_initialized) { &mdash; {{ 'settings.cloud.initialized' | transloco }} }
                  @else { &mdash; {{ 'settings.cloud.notInitialized' | transloco }} }
                </span>
                <app-button
                  variant="ghost"
                  size="sm"
                  [ariaLabel]="'settings.cloud.refresh' | transloco"
                  (clicked)="loadCloudSettings()"
                >
                  <app-icon size="sm">refresh</app-icon>
                </app-button>
              </div>

              <!-- Backend selector -->
              <app-form-field [label]="'settings.cloud.backend' | transloco">
                <app-select
                  [value]="cloudForm().backend_id"
                  (changed)="updateCloudForm('backend_id', $event ?? '')"
                >
                  @for (backend of s.allowed_backends; track backend) {
                    <option [value]="backend">{{ backend }}</option>
                  }
                </app-select>
              </app-form-field>

              <!-- Common URL fields -->
              <app-form-field [label]="'settings.cloud.baseUrl' | transloco">
                <app-input
                  [value]="cloudForm().base_url || ''"
                  (changed)="updateCloudForm('base_url', $event)"
                />
              </app-form-field>
              <app-form-field [label]="'settings.cloud.publicUrl' | transloco">
                <app-input
                  [value]="cloudForm().public_url || ''"
                  (changed)="updateCloudForm('public_url', $event)"
                />
              </app-form-field>

              @if (cloudForm().backend_id === 'opencloud') {
                <app-form-field [label]="'settings.cloud.keycloakIssuer' | transloco">
                  <app-input
                    [value]="cloudForm().keycloak_issuer || ''"
                    (changed)="updateCloudForm('keycloak_issuer', $event)"
                  />
                </app-form-field>
                <app-form-field [label]="'settings.cloud.keycloakClientId' | transloco">
                  <app-input
                    [value]="cloudForm().keycloak_client_id || ''"
                    (changed)="updateCloudForm('keycloak_client_id', $event)"
                  />
                </app-form-field>
                <app-form-field [label]="'settings.cloud.adminRole' | transloco">
                  <app-input
                    [value]="cloudForm().admin_role_claim_value || ''"
                    (changed)="updateCloudForm('admin_role_claim_value', $event)"
                  />
                </app-form-field>
                <app-form-field [label]="'settings.cloud.spaceQuota' | transloco">
                  <app-input
                    type="number"
                    [value]="cloudQuotaText()"
                    (changed)="onCloudQuotaChange($event)"
                  />
                </app-form-field>
              }

              @if (cloudForm().backend_id === 'nextcloud') {
                <app-form-field [label]="'settings.cloud.adminUser' | transloco">
                  <app-input
                    [value]="cloudForm().admin_user || ''"
                    (changed)="updateCloudForm('admin_user', $event)"
                  />
                </app-form-field>
                <app-form-field [label]="'settings.cloud.agentUser' | transloco">
                  <app-input
                    [value]="cloudForm().agent_user || ''"
                    (changed)="updateCloudForm('agent_user', $event)"
                  />
                </app-form-field>
              }

              <!-- Credentials ref -->
              <app-form-field [label]="'settings.cloud.credentialsRef' | transloco">
                <app-input
                  [value]="cloudCredentialsRef()"
                  placeholder="env:OPENCLOUD_KEYCLOAK_CLIENT_SECRET"
                  (changed)="cloudCredentialsRef.set($event)"
                />
              </app-form-field>

              <!-- Secret provenance -->
              @if (secretProvenanceEntries().length > 0) {
                <div class="codex-accounts secret-provenance">
                  <h3 class="form-title">{{ 'settings.cloud.secretProvenance' | transloco }}</h3>
                  @for (entry of secretProvenanceEntries(); track entry.field) {
                    <div class="codex-account-row">
                      <span class="mono">{{ entry.field }}</span>
                      <span class="mono">{{ entry.env_var }}</span>
                      <span
                        class="codex-account-status"
                        [class.connected]="entry.set"
                      >
                        {{ entry.set ? ('settings.cloud.secretSet' | transloco: {chars: entry.length}) : ('settings.cloud.secretUnset' | transloco) }}
                      </span>
                    </div>
                  }
                </div>
              }

              <!-- Buttons -->
              <div class="cloud-button-row">
                <app-button
                  variant="primary"
                  size="md"
                  [loading]="cloudTesting()"
                  [disabled]="cloudBusy()"
                  (clicked)="testCloudSettings()"
                >
                  {{ (cloudTesting() ? 'settings.cloud.testing' : 'settings.cloud.test') | transloco }}
                </app-button>
                <app-button
                  variant="primary"
                  size="md"
                  [loading]="cloudSaving()"
                  [disabled]="cloudBusy()"
                  (clicked)="saveCloudSettings()"
                >
                  {{ (cloudSaving() ? 'settings.cloud.saving' : 'settings.cloud.saveReload') | transloco }}
                </app-button>
                @if (s.overlay.present) {
                  <app-button
                    variant="danger"
                    size="md"
                    [disabled]="cloudBusy()"
                    (clicked)="resetCloudSettings()"
                  >
                    {{ 'settings.cloud.resetEnv' | transloco }}
                  </app-button>
                }
              </div>

              @if (cloudMessage()) {
                <p
                  class="section-desc cloud-message"
                  [class.codex-callback-error]="cloudMessageIsError()"
                >
                  {{ cloudMessage() }}
                </p>
              }

              @if (s.overlay.present) {
                <p class="section-desc cloud-overlay-info">
                  {{ 'settings.cloud.persistedOverlayLastSaved' | transloco }}
                  @if (s.overlay.updated_at) { {{ formatDate(s.overlay.updated_at) }} }
                  @if (s.overlay.updated_by) { by {{ s.overlay.updated_by }} }
                </p>
              }
            }
          </section>
        }
      </div>
    </div>
  `,
  styles: [`
    :host { display: block; height: 100%; overflow: auto; }

    .settings-page {
      padding: 32px;
      max-width: 800px;
      margin: 0 auto;
      color: var(--text-primary);
    }

    .page-header {
      display: flex;
      align-items: center;
      gap: 12px;
      margin-bottom: 32px;
    }

    .page-title {
      font-size: 24px;
      font-weight: 700;
      margin: 0;
      color: var(--text-primary);
    }

    .settings-section {
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

    .section-desc code {
      background: var(--surface-0);
      padding: 2px 6px;
      border-radius: 4px;
      font-size: 12px;
    }

    /* Key + Token tables */
    .key-table, .token-table {
      margin-bottom: 20px;
      border: 1px solid var(--border-color);
      border-radius: 8px;
      overflow: hidden;
    }

    .key-header, .key-row {
      display: grid;
      grid-template-columns: 1.5fr 1.2fr 1.2fr 1fr 90px;
      padding: 10px 14px;
      gap: 8px;
      align-items: center;
      font-size: 13px;
    }

    .token-header, .token-row {
      display: grid;
      grid-template-columns: 2fr 1.2fr 1fr 0.8fr 1fr 1fr 90px;
      padding: 10px 14px;
      gap: 8px;
      align-items: center;
      font-size: 13px;
    }

    .key-header, .token-header {
      background: var(--surface-0);
      font-weight: 600;
      font-size: 12px;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }

    .key-row, .token-row {
      border-top: 1px solid var(--border-color);
    }

    .mono {
      font-family: 'JetBrains Mono', 'Fira Code', monospace;
      font-size: 12px;
      color: var(--text-muted);
    }

    .empty-state {
      font-size: 13px;
      color: var(--text-muted);
      text-align: center;
      padding: 24px;
      margin-bottom: 20px;
    }

    /* New token banner */
    .new-token-banner {
      background: var(--success-tint);
      border: 1px solid var(--success);
      border-radius: 8px;
      padding: 14px;
      margin-bottom: 20px;
    }

    .new-token-warning {
      font-size: 13px;
      font-weight: 600;
      color: var(--success);
      margin-bottom: 10px;
    }

    .new-token-row {
      display: flex;
      gap: 8px;
    }

    .new-token-input,
    .readonly-input {
      flex: 1;
      padding: 8px 12px;
      background: var(--surface-0);
      border: 1px solid var(--border-color);
      border-radius: 6px;
      color: var(--text-primary);
      font-family: 'JetBrains Mono', 'Fira Code', monospace;
      font-size: 12px;
      outline: none;
    }

    /* Form layout */
    .form-block {
      display: flex;
      flex-direction: column;
      gap: 12px;
    }

    .create-form {
      border-top: 1px solid var(--border-color);
      padding-top: 20px;
      margin-bottom: 20px;
    }

    .form-title {
      font-size: 14px;
      font-weight: 600;
      margin-bottom: 12px;
      color: var(--text-primary);
    }

    .form-row {
      margin-bottom: 10px;
    }

    .two-col {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
    }

    .actions-row {
      display: flex;
      align-items: center;
      gap: 12px;
      margin-top: 12px;
    }

    .channel-list {
      display: flex;
      gap: 16px;
      flex-wrap: wrap;
      margin-top: 4px;
    }

    .quiet-hours-row { margin-top: 12px; }

    .time-input {
      width: 100%;
      padding: 8px 12px;
      background: var(--surface-0);
      border: 1px solid var(--border-color);
      border-radius: 6px;
      color: var(--text-primary);
      font-family: inherit;
      font-size: 13px;
      outline: none;
    }
    .time-input:focus { border-color: var(--accent-color); }

    /* LLM Endpoints */
    .endpoint-card {
      border: 1px solid var(--border-color);
      border-radius: 8px;
      padding: 12px;
      margin-bottom: 12px;
      background: var(--surface-0);
    }

    .endpoint-head {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 12px;
      margin-bottom: 10px;
    }

    .endpoint-title {
      display: flex;
      flex-direction: column;
      gap: 2px;
      min-width: 0;
    }

    .endpoint-url {
      font-size: 12px;
      color: var(--text-muted);
      word-break: break-all;
    }

    .endpoint-key {
      font-size: 11px;
      color: var(--text-muted);
    }

    .endpoint-key.muted {
      font-style: italic;
    }

    .endpoint-actions {
      display: flex;
      gap: 8px;
      flex-shrink: 0;
    }

    .test-result {
      font-size: 12px;
      padding: 6px 10px;
      border-radius: 6px;
      margin-bottom: 10px;
    }

    .test-result.ok {
      background: var(--success-tint);
      color: var(--success);
    }

    .test-result.err {
      background: var(--danger-tint);
      color: var(--danger);
      word-break: break-word;
    }

    .form-error {
      color: var(--danger);
      font-size: 12px;
      margin: 0 0 10px;
    }

    .subsection-title {
      font-size: 12px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      color: var(--text-muted);
      margin: 16px 0 8px;
      padding-top: 12px;
      border-top: 1px solid var(--border-color);
    }

    .catalog-hint {
      font-size: 12px;
      color: var(--text-muted);
      margin: 0;
    }

    /* Instructions */
    .instructions {
      border-top: 1px solid var(--border-color);
      padding-top: 20px;
    }

    .code-block-wrapper {
      position: relative;
    }

    .code-block {
      background: var(--surface-0);
      border: 1px solid var(--border-color);
      border-radius: 8px;
      padding: 14px;
      padding-right: 80px;
      font-family: 'JetBrains Mono', 'Fira Code', monospace;
      font-size: 12px;
      line-height: 1.5;
      color: var(--text-primary);
      overflow-x: auto;
      white-space: pre;
    }

    .code-copy-btn {
      position: absolute;
      top: 8px;
      right: 8px;
    }

    .connector-url-row {
      display: flex;
      gap: 8px;
      margin-bottom: 8px;
    }

    .section-hint {
      font-size: 12px;
      color: var(--text-secondary);
      line-height: 1.5;
    }

    /* Codex Proxy */
    .codex-status-card {
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 12px 16px;
      background: var(--surface-0);
      border: 1px solid var(--border-color);
      border-radius: 8px;
      margin-bottom: 16px;
    }

    .codex-status-dot {
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: var(--danger);
      flex-shrink: 0;
    }

    .codex-status-dot.connected {
      background: var(--success);
    }

    .codex-status-text {
      font-size: 13px;
      color: var(--text-secondary);
      flex: 1;
    }

    .codex-accounts {
      margin-bottom: 16px;
    }

    .secret-provenance { margin-top: 16px; }

    .codex-account-row {
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 8px 14px;
      border: 1px solid var(--border-color);
      border-radius: 8px;
      margin-bottom: 6px;
    }

    .codex-account-row .mono { flex: 1; }

    .codex-account-status {
      font-size: 12px;
      color: var(--text-muted);
    }
    .codex-account-status.connected { color: var(--success); }

    .codex-models {
      margin-bottom: 16px;
    }

    .codex-model-chips {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }

    .codex-model-chip {
      padding: 4px 10px;
      background: var(--surface-0);
      border: 1px solid var(--border-color);
      border-radius: 6px;
      font-family: 'JetBrains Mono', 'Fira Code', monospace;
      font-size: 12px;
      color: var(--text-secondary);
    }

    /* Codex callback paste flow */
    .codex-callback-help {
      margin-top: 16px;
      padding: 16px;
      background: var(--surface-0);
      border: 1px solid var(--accent-color);
      border-radius: 8px;
    }

    .codex-callback-title {
      font-size: 14px;
      font-weight: 600;
      color: var(--accent-color);
      margin-bottom: 10px;
    }

    .codex-callback-steps {
      font-size: 13px;
      color: var(--text-secondary);
      line-height: 1.6;
      margin: 0 0 14px 0;
      padding-left: 20px;
    }

    .codex-callback-steps code {
      background: var(--surface-0);
      padding: 2px 6px;
      border-radius: 4px;
      font-size: 12px;
    }

    .codex-callback-input-row {
      display: flex;
      gap: 8px;
      margin-bottom: 8px;
      align-items: stretch;
    }
    .codex-callback-input-row > app-input { flex: 1; }

    .codex-callback-error {
      font-size: 13px;
      color: var(--danger);
      margin: 6px 0 0 0;
    }

    .codex-callback-hint {
      font-size: 12px;
      color: var(--text-muted);
      margin: 10px 0 0 0;
    }

    .codex-callback-hint code {
      background: var(--surface-0);
      padding: 2px 6px;
      border-radius: 4px;
      font-size: 11px;
    }

    /* Cloud Storage */
    .cloud-button-row {
      display: flex;
      gap: 12px;
      margin-top: 20px;
      flex-wrap: wrap;
    }
    .cloud-message { margin-top: 12px; }
    .cloud-overlay-info { margin-top: 8px; }
  `],
})
export class SettingsComponent implements OnInit {
  readonly tokenService = inject(McpTokenService);
  readonly userService = inject(UserService);
  readonly settingsService = inject(SettingsService);
  readonly modelService = inject(ModelService);
  readonly i18n = inject(I18nService);
  private readonly apiService = inject(ApiService);
  private readonly router = inject(Router);
  private readonly transloco = inject(TranslocoService);

  // Provider list for dropdown
  readonly providers = PROVIDERS;

  // MCP token form state
  readonly newName = signal('');
  readonly newScope = signal('user');
  readonly newExpiry = signal<number | null>(null);
  readonly newExpiryText = computed(() => {
    const v = this.newExpiry();
    return v == null ? '' : String(v);
  });
  readonly creating = signal(false);
  readonly newToken = signal<McpTokenCreateResponse | null>(null);
  readonly copied = signal(false);
  readonly snippetCopied = signal(false);
  readonly connectorCopied = signal(false);
  readonly projects = signal<Project[]>([]);

  // API key form state
  readonly keyProvider = signal<ApiKeyProvider>('openai');
  readonly keyValue = signal('');
  readonly keyLabel = signal('');
  readonly settingKey = signal(false);

  // LLM endpoint form state
  readonly newEndpointLabel = signal('');
  readonly newEndpointBaseUrl = signal('');
  readonly newEndpointApiKey = signal('');
  readonly newEndpointAllowInsecure = signal(false);
  readonly creatingEndpoint = signal(false);
  readonly endpointFormError = signal<string>('');
  readonly testingEndpointId = signal<string | null>(null);
  readonly testResults = signal<Record<string, LlmEndpointTestResult>>({});

  // Preferences form state — null = user hasn't overridden, use resolved default
  readonly prefModel = signal<string | null>(null);
  readonly prefAuxModel = signal<string | null>(null);
  readonly prefAutonomy = signal<string | null>(null);
  readonly prefReasoning = signal<string | null>(null);
  readonly prefVisionModel = signal<string | null>(null);
  readonly prefWhisperModel = signal<string | null>(null);
  readonly prefEmbeddingModel = signal<string | null>(null);
  readonly prefEmbeddingProvider = signal<string | null>(null);
  readonly savingPrefs = signal(false);
  readonly prefsSaved = signal(false);

  /** Resolved defaults shortcut for template use. */
  readonly resolved = this.settingsService.resolvedDefaults;

  // Persistent Agent form state — null = use resolved default
  readonly paModel = signal<string | null>(null);
  readonly paPermissionMode = signal<string | null>(null);
  readonly paConfigName = signal('');
  readonly paGreeting = signal('');
  readonly paIdleTimeout = signal<number | null>(null);
  readonly paIdleTimeoutText = computed(() => {
    const v = this.paIdleTimeout();
    return v == null ? '' : String(v);
  });
  readonly paCommandAllowlist = signal('');
  readonly savingPA = signal(false);
  readonly paSaved = signal(false);

  // Communication form state
  readonly commDelivery = signal<'next_strategic_phase' | 'immediate_interrupt' | 'llm_triage'>('next_strategic_phase');
  readonly commChannelEmail = signal(true);
  readonly commChannelNtfy = signal(false);
  readonly commChannelSlack = signal(false);
  readonly commChannelDiscord = signal(false);
  readonly commQuietEnabled = signal(false);
  readonly commQuietStart = signal('22:00');
  readonly commQuietEnd = signal('08:00');
  readonly commQuietTimezone = signal('');
  readonly savingComm = signal(false);
  readonly commSaved = signal(false);

  // Codex proxy state (admin-only)
  readonly codexStatus = signal<CodexStatus>({ connected: false, accounts: [], model_count: 0 });
  readonly codexModels = signal<string[]>([]);
  readonly codexLoading = signal(false);
  readonly codexConnecting = signal(false);
  readonly codexCallbackUrl = signal('');
  readonly codexCallbackSubmitting = signal(false);
  readonly codexCallbackError = signal('');
  private codexPollTimer: ReturnType<typeof setInterval> | null = null;

  // Cloud storage state (admin-only, Phase 4)
  readonly cloudSettings = signal<MainCloudSettingsResponse | null>(null);
  readonly cloudLoading = signal(false);
  readonly cloudSaving = signal(false);
  readonly cloudTesting = signal(false);
  readonly cloudMessage = signal('');
  readonly cloudMessageIsError = signal(false);
  readonly cloudCredentialsRef = signal('');
  readonly cloudForm = signal<MainCloudFormState>({
    backend_id: 'opencloud',
    base_url: '',
    public_url: '',
    admin_user: '',
    agent_user: '',
    keycloak_issuer: '',
    keycloak_client_id: '',
    admin_role_claim_value: '',
    default_quota_bytes: null,
  });

  readonly cloudBusy = computed(() => this.cloudSaving() || this.cloudTesting());
  readonly cloudQuotaText = computed(() => {
    const v = this.cloudForm().default_quota_bytes;
    return v == null ? '' : String(v);
  });
  readonly secretProvenanceEntries = computed(() => {
    const s = this.cloudSettings();
    if (!s) return [];
    return Object.entries(s.secrets).map(([field, prov]) => ({
      field,
      env_var: prov.env_var,
      set: prov.set,
      length: prov.length,
    }));
  });

  constructor() {
    // Reactively sync preference form fields when the preferences signal updates.
    // null = user hasn't overridden this field (show resolved default).
    effect(() => {
      const prefs = this.settingsService.preferences();
      if (Object.keys(prefs).length > 0) {
        this.prefModel.set(prefs.default_model ?? null);
        this.prefAuxModel.set(prefs.default_auxiliary_model ?? null);
        this.prefAutonomy.set(prefs.default_autonomy ?? null);
        this.prefReasoning.set(prefs.default_reasoning_level ?? null);
        this.prefVisionModel.set(prefs.default_vision_model ?? null);
        this.prefWhisperModel.set(prefs.default_whisper_model ?? null);
        this.prefEmbeddingModel.set(prefs.default_embedding_model ?? null);
        this.prefEmbeddingProvider.set(prefs.embedding_provider ?? null);

        // Sync persistent agent preferences
        const pa = prefs.persistent_agent;
        if (pa) {
          this.paModel.set(pa.model ?? null);
          this.paPermissionMode.set(pa.permission_mode ?? null);
          this.paConfigName.set(pa.config_name || '');
          this.paGreeting.set(pa.greeting || '');
          this.paIdleTimeout.set(pa.idle_timeout_minutes ?? null);
          this.paCommandAllowlist.set((pa.command_allowlist || []).join(', '));
        }

        // Sync communication preferences
        const comm = prefs.communication;
        if (comm) {
          this.commDelivery.set(comm.delivery?.async_reply || 'next_strategic_phase');
          this.commChannelEmail.set(comm.channels?.email ?? true);
          this.commChannelNtfy.set(comm.channels?.ntfy ?? false);
          this.commChannelSlack.set(comm.channels?.slack_webhook ?? false);
          this.commChannelDiscord.set(comm.channels?.discord_webhook ?? false);
          this.commQuietEnabled.set(comm.quiet_hours?.enabled ?? false);
          this.commQuietStart.set(comm.quiet_hours?.start || '22:00');
          this.commQuietEnd.set(comm.quiet_hours?.end || '08:00');
          this.commQuietTimezone.set(comm.quiet_hours?.timezone || '');
        }
      }
    });

    // Load projects reactively — waits for currentUserId on F5 refresh
    effect(() => {
      const userId = this.userService.currentUserId();
      if (userId) {
        this.apiService
            .getProjects(userId)
            .subscribe((p) => this.projects.set(p));
      }
    });

    // Load admin-only sections reactively — waits for currentUser on F5
    // refresh, otherwise the panels render their headers but never fetch
    // (currentUser() is null when ngOnInit runs after a hard reload).
    // Guarded by `_adminLoadersFired` so subsequent user signal updates
    // (e.g. background refresh) don't re-trigger the fetches.
    effect(() => {
      if (this._adminLoadersFired) return;
      const user = this.userService.currentUser();
      if (user?.is_admin) {
        this._adminLoadersFired = true;
        this.loadCodexStatus();
        this.loadCloudSettings();
      }
    });
  }

  private _adminLoadersFired = false;

  /** Only show active (non-revoked) tokens. */
  activeTokens = () =>
    this.tokenService.tokens().filter((t) => !t.revoked_at);

  ngOnInit(): void {
    this.modelService.load();
    this.tokenService.loadTokens();
    this.settingsService.loadApiKeys();
    this.settingsService.loadLlmEndpoints();
    this.settingsService.loadPreferences();
    // Admin-only loaders (codex status + cloud settings) are triggered
    // by the effect in the constructor — that path waits for currentUser()
    // to populate, which is the only thing that works on a hard F5 reload.
  }

  providerLabel(provider: string): string {
    return PROVIDERS.find((p) => p.value === provider)?.label || provider;
  }

  formatScope(scope: string): string {
    if (scope === 'user') return this.transloco.translate('settings.helpers.scopeMyData');
    if (scope === 'all') return this.transloco.translate('settings.helpers.scopeFullAccess');
    if (scope.startsWith('project:')) {
      const pid = scope.split(':', 2)[1];
      const p = this.projects().find((pr) => pr.id === pid);
      return p ? `Project: ${p.name}` : `Project`;
    }
    return scope;
  }

  formatOrigin(origin: string | null): string {
    if (!origin) return this.transloco.translate('settings.helpers.originManual');
    if (origin.startsWith('oauth:')) {
      const name = origin.slice(6).replace(/:refresh$/, '');
      return name.charAt(0).toUpperCase() + name.slice(1);
    }
    return origin;
  }

  formatDate(iso: string): string {
    const d = new Date(iso);
    const now = new Date();
    const diffMs = now.getTime() - d.getTime();
    if (diffMs < 60_000) return this.transloco.translate('settings.helpers.timeJustNow');
    if (diffMs < 3600_000) return this.transloco.translate('settings.helpers.timeMinutesAgo', {n: Math.floor(diffMs / 60_000)});
    if (diffMs < 86400_000) return this.transloco.translate('settings.helpers.timeHoursAgo', {n: Math.floor(diffMs / 3600_000)});
    return d.toLocaleDateString(this.transloco.getActiveLang(), { month: 'short', day: 'numeric' });
  }

  mcpJsonSnippet = () => {
    const token = this.newToken()?.token ?? 'srw_YOUR_TOKEN_HERE';
    const mcpUrl = environment.mcpUrl;
    return JSON.stringify(
      {
        mcpServers: {
          orchestrator: {
            type: 'http',
            url: mcpUrl,
            headers: {
              Authorization: `Bearer ${token}`,
            },
          },
        },
      },
      null,
      2,
    );
  };

  mcpServerUrl = () => {
    return environment.mcpUrl;
  };

  asInputValue(event: Event): string {
    return (event.target as HTMLInputElement).value;
  }

  copyText(text: string, target: string = 'snippet'): void {
    navigator.clipboard.writeText(text).then(() => {
      if (target === 'connector') {
        this.connectorCopied.set(true);
        setTimeout(() => this.connectorCopied.set(false), 2000);
      } else {
        this.snippetCopied.set(true);
        setTimeout(() => this.snippetCopied.set(false), 2000);
      }
    });
  }

  // ── Three-state default helpers ─────────────────────────────────

  /**
   * Update a "null = use resolved default" preference signal from a select change.
   * If the chosen value matches the resolved default, store null (so the user is
   * still tracking the framework default); otherwise store the override.
   */
  onPrefChange(
    target: { set: (v: string | null) => void },
    resolvedValue: string | undefined | null,
    next: string | null,
  ): void {
    if (next === null || next === '') {
      target.set(null);
      return;
    }
    target.set(next === resolvedValue ? null : next);
  }

  // ── API Keys ──────────────────────────────────────────────────────

  onKeyProviderChange(value: string | null): void {
    if (value) this.keyProvider.set(value as ApiKeyProvider);
  }

  saveApiKey(): void {
    const value = this.keyValue().trim();
    if (!value) return;
    this.settingKey.set(true);

    this.settingsService
      .setApiKey(this.keyProvider(), {
        api_key: value,
        label: this.keyLabel().trim() || undefined,
      })
      .subscribe({
        next: () => {
          this.keyValue.set('');
          this.keyLabel.set('');
          this.settingKey.set(false);
        },
        error: () => this.settingKey.set(false),
      });
  }

  deleteApiKey(provider: string): void {
    this.settingsService.deleteApiKey(provider).subscribe();
  }

  // ── LLM Endpoints ─────────────────────────────────────────────────

  createLlmEndpoint(): void {
    const label = this.newEndpointLabel().trim();
    const baseUrl = this.newEndpointBaseUrl().trim();
    if (!label || !baseUrl) return;

    const httpsLike = /^https?:\/\//i.test(baseUrl);
    if (!httpsLike) {
      this.endpointFormError.set(
        this.transloco.translate('settings.llmEndpoints.errorUrlScheme'),
      );
      return;
    }
    if (baseUrl.startsWith('http://') && !this.newEndpointAllowInsecure()) {
      this.endpointFormError.set(
        this.transloco.translate('settings.llmEndpoints.errorHttpNeedsOptIn'),
      );
      return;
    }

    this.endpointFormError.set('');
    this.creatingEndpoint.set(true);
    this.settingsService
      .createLlmEndpoint({
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
          this.creatingEndpoint.set(false);
          const detail = err?.error?.detail ?? err?.message ?? 'Failed';
          this.endpointFormError.set(String(detail));
        },
      });
  }

  deleteLlmEndpoint(endpointId: string): void {
    const msg = this.transloco.translate('settings.llmEndpoints.confirmDelete');
    if (!window.confirm(msg)) return;
    this.settingsService.deleteLlmEndpoint(endpointId).subscribe({
      next: () => {
        const next = {...this.testResults()};
        delete next[endpointId];
        this.testResults.set(next);
      },
    });
  }

  testLlmEndpoint(endpointId: string): void {
    this.testingEndpointId.set(endpointId);
    this.settingsService.testLlmEndpoint(endpointId).subscribe({
      next: (result) => {
        this.testResults.set({...this.testResults(), [endpointId]: result});
        this.testingEndpointId.set(null);
      },
      error: (err) => {
        this.testResults.set({
          ...this.testResults(),
          [endpointId]: {
            ok: false,
            status: null,
            error: err?.error?.detail ?? err?.message ?? 'Request failed',
            probe_url: '',
          },
        });
        this.testingEndpointId.set(null);
      },
    });
  }

  // ── Preferences ───────────────────────────────────────────────────

  savePreferences(): void {
    this.savingPrefs.set(true);
    this.prefsSaved.set(false);

    const settings: Record<string, string | null> = {};
    settings['default_model'] = this.prefModel()?.trim() || null;
    settings['default_auxiliary_model'] = this.prefAuxModel()?.trim() || null;
    settings['default_autonomy'] = this.prefAutonomy() || null;
    settings['default_reasoning_level'] = this.prefReasoning() || null;
    settings['default_vision_model'] = this.prefVisionModel() || null;
    settings['default_whisper_model'] = this.prefWhisperModel() || null;
    settings['default_embedding_model'] = this.prefEmbeddingModel() || null;
    settings['embedding_provider'] = this.prefEmbeddingProvider() || null;

    this.settingsService.updatePreferences(settings).subscribe({
      next: () => {
        this.savingPrefs.set(false);
        this.prefsSaved.set(true);
        setTimeout(() => this.prefsSaved.set(false), 2000);
      },
      error: () => this.savingPrefs.set(false),
    });
  }

  onLanguageChange(lang: SupportedLang): void {
    this.i18n.setLanguage(lang).subscribe();
  }

  // ── Persistent Agent Settings ──────────────────────────────────

  onPaModelChange(text: string): void {
    this.paModel.set(text.trim() || null);
  }

  onPaIdleTimeoutChange(text: string): void {
    if (text === '' || text == null) {
      this.paIdleTimeout.set(null);
      return;
    }
    const n = Number(text);
    this.paIdleTimeout.set(Number.isFinite(n) ? n : null);
  }

  savePersistentAgent(): void {
    this.savingPA.set(true);
    this.paSaved.set(false);

    const allowlistText = this.paCommandAllowlist().trim();
    const allowlist = allowlistText
        ? allowlistText.split(',').map(s => s.trim()).filter(Boolean)
        : null;

    const settings: Record<string, unknown> = {
      persistent_agent: {
        model: this.paModel()?.trim() || null,
        permission_mode: this.paPermissionMode() || null,
        config_name: this.paConfigName() || null,
        greeting: this.paGreeting().trim() || null,
        idle_timeout_minutes: this.paIdleTimeout() || null,
        command_allowlist: allowlist,
      },
    };

    this.settingsService.updatePreferences(settings).subscribe({
      next: () => {
        this.savingPA.set(false);
        this.paSaved.set(true);
        setTimeout(() => this.paSaved.set(false), 2000);
      },
      error: () => this.savingPA.set(false),
    });
  }

  // ── Communication Settings ──────────────────────────────────────

  saveCommunication(): void {
    this.savingComm.set(true);
    this.commSaved.set(false);

    const communication: CommunicationSettings = {
      delivery: {
        async_reply: this.commDelivery(),
        urgent_override: true,
      },
      channels: {
        email: this.commChannelEmail(),
        cockpit: true,
        ntfy: this.commChannelNtfy(),
        slack_webhook: this.commChannelSlack(),
        discord_webhook: this.commChannelDiscord(),
      },
      quiet_hours: {
        enabled: this.commQuietEnabled(),
        start: this.commQuietStart(),
        end: this.commQuietEnd(),
        timezone: this.commQuietTimezone() || undefined,
      },
    };

    this.settingsService.updatePreferences({ communication }).subscribe({
      next: () => {
        this.savingComm.set(false);
        this.commSaved.set(true);
        setTimeout(() => this.commSaved.set(false), 2000);
      },
      error: () => this.savingComm.set(false),
    });
  }

  // ── MCP Tokens ────────────────────────────────────────────────────

  onNewExpiryChange(value: string | null): void {
    if (value === null || value === '') {
      this.newExpiry.set(null);
      return;
    }
    const n = Number(value);
    this.newExpiry.set(Number.isFinite(n) ? n : null);
  }

  createToken(): void {
    const name = this.newName().trim();
    if (!name) return;
    this.creating.set(true);
    this.newToken.set(null);
    this.copied.set(false);

    this.tokenService
      .createToken({
        name,
        scope: this.newScope(),
        expires_in_days: this.newExpiry(),
      })
      .subscribe({
        next: (res) => {
          this.newToken.set(res);
          this.newName.set('');
          this.creating.set(false);
        },
        error: () => this.creating.set(false),
      });
  }

  revokeToken(tokenId: string): void {
    this.tokenService.revokeToken(tokenId).subscribe();
  }

  copyToken(input: HTMLInputElement): void {
    navigator.clipboard.writeText(input.value).then(() => {
      this.copied.set(true);
      setTimeout(() => this.copied.set(false), 2000);
    });
  }

  // ── Codex Proxy Management ──────────────────────────────────

  loadCodexStatus(): void {
    this.codexLoading.set(true);
    this.settingsService.getCodexStatus().subscribe((status) => {
      this.codexStatus.set(status);
      this.codexLoading.set(false);
    });
    this.settingsService.getCodexModels().subscribe((res) => {
      this.codexModels.set(res.models);
    });
  }

  connectCodexAccount(): void {
    this.codexConnecting.set(true);
    this.settingsService.startCodexLogin().subscribe({
      next: (res) => {
        window.open(res.auth_url, '_blank');
        this.codexPollTimer = setInterval(() => {
          this.settingsService.pollCodexLogin(res.state).subscribe((poll) => {
            if (poll.status !== 'wait') {
              this.stopCodexPoll();
              this.codexConnecting.set(false);
              this.loadCodexStatus();
            }
          });
        }, 2000);
      },
      error: () => this.codexConnecting.set(false),
    });
  }

  submitCodexCallback(): void {
    const url = this.codexCallbackUrl().trim();
    if (!url) return;

    try {
      const parsed = new URL(url);
      if (!parsed.searchParams.get('code') || !parsed.searchParams.get('state')) {
        this.codexCallbackError.set(this.transloco.translate('settings.codex.errors.urlMissingParams'));
        return;
      }
    } catch {
      this.codexCallbackError.set(this.transloco.translate('settings.codex.errors.invalidUrl'));
      return;
    }

    this.codexCallbackError.set('');
    this.codexCallbackSubmitting.set(true);

    this.settingsService.completeCodexLogin(url).subscribe({
      next: () => {
        this.codexCallbackSubmitting.set(false);
        this.codexCallbackUrl.set('');
        this.stopCodexPoll();
        this.codexConnecting.set(false);
        this.loadCodexStatus();
      },
      error: (err) => {
        this.codexCallbackSubmitting.set(false);
        const detail = err?.error?.detail || this.transloco.translate('settings.codex.errors.completeFailed');
        this.codexCallbackError.set(detail);
      },
    });
  }

  disconnectCodexAccount(name: string): void {
    this.settingsService.deleteCodexCredential(name).subscribe(() => {
      this.loadCodexStatus();
    });
  }

  private stopCodexPoll(): void {
    if (this.codexPollTimer) {
      clearInterval(this.codexPollTimer);
      this.codexPollTimer = null;
    }
  }

  // ── Cloud Storage (Phase 4) ────────────────────────────────

  loadCloudSettings(): void {
    this.cloudLoading.set(true);
    this.cloudMessage.set('');
    this.cloudMessageIsError.set(false);
    this.settingsService.getMainCloudSettings().subscribe({
      next: (res) => {
        this.cloudSettings.set(res);
        // Seed the form from the persisted overlay if present; otherwise
        // from the effective config. Both sources are JSON-shaped, so we
        // bracket-access them through Record<> wrappers and coerce to the
        // typed MainCloudFormState shape.
        const overlayValue = (res.overlay?.value as Record<string, unknown>) || {};
        const eff = res.effective as unknown as Record<string, unknown>;
        const pickStr = (key: string): string => {
          const v = overlayValue[key] ?? eff[key];
          if (v === undefined || v === null) return '';
          return typeof v === 'string' ? v : String(v);
        };
        const pickQuota = (): number | null => {
          const v = overlayValue['default_quota_bytes'] ?? eff['default_quota_bytes'];
          if (v === undefined || v === null) return null;
          return typeof v === 'number' ? v : Number(v);
        };
        this.cloudForm.set({
          backend_id: res.effective.backend_id,
          base_url: pickStr('base_url'),
          public_url: pickStr('public_url'),
          admin_user: pickStr('admin_user'),
          agent_user: pickStr('agent_user'),
          keycloak_issuer: pickStr('keycloak_issuer'),
          keycloak_client_id: pickStr('keycloak_client_id'),
          admin_role_claim_value: pickStr('admin_role_claim_value'),
          default_quota_bytes: pickQuota(),
        });
        this.cloudCredentialsRef.set(res.overlay?.credentials_ref ?? '');
        this.cloudLoading.set(false);
      },
      error: (err) => {
        this.cloudLoading.set(false);
        this.cloudMessage.set(err?.error?.detail || this.transloco.translate('settings.cloud.messages.loadFailed'));
        this.cloudMessageIsError.set(true);
      },
    });
  }

  updateCloudForm<K extends keyof MainCloudFormState>(
    key: K,
    value: MainCloudFormState[K],
  ): void {
    this.cloudForm.update((form) => ({ ...form, [key]: value }));
  }

  onCloudQuotaChange(text: string): void {
    if (text === '' || text == null) {
      this.updateCloudForm('default_quota_bytes', null);
      return;
    }
    const n = Number(text);
    this.updateCloudForm('default_quota_bytes', Number.isFinite(n) ? n : null);
  }

  private buildCloudRequestBody() {
    const form = this.cloudForm();
    const credRef = this.cloudCredentialsRef().trim() || null;
    return {
      value: { ...form },
      credentials_ref: credRef,
    };
  }

  testCloudSettings(): void {
    this.cloudTesting.set(true);
    this.cloudMessage.set('');
    this.cloudMessageIsError.set(false);
    this.settingsService.testMainCloudSettings(this.buildCloudRequestBody()).subscribe({
      next: (res) => {
        this.cloudTesting.set(false);
        this.cloudMessage.set(
          res.ok
            ? this.transloco.translate('settings.cloud.messages.testOk', {ms: res.latency_ms?.toFixed(0) ?? '?'})
            : this.transloco.translate('settings.cloud.messages.testFailed', {error: res.detail}),
        );
        this.cloudMessageIsError.set(!res.ok);
      },
      error: (err) => {
        this.cloudTesting.set(false);
        this.cloudMessage.set(err?.error?.detail || this.transloco.translate('settings.cloud.messages.testRequestFailed'));
        this.cloudMessageIsError.set(true);
      },
    });
  }

  saveCloudSettings(): void {
    this.cloudSaving.set(true);
    this.cloudMessage.set('');
    this.cloudMessageIsError.set(false);
    this.settingsService.putMainCloudSettings(this.buildCloudRequestBody()).subscribe({
      next: (res) => {
        this.cloudSaving.set(false);
        this.cloudMessage.set(
          this.transloco.translate(
            res.reloaded ? 'settings.cloud.messages.savedReloaded' : 'settings.cloud.messages.saved',
            {backend: res.backend_id},
          ),
        );
        this.cloudMessageIsError.set(false);
        this.loadCloudSettings();
      },
      error: (err) => {
        this.cloudSaving.set(false);
        this.cloudMessage.set(err?.error?.detail || this.transloco.translate('settings.cloud.messages.saveFailed'));
        this.cloudMessageIsError.set(true);
      },
    });
  }

  resetCloudSettings(): void {
    this.cloudSaving.set(true);
    this.cloudMessage.set('');
    this.cloudMessageIsError.set(false);
    this.settingsService.deleteMainCloudSettings().subscribe({
      next: () => {
        this.cloudSaving.set(false);
        this.cloudMessage.set(this.transloco.translate('settings.cloud.messages.overlayCleared'));
        this.cloudMessageIsError.set(false);
        this.loadCloudSettings();
      },
      error: (err) => {
        this.cloudSaving.set(false);
        this.cloudMessage.set(err?.error?.detail || this.transloco.translate('settings.cloud.messages.resetFailed'));
        this.cloudMessageIsError.set(true);
      },
    });
  }
}
