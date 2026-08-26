import { Component, inject } from '@angular/core';
import { Router } from '@angular/router';
import { TranslocoService } from '@jsverse/transloco';
import { ActionCenterService } from '../../core/services/action-center.service';
import { AppIconComponent } from '../../ui/icon';

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
      @if (actionCenter.badgeCount() > 0) {
        <span class="badge">{{ actionCenter.badgeCount() > 99 ? '99+' : actionCenter.badgeCount() }}</span>
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
      border-radius: var(--radius-control);
      color: var(--text-secondary);
      font-size: 20px;
      line-height: 1;
      transition: color 0.15s, background 0.15s;
    }

    .bell-btn:hover {
      color: var(--text-primary);
      background: var(--surface-0);
    }

    .badge {
      position: absolute;
      top: 2px;
      right: 0;
      min-width: 16px;
      height: 16px;
      padding: 0 4px;
      border-radius: var(--radius-pill);
      background: var(--accent-color);
      color: var(--on-accent, var(--panel-bg));
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

  /** "N new notifications" (server `unseen`), plus how many still need
   *  someone when that differs. */
  tooltipText(): string {
    const c = this.actionCenter.counts();
    const t = this.transloco;
    if (this.actionCenter.badgeCount() === 0) return t.translate('notificationBell.title');
    const parts: string[] = [];
    if (c.unseen > 0) {
      parts.push(t.translate(c.unseen === 1 ? 'notificationBell.unseenSingle' : 'notificationBell.unseenPlural', {n: c.unseen}));
    }
    if (c.total > 0 && c.total !== c.unseen) {
      parts.push(t.translate(c.total === 1 ? 'notificationBell.pendingSingle' : 'notificationBell.pendingPlural', {n: c.total}));
    }
    return parts.join(', ');
  }
}
