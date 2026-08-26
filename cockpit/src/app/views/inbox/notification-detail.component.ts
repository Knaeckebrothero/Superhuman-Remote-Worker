import {Component, computed, effect, inject, input, signal, untracked} from '@angular/core';
import {Router} from '@angular/router';
import {MarkdownComponent} from 'ngx-markdown';
import {TranslocoPipe, TranslocoService} from '@jsverse/transloco';
import {ActionItem} from '../../core/models/action.model';
import {Notification} from '../../core/models/notification.model';
import {ActionCenterService} from '../../core/services/action-center.service';
import {NotificationService} from '../../core/services/notification.service';
import {AppBadgeComponent, type BadgeTone} from '../../ui/badge';
import {AppIconComponent} from '../../ui/icon';
import {AppCopyFieldComponent} from '../../ui/copy-field';
import {ExternalImageDirective} from '../../ui/external-image';
import {NotificationActEvent, NotificationActionsComponent} from './notification-actions.component';

/** Icon per category; unknown categories fall back to a generic bell. */
const CATEGORY_ICON: Record<string, string> = {
  review_queue: 'rate_review',
  vm_upgrade: 'cloud_upload',
  budget_exceeded: 'account_balance_wallet',
  incident: 'error',
  officer_question: 'campaign',
  officer_runtime: 'military_tech',
};

export function categoryIcon(category: string): string {
  return CATEGORY_ICON[category] ?? 'notifications';
}

/**
 * Detail pane for one feed row (unified notification system).
 *
 * Presentation only: subject, body, engagement timeline, the source's
 * summary payload (fetched from `GET /api/notifications/{id}`), and the
 * generic action bar. Category-specific *meaning* stays on the server —
 * this pane renders whatever the row declares.
 */
@Component({
  selector: 'app-notification-detail',
  standalone: true,
  imports: [
    MarkdownComponent,
    ExternalImageDirective,
    TranslocoPipe,
    AppBadgeComponent,
    AppIconComponent,
    AppCopyFieldComponent,
    NotificationActionsComponent,
  ],
  template: `
    <div class="detail-content notification-detail">
      <div class="detail-header">
        <span class="detail-type-badge notification">
          <app-icon size="sm">{{ icon() }}</app-icon>
          {{ ('notifications.category.' + n().category) | transloco }}
        </span>
        <app-badge [tone]="severityTone()" appearance="solid" size="xs" [uppercase]="true">
          {{ ('notifications.severity.' + n().severity) | transloco }}
        </app-badge>
        @if (n().resolved_at) {
          <app-badge tone="neutral" size="xs" [uppercase]="true">
            {{ 'notifications.status.resolved' | transloco }}
          </app-badge>
        } @else if (!n().seen_at) {
          <app-badge tone="accent" size="xs" [uppercase]="true">
            {{ 'notifications.status.unseen' | transloco }}
          </app-badge>
        }
      </div>

      <h3 class="notification-subject">{{ n().subject }}</h3>

      @if (n().body) {
        <div class="notification-body">
          <markdown [data]="n().body"></markdown>
        </div>
      }

      @if (error()) {
        <div class="action-error">{{ error() }}</div>
      }

      <div class="action-bar">
        <app-notification-actions
          [actions]="n().actions"
          [resolved]="!!n().resolved_at"
          [busy]="acting()"
          (act)="onAct($event)"
        />
      </div>

      @if (n().source_ref) {
        <div class="source-section">
          <div class="section-title">{{ 'notifications.detail.source' | transloco }}</div>
          <div class="detail-ids">
            <app-copy-field
              [label]="('notifications.source.' + n().source_ref!.kind) | transloco"
              [value]="n().source_ref!.id"
            />
          </div>
          @if (loadingSource()) {
            <div class="source-loading">{{ 'notifications.detail.loadingSource' | transloco }}</div>
          } @else if (sourceJob()) {
            <div class="source-summary">
              <span class="meta-label">{{ 'notifications.detail.jobStatus' | transloco }}</span>
              <span class="meta-value">{{ sourceJob()!['status'] }}</span>
              @if (sourceJob()!['description']) {
                <span class="meta-label">{{ 'notifications.detail.jobDescription' | transloco }}</span>
                <span class="meta-value">{{ sourceJob()!['description'] }}</span>
              }
            </div>
          } @else if (sourceRequest()) {
            <div class="source-summary">
              <span class="meta-label">{{ 'notifications.detail.command' | transloco }}</span>
              <code class="meta-value mono">{{ sourceRequest()!['command'] }}</code>
              <span class="meta-label">{{ 'notifications.detail.requestStatus' | transloco }}</span>
              <span class="meta-value">{{ sourceRequest()!['status'] }}</span>
            </div>
          } @else if (sourceThread()) {
            <div class="source-summary">
              <span class="meta-label">{{ 'notifications.detail.session' | transloco }}</span>
              <span class="meta-value">{{ sourceThread()!['title'] || sourceThread()!['id'] }}</span>
            </div>
          }
        </div>
      }

      <div class="timeline">
        <div class="section-title">{{ 'notifications.detail.timeline' | transloco }}</div>
        <div class="timeline-grid">
          <span class="meta-label">{{ 'notifications.detail.created' | transloco }}</span>
          <span class="meta-value">{{ fmt(n().created_at) }}</span>
          <span class="meta-label">{{ 'notifications.detail.seen' | transloco }}</span>
          <span class="meta-value">{{ fmt(n().seen_at) }}</span>
          <span class="meta-label">{{ 'notifications.detail.read' | transloco }}</span>
          <span class="meta-value">{{ fmt(n().read_at) }}</span>
          <span class="meta-label">{{ 'notifications.detail.resolved' | transloco }}</span>
          <span class="meta-value">
            {{ fmt(n().resolved_at) }}
            @if (n().resolved_by) {
              <span class="resolved-by">· {{ n().resolved_by }}</span>
            }
          </span>
        </div>
      </div>
    </div>
  `,
  styles: [`
    :host { display: block; }
    .detail-header {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      margin-bottom: 12px;
    }
    .detail-type-badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      font-size: 12px;
      font-weight: 600;
      color: var(--text-secondary);
    }
    .notification-subject {
      margin: 0 0 12px 0;
      font-size: 16px;
      font-weight: 600;
      color: var(--text-primary);
    }
    .notification-body {
      font-size: 14px;
      line-height: 1.55;
      color: var(--text-primary);
      margin-bottom: 16px;
    }
    .action-bar { margin-bottom: 20px; }
    .action-error {
      color: var(--danger-color, #e64553);
      font-size: 12px;
      margin-bottom: 8px;
    }
    .section-title {
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      color: var(--text-secondary);
      margin-bottom: 6px;
    }
    .source-section { margin-bottom: 16px; }
    .detail-ids { margin-bottom: 8px; }
    .source-summary, .timeline-grid {
      display: grid;
      grid-template-columns: max-content 1fr;
      gap: 4px 12px;
      font-size: 13px;
    }
    .meta-label { color: var(--text-secondary); }
    .meta-value { color: var(--text-primary); overflow-wrap: anywhere; }
    .mono { font-family: var(--font-mono, monospace); }
    .source-loading { font-size: 12px; color: var(--text-secondary); }
    .resolved-by { color: var(--text-secondary); font-size: 12px; }
  `],
})
export class NotificationDetailComponent {
  readonly item = input.required<ActionItem>();

