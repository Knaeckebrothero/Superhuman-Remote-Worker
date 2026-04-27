import { Component, inject } from '@angular/core';
import { Router } from '@angular/router';
import { TranslocoService } from '@jsverse/transloco';
import { ActionCenterService } from '../../../core/services/action-center.service';
import { AppIconComponent } from '../../../ui/icon';

@Component({
  selector: 'app-notification-bell',
  standalone: true,
  imports: [AppIconComponent],
  template: `
    <button
      class="bell-btn"
      (click)="goToInbox()"
      [title]="tooltipText()"
    >
      <app-icon size="lg">inbox</app-icon>
      @if (actionCenter.counts().total > 0) {
        <span class="badge">{{ actionCenter.counts().total > 99 ? '99+' : actionCenter.counts().total }}</span>
      }
    </button>
  `,
  styles: [`
    :host {
      position: relative;
      display: inline-flex;
    }

    .bell-btn {
      background: none;
      border: none;
      cursor: pointer;
      position: relative;
      padding: 6px;
      border-radius: 6px;
      color: var(--text-secondary, #a6adc8);
      font-size: 20px;
      line-height: 1;
      transition: color 0.15s, background 0.15s;
    }

    .bell-btn:hover {
      color: var(--text-primary, #cdd6f4);
      background: var(--surface-0, #313244);
    }

    .badge {
      position: absolute;
      top: 2px;
      right: 0;
      min-width: 16px;
      height: 16px;
      padding: 0 4px;
      border-radius: 8px;
      background: var(--accent-color, #cba6f7);
      color: var(--panel-bg, #1e1e2e);
      font-size: 10px;
      font-weight: 700;
      line-height: 16px;
      text-align: center;
    }
  `],
})
export class NotificationBellComponent {
  readonly actionCenter = inject(ActionCenterService);
  private readonly router = inject(Router);
  private readonly transloco = inject(TranslocoService);

  goToInbox(): void {
    this.router.navigate(['/inbox']);
  }

  tooltipText(): string {
    const c = this.actionCenter.counts();
    if (c.total === 0) return this.transloco.translate('notificationBell.title');
    const t = this.transloco;
    const parts: string[] = [];
    if (c.messages > 0) parts.push(t.translate(c.messages === 1 ? 'notificationBell.messagesSingle' : 'notificationBell.messagesPlural', {n: c.messages}));
    if (c.sudo > 0) parts.push(t.translate('notificationBell.sudo', {n: c.sudo}));
    if (c.reviews > 0) parts.push(t.translate(c.reviews === 1 ? 'notificationBell.reviewsSingle' : 'notificationBell.reviewsPlural', {n: c.reviews}));
    if (c.sessions > 0) parts.push(t.translate(c.sessions === 1 ? 'notificationBell.sessionsSingle' : 'notificationBell.sessionsPlural', {n: c.sessions}));
    return parts.join(', ');
  }
}
