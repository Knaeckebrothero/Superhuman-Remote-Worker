import {computed, DestroyRef, inject, Injectable} from '@angular/core';
import {Observable, tap} from 'rxjs';
import {NotificationService} from './notification.service';
import {ActionItem} from '../models/action.model';
import {
  Notification,
  NotificationActResponse,
  SEVERITY_URGENCY,
  SourceRef,
  sourceKey,
} from '../models/notification.model';

/** Sort: pending before resolved, then urgency desc, then timestamp desc. */
function actionItemComparator(a: ActionItem, b: ActionItem): number {
  // Pending always above resolved
  if (a.status !== b.status) {
    return a.status === 'pending' ? -1 : 1;
  }
  // Higher urgency first
  if (a.urgency !== b.urgency) return b.urgency - a.urgency;
  // Newer first
  return new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime();
}

/** Debounce for the batched seen-stamp POST. */
const SEEN_FLUSH_MS = 750;

/**
 * The action center's item list: the durable feed (`NotificationService.feed`)
 * and nothing else. Every producer records through the server's `record()`,
 * so a sudo request, an agent message, a review, a headless permission gate
 * all arrive as rows with server-declared actions (D7). Counts are the
 * server's.
 */
@Injectable({ providedIn: 'root' })
export class ActionCenterService {
  private readonly notifications = inject(NotificationService);
  private readonly destroyRef = inject(DestroyRef);

  // --- SSE lifecycle (owned here, not by individual components) ---
  private sseInitialized = false;

  initSSE(): void {
    if (this.sseInitialized) return;
    this.sseInitialized = true;
    this.notifications.connectSSE();
    this.destroyRef.onDestroy(() => {
      this.notifications.disconnectSSE();
    });
  }

  // --- Items ---
  readonly items = computed<ActionItem[]>(() =>
    this.notifications.feed().map((n) => this.mapNotification(n)).sort(actionItemComparator),
  );

  /**
   * Server-driven counts. `pending` is what still needs someone; `unseen`
   * is the bell's signal (D4: a row the user has had in front of them stops
   * nagging); `byCategory` feeds the inbox chips.
   */
  readonly counts = computed(() => {
    const c = this.notifications.feedCounts();
    return {
      notifications: c.pending,
      unseen: c.unseen,
      total: c.pending,
      byCategory: c.by_category,
    };
  });

  /** Bell badge: unseen feed rows, straight from the server. */
  readonly badgeCount = computed(() => this.counts().unseen);

  // --- Feed engagement ---
  private pendingSeen = new Set<string>();
  private seenTimer: ReturnType<typeof setTimeout> | null = null;

  /** A feed row rendered in the list. Batched + debounced into one POST;
   *  stamped optimistically so the badge drops immediately. */
  noteSeen(notificationId: string): void {
    const row = this.notifications.feed().find((n) => n.id === notificationId);
    if (!row || row.seen_at || this.pendingSeen.has(notificationId)) return;
    this.pendingSeen.add(notificationId);
    if (this.seenTimer) clearTimeout(this.seenTimer);
    this.seenTimer = setTimeout(() => this.flushSeen(), SEEN_FLUSH_MS);
  }

  /** Exposed for specs (fake timers) and for `ngOnDestroy` paths. */
  flushSeen(): void {
    this.seenTimer = null;
    const ids = Array.from(this.pendingSeen);
    this.pendingSeen.clear();
    if (!ids.length) return;
    const now = new Date().toISOString();
    for (const id of ids) this.notifications.patchFeedRow({ id, seen_at: now });
    this.notifications.markSeen(ids).subscribe({
      error: () => {
        /* the next load corrects the optimistic stamp */
      },
    });
  }

  /** Selecting a row is reading it; the server stamps read (and seen). */
  markRead(notificationId: string): void {
    const row = this.notifications.feed().find((n) => n.id === notificationId);
    if (!row || row.read_at) return;
    this.notifications.markReadV2(notificationId).subscribe({
      next: (res) => this.notifications.upsertFeedRow(res.notification),
      error: () => {
        /* stays unread; nothing to undo */
      },
    });
  }

  /** Run a declared action; the response row replaces the local one. */
  act(
    notificationId: string,
    actionType: string,
    params: Record<string, unknown>,
  ): Observable<NotificationActResponse> {
    return this.notifications
      .act(notificationId, actionType, params)
      .pipe(tap((res) => this.notifications.upsertFeedRow(res.notification)));
  }

  /** Deep link `?n=<id>` for a row not in the loaded page: fetch + upsert. */
  fetchNotification(notificationId: string): Observable<Notification | null> {
    return new Observable((subscriber) => {
      this.notifications.getNotification(notificationId).subscribe((detail) => {
        if (detail?.notification) this.notifications.upsertFeedRow(detail.notification);
        subscriber.next(detail?.notification ?? null);
        subscriber.complete();
      });
    });
  }

  /**
   * The legacy email deep links (`?sudo=<id>`, `?job=<id>&thread=<tid>`,
   * `?job=<id>&review=1`) name a *source*, not a row. Find the newest feed
   * row about it — in the loaded page first, else from the server — and
   * upsert it so the caller can select it.
   */
  fetchBySource(ref: SourceRef): Observable<Notification | null> {
    const key = sourceKey(ref);
    return new Observable((subscriber) => {
      const local = this.notifications.feed().find((n) => sourceKey(n.source_ref) === key);
      if (local) {
        subscriber.next(local);
        subscriber.complete();
        return;
      }
      this.notifications.listBySource(ref.kind, ref.id, 1).subscribe((page) => {
        const row = page?.items?.[0] ?? null;
        if (row) this.notifications.upsertFeedRow(row);
        subscriber.next(row);
        subscriber.complete();
      });
    });
  }

  loadMore(): void {
    this.notifications.loadMoreFeed();
  }

  // --- Refresh ---
  refreshAll(): void {
    this.notifications.loadNotifications();
  }

  // --- Mapping ---

  private mapNotification(n: Notification): ActionItem {
    const pending = !n.resolved_at;
    const payload = n.payload || {};
    const configName = typeof payload['config_name'] === 'string' ? payload['config_name'] : null;
    const jobDescription =
      typeof payload['job_description'] === 'string' ? payload['job_description'] : null;
    const title = typeof payload['title'] === 'string' ? payload['title'] : null;
    const jobId = typeof payload['job_id'] === 'string' ? payload['job_id'] : null;
    return {
      id: `ntf:${n.id}`,
      status: pending ? 'pending' : 'resolved',
      urgency: pending ? (SEVERITY_URGENCY[n.severity] ?? 40) : 0,
      timestamp: n.created_at || new Date(0).toISOString(),
      title: n.subject || n.category,
      // Empty when the payload carries nothing to say; the row then shows the
      // translated category label rather than the raw key.
      subtitle: [configName, jobDescription || title].filter(Boolean).join(' · '),
      jobId,
      notification: n,
      category: n.category,
    };
  }
}
