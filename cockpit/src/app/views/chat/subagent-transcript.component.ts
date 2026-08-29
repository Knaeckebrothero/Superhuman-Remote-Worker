import {ChangeDetectionStrategy, Component, input, output} from '@angular/core';
import {RouterLink} from '@angular/router';
import {TranslocoPipe} from '@jsverse/transloco';
import {PersistentThreadMessage, Thread} from '../../core/models/api.model';
import {AppIconComponent} from '../../ui/icon';

@Component({
  selector: 'app-subagent-transcript',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, TranslocoPipe, AppIconComponent],
  template: `
    <main class="subagent-transcript">
      <header class="subagent-banner" data-testid="subagent-banner">
        <a class="back-link" routerLink="/jobs">
          <app-icon size="sm">arrow_back</app-icon>
          {{ 'chat.subagent.parentJob' | transloco: {id: thread().parent_job_id} }}
        </a>
        <div class="banner-title">
          {{
            'chat.subagent.banner'
              | transloco: {
                  handle: thread().subagent_handle || ('jobs.detail.unknown' | transloco),
                  type: thread().subagent_type || ('jobs.detail.unknown' | transloco),
                  status: (statusKey() | transloco)
                }
          }}
        </div>
        @if (thread().subagent_status === 'running') {
          <button type="button" class="refresh-action" (click)="refresh.emit()">
            <app-icon size="sm">refresh</app-icon>
            {{ 'common.refresh' | transloco }}
          </button>
        }
      </header>

      <section class="transcript" aria-live="polite">
        @if (loading()) {
          <p class="transcript-note">{{ 'chat.subagent.loading' | transloco }}</p>
        } @else if (error()) {
          <p class="transcript-note transcript-error" role="alert">
            {{ 'chat.subagent.loadFailed' | transloco }}
          </p>
        } @else if (messages().length === 0) {
          <p class="transcript-note">{{ 'chat.subagent.empty' | transloco }}</p>
        } @else {
          @for (message of messages(); track message.id) {
            <article class="transcript-message" [attr.data-role]="messageKind(message.role)">
              <div class="message-meta">
                <span>{{ roleLabel(message.role) | transloco }}</span>
                @if (message.turn_number !== null) {
                  <span>{{ 'chat.subagent.turn' | transloco: {count: message.turn_number} }}</span>
                }
              </div>
              @if (message.thinking) {
                <details class="thinking">
                  <summary>{{ 'chat.subagent.thinking' | transloco }}</summary>
                  <pre>{{ message.thinking }}</pre>
                </details>
              }
              @if (message.tool_calls?.length) {
                <div class="tool-calls">
                  @for (call of message.tool_calls; track call.id) {
                    <div class="tool-call">
                      <strong>{{ call.name }}</strong>
                      <pre>{{ formatArgs(call.args) }}</pre>
                    </div>
                  }
                </div>
              }
              @if (message.content) {
                <pre class="message-content">{{ message.content }}</pre>
              }
            </article>
          }
        }
      </section>
    </main>
  `,
  styles: [`
    :host {
      display: block;
      width: 100%;
      height: 100%;
      overflow: auto;
      background: var(--surface-0);
    }
    .subagent-transcript {
      min-height: 100%;
      color: var(--text-primary);
    }
    .subagent-banner {
      position: sticky;
      top: 0;
      z-index: 1;
      display: flex;
      align-items: center;
      gap: 14px;
      padding: 12px 18px;
      background: var(--surface-1);
      border-bottom: 1px solid var(--border-color);
    }
    .back-link,
    .refresh-action {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      color: var(--accent-color);
    }
    .back-link {
      white-space: nowrap;
      text-decoration: none;
    }
    .banner-title {
      flex: 1;
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-weight: 600;
    }
    .refresh-action {
      border: 1px solid var(--border-color);
      border-radius: var(--radius-control);
      padding: 5px 9px;
      background: var(--surface-2);
      cursor: pointer;
      font: inherit;
    }
    .transcript {
      width: min(860px, calc(100% - 32px));
      margin: 0 auto;
      padding: 24px 0 40px;
    }
    .transcript-message {
      margin-bottom: 14px;
      padding: 12px 14px;
      background: var(--surface-1);
      border: 1px solid var(--border-color);
      border-radius: var(--radius-control);
    }
    .transcript-message[data-role='user'] {
      border-left: 3px solid var(--accent-color);
    }
    .transcript-message[data-role='tool'],
    .transcript-message[data-role='system'] {
      background: var(--surface-2);
    }
    .message-meta {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 8px;
      color: var(--text-muted);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }
    .message-content,
    .thinking pre,
    .tool-call pre {
      margin: 0;
      color: var(--text-primary);
      font: inherit;
      line-height: 1.55;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }
    .thinking,
    .tool-call {
      margin-bottom: 8px;
      color: var(--text-secondary);
    }
    .tool-call pre {
      margin-top: 4px;
      color: var(--text-secondary);
      font-family: var(--font-mono);
      font-size: 12px;
    }
    .transcript-note {
      color: var(--text-muted);
      text-align: center;
    }
    .transcript-error {
      color: var(--danger);
    }
    @media (max-width: 720px) {
      .subagent-banner {
        align-items: flex-start;
        flex-wrap: wrap;
      }
      .banner-title {
        order: 3;
        flex-basis: 100%;
        white-space: normal;
      }
    }
  `],
})
export class SubagentTranscriptComponent {
  readonly thread = input.required<Thread>();
  readonly messages = input<PersistentThreadMessage[]>([]);
  readonly loading = input(false);
  readonly error = input(false);
  readonly refresh = output<void>();

  statusKey(): string {
    const status = this.thread().subagent_status;
    return status ? `jobs.detail.subagentsStatuses.${status}` : 'jobs.detail.unknown';
  }

  messageKind(role: string): 'user' | 'assistant' | 'tool' | 'system' {
    if (role === 'human' || role === 'user') return 'user';
    if (role === 'ai' || role === 'assistant') return 'assistant';
    if (role === 'tool') return 'tool';
    return 'system';
  }

  roleLabel(role: string): string {
    return `chat.subagent.roles.${this.messageKind(role)}`;
  }

  formatArgs(args: Record<string, unknown>): string {
    return JSON.stringify(args, null, 2);
  }
}
