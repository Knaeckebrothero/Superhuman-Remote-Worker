import {Component, computed, effect, inject, OnInit, signal} from '@angular/core';
import {FormsModule} from '@angular/forms';
import {Router} from '@angular/router';
import {environment} from '../../core/environment';
import {McpTokenService} from '../../core/services/mcp-token.service';
import {UserService} from '../../core/services/user.service';
import {ApiService} from '../../core/services/api.service';
import {
  SettingsService,
  MainCloudSettingsResponse,
  MainCloudFormState,
} from '../../core/services/settings.service';
import {ModelService} from '../../core/services/model.service';
import {I18nService, SupportedLang} from '../../core/services/i18n.service';
import {
    ApiKeyProvider,
    CodexStatus,
    CommunicationSettings,
    McpTokenCreateResponse,
    Project
} from '../../core/models/api.model';
import {SidebarToggleComponent} from '../../simple/layout/sidebar-toggle/sidebar-toggle.component';
import {TranslocoPipe, TranslocoService} from '@jsverse/transloco';

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

@Component({
  selector: 'app-settings',
  standalone: true,
  imports: [FormsModule, SidebarToggleComponent, TranslocoPipe],
  template: `
    <div class="settings-page">
      <div class="settings-container">
        <div class="page-header">
          <app-sidebar-toggle />
          <h1 class="page-title">{{ 'settings.title' | transloco }}</h1>
        </div>

        <!-- Language Section -->
        <section class="settings-section">
          <h2 class="section-title">{{ 'settings.language.title' | transloco }}</h2>
          <p class="section-desc">{{ 'settings.language.desc' | transloco }}</p>
          <div class="create-form" style="border-top: none; padding-top: 0;">
            <div class="form-row">
              <div>
                <label class="field-label">{{ 'settings.language.label' | transloco }}</label>
                <select
                  class="form-input"
                  [ngModel]="i18n.activeLang()"
                  (ngModelChange)="onLanguageChange($event)"
                >
                  <option value="en">English</option>
                  <option value="de-DE">Deutsch</option>
                </select>
              </div>
            </div>
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
                    <button class="revoke-btn" (click)="deleteApiKey(key.provider)">{{ 'common.delete' | transloco }}</button>
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
              <select class="form-input" [(ngModel)]="keyProvider" [disabled]="settingKey()">
                @for (p of providers; track p.value) {
                  <option [value]="p.value">{{ p.label }}</option>
                }
              </select>
              <input
                type="text"
                class="form-input"
                [placeholder]="'settings.apiKeys.labelPlaceholder' | transloco"
                [(ngModel)]="keyLabel"
                [disabled]="settingKey()"
              />
            </div>
            <div class="form-row">
              <input
                type="password"
                class="form-input"
                [placeholder]="'settings.apiKeys.keyPlaceholder' | transloco"
                [(ngModel)]="keyValue"
                [disabled]="settingKey()"
              />
            </div>
            <button
              class="create-btn"
              (click)="saveApiKey()"
              [disabled]="settingKey() || !keyValue.trim()"
            >
              {{ settingKey() ? ('common.saving' | transloco) : ('settings.apiKeys.saveButton' | transloco) }}
            </button>
          </div>
        </section>

        <!-- Preferences Section -->
        <section class="settings-section" style="margin-top: 24px;">
          <h2 class="section-title">{{ 'settings.preferences.title' | transloco }}</h2>
          <p class="section-desc">{{ 'settings.preferences.desc' | transloco }}</p>

          <div class="create-form" style="border-top: none; padding-top: 0;">
            <div class="form-row two-col">
              <div>
                <label class="field-label">{{ 'settings.preferences.defaultModel' | transloco }}</label>
                <select
                  class="form-input"
                  [class.is-default]="!prefModel()"
                  [ngModel]="prefModel() ?? resolved().default_model ?? ''"
                  (ngModelChange)="prefModel.set($event === resolved().default_model ? null : $event)"
                >
                  @for (group of modelService.models(); track group.group) {
                    <optgroup [label]="group.configured ? group.group : group.group + ' ' + ('settings.preferences.noApiKey' | transloco)">
                      @for (model of group.models; track model) {
                        <option [value]="model">{{ model }}{{ !prefModel() && model === resolved().default_model ? ' (' + ('common.default' | transloco) + ')' : '' }}</option>
                      }
                    </optgroup>
                  }
                </select>
              </div>
              <div>
                <label class="field-label">{{ 'settings.preferences.auxModel' | transloco }}</label>
                <select
                  class="form-input"
                  [class.is-default]="!prefAuxModel()"
                  [ngModel]="prefAuxModel() ?? resolved().default_auxiliary_model ?? ''"
                  (ngModelChange)="prefAuxModel.set($event === resolved().default_auxiliary_model ? null : $event)"
                >
                  @for (m of modelService.auxiliaryModels(); track m.id) {
                    <option [value]="m.id">{{ m.label }}{{ !prefAuxModel() && m.id === resolved().default_auxiliary_model ? ' (' + ('common.default' | transloco) + ')' : '' }}{{ m.configured ? '' : ' ' + ('common.noKey' | transloco) }}</option>
                  }
                </select>
              </div>
            </div>
            <div class="form-row two-col">
              <div>
                <label class="field-label">{{ 'settings.preferences.autonomy' | transloco }}</label>
                <select
                  class="form-input"
                  [class.is-default]="!prefAutonomy()"
                  [ngModel]="prefAutonomy() ?? resolved().default_autonomy ?? ''"
                  (ngModelChange)="prefAutonomy.set($event === resolved().default_autonomy ? null : $event)"
                >
                  <option value="full">{{ 'settings.preferences.autonomyFull' | transloco }}{{ !prefAutonomy() && resolved().default_autonomy === 'full' ? ' (' + ('common.default' | transloco) + ')' : '' }}</option>
                  <option value="review">{{ 'settings.preferences.autonomyReview' | transloco }}{{ !prefAutonomy() && resolved().default_autonomy === 'review' ? ' (' + ('common.default' | transloco) + ')' : '' }}</option>
                  <option value="partial">{{ 'settings.preferences.autonomyPartial' | transloco }}{{ !prefAutonomy() && resolved().default_autonomy === 'partial' ? ' (' + ('common.default' | transloco) + ')' : '' }}</option>
                  <option value="guided">{{ 'settings.preferences.autonomyGuided' | transloco }}{{ !prefAutonomy() && resolved().default_autonomy === 'guided' ? ' (' + ('common.default' | transloco) + ')' : '' }}</option>
                  <option value="dependent">{{ 'settings.preferences.autonomyDependent' | transloco }}{{ !prefAutonomy() && resolved().default_autonomy === 'dependent' ? ' (' + ('common.default' | transloco) + ')' : '' }}</option>
                </select>
              </div>
              <div>
                <label class="field-label">{{ 'settings.preferences.reasoning' | transloco }}</label>
                <select
                  class="form-input"
                  [class.is-default]="!prefReasoning()"
                  [ngModel]="prefReasoning() ?? resolved().default_reasoning_level ?? ''"
                  (ngModelChange)="prefReasoning.set($event === resolved().default_reasoning_level ? null : $event)"
                >
                  <option value="low">{{ 'settings.preferences.reasoningLow' | transloco }}{{ !prefReasoning() && resolved().default_reasoning_level === 'low' ? ' (' + ('common.default' | transloco) + ')' : '' }}</option>
                  <option value="medium">{{ 'settings.preferences.reasoningMedium' | transloco }}{{ !prefReasoning() && resolved().default_reasoning_level === 'medium' ? ' (' + ('common.default' | transloco) + ')' : '' }}</option>
                  <option value="high">{{ 'settings.preferences.reasoningHigh' | transloco }}{{ !prefReasoning() && resolved().default_reasoning_level === 'high' ? ' (' + ('common.default' | transloco) + ')' : '' }}</option>
                </select>
              </div>
            </div>

            <h3 class="subsection-title">{{ 'settings.preferences.helperModels' | transloco }}</h3>
            <div class="form-row two-col">
              <div>
                <label class="field-label">{{ 'settings.preferences.visionModel' | transloco }}</label>
                <select
                  class="form-input"
                  [class.is-default]="!prefVisionModel()"
                  [ngModel]="prefVisionModel() ?? resolved().default_vision_model ?? ''"
                  (ngModelChange)="prefVisionModel.set($event === resolved().default_vision_model ? null : $event)"
                >
                  @for (m of modelService.visionModels(); track m.id) {
                    <option [value]="m.id">{{ m.label }}{{ !prefVisionModel() && m.id === resolved().default_vision_model ? ' (' + ('common.default' | transloco) + ')' : '' }}{{ m.configured ? '' : ' ' + ('common.noKey' | transloco) }}</option>
                  }
                </select>
                <span class="field-hint">{{ 'settings.preferences.visionHint' | transloco }}</span>
              </div>
              <div>
                <label class="field-label">{{ 'settings.preferences.whisperModel' | transloco }}</label>
                <select
                  class="form-input"
                  [class.is-default]="!prefWhisperModel()"
                  [ngModel]="prefWhisperModel() ?? resolved().default_whisper_model ?? ''"
                  (ngModelChange)="prefWhisperModel.set($event === resolved().default_whisper_model ? null : $event)"
                >
                  @for (m of modelService.whisperModels(); track m.id) {
                    <option [value]="m.id">{{ m.label }}{{ !prefWhisperModel() && m.id === resolved().default_whisper_model ? ' (' + ('common.default' | transloco) + ')' : '' }}{{ m.configured ? '' : ' ' + ('common.noKey' | transloco) }}</option>
                  }
                </select>
                <span class="field-hint">{{ 'settings.preferences.whisperHint' | transloco }}</span>
              </div>
            </div>
            <div class="form-row two-col">
              <div>
                <label class="field-label">{{ 'settings.preferences.embeddingModel' | transloco }}</label>
                <select
                  class="form-input"
                  [class.is-default]="!prefEmbeddingModel()"
                  [ngModel]="prefEmbeddingModel() ?? resolved().default_embedding_model ?? ''"
                  (ngModelChange)="prefEmbeddingModel.set($event === resolved().default_embedding_model ? null : $event)"
                >
                  @for (m of modelService.embeddingModels(); track m.id) {
                    <option [value]="m.id">{{ m.label }}{{ m.dimensions ? ' (' + m.dimensions + 'd)' : '' }}{{ !prefEmbeddingModel() && m.id === resolved().default_embedding_model ? ' (' + ('common.default' | transloco) + ')' : '' }}{{ m.configured ? '' : ' ' + ('common.noKey' | transloco) }}</option>
                  }
                </select>
                <span class="field-hint">{{ 'settings.preferences.embeddingHint' | transloco }}</span>
              </div>
              <div>
                <label class="field-label">{{ 'settings.preferences.embeddingProvider' | transloco }}</label>
                <select
                  class="form-input"
                  [class.is-default]="!prefEmbeddingProvider()"
                  [ngModel]="prefEmbeddingProvider() ?? resolved().embedding_provider ?? ''"
                  (ngModelChange)="prefEmbeddingProvider.set($event === resolved().embedding_provider ? null : $event)"
                >
                  <option value="local">{{ 'settings.preferences.providerLocal' | transloco }}{{ !prefEmbeddingProvider() && resolved().embedding_provider === 'local' ? ' (' + ('common.default' | transloco) + ')' : '' }}</option>
                  <option value="openrouter">{{ 'settings.preferences.providerOpenrouter' | transloco }}{{ !prefEmbeddingProvider() && resolved().embedding_provider === 'openrouter' ? ' (' + ('common.default' | transloco) + ')' : '' }}</option>
                </select>
                <span class="field-hint">{{ 'settings.preferences.embeddingProviderHint' | transloco }}</span>
              </div>
            </div>

            <button
              class="create-btn"
              (click)="savePreferences()"
              [disabled]="savingPrefs()"
            >
              {{ savingPrefs() ? ('common.saving' | transloco) : ('settings.preferences.save' | transloco) }}
            </button>
            @if (prefsSaved()) {
              <span class="save-feedback">{{ 'common.saved' | transloco }}</span>
            }
          </div>
        </section>

        <!-- Persistent Agent Section -->
        <section class="settings-section" style="margin-top: 24px;">
          <h2 class="section-title">{{ 'settings.persistent.title' | transloco }}</h2>
          <p class="section-desc">{{ 'settings.persistent.desc' | transloco }}</p>

          <div class="create-form" style="border-top: none; padding-top: 0;">
            <div class="form-row">
              <label class="field-label">{{ 'settings.persistent.model' | transloco }}</label>
              <input
                type="text"
                class="form-input"
                [class.is-default]="!paModel()"
                [placeholder]="resolved().persistent_agent?.model ?? ('settings.persistent.modelPlaceholder' | transloco)"
                [ngModel]="paModel() ?? ''"
                (ngModelChange)="paModel.set($event?.trim() || null)"
              />
              @if (!paModel() && resolved().persistent_agent?.model) {
                <span class="field-hint">{{ 'settings.persistent.defaultPrefix' | transloco }} {{ resolved().persistent_agent?.model }}</span>
              }
            </div>
            <div class="form-row two-col">
              <div>
                <label class="field-label">{{ 'settings.persistent.permissionMode' | transloco }}</label>
                <select
                  class="form-input"
                  [class.is-default]="!paPermissionMode()"
                  [ngModel]="paPermissionMode() ?? resolved().persistent_agent?.permission_mode ?? ''"
                  (ngModelChange)="paPermissionMode.set($event === resolved().persistent_agent?.permission_mode ? null : $event)"
                >
                  <option value="supervised">{{ 'settings.persistent.permissionSupervised' | transloco }}{{ !paPermissionMode() && resolved().persistent_agent?.permission_mode === 'supervised' ? ' (' + ('common.default' | transloco) + ')' : '' }}</option>
                  <option value="auto_accept">{{ 'settings.persistent.permissionAutoAccept' | transloco }}{{ !paPermissionMode() && resolved().persistent_agent?.permission_mode === 'auto_accept' ? ' (' + ('common.default' | transloco) + ')' : '' }}</option>
                  <option value="autonomous">{{ 'settings.persistent.permissionAutonomous' | transloco }}{{ !paPermissionMode() && resolved().persistent_agent?.permission_mode === 'autonomous' ? ' (' + ('common.default' | transloco) + ')' : '' }}</option>
                </select>
              </div>
              <div>
                <label class="field-label">{{ 'settings.persistent.config' | transloco }}</label>
                <select class="form-input" [(ngModel)]="paConfigName">
                  <option value="">{{ 'settings.persistent.configDefault' | transloco }}</option>
                  <option value="developer">{{ 'settings.persistent.configDeveloper' | transloco }}</option>
                  <option value="scholar">{{ 'settings.persistent.configScholar' | transloco }}</option>
                </select>
              </div>
            </div>
            <div class="form-row">
              <label class="field-label">{{ 'settings.persistent.greeting' | transloco }}</label>
              <input
                type="text"
                class="form-input"
                [placeholder]="'settings.persistent.greetingPlaceholder' | transloco"
                [(ngModel)]="paGreeting"
              />
            </div>
            <div class="form-row two-col">
              <div>
                <label class="field-label">{{ 'settings.persistent.idleTimeout' | transloco }}</label>
                <input
                  type="number"
                  class="form-input"
                  [class.is-default]="!paIdleTimeout()"
                  [placeholder]="resolved().persistent_agent?.idle_timeout_minutes ?? 30"
                  [ngModel]="paIdleTimeout()"
                  (ngModelChange)="paIdleTimeout.set($event || null)"
                />
              </div>
              <div>
                <label class="field-label">{{ 'settings.persistent.commandAllowlist' | transloco }}</label>
                <input
                  type="text"
                  class="form-input"
                  [placeholder]="'settings.persistent.commandAllowlistPlaceholder' | transloco"
                  [(ngModel)]="paCommandAllowlist"
                />
              </div>
            </div>
            <button
              class="create-btn"
              (click)="savePersistentAgent()"
              [disabled]="savingPA()"
            >
              {{ savingPA() ? ('common.saving' | transloco) : ('settings.persistent.save' | transloco) }}
            </button>
            @if (paSaved()) {
              <span class="save-feedback">{{ 'common.saved' | transloco }}</span>
            }
          </div>
        </section>

        <!-- Communication Preferences Section -->
        <section class="settings-section" style="margin-top: 24px;">
          <h2 class="section-title">{{ 'settings.communication.title' | transloco }}</h2>
          <p class="section-desc">{{ 'settings.communication.desc' | transloco }}</p>

          <div class="create-form" style="border-top: none; padding-top: 0;">
            <div class="form-row">
              <label class="field-label">{{ 'settings.communication.replyDelivery' | transloco }}</label>
              <select class="form-input" [(ngModel)]="commDelivery">
                <option value="next_strategic_phase">{{ 'settings.communication.deliveryNextStrategic' | transloco }}</option>
                <option value="immediate_interrupt">{{ 'settings.communication.deliveryImmediate' | transloco }}</option>
                <option value="llm_triage">{{ 'settings.communication.deliveryLlmTriage' | transloco }}</option>
              </select>
            </div>

            <div class="form-row">
              <label class="field-label">{{ 'settings.communication.channels' | transloco }}</label>
              <div style="display: flex; gap: 16px; flex-wrap: wrap; margin-top: 4px;">
                <label class="checkbox-label">
                  <input type="checkbox" [(ngModel)]="commChannelEmail" /> Email
                </label>
                <label class="checkbox-label">
                  <input type="checkbox" [(ngModel)]="commChannelNtfy" /> Ntfy
                </label>
                <label class="checkbox-label">
                  <input type="checkbox" [(ngModel)]="commChannelSlack" /> Slack
                </label>
                <label class="checkbox-label">
                  <input type="checkbox" [(ngModel)]="commChannelDiscord" /> Discord
                </label>
              </div>
            </div>

            <div class="form-row">
              <label class="field-label">
                <input type="checkbox" [(ngModel)]="commQuietEnabled" style="margin-right: 6px;" />
                {{ 'settings.communication.quietHours' | transloco }}
              </label>
            </div>
            @if (commQuietEnabled) {
              <div class="form-row two-col">
                <div>
                  <label class="field-label">{{ 'settings.communication.start' | transloco }}</label>
                  <input type="time" class="form-input" [(ngModel)]="commQuietStart" />
                </div>
                <div>
                  <label class="field-label">{{ 'settings.communication.end' | transloco }}</label>
                  <input type="time" class="form-input" [(ngModel)]="commQuietEnd" />
                </div>
              </div>
              <div class="form-row">
                <label class="field-label">{{ 'settings.communication.timezone' | transloco }}</label>
                <input
                  type="text"
                  class="form-input"
                  [placeholder]="'settings.communication.timezonePlaceholder' | transloco"
                  [(ngModel)]="commQuietTimezone"
                />
              </div>
            }

            <button
              class="create-btn"
              (click)="saveCommunication()"
              [disabled]="savingComm()"
            >
              {{ savingComm() ? ('common.saving' | transloco) : ('settings.communication.save' | transloco) }}
            </button>
            @if (commSaved()) {
              <span class="save-feedback">{{ 'common.saved' | transloco }}</span>
            }
          </div>
        </section>

        <!-- MCP Tokens Section -->
        <section class="settings-section" style="margin-top: 24px;">
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
                    <button class="revoke-btn" (click)="revokeToken(token.id)">{{ 'settings.mcp.revoke' | transloco }}</button>
                  </span>
                </div>
              }
            </div>
          } @else {
            <p class="empty-state">{{ 'settings.mcp.empty' | transloco }}</p>
          }

          <!-- Newly Created Token -->
          @if (newToken()) {
            <div class="new-token-banner">
              <p class="new-token-warning">{{ 'settings.mcp.copyWarning' | transloco }}</p>
              <div class="new-token-row">
                <input
                  type="text"
                  class="new-token-input"
                  [value]="newToken()!.token"
                  readonly
                  #tokenInput
                />
                <button class="copy-btn" (click)="copyToken(tokenInput)">
                  {{ copied() ? ('common.copied' | transloco) : ('common.copy' | transloco) }}
                </button>
              </div>
            </div>
          }

          <!-- Create Token Form -->
          <div class="create-form">
            <h3 class="form-title">{{ 'settings.mcp.createTitle' | transloco }}</h3>
            <div class="form-row">
              <input
                type="text"
                class="form-input"
                [placeholder]="'settings.mcp.namePlaceholder' | transloco"
                [(ngModel)]="newName"
                [disabled]="creating()"
              />
            </div>
            <div class="form-row two-col">
              <select class="form-input" [(ngModel)]="newScope" [disabled]="creating()">
                <option value="user">{{ 'settings.mcp.scopeUser' | transloco }}</option>
                @for (p of projects(); track p.id) {
                  <option [value]="'project:' + p.id">{{ 'settings.mcp.scopeProjectPrefix' | transloco }} {{ p.name }}</option>
                }
                @if (userService.currentUser()?.is_admin) {
                  <option value="all">{{ 'settings.mcp.scopeAll' | transloco }}</option>
                }
              </select>
              <select class="form-input" [(ngModel)]="newExpiry" [disabled]="creating()">
                <option [ngValue]="null">{{ 'settings.mcp.expiryNever' | transloco }}</option>
                <option [ngValue]="30">{{ 'settings.mcp.expiry30' | transloco }}</option>
                <option [ngValue]="90">{{ 'settings.mcp.expiry90' | transloco }}</option>
                <option [ngValue]="365">{{ 'settings.mcp.expiry365' | transloco }}</option>
              </select>
            </div>
            <button
              class="create-btn"
              (click)="createToken()"
              [disabled]="creating() || !newName.trim()"
            >
              {{ creating() ? ('settings.mcp.creating' | transloco) : ('settings.mcp.create' | transloco) }}
            </button>
          </div>

          <!-- Connection Instructions (shown after token creation) -->
          @if (newToken()) {
            <div class="instructions">
              <h3 class="form-title">{{ 'settings.mcp.claudeCodeTitle' | transloco }}</h3>
              <p class="section-desc" [innerHTML]="'settings.mcp.claudeCodeDesc' | transloco"></p>
              <div class="code-block-wrapper">
                <pre class="code-block">{{mcpJsonSnippet()}}</pre>
                <button class="code-copy-btn" (click)="copyText(mcpJsonSnippet())">
                  {{ snippetCopied() ? ('common.copied' | transloco) : ('common.copy' | transloco) }}
                </button>
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
                class="form-input mono"
                [value]="mcpServerUrl()"
                readonly
                #mcpUrlInput
              />
              <button class="copy-btn" (click)="copyText(mcpUrlInput.value, 'connector')">
                {{ connectorCopied() ? ('common.copied' | transloco) : ('common.copy' | transloco) }}
              </button>
            </div>
            <p class="section-hint">{{ 'settings.mcp.webConnectorHint' | transloco }}</p>
          </div>
        </section>

        <!-- Codex Proxy Section (Admin Only) -->
        @if (userService.currentUser()?.is_admin) {
          <section class="settings-section" style="margin-top: 24px;">
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
                <button class="refresh-btn" (click)="loadCodexStatus()" [title]="'settings.codex.refreshStatus' | transloco">&#x21bb;</button>
              }
            </div>

            <!-- Accounts -->
            @if (codexStatus().accounts.length > 0) {
              <div class="codex-accounts">
                @for (acct of codexStatus().accounts; track acct.name) {
                  <div class="codex-account-row">
                    <span class="mono">{{ acct.name }}</span>
                    <span class="codex-account-status">{{ acct.status }}</span>
                    <button
                      class="revoke-btn"
                      (click)="disconnectCodexAccount(acct.name)"
                    >
                      {{ 'settings.codex.disconnect' | transloco }}
                    </button>
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
              <button
                class="create-btn"
                (click)="connectCodexAccount()"
                [disabled]="codexConnecting()"
              >
                {{ (codexConnecting() ? 'settings.codex.waiting' : 'settings.codex.connectAccount') | transloco }}
              </button>
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
                    <input
                      type="text"
                      class="form-input"
                      [placeholder]="'settings.codex.callbackPlaceholder' | transloco"
                      [ngModel]="codexCallbackUrl()"
                      (ngModelChange)="codexCallbackUrl.set($event)"
                    />
                    <button
                      class="create-btn"
                      (click)="submitCodexCallback()"
                      [disabled]="codexCallbackSubmitting()"
                    >
                      {{ (codexCallbackSubmitting() ? 'settings.codex.submitting' : 'settings.codex.completeLogin') | transloco }}
                    </button>
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
          <section class="settings-section" style="margin-top: 24px;">
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
                <button class="refresh-btn" (click)="loadCloudSettings()" [title]="'settings.cloud.refresh' | transloco">&#x21bb;</button>
              </div>

              <!-- Backend selector -->
              <div class="form-row" style="margin-top: 16px;">
                <label class="form-label">{{ 'settings.cloud.backend' | transloco }}</label>
                <select
                  class="form-input"
                  [ngModel]="cloudForm().backend_id"
                  (ngModelChange)="updateCloudForm('backend_id', $event)"
                >
                  @for (backend of s.allowed_backends; track backend) {
                    <option [value]="backend">{{ backend }}</option>
                  }
                </select>
              </div>

              <!-- Common URL fields -->
              <div class="form-row">
                <label class="form-label">{{ 'settings.cloud.baseUrl' | transloco }}</label>
                <input
                  type="text"
                  class="form-input"
                  [ngModel]="cloudForm().base_url || ''"
                  (ngModelChange)="updateCloudForm('base_url', $event)"
                />
              </div>
              <div class="form-row">
                <label class="form-label">{{ 'settings.cloud.publicUrl' | transloco }}</label>
                <input
                  type="text"
                  class="form-input"
                  [ngModel]="cloudForm().public_url || ''"
                  (ngModelChange)="updateCloudForm('public_url', $event)"
                />
              </div>

              @if (cloudForm().backend_id === 'opencloud') {
                <div class="form-row">
                  <label class="form-label">{{ 'settings.cloud.keycloakIssuer' | transloco }}</label>
                  <input
                    type="text"
                    class="form-input"
                    [ngModel]="cloudForm().keycloak_issuer || ''"
                    (ngModelChange)="updateCloudForm('keycloak_issuer', $event)"
                  />
                </div>
                <div class="form-row">
                  <label class="form-label">{{ 'settings.cloud.keycloakClientId' | transloco }}</label>
                  <input
                    type="text"
                    class="form-input"
                    [ngModel]="cloudForm().keycloak_client_id || ''"
                    (ngModelChange)="updateCloudForm('keycloak_client_id', $event)"
                  />
                </div>
                <div class="form-row">
                  <label class="form-label">{{ 'settings.cloud.adminRole' | transloco }}</label>
                  <input
                    type="text"
                    class="form-input"
                    [ngModel]="cloudForm().admin_role_claim_value || ''"
                    (ngModelChange)="updateCloudForm('admin_role_claim_value', $event)"
                  />
                </div>
                <div class="form-row">
                  <label class="form-label">{{ 'settings.cloud.spaceQuota' | transloco }}</label>
                  <input
                    type="number"
                    class="form-input"
                    [ngModel]="cloudForm().default_quota_bytes"
                    (ngModelChange)="updateCloudForm('default_quota_bytes', $event)"
                  />
                </div>
              }

              @if (cloudForm().backend_id === 'nextcloud') {
                <div class="form-row">
                  <label class="form-label">{{ 'settings.cloud.adminUser' | transloco }}</label>
                  <input
                    type="text"
                    class="form-input"
                    [ngModel]="cloudForm().admin_user || ''"
                    (ngModelChange)="updateCloudForm('admin_user', $event)"
                  />
                </div>
                <div class="form-row">
                  <label class="form-label">{{ 'settings.cloud.agentUser' | transloco }}</label>
                  <input
                    type="text"
                    class="form-input"
                    [ngModel]="cloudForm().agent_user || ''"
                    (ngModelChange)="updateCloudForm('agent_user', $event)"
                  />
                </div>
              }

              <!-- Credentials ref -->
              <div class="form-row">
                <label class="form-label">{{ 'settings.cloud.credentialsRef' | transloco }}</label>
                <input
                  type="text"
                  class="form-input"
                  placeholder="env:OPENCLOUD_KEYCLOAK_CLIENT_SECRET"
                  [ngModel]="cloudCredentialsRef()"
                  (ngModelChange)="cloudCredentialsRef.set($event)"
                />
              </div>

              <!-- Secret provenance -->
              @if (secretProvenanceEntries().length > 0) {
                <div class="codex-accounts" style="margin-top: 16px;">
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
              <div class="create-form" style="margin-top: 20px; display: flex; gap: 12px;">
                <button
                  class="create-btn"
                  (click)="testCloudSettings()"
                  [disabled]="cloudBusy()"
                >
                  {{ (cloudTesting() ? 'settings.cloud.testing' : 'settings.cloud.test') | transloco }}
                </button>
                <button
                  class="create-btn"
                  (click)="saveCloudSettings()"
                  [disabled]="cloudBusy()"
                >
                  {{ (cloudSaving() ? 'settings.cloud.saving' : 'settings.cloud.saveReload') | transloco }}
                </button>
                @if (s.overlay.present) {
                  <button
                    class="revoke-btn"
                    (click)="resetCloudSettings()"
                    [disabled]="cloudBusy()"
                  >
                    {{ 'settings.cloud.resetEnv' | transloco }}
                  </button>
                }
              </div>

              @if (cloudMessage()) {
                <p
                  class="section-desc"
                  style="margin-top: 12px;"
                  [class.codex-callback-error]="cloudMessageIsError()"
                >
                  {{ cloudMessage() }}
                </p>
              }

              @if (s.overlay.present) {
                <p class="section-desc" style="margin-top: 8px;">
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
    .settings-page {
      padding: 32px;
      max-width: 800px;
      margin: 0 auto;
      color: var(--text-primary, #cdd6f4);
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
      color: var(--text-primary, #cdd6f4);
    }

    .settings-section {
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

    .section-desc code {
      background: var(--surface-0, #313244);
      padding: 2px 6px;
      border-radius: 4px;
      font-size: 12px;
    }

    /* Key + Token tables */
    .key-table, .token-table {
      margin-bottom: 20px;
      border: 1px solid var(--border-color, #313244);
      border-radius: 8px;
      overflow: hidden;
    }

    .key-header, .key-row {
      display: grid;
      grid-template-columns: 1.5fr 1.2fr 1.2fr 1fr 80px;
      padding: 10px 14px;
      gap: 8px;
      align-items: center;
      font-size: 13px;
    }

    .token-header, .token-row {
      display: grid;
      grid-template-columns: 2fr 1.2fr 1fr 0.8fr 1fr 1fr 80px;
      padding: 10px 14px;
      gap: 8px;
      align-items: center;
      font-size: 13px;
    }

    .key-header, .token-header {
      background: var(--surface-0, #313244);
      font-weight: 600;
      font-size: 12px;
      color: var(--text-muted, #6c7086);
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }

    .key-row, .token-row {
      border-top: 1px solid var(--border-color, #313244);
    }

    .mono {
      font-family: 'JetBrains Mono', 'Fira Code', monospace;
      font-size: 12px;
      color: var(--text-muted, #6c7086);
    }

    .revoke-btn {
      padding: 4px 10px;
      background: transparent;
      border: 1px solid var(--red, #f38ba8);
      border-radius: 6px;
      color: var(--red, #f38ba8);
      font-size: 12px;
      font-family: inherit;
      cursor: pointer;
      transition: all 0.15s ease;
    }

    .revoke-btn:hover {
      background: var(--red, #f38ba8);
      color: var(--timeline-bg, #11111b);
    }

    .empty-state {
      font-size: 13px;
      color: var(--text-muted, #6c7086);
      text-align: center;
      padding: 24px;
      margin-bottom: 20px;
    }

    /* New token banner */
    .new-token-banner {
      background: rgba(166, 227, 161, 0.08);
      border: 1px solid var(--green, #a6e3a1);
      border-radius: 8px;
      padding: 14px;
      margin-bottom: 20px;
    }

    .new-token-warning {
      font-size: 13px;
      font-weight: 600;
      color: var(--green, #a6e3a1);
      margin-bottom: 10px;
    }

    .new-token-row {
      display: flex;
      gap: 8px;
    }

    .new-token-input {
      flex: 1;
      padding: 8px 12px;
      background: var(--surface-0, #313244);
      border: 1px solid var(--border-color, #313244);
      border-radius: 6px;
      color: var(--text-primary, #cdd6f4);
      font-family: 'JetBrains Mono', 'Fira Code', monospace;
      font-size: 12px;
      outline: none;
    }

    .copy-btn {
      padding: 8px 16px;
      background: var(--accent-color, #cba6f7);
      color: var(--timeline-bg, #11111b);
      border: none;
      border-radius: 6px;
      font-size: 13px;
      font-weight: 600;
      font-family: inherit;
      cursor: pointer;
      white-space: nowrap;
      min-width: 72px;
    }

    .copy-btn:hover { opacity: 0.9; }

    /* Create form */
    .create-form {
      border-top: 1px solid var(--border-color, #313244);
      padding-top: 20px;
      margin-bottom: 20px;
    }

    .form-title {
      font-size: 14px;
      font-weight: 600;
      margin-bottom: 12px;
      color: var(--text-primary, #cdd6f4);
    }

    .field-label {
      display: block;
      font-size: 12px;
      font-weight: 600;
      color: var(--text-muted, #6c7086);
      margin-bottom: 6px;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }

    .form-row {
      margin-bottom: 10px;
    }

    .two-col {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
    }

    .subsection-title {
      font-size: 12px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      color: var(--text-muted, #6c7086);
      margin: 16px 0 8px;
      padding-top: 12px;
      border-top: 1px solid var(--border-color, #313244);
    }

    .field-hint {
      display: block;
      font-size: 11px;
      color: var(--text-muted, #6c7086);
      margin-top: 4px;
    }

    .form-input {
      width: 100%;
      padding: 10px 14px;
      background: var(--surface-0, #313244);
      border: 1px solid var(--border-color, #313244);
      border-radius: 8px;
      color: var(--text-primary, #cdd6f4);
      font-size: 14px;
      font-family: inherit;
      outline: none;
      transition: border-color 0.15s ease;
    }

    .form-input:focus {
      border-color: var(--accent-color, #cba6f7);
    }

    .form-input::placeholder {
      color: var(--text-muted, #6c7086);
    }

    .form-input:disabled {
      opacity: 0.6;
    }

    select.form-input {
      cursor: pointer;
      appearance: none;
      background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%236c7086' d='M6 8L1 3h10z'/%3E%3C/svg%3E");
      background-repeat: no-repeat;
      background-position: right 12px center;
      padding-right: 32px;
    }

    select.form-input option {
      background: var(--panel-bg, #181825);
      color: var(--text-primary, #cdd6f4);
    }

    .create-btn {
      padding: 10px 20px;
      background: var(--accent-color, #cba6f7);
      color: var(--timeline-bg, #11111b);
      border: none;
      border-radius: 8px;
      font-size: 14px;
      font-weight: 600;
      font-family: inherit;
      cursor: pointer;
      transition: opacity 0.15s ease;
    }

    .create-btn:hover:not(:disabled) { opacity: 0.9; }
    .create-btn:disabled { opacity: 0.5; cursor: not-allowed; }

    .save-feedback {
      margin-left: 12px;
      font-size: 13px;
      color: var(--green, #a6e3a1);
      font-weight: 600;
    }

    .checkbox-label {
      display: flex;
      align-items: center;
      gap: 4px;
      color: var(--text-secondary, #a6adc8);
      font-size: 13px;
      cursor: pointer;
    }

    .checkbox-label input[type="checkbox"] {
      accent-color: var(--accent-color, #cba6f7);
    }

    /* Instructions */
    .instructions {
      border-top: 1px solid var(--border-color, #313244);
      padding-top: 20px;
    }

    .code-block-wrapper {
      position: relative;
    }

    .code-block {
      background: var(--surface-0, #313244);
      border: 1px solid var(--border-color, #313244);
      border-radius: 8px;
      padding: 14px;
      padding-right: 80px;
      font-family: 'JetBrains Mono', 'Fira Code', monospace;
      font-size: 12px;
      line-height: 1.5;
      color: var(--text-primary, #cdd6f4);
      overflow-x: auto;
      white-space: pre;
    }

    .code-copy-btn {
      position: absolute;
      top: 8px;
      right: 8px;
      padding: 4px 12px;
      background: var(--accent-color, #cba6f7);
      color: var(--timeline-bg, #11111b);
      border: none;
      border-radius: 4px;
      font-size: 12px;
      font-weight: 600;
      font-family: inherit;
      cursor: pointer;
      min-width: 60px;
    }

    .code-copy-btn:hover { opacity: 0.9; }

    .connector-url-row {
      display: flex;
      gap: 8px;
      margin-bottom: 8px;
    }

    .connector-url-row .form-input {
      flex: 1;
    }

    .section-hint {
      font-size: 12px;
      color: var(--text-secondary, #a6adc8);
      line-height: 1.5;
    }

    /* Codex Proxy */
    .codex-status-card {
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 12px 16px;
      background: var(--surface-0, #313244);
      border: 1px solid var(--border-color, #313244);
      border-radius: 8px;
      margin-bottom: 16px;
    }

    .codex-status-dot {
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: var(--red, #f38ba8);
      flex-shrink: 0;
    }

    .codex-status-dot.connected {
      background: var(--green, #a6e3a1);
    }

    .codex-status-text {
      font-size: 13px;
      color: var(--text-secondary, #a6adc8);
      flex: 1;
    }

    .refresh-btn {
      background: transparent;
      border: 1px solid var(--border-color, #313244);
      border-radius: 6px;
      color: var(--text-muted, #6c7086);
      font-size: 16px;
      padding: 4px 8px;
      cursor: pointer;
      transition: all 0.15s ease;
    }

    .refresh-btn:hover {
      border-color: var(--accent-color, #cba6f7);
      color: var(--accent-color, #cba6f7);
    }

    .codex-accounts {
      margin-bottom: 16px;
    }

    .codex-account-row {
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 8px 14px;
      border: 1px solid var(--border-color, #313244);
      border-radius: 8px;
      margin-bottom: 6px;
    }

    .codex-account-row .mono { flex: 1; }

    .codex-account-status {
      font-size: 12px;
      color: var(--text-muted, #6c7086);
    }

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
      background: var(--surface-0, #313244);
      border: 1px solid var(--border-color, #313244);
      border-radius: 6px;
      font-family: 'JetBrains Mono', 'Fira Code', monospace;
      font-size: 12px;
      color: var(--text-secondary, #a6adc8);
    }

    /* Codex callback paste flow */
    .codex-callback-help {
      margin-top: 16px;
      padding: 16px;
      background: rgba(203, 166, 247, 0.06);
      border: 1px solid var(--accent-color, #cba6f7);
      border-radius: 8px;
    }

    .codex-callback-title {
      font-size: 14px;
      font-weight: 600;
      color: var(--accent-color, #cba6f7);
      margin-bottom: 10px;
    }

    .codex-callback-steps {
      font-size: 13px;
      color: var(--text-secondary, #a6adc8);
      line-height: 1.6;
      margin: 0 0 14px 0;
      padding-left: 20px;
    }

    .codex-callback-steps code {
      background: var(--surface-0, #313244);
      padding: 2px 6px;
      border-radius: 4px;
      font-size: 12px;
    }

    .codex-callback-input-row {
      display: flex;
      gap: 8px;
      margin-bottom: 8px;
    }

    .codex-callback-input-row .form-input {
      flex: 1;
    }

    .codex-callback-input-row .create-btn {
      white-space: nowrap;
      flex-shrink: 0;
    }

    .codex-callback-error {
      font-size: 13px;
      color: var(--red, #f38ba8);
      margin: 6px 0 0 0;
    }

    .codex-callback-hint {
      font-size: 12px;
      color: var(--text-muted, #6c7086);
      margin: 10px 0 0 0;
    }

    .codex-callback-hint code {
      background: var(--surface-0, #313244);
      padding: 2px 6px;
      border-radius: 4px;
      font-size: 11px;
    }

    /* Default value indication — dimmed text when showing a resolved default */
    select.is-default,
    input.is-default {
      color: var(--text-muted, #6c7086);
    }
    select.is-default option {
      color: var(--text-primary, #cdd6f4);
    }
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
  newName = '';
  newScope = 'user';
  newExpiry: number | null = null;
  readonly creating = signal(false);
  readonly newToken = signal<McpTokenCreateResponse | null>(null);
  readonly copied = signal(false);
  readonly snippetCopied = signal(false);
  readonly connectorCopied = signal(false);
  readonly projects = signal<Project[]>([]);

  // API key form state
  keyProvider: ApiKeyProvider = 'openai';
  keyValue = '';
  keyLabel = '';
  readonly settingKey = signal(false);

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
  paConfigName = '';
  paGreeting = '';
  readonly paIdleTimeout = signal<number | null>(null);
  paCommandAllowlist = '';
  readonly savingPA = signal(false);
  readonly paSaved = signal(false);

  // Communication form state
  commDelivery = 'next_strategic_phase';
  commChannelEmail = true;
  commChannelNtfy = false;
  commChannelSlack = false;
  commChannelDiscord = false;
  commQuietEnabled = false;
  commQuietStart = '22:00';
  commQuietEnd = '08:00';
  commQuietTimezone = '';
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
          this.paConfigName = pa.config_name || '';
          this.paGreeting = pa.greeting || '';
          this.paIdleTimeout.set(pa.idle_timeout_minutes ?? null);
          this.paCommandAllowlist = (pa.command_allowlist || []).join(', ');
        }

        // Sync communication preferences
        const comm = prefs.communication;
        if (comm) {
          this.commDelivery = comm.delivery?.async_reply || 'next_strategic_phase';
          this.commChannelEmail = comm.channels?.email ?? true;
          this.commChannelNtfy = comm.channels?.ntfy ?? false;
          this.commChannelSlack = comm.channels?.slack_webhook ?? false;
          this.commChannelDiscord = comm.channels?.discord_webhook ?? false;
          this.commQuietEnabled = comm.quiet_hours?.enabled ?? false;
          this.commQuietStart = comm.quiet_hours?.start || '22:00';
          this.commQuietEnd = comm.quiet_hours?.end || '08:00';
          this.commQuietTimezone = comm.quiet_hours?.timezone || '';
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

  // ── API Keys ──────────────────────────────────────────────────────

  saveApiKey(): void {
    if (!this.keyValue.trim()) return;
    this.settingKey.set(true);

    this.settingsService
      .setApiKey(this.keyProvider, {
        api_key: this.keyValue.trim(),
        label: this.keyLabel.trim() || undefined,
      })
      .subscribe({
        next: () => {
          this.keyValue = '';
          this.keyLabel = '';
          this.settingKey.set(false);
        },
        error: () => this.settingKey.set(false),
      });
  }

  deleteApiKey(provider: string): void {
    this.settingsService.deleteApiKey(provider).subscribe();
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

  savePersistentAgent(): void {
    this.savingPA.set(true);
    this.paSaved.set(false);

    const allowlist = this.paCommandAllowlist.trim()
        ? this.paCommandAllowlist.split(',').map(s => s.trim()).filter(Boolean)
        : null;

    const settings: Record<string, unknown> = {
      persistent_agent: {
        model: this.paModel()?.trim() || null,
        permission_mode: this.paPermissionMode() || null,
        config_name: this.paConfigName || null,
        greeting: this.paGreeting.trim() || null,
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
        async_reply: this.commDelivery as 'immediate_interrupt' | 'next_strategic_phase' | 'llm_triage',
        urgent_override: true,
      },
      channels: {
        email: this.commChannelEmail,
        cockpit: true,
        ntfy: this.commChannelNtfy,
        slack_webhook: this.commChannelSlack,
        discord_webhook: this.commChannelDiscord,
      },
      quiet_hours: {
        enabled: this.commQuietEnabled,
        start: this.commQuietStart,
        end: this.commQuietEnd,
        timezone: this.commQuietTimezone || undefined,
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

  createToken(): void {
    if (!this.newName.trim()) return;
    this.creating.set(true);
    this.newToken.set(null);
    this.copied.set(false);

    this.tokenService
      .createToken({
        name: this.newName.trim(),
        scope: this.newScope,
        expires_in_days: this.newExpiry,
      })
      .subscribe({
        next: (res) => {
          this.newToken.set(res);
          this.newName = '';
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
