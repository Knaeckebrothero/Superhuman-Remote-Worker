import { Component, inject } from '@angular/core';
import { Router } from '@angular/router';
import { ActionCenterService } from '../../../core/services/action-center.service';

@Component({
  selector: 'app-notification-bell',
  standalone: true,
  template: `
    <button
      class="bell-btn"
      (click)="goToInbox()"
      [title]="tooltipText()"
    >
      <span class="bell-icon">inbox</span>
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

    .bell-icon {
      font-family: 'Material Symbols Outlined', sans-serif;
      font-size: 20px;
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

  goToInbox(): void {
    this.router.navigate(['/inbox']);
  }

  tooltipText(): string {
    const c = this.actionCenter.counts();
    if (c.total === 0) return 'Action Center';
    const parts: string[] = [];
    if (c.messages > 0) parts.push(`${c.messages} message${c.messages > 1 ? 's' : ''}`);
    if (c.sudo > 0) parts.push(`${c.sudo} sudo`);
    if (c.reviews > 0) parts.push(`${c.reviews} review${c.reviews > 1 ? 's' : ''}`);
      if (c.sessions > 0) parts.push(`${c.sessions} session${c.sessions > 1 ? 's' : ''}`);
    return parts.join(', ');
  }
}
