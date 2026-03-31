import {Component, effect, inject, OnInit, signal} from '@angular/core';
import {FormsModule} from '@angular/forms';
import {Router} from '@angular/router';
import {McpTokenService} from '../../core/services/mcp-token.service';
import {UserService} from '../../core/services/user.service';
import {ApiService} from '../../core/services/api.service';
import {SettingsService} from '../../core/services/settings.service';
import {
  ApiKeyProvider,
  CodexStatus,
  CommunicationSettings,
  McpTokenCreateResponse,
  Project
} from '../../core/models/api.model';

const PROVIDERS: { value: ApiKeyProvider; label: string }[] = [
  { value: 'openai', label: 'OpenAI' },
  { value: 'anthropic', label: 'Anthropic' },
  { value: 'google', label: 'Google' },
  { value: 'groq', label: 'Groq' },
  { value: 'openrouter', label: 'OpenRouter' },
  { value: 'tavily', label: 'Tavily (Web Search)' },
  { value: 'vision', label: 'Vision' },
];

@Component({
  selector: 'app-settings',
  standalone: true,
  imports: [FormsModule],
  template: `
    <div class="settings-page">
      <div class="settings-container">
        <h1 class="page-title">Settings</h1>

        <!-- API Keys Section -->
        <section class="settings-section">
          <h2 class="section-title">API Keys</h2>
          <p class="section-desc">
            Set your own API keys for LLM providers and tools. These take priority over
            system-wide keys when running jobs.
          </p>

          <!-- Key List -->
          @if (settingsService.apiKeys().length > 0) {
            <div class="key-table">
              <div class="key-header">
                <span class="col-provider">Provider</span>
                <span class="col-prefix">Key</span>
                <span class="col-label">Label</span>
                <span class="col-updated">Updated</span>
                <span class="col-action"></span>
              </div>
              @for (key of settingsService.apiKeys(); track key.id) {
                <div class="key-row">
                  <span class="col-provider">{{ providerLabel(key.provider) }}</span>
                  <span class="col-prefix mono">{{ key.key_prefix }}...</span>
                  <span class="col-label">{{ key.label || '-' }}</span>
                  <span class="col-updated">{{ formatDate(key.updated_at) }}</span>
                  <span class="col-action">
                    <button class="revoke-btn" (click)="deleteApiKey(key.provider)">Delete</button>
                  </span>
                </div>
              }
            </div>
          } @else {
            <p class="empty-state">No API keys configured. Add one to use your own provider keys.</p>
          }

          <!-- Set Key Form -->
          <div class="create-form">
            <h3 class="form-title">Add / Update Key</h3>
            <div class="form-row two-col">
              <select class="form-input" [(ngModel)]="keyProvider" [disabled]="settingKey()">
                @for (p of providers; track p.value) {
                  <option [value]="p.value">{{ p.label }}</option>
                }
              </select>
              <input
                type="text"
                class="form-input"
                placeholder="Label (optional)"
                [(ngModel)]="keyLabel"
                [disabled]="settingKey()"
              />
            </div>
            <div class="form-row">
              <input
                type="password"
                class="form-input"
                placeholder="API key (e.g. sk-...)"
                [(ngModel)]="keyValue"
                [disabled]="settingKey()"
              />
            </div>
            <button
              class="create-btn"
              (click)="saveApiKey()"
              [disabled]="settingKey() || !keyValue.trim()"
            >
              {{ settingKey() ? 'Saving...' : 'Save Key' }}
            </button>
          </div>
        </section>

        <!-- Preferences Section -->
        <section class="settings-section" style="margin-top: 24px;">
          <h2 class="section-title">Preferences</h2>
          <p class="section-desc">
            Default settings applied when creating new jobs. Can be overridden per-job.
          </p>

          <div class="create-form" style="border-top: none; padding-top: 0;">
            <div class="form-row">
              <label class="field-label">Default Model</label>
              <input
                type="text"
                class="form-input"
                placeholder="e.g. gpt-4o, claude-sonnet-4-6"
                [(ngModel)]="prefModel"
              />
            </div>
            <div class="form-row">
              <label class="field-label">Auxiliary Model</label>
              <input
                type="text"
                class="form-input"
                placeholder="e.g. groq/llama-3.3-70b-versatile (blank = use default)"
                [(ngModel)]="prefAuxModel"
              />
            </div>
            <div class="form-row two-col">
              <div>
                <label class="field-label">Default Autonomy</label>
                <select class="form-input" [(ngModel)]="prefAutonomy">
                  <option value="">Not set</option>
                  <option value="full">Full</option>
                  <option value="review">Review</option>
                  <option value="partial">Partial</option>
                  <option value="guided">Guided</option>
                  <option value="dependent">Dependent</option>
                </select>
              </div>
              <div>
                <label class="field-label">Default Reasoning Level</label>
                <select class="form-input" [(ngModel)]="prefReasoning">
                  <option value="">Not set</option>
                  <option value="low">Low</option>
                  <option value="medium">Medium</option>
                  <option value="high">High</option>
                </select>
              </div>
            </div>
            <div class="form-row">
              <label class="field-label">Embedding Provider</label>
              <select class="form-input" [(ngModel)]="prefEmbeddingProvider">
                <option value="">Server default</option>
                <option value="local">Local</option>
                <option value="openrouter">OpenRouter</option>
              </select>
            </div>
            <button
              class="create-btn"
              (click)="savePreferences()"
              [disabled]="savingPrefs()"
            >
              {{ savingPrefs() ? 'Saving...' : 'Save Preferences' }}
            </button>
            @if (prefsSaved()) {
              <span class="save-feedback">Saved</span>
            }
          </div>
        </section>

        <!-- Persistent Agent Section -->
        <section class="settings-section" style="margin-top: 24px;">
          <h2 class="section-title">Persistent Agent</h2>
          <p class="section-desc">
            Default settings for interactive persistent agent sessions. Can be overridden per-session.
          </p>

          <div class="create-form" style="border-top: none; padding-top: 0;">
            <div class="form-row">
              <label class="field-label">Model</label>
              <input
                type="text"
                class="form-input"
                placeholder="e.g. claude-sonnet-4-6"
                [(ngModel)]="paModel"
              />
            </div>
            <div class="form-row two-col">
              <div>
                <label class="field-label">Permission Mode</label>
                <select class="form-input" [(ngModel)]="paPermissionMode">
                  <option value="">Not set</option>
                  <option value="supervised">Supervised</option>
                  <option value="auto_accept">Auto-accept</option>
                  <option value="autonomous">Autonomous</option>
                </select>
              </div>
              <div>
                <label class="field-label">Config</label>
                <select class="form-input" [(ngModel)]="paConfigName">
                  <option value="">Default (interactive)</option>
                  <option value="interactive">Interactive</option>
                  <option value="developer">Developer</option>
                  <option value="scholar">Scholar</option>
                  <option value="defaults">Framework Defaults</option>
                </select>
              </div>
            </div>
            <div class="form-row">
              <label class="field-label">Greeting</label>
              <input
                type="text"
                class="form-input"
                placeholder="Hello! I'm ready to help."
                [(ngModel)]="paGreeting"
              />
            </div>
            <div class="form-row two-col">
              <div>
                <label class="field-label">Idle Timeout (minutes)</label>
                <input
                  type="number"
                  class="form-input"
                  placeholder="120"
                  [(ngModel)]="paIdleTimeout"
                />
              </div>
              <div>
                <label class="field-label">Command Allowlist</label>
                <input
                  type="text"
                  class="form-input"
                  placeholder="pytest*, npm test, git status"
                  [(ngModel)]="paCommandAllowlist"
                />
              </div>
            </div>
            <button
              class="create-btn"
              (click)="savePersistentAgent()"
              [disabled]="savingPA()"
            >
              {{ savingPA() ? 'Saving...' : 'Save Persistent Agent Settings' }}
            </button>
            @if (paSaved()) {
              <span class="save-feedback">Saved</span>
            }
          </div>
        </section>

        <!-- Communication Preferences Section -->
        <section class="settings-section" style="margin-top: 24px;">
          <h2 class="section-title">Communication</h2>
          <p class="section-desc">
            Configure how agent messages are delivered and when to suppress notifications.
          </p>

          <div class="create-form" style="border-top: none; padding-top: 0;">
            <div class="form-row">
              <label class="field-label">Reply Delivery</label>
              <select class="form-input" [(ngModel)]="commDelivery">
                <option value="next_strategic_phase">Next strategic phase (default)</option>
                <option value="immediate_interrupt">Immediate interrupt</option>
                <option value="llm_triage">LLM triage (auto-decide)</option>
              </select>
            </div>

            <div class="form-row">
              <label class="field-label">Notification Channels</label>
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
                Quiet Hours
              </label>
            </div>
            @if (commQuietEnabled) {
              <div class="form-row two-col">
                <div>
                  <label class="field-label">Start</label>
                  <input type="time" class="form-input" [(ngModel)]="commQuietStart" />
                </div>
                <div>
                  <label class="field-label">End</label>
                  <input type="time" class="form-input" [(ngModel)]="commQuietEnd" />
                </div>
              </div>
              <div class="form-row">
                <label class="field-label">Timezone</label>
                <input
                  type="text"
                  class="form-input"
                  placeholder="e.g. Europe/Berlin"
                  [(ngModel)]="commQuietTimezone"
                />
              </div>
            }

            <button
              class="create-btn"
              (click)="saveCommunication()"
              [disabled]="savingComm()"
            >
              {{ savingComm() ? 'Saving...' : 'Save Communication Settings' }}
            </button>
            @if (commSaved()) {
              <span class="save-feedback">Saved</span>
            }
          </div>
        </section>

        <!-- MCP Tokens Section -->
        <section class="settings-section" style="margin-top: 24px;">
          <h2 class="section-title">MCP Tokens</h2>
          <p class="section-desc">
            Generate API tokens for Claude Code or other MCP clients to access the orchestrator.
          </p>

          <!-- Token List -->
          @if (tokenService.tokens().length > 0) {
            <div class="token-table">
              <div class="token-header">
                <span class="col-name">Name</span>
                <span class="col-prefix">Token</span>
                <span class="col-scope">Scope</span>
                <span class="col-origin">Origin</span>
                <span class="col-used">Last Used</span>
                <span class="col-expires">Expires</span>
                <span class="col-action"></span>
              </div>
              @for (token of activeTokens(); track token.id) {
                <div class="token-row">
                  <span class="col-name">{{ token.name }}</span>
                  <span class="col-prefix mono">{{ token.token_prefix }}...</span>
                  <span class="col-scope">{{ formatScope(token.scope) }}</span>
                  <span class="col-origin">{{ formatOrigin(token.origin) }}</span>
                  <span class="col-used">{{ token.last_used_at ? formatDate(token.last_used_at) : 'Never' }}</span>
                  <span class="col-expires">{{ token.expires_at ? formatDate(token.expires_at) : 'Never' }}</span>
                  <span class="col-action">
                    <button class="revoke-btn" (click)="revokeToken(token.id)">Revoke</button>
                  </span>
                </div>
              }
            </div>
          } @else {
            <p class="empty-state">No tokens yet. Create one to connect your MCP client.</p>
          }

          <!-- Newly Created Token -->
          @if (newToken()) {
            <div class="new-token-banner">
              <p class="new-token-warning">Copy this token now — it won't be shown again.</p>
              <div class="new-token-row">
                <input
                  type="text"
                  class="new-token-input"
                  [value]="newToken()!.token"
                  readonly
                  #tokenInput
                />
                <button class="copy-btn" (click)="copyToken(tokenInput)">
                  {{ copied() ? 'Copied' : 'Copy' }}
                </button>
              </div>
            </div>
          }

          <!-- Create Token Form -->
          <div class="create-form">
            <h3 class="form-title">Create New Token</h3>
            <div class="form-row">
              <input
                type="text"
                class="form-input"
                placeholder="Token name (e.g. Claude Code - laptop)"
                [(ngModel)]="newName"
                [disabled]="creating()"
              />
            </div>
            <div class="form-row two-col">
              <select class="form-input" [(ngModel)]="newScope" [disabled]="creating()">
                <option value="user">My Data Only</option>
                @for (p of projects(); track p.id) {
                  <option [value]="'project:' + p.id">Project: {{ p.name }}</option>
                }
                @if (userService.currentUser()?.is_admin) {
                  <option value="all">Full Access</option>
                }
              </select>
              <select class="form-input" [(ngModel)]="newExpiry" [disabled]="creating()">
                <option [ngValue]="null">Never expires</option>
                <option [ngValue]="30">30 days</option>
                <option [ngValue]="90">90 days</option>
                <option [ngValue]="365">1 year</option>
              </select>
            </div>
            <button
              class="create-btn"
              (click)="createToken()"
              [disabled]="creating() || !newName.trim()"
            >
              {{ creating() ? 'Creating...' : 'Create Token' }}
            </button>
          </div>

          <!-- Connection Instructions (shown after token creation) -->
          @if (newToken()) {
            <div class="instructions">
              <h3 class="form-title">Connection</h3>
              <p class="section-desc">
                Add this to your <code>.mcp.json</code> file:
              </p>
              <pre class="code-block">{{mcpJsonSnippet()}}</pre>
            </div>
          }
        </section>

        <!-- Codex Proxy Section (Admin Only) -->
        @if (userService.currentUser()?.is_admin) {
          <section class="settings-section" style="margin-top: 24px;">
            <h2 class="section-title">Codex Proxy</h2>
            <p class="section-desc">
              Manage the Codex OAuth proxy for ChatGPT subscription-backed models
              (<code>codex/*</code>).
            </p>

            <!-- Status -->
            <div class="codex-status-card">
              @if (codexLoading()) {
                <span class="codex-status-text">Checking proxy...</span>
              } @else {
                <span class="codex-status-dot" [class.connected]="codexStatus().connected"></span>
                <span class="codex-status-text">
                  {{ codexStatus().connected ? 'Connected' : 'Not connected' }}
                  @if (codexStatus().model_count > 0) {
                    &mdash; {{ codexStatus().model_count }} model(s) available
                  }
                </span>
                <button class="refresh-btn" (click)="loadCodexStatus()" title="Refresh status">&#x21bb;</button>
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
                      Disconnect
                    </button>
                  </div>
                }
              </div>
            }

            <!-- Models -->
            @if (codexModels().length > 0) {
              <div class="codex-models">
                <h3 class="form-title">Available Models</h3>
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
                {{ codexConnecting() ? 'Waiting for OAuth...' : 'Connect ChatGPT Account' }}
              </button>
              @if (codexConnecting()) {
                <span class="save-feedback">Complete sign-in in the opened tab</span>
              }
            </div>
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

    .page-title {
      font-size: 24px;
      font-weight: 700;
      margin-bottom: 32px;
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

    .code-block {
      background: var(--surface-0, #313244);
      border: 1px solid var(--border-color, #313244);
      border-radius: 8px;
      padding: 14px;
      font-family: 'JetBrains Mono', 'Fira Code', monospace;
      font-size: 12px;
      line-height: 1.5;
      color: var(--text-primary, #cdd6f4);
      overflow-x: auto;
      white-space: pre;
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
  `],
})
export class SettingsComponent implements OnInit {
  readonly tokenService = inject(McpTokenService);
  readonly userService = inject(UserService);
  readonly settingsService = inject(SettingsService);
  private readonly apiService = inject(ApiService);
  private readonly router = inject(Router);