  private readonly actionCenter = inject(ActionCenterService);
  private readonly notifications = inject(NotificationService);
  private readonly router = inject(Router);
  private readonly transloco = inject(TranslocoService);

  readonly n = computed<Notification>(() => this.item().notification!);
  readonly icon = computed(() => categoryIcon(this.n().category));

  readonly source = signal<Record<string, unknown> | null>(null);
  readonly loadingSource = signal(false);
  readonly acting = signal(false);
  readonly error = signal<string | null>(null);

  readonly sourceJob = computed(() => this.sub('job'));
  readonly sourceRequest = computed(() => this.sub('request'));
  readonly sourceThread = computed(() => this.sub('thread'));

  constructor() {
    // Re-fetch the source payload whenever a different row is selected.
    effect(() => {
      const id = this.n().id;
      untracked(() => this.loadSource(id));
    });
  }

  private sub(key: string): Record<string, unknown> | null {
    const src = this.source();
    const value = src?.[key];
    return value && typeof value === 'object' ? (value as Record<string, unknown>) : null;
  }

  private loadSource(id: string): void {
    this.error.set(null);
    if (!this.n().source_ref) {
      this.source.set(null);
      return;
    }
    this.loadingSource.set(true);
    this.source.set(null);
    this.notifications.getNotification(id).subscribe((detail) => {
      this.source.set(detail?.source ?? null);
      this.loadingSource.set(false);
    });
  }

  onAct(event: NotificationActEvent): void {
    this.acting.set(true);
    this.error.set(null);
    this.actionCenter.act(this.n().id, event.type, event.params).subscribe({
      next: (res) => {
        this.acting.set(false);
        const nav = res.result?.['navigate'];
        if (typeof nav === 'string' && nav) this.router.navigateByUrl(nav);
      },
      error: (err: {error?: {detail?: unknown}}) => {
        this.acting.set(false);
        const detail = err?.error?.detail;
        this.error.set(
          typeof detail === 'string'
            ? detail
            : this.transloco.translate('notifications.detail.actionFailed'),
        );
      },
    });
  }

  severityTone(): BadgeTone {
    switch (this.n().severity) {
      case 'critical': return 'danger';
      case 'high': return 'warning';
      case 'normal': return 'accent';
      default: return 'neutral';
    }
  }

  fmt(iso: string | null): string {
    if (!iso) return '—';
    return new Date(iso).toLocaleString(this.transloco.getActiveLang(), {
      dateStyle: 'medium',
      timeStyle: 'short',
    });
  }
}
