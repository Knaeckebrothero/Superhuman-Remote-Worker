import { Injectable, inject, signal, computed, NgZone } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { TranslocoService } from '@jsverse/transloco';
import { catchError, of } from 'rxjs';
import { AppNotification } from '../models/api.model';
import { environment } from '../environment';
import { AppToastService } from '../../ui/toast';

/** A session event from a persistent agent (permission request, VM upgrade, etc.). */
export interface SessionEvent {
    event_id: string;
    type: string;
    thread_id: string;
    title: string;
    config_name?: string;
    tool?: string;
    args?: Record<string, unknown>;
    reason?: string;
    command?: string;
    created_at: string;
}

/**
 * One session-lifecycle phase transition emitted by the orchestrator while
 * a session is starting up. Single source of truth for the cockpit's
 * "Starting session" card — see notification.service.connectSSE().
 */
export interface LifecycleEvent {
    thread_id: string;
    state: 'provisioning' | 'booting' | 'ready' | 'failed' | (string & {});
    reason?: string;
}

/**
 * A `user_registered` SSE frame — the orchestrator fans this out to admins
 * only (app-side admission, notify_admins_user_registered) when a new user
 * registers and lands pending.
 */
export interface AdminUserRegisteredEvent {
    user_id: string;
    display_name?: string | null;
    email?: string | null;
}

@Injectable({ providedIn: 'root' })
export class NotificationService {
  private readonly http = inject(HttpClient);
  private readonly zone = inject(NgZone);
  private readonly toast = inject(AppToastService);
  private readonly transloco = inject(TranslocoService);
  private readonly baseUrl = environment.apiUrl;

  /** All loaded notifications. */
  readonly notifications = signal<AppNotification[]>([]);

  /** Unread count from the API. */
  readonly unreadCount = signal(0);

  /** SSE connection state. */
  readonly isConnected = signal(false);

  /** Whether notifications are loading. */
  readonly isLoading = signal(false);

    /** Persistent session events (permission requests, VM upgrades, waiting). */
    readonly sessionEvents = signal<SessionEvent[]>([]);

    /**
     * Latest session.lifecycle phase transition (or null if none seen this
     * session). Consumers filter by `thread_id` to react only to events
     * matching the thread they care about. Cleared on disconnectSSE.
     */
    readonly lifecycleEvent = signal<LifecycleEvent | null>(null);

    /**
     * Latest `user_registered` frame (admins only receive these). The admin
     * Users page reacts via effect() to refresh its pending list live.
     */
    readonly adminUserRegistered = signal<AdminUserRegisteredEvent | null>(null);

  /** Derived: only unread notifications. */
  readonly unreadNotifications = computed(() =>
    this.notifications().filter((n) => !n.read_at),
  );

  private eventSource: EventSource | null = null;

  /** Load notifications from REST API. */
  loadNotifications(limit = 50, unreadOnly = false): void {
    this.isLoading.set(true);
    const params: Record<string, string> = { limit: String(limit) };
    if (unreadOnly) params['unread_only'] = 'true';

    this.http
      .get<{ notifications: AppNotification[]; unread_count: number }>(
        `${this.baseUrl}/notifications`,
        { params },
      )
      .pipe(catchError(() => of({ notifications: [], unread_count: 0 })))
      .subscribe((data) => {
        this.notifications.set(data.notifications);
        this.unreadCount.set(data.unread_count);
        this.isLoading.set(false);
      });
  }

  /** Mark a notification as read. */
  markRead(notificationId: string): void {
    this.http
      .patch<{ status: string }>(`${this.baseUrl}/notifications/${notificationId}`, {})
      .subscribe({
        next: () => {
          this.notifications.update((ns) =>
            ns.map((n) =>
              n.id === notificationId
                ? { ...n, read_at: new Date().toISOString() }
                : n,
            ),
          );
          this.unreadCount.update((c) => Math.max(0, c - 1));
        },
      });
  }

  /** Connect to SSE stream for real-time notification updates. */
  connectSSE(): void {
    if (this.eventSource) return;

    // ngsw-bypass keeps the service worker from buffering this SSE stream.
    this.eventSource = new EventSource(`${this.baseUrl}/notifications/events?ngsw-bypass=true`, {
      withCredentials: true,
    });

    this.eventSource.onmessage = (e: MessageEvent) => {
      this.zone.run(() => {
        try {
          this.handleSseEvent(JSON.parse(e.data));
        } catch {
          // Ignore parse errors (keepalive, malformed events)
        }
      });
    };

    this.eventSource.onopen = () => {
      this.zone.run(() => this.isConnected.set(true));
    };

    this.eventSource.onerror = () => {
      this.zone.run(() => this.isConnected.set(false));
    };
  }

  /**
   * Dispatch one parsed SSE frame. Extracted from the EventSource callback
   * so specs can drive frames through without a live stream — connectSSE()
   * is the only production caller.
   */
  // The frame is a raw JSON.parse result — same effective typing as the
  // previous inline handler (strict tsconfig forbids dot access on index
  // signatures, so Record<string, unknown> would force bracket noise).
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  handleSseEvent(data: any): void {
    if (data.type === 'new_message') {
      this.unreadCount.update((c) => c + 1);
      // Prepend a lightweight notification entry
      this.notifications.update((ns) => [
        {
          id: data.id || crypto.randomUUID(),
          job_id: data.job_id,
          thread_id: data.thread_id || null,
          subject: data.subject || 'New message',
          message: '',
          job_description: null,
          config_name: null,
          status: 'sent',
          read_at: null,
          created_at: new Date().toISOString(),
        },
        ...ns,
      ]);
    } else if (data.type === 'reply_delivered') {
      // Optionally refresh to show the updated thread
      this.loadNotifications();
    } else if (
        data.type === 'session.permission_request' ||
        data.type === 'session.vm_upgrade' ||
        data.type === 'session.waiting'
    ) {
        this.sessionEvents.update((events) => [
            {...data, created_at: data.created_at || new Date().toISOString()} as SessionEvent,
            ...events,
        ]);
    } else if (data.type === 'session.resolved') {
        this.sessionEvents.update((events) =>
            events.filter((e) => e.thread_id !== data.thread_id),
        );
    } else if (data.type === 'session.lifecycle') {
        // Single source of truth for the cockpit's "Starting
        // session" card phase transitions. Both server-side
        // binding paths (orchestrator/services/provision_or_assign.py
        // for the create-thread fast path, and
        // orchestrator/routers/sessions.py::_do_prepare for the
        // cold path) emit on this channel — the cockpit's
        // PersistentChatService reacts via an effect() that
        // filters on the active thread id.
        this.lifecycleEvent.set({
            thread_id: data.thread_id,
            state: data.state,
            reason: data.reason,
        });
    } else if (data.type === 'user_registered') {
        // Admin-only fan-out (app-side admission): a new user
        // registered and awaits approval.
        this.adminUserRegistered.set({
            user_id: data.user_id,
            display_name: data.display_name,
            email: data.email,
        });
        this.toast.info(
            this.transloco.translate('toasts.admin.userRegistered', {
                name: data.display_name || data.email || data.user_id,
            }),
        );
    } else if (data.type === 'automation_auto_disabled') {
        this.toast.warning(
            this.transloco.translate('toasts.automations.autoDisabled', {
                name: data.automation_name || '',
            }),
        );
    }
  }

  /** Disconnect from SSE stream. */
  disconnectSSE(): void {
    if (this.eventSource) {
      this.eventSource.close();
      this.eventSource = null;
      this.isConnected.set(false);
      this.lifecycleEvent.set(null);
    }
  }
}