  // Provider list for dropdown
  readonly providers = PROVIDERS;

  // MCP token form state
  newName = '';
  newScope = 'user';
  newExpiry: number | null = null;
  readonly creating = signal(false);
  readonly newToken = signal<McpTokenCreateResponse | null>(null);
  readonly copied = signal(false);
  readonly projects = signal<Project[]>([]);

  // API key form state
  keyProvider: ApiKeyProvider = 'openai';
  keyValue = '';
  keyLabel = '';
  readonly settingKey = signal(false);

  // Preferences form state
  prefModel = '';
  prefAuxModel = '';
  prefAutonomy = '';
  prefReasoning = '';
  prefEmbeddingProvider = '';
  readonly savingPrefs = signal(false);
  readonly prefsSaved = signal(false);

  // Persistent Agent form state
  paModel = '';
  paPermissionMode = '';
  paConfigName = '';
  paGreeting = '';
  paIdleTimeout: number | null = null;
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
  private codexPollTimer: ReturnType<typeof setInterval> | null = null;

  constructor() {
    // Reactively sync preference form fields when the preferences signal updates.
    effect(() => {
      const prefs = this.settingsService.preferences();
      if (Object.keys(prefs).length > 0) {
        this.prefModel = prefs.default_model || '';
        this.prefAuxModel = prefs.default_auxiliary_model || '';
        this.prefAutonomy = prefs.default_autonomy || '';
        this.prefReasoning = prefs.default_reasoning_level || '';
        this.prefEmbeddingProvider = prefs.embedding_provider || '';

        // Sync persistent agent preferences
        const pa = prefs.persistent_agent;
        if (pa) {
          this.paModel = pa.model || '';
          this.paPermissionMode = pa.permission_mode || '';
          this.paConfigName = pa.config_name || '';
          this.paGreeting = pa.greeting || '';
          this.paIdleTimeout = pa.idle_timeout_minutes || null;
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
  }

  /** Only show active (non-revoked) tokens. */
  activeTokens = () =>
    this.tokenService.tokens().filter((t) => !t.revoked_at);

  ngOnInit(): void {
    this.tokenService.loadTokens();
    this.settingsService.loadApiKeys();
    this.settingsService.loadPreferences();

    // Load Codex proxy status for admins
    if (this.userService.currentUser()?.is_admin) {
      this.loadCodexStatus();
    }
  }

  providerLabel(provider: string): string {
    return PROVIDERS.find((p) => p.value === provider)?.label || provider;
  }

  formatScope(scope: string): string {
    if (scope === 'user') return 'My Data';
    if (scope === 'all') return 'Full Access';
    if (scope.startsWith('project:')) {
      const pid = scope.split(':', 2)[1];
      const p = this.projects().find((pr) => pr.id === pid);
      return p ? `Project: ${p.name}` : `Project`;
    }
    return scope;
  }

  formatOrigin(origin: string | null): string {
    if (!origin) return 'Manual';
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
    if (diffMs < 60_000) return 'Just now';
    if (diffMs < 3600_000) return `${Math.floor(diffMs / 60_000)}m ago`;
    if (diffMs < 86400_000) return `${Math.floor(diffMs / 3600_000)}h ago`;
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
  }

  mcpJsonSnippet = () => {
    const token = this.newToken()?.token ?? 'srw_YOUR_TOKEN_HERE';
    return JSON.stringify(
      {
        mcpServers: {
          orchestrator: {
            type: 'http',
            url: `${window.location.origin.replace(/:\d+$/, ':8055')}/mcp`,
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
    settings['default_model'] = this.prefModel.trim() || null;
    settings['default_auxiliary_model'] = this.prefAuxModel.trim() || null;
    settings['default_autonomy'] = this.prefAutonomy || null;
    settings['default_reasoning_level'] = this.prefReasoning || null;
    settings['embedding_provider'] = this.prefEmbeddingProvider || null;

    this.settingsService.updatePreferences(settings).subscribe({
      next: () => {
        this.savingPrefs.set(false);
        this.prefsSaved.set(true);
        setTimeout(() => this.prefsSaved.set(false), 2000);
      },
      error: () => this.savingPrefs.set(false),
    });
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
        model: this.paModel.trim() || null,
        permission_mode: this.paPermissionMode || null,
        config_name: this.paConfigName || null,
        greeting: this.paGreeting.trim() || null,
        idle_timeout_minutes: this.paIdleTimeout || null,
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
}
