import { Component, inject, OnInit, OnDestroy, signal } from '@angular/core';
import { Router } from '@angular/router';
import { NotificationService } from '../../../core/services/notification.service';
import { AppNotification } from '../../../core/models/api.model';

@Component({
  selector: 'app-notification-bell',
  standalone: true,
  template: `
    <div class="bell-wrapper">
      <button class="bell-btn" (click)="toggleDropdown()" title="Notifications">
        <span class="bell-icon">notifications</span>
        @if (svc.unreadCount() > 0) {
          <span class="badge">{{ svc.unreadCount() > 99 ? '99+' : svc.unreadCount() }}</span>
        }
      </button>

      @if (isOpen()) {
        <div class="dropdown" (click)="$event.stopPropagation()">
          <div class="dropdown-header">
            <span>Notifications</span>
            @if (svc.unreadCount() > 0) {
              <span class="unread-label">{{ svc.unreadCount() }} unread</span>
            }
          </div>

          <div class="dropdown-body">
            @if (svc.isLoading()) {
              <div class="empty-state">Loading...</div>
            } @else if (svc.notifications().length === 0) {
              <div class="empty-state">No notifications</div>
            } @else {
              @for (n of svc.notifications().slice(0, 20); track n.id) {
                <button
                  class="notif-item"
                  [class.unread]="!n.read_at"
                  (click)="onNotificationClick(n)"
                >
                  <div class="notif-subject">{{ n.subject }}</div>
                  <div class="notif-meta">
                    {{ n.config_name || 'agent' }}
                    @if (n.job_description) {
                      &middot; {{ n.job_description }}
                    }
                  </div>
                  <div class="notif-time">{{ formatTime(n.created_at) }}</div>
                </button>
              }
            }
          </div>
        </div>
      }
    </div>
  `,
  styles: [`
    :host {
      position: relative;
      display: inline-flex;
    }

    .bell-wrapper {
      position: relative;
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

    .dropdown {
      position: absolute;
      top: calc(100% + 8px);
      right: 0;
      width: 360px;
      max-height: 480px;
      background: var(--panel-bg, #1e1e2e);
      border: 1px solid var(--border-color, #313244);
      border-radius: 12px;
      box-shadow: 0 8px 32px rgba(0, 0, 0, 0.4);
      overflow: hidden;
      z-index: 1000;
      display: flex;
      flex-direction: column;
    }

    .dropdown-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 12px 16px;
      border-bottom: 1px solid var(--border-color, #313244);
      font-size: 13px;
      font-weight: 600;
      color: var(--text-primary, #cdd6f4);
    }

    .unread-label {
      font-size: 11px;
      font-weight: 400;
      color: var(--accent-color, #cba6f7);
    }

    .dropdown-body {
      overflow-y: auto;
      flex: 1;
    }

    .empty-state {
      padding: 32px 16px;
      text-align: center;
      color: var(--text-muted, #6c7086);
      font-size: 13px;
    }

    .notif-item {
      display: block;
      width: 100%;
      padding: 10px 16px;
      border: none;
      background: transparent;
      cursor: pointer;
      text-align: left;
      border-bottom: 1px solid var(--border-color, #313244);
      transition: background 0.1s;
    }

    .notif-item:hover {
      background: var(--surface-0, #313244);
    }

    .notif-item.unread {
      border-left: 3px solid var(--accent-color, #cba6f7);
    }

    .notif-subject {
      font-size: 13px;
      font-weight: 500;
      color: var(--text-primary, #cdd6f4);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .notif-meta {
      font-size: 11px;
      color: var(--text-muted, #6c7086);
      margin-top: 2px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .notif-time {
      font-size: 10px;
      color: var(--text-muted, #6c7086);
      margin-top: 2px;
    }
  `],
  host: {
    '(document:click)': 'closeDropdown()',
  },
})
export class NotificationBellComponent implements OnInit, OnDestroy {
  readonly svc = inject(NotificationService);
  private readonly router = inject(Router);

  readonly isOpen = signal(false);

  ngOnInit(): void {
    this.svc.loadNotifications();
    this.svc.connectSSE();
  }

  ngOnDestroy(): void {
    this.svc.disconnectSSE();
  }

  toggleDropdown(): void {
    this.isOpen.update((v) => !v);
    if (this.isOpen()) {
      this.svc.loadNotifications();
    }
  }

  closeDropdown(): void {
    this.isOpen.set(false);
  }

  onNotificationClick(n: AppNotification): void {
    if (!n.read_at) {
      this.svc.markRead(n.id);
    }
    this.isOpen.set(false);
    if (n.job_id) {
      this.router.navigate(['/jobs'], { queryParams: { id: n.job_id } });
    }
  }

  formatTime(iso: string): string {
    if (!iso) return '';
    const d = new Date(iso);
    const now = new Date();
    const diff = now.getTime() - d.getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs}h ago`;
    const days = Math.floor(hrs / 24);
    return `${days}d ago`;
  }
}
