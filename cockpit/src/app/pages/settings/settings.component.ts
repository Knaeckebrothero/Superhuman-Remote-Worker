import { Component, inject, signal, OnInit } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { McpTokenService } from '../../core/services/mcp-token.service';
import { UserService } from '../../core/services/user.service';
import { ApiService } from '../../core/services/api.service';
import { McpTokenCreateResponse, Project } from '../../core/models/api.model';

@Component({
  selector: 'app-settings',
  standalone: true,
  imports: [FormsModule],
  template: `
    <div class="settings-page">
      <div class="settings-container">
        <h1 class="page-title">Settings</h1>

        <!-- MCP Tokens Section -->
        <section class="settings-section">
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
                <span class="col-used">Last Used</span>
                <span class="col-expires">Expires</span>
                <span class="col-action"></span>
              </div>
              @for (token of activeTokens(); track token.id) {
                <div class="token-row">
                  <span class="col-name">{{ token.name }}</span>
                  <span class="col-prefix mono">{{ token.token_prefix }}...</span>
                  <span class="col-scope">{{ formatScope(token.scope) }}</span>
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
                <option value="all">Full Access</option>
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

          <!-- Connection Instructions -->
          <div class="instructions">
            <h3 class="form-title">Connection</h3>
            <p class="section-desc">
              Add this to your <code>.mcp.json</code> file:
            </p>
            <pre class="code-block">{{mcpJsonSnippet()}}</pre>
          </div>
        </section>
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

    /* Token table */
    .token-table {
      margin-bottom: 20px;
      border: 1px solid var(--border-color, #313244);
      border-radius: 8px;
      overflow: hidden;
    }

    .token-header, .token-row {
      display: grid;
      grid-template-columns: 2fr 1.2fr 1.2fr 1fr 1fr 80px;
      padding: 10px 14px;
      gap: 8px;
      align-items: center;
      font-size: 13px;
    }

    .token-header {
      background: var(--surface-0, #313244);
      font-weight: 600;
      font-size: 12px;
      color: var(--text-muted, #6c7086);
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }

    .token-row {
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
  `],
})
export class SettingsComponent implements OnInit {
  readonly tokenService = inject(McpTokenService);
  private readonly userService = inject(UserService);
  private readonly apiService = inject(ApiService);
  private readonly router = inject(Router);

  // Create form state
  newName = '';
  newScope = 'user';
  newExpiry: number | null = null;

  readonly creating = signal(false);
  readonly newToken = signal<McpTokenCreateResponse | null>(null);
  readonly copied = signal(false);
  readonly projects = signal<Project[]>([]);

  /** Only show active (non-revoked) tokens. */
  activeTokens = () =>
    this.tokenService.tokens().filter((t) => !t.revoked_at);

  ngOnInit(): void {
    this.tokenService.loadTokens();
    this.apiService
      .getProjects(this.userService.currentUserId() ?? undefined)
      .subscribe((p) => this.projects.set(p));
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

  formatDate(iso: string): string {
    const d = new Date(iso);
    const now = new Date();
    const diffMs = now.getTime() - d.getTime();
    if (diffMs < 60_000) return 'Just now';
    if (diffMs < 3600_000) return `${Math.floor(diffMs / 60_000)}m ago`;
    if (diffMs < 86400_000) return `${Math.floor(diffMs / 3600_000)}h ago`;
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
  }

  mcpJsonSnippet = () =>
    JSON.stringify(
      {
        mcpServers: {
          orchestrator: {
            type: 'http',
            url: `${window.location.origin.replace(/:\d+$/, ':8055')}/mcp`,
            auth: 'srw_YOUR_TOKEN_HERE',
          },
        },
      },
      null,
      2,
    );

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
}
