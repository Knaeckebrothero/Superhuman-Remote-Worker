import {computed, DestroyRef, inject, Injectable, signal} from '@angular/core';
import {Observable, tap} from 'rxjs';
import {SudoRequest, SudoService} from './sudo.service';
import {NotificationService, SessionEvent} from './notification.service';
import {ApiService} from './api.service';
import {AppNotification, Job} from '../models/api.model';
import {ActionItem, ActionItemStatus, MessageActionData, ReviewActionData,} from '../models/action.model';
import {
  Notification,
  NotificationActResponse,
  SEVERITY_URGENCY,
} from '../models/notification.model';

/**
 * Full UUID — the shape of a persistent-session thread id. Job-message
 * thread ids are short hex tokens and loop keys are "loop-" + 6 chars, so
 * this cleanly separates session-keyed rows (officer pages) from job-keyed
 * ones.
 */
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * An officer page: keyed to a persistent session, no job behind it. REST rows
 * persist job_id NULL; live SSE frames carry job_id === thread_id (the
 * dispatch keys its queue row with the thread UUID).
 */
function isSessionPage(n: AppNotification): boolean {
  return (
    !!n.thread_id &&
    UUID_RE.test(n.thread_id) &&
    (!n.job_id || n.job_id === n.thread_id)
  );
}

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
 * The action center's item list.
 *
 * Since the unified notification system (slice 1) the durable feed
 * (`NotificationService.feed`) is the source of truth for anything the
 * server recorded through `record()` — freeze reviews, VM upgrades,
 * incidents, officer questions. The four legacy sources below are the
 * client-side join slice 3 retires; a feed row that already covers a legacy
 * item's source (`source_ref`) hides the twin so nothing shows twice.
 */
@Injectable({ providedIn: 'root' })
export class ActionCenterService {
  private readonly sudo = inject(SudoService);
  private readonly notifications = inject(NotificationService);
  private readonly api = inject(ApiService);
  private readonly destroyRef = inject(DestroyRef);

  // --- SSE lifecycle (owned here, not by individual components) ---
  private sseInitialized = false;

  initSSE(): void {
    if (this.sseInitialized) return;
    this.sseInitialized = true;
    this.sudo.connectSSE();
    this.notifications.connectSSE();
    this.destroyRef.onDestroy(() => {
      this.sudo.disconnectSSE();
      this.notifications.disconnectSSE();
    });
  }

  // --- Review jobs (event-driven, not polled) ---
  readonly reviewJobs = signal<Job[]>([]);
  private reviewLoading = false;

  loadReviewJobs(): void {
    if (this.reviewLoading) return;
    this.reviewLoading = true;
    this.api.getJobs('pending_review').subscribe({
      next: (jobs) => {
        this.reviewJobs.set(jobs as unknown as Job[]);
        this.reviewLoading = false;
      },
      error: () => {
        this.reviewLoading = false;
      },
    });
  }

  // --- Merged action items ---
  readonly items = computed<ActionItem[]>(() => {
    const feedItems = this.notifications.feed().map((n) => this.mapNotification(n));
    // Sources the feed already carries. A legacy twin of the same job /
    // message thread / officer session must not appear next to it.
    const covered = new Set<string>();
    for (const n of this.notifications.feed()) {
      if (n.source_ref) covered.add(`${n.source_ref.kind}:${n.source_ref.id}`);
    }
    const sudoItems = this.sudo.requests().map((r) => this.mapSudo(r));
    const messageItems = this.deduplicateThreads(this.notifications.notifications()).filter(
      (i) =>
        !(i.message?.sessionThreadId && covered.has(`thread:${i.message.sessionThreadId}`)) &&
        !(i.message && covered.has(`message_thread:${i.message.threadId}`)),
    );
    const reviewItems = this.reviewJobs()
      .filter((j) => !covered.has(`job:${j.id}`))
      .map((j) => this.mapReview(j));
    const sessionItems = this.notifications.sessionEvents().map((e) => this.mapSession(e));
    return [...feedItems, ...sudoItems, ...messageItems, ...reviewItems, ...sessionItems].sort(
      actionItemComparator,
    );
  });

  readonly counts = computed(() => {
    const pending = this.items().filter((i) => i.status === 'pending');
    return {
      notifications: pending.filter((i) => i.type === 'notification').length,
      sudo: pending.filter((i) => i.type === 'sudo').length,
      messages: pending.filter((i) => i.type === 'message').length,
      reviews: pending.filter((i) => i.type === 'review').length,
      sessions: pending.filter((i) => i.type === 'session').length,
      /** Server-side unseen feed rows — the bell's first-class signal. */
      unseen: this.notifications.feedCounts().unseen,
      total: pending.length,
    };
  });

  /**
   * Bell badge: unseen feed rows plus pending legacy items. A feed row the
   * user has already had in front of them stops counting — that is the
   * whole point of `seen` (D4). Pure server `unseen` once slice 3 lands.
   */
  readonly badgeCount = computed(() => {
    const c = this.counts();
    return c.unseen + (c.total - c.notifications);
  });

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

  loadMore(): void {
    this.notifications.loadMoreFeed();
  }

  // --- Legacy pass-throughs (slice 3 removes them with their panes) ---

  loadThread(jobId: string, threadId: string) {
    return this.api.getThreadMessages(jobId, threadId);
  }

  reply(jobId: string, threadId: string, message: string, urgent: boolean) {
    return this.api.replyToThread(jobId, threadId, message, urgent);
  }

  approveJob(jobId: string, notes?: string) {
    return this.api.approveJob(jobId, notes);
  }

  resumeJob(jobId: string, feedback: string) {
    return this.api.resumeJob(jobId, feedback);
  }

  upgradeJobToVm(jobId: string) {
    return this.api.upgradeJobToVm(jobId);
  }

  // --- Refresh all data ---
  refreshAll(): void {
    this.sudo.loadRequests();
    this.notifications.loadNotifications();
    this.loadReviewJobs();
  }

  // --- Mapping helpers ---

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
      type: 'notification',
      status: pending ? 'pending' : 'resolved',
      urgency: pending ? (SEVERITY_URGENCY[n.severity] ?? 40) : 0,
      timestamp: n.created_at || new Date(0).toISOString(),
      title: n.subject || n.category,
      subtitle: [configName, jobDescription || title].filter(Boolean).join(' · ') || n.category,
      jobId,
      notification: n,
      category: n.category,
    };
  }

  private deduplicateThreads(notifications: AppNotification[]): ActionItem[] {
    const threadMap = new Map<string, AppNotification>();

    for (const n of notifications) {
      if (!n.thread_id) continue;
      // Session pages have no job; every other row still requires one.
      if (!n.job_id && !isSessionPage(n)) continue;
      // Session pages key on the thread UUID alone so the REST row
      // (job_id NULL), the live SSE frame (job_id === thread_id), and the
      // email deep link (?job={thread}&thread={thread}) all converge.
      const key = `${n.job_id || n.thread_id}:${n.thread_id}`;
      const existing = threadMap.get(key);
      if (!existing || new Date(n.created_at) > new Date(existing.created_at)) {
        threadMap.set(key, n);
      }
    }

    return Array.from(threadMap.values()).map((n) => {
      const sessionPage = isSessionPage(n);
      const isUnread = !n.read_at;
      // Determine blocking status from notification subject/status
      const isBlocking = n.status === 'waiting_for_reply';
      const status: ActionItemStatus = isUnread || isBlocking ? 'pending' : 'resolved';

      let urgency: number;
      if (isBlocking) urgency = 80;
      else if (isUnread) urgency = 40;
      else urgency = 20;

      return {
        id: `msg:${n.job_id || n.thread_id}:${n.thread_id}`,
        type: 'message' as const,
        status,
        urgency,
        timestamp: n.created_at,
        title: n.subject || 'Message',
        subtitle: sessionPage
          ? [n.config_name, `session ${n.thread_id!.slice(0, 8)}`].filter(Boolean).join(' · ')
          : [n.config_name || 'agent', n.job_description].filter(Boolean).join(' · '),
        // No resolvable job behind a session page: a null jobId keeps the
        // detail pane from firing the thread lookup that 404s (today's
        // dead-end bug) and disables the job-scoped reply path.
        jobId: sessionPage ? null : n.job_id,
        message: {
          threadId: n.thread_id!,
          subject: n.subject,
          mode: isBlocking ? 'blocking' : 'async',
          lastMessage: n.message?.slice(0, 200) || '',
          configName: n.config_name,
          jobDescription: n.job_description,
          unread: isUnread,
          sessionThreadId: sessionPage ? n.thread_id : null,
        } as MessageActionData,
      };
    });
  }

  private mapSudo(req: SudoRequest): ActionItem {
    const isPending = req.status === 'pending';
    const isVmUpgrade = req.request_type === 'vm_upgrade';

    let urgency: number;
    if (!isPending) {
      urgency = 0;
    } else if (isVmUpgrade) {
      urgency = 60; // Important but not TTL-critical
    } else {
      const secondsLeft = req.expires_at
        ? Math.max(0, Math.floor((new Date(req.expires_at).getTime() - Date.now()) / 1000))
        : 0;
      if (secondsLeft < 30) urgency = 90;
      else if (secondsLeft < 120) urgency = 70;
      else urgency = 50;
    }

    const command = req.arguments?.join(' ') || req.command;
    const title = isVmUpgrade ? `VM Upgrade: ${command}` : command;
    const subtitle = isVmUpgrade
      ? [req.vm_name, 'sudo in container'].filter(Boolean).join(' · ')
      : [req.vm_name, `${req.requesting_user} → ${req.target_user}`]
          .filter(Boolean)
          .join(' · ');

    return {
      id: `sudo:${req.id}`,
      type: 'sudo',
      status: isPending ? 'pending' : 'resolved',
      urgency,
      timestamp: req.requested_at,
      title,
      subtitle,
      jobId: req.job_id,
      sudo: req,
    };
  }

  private mapReview(job: Job): ActionItem {
    const isPending = job.status === 'pending_review';

    return {
      id: `rev:${job.id}`,
      type: 'review',
      status: isPending ? 'pending' : 'resolved',
      urgency: 30, // Default; will be refined when frozen data loads
      timestamp: job.updated_at || job.created_at,
      title: job.description,
      subtitle: [job.config_name, `job ${job.id.slice(0, 8)}`].filter(Boolean).join(' · '),
      jobId: job.id,
      review: {
        jobId: job.id,
        jobDescription: job.description,
        configName: job.config_name,
        freezeType: 'job_complete',
        phaseType: null,
        phaseNumber: null,
        summary: null,
        confidence: null,
        deliverables: [],
        frozenAt: null,
        command: null,
      } as ReviewActionData,
    };
  }

    private mapSession(e: SessionEvent): ActionItem {
        const isPermission = e.type === 'session.permission_request';
        const isVmUpgrade = e.type === 'session.vm_upgrade';
        const isWorkspaceUpgrade = e.type === 'session.workspace_upgrade';
        const isUpgrade = isVmUpgrade || isWorkspaceUpgrade;

        return {
            id: `sess:${e.event_id}`,
            type: 'session',
            status: 'pending',
            urgency: isPermission ? 90 : isUpgrade ? 70 : 30,
            timestamp: e.created_at,
            title: isPermission
                ? `Approve: ${e.tool}`
                : isVmUpgrade
                    ? 'VM Upgrade Needed'
                    : isWorkspaceUpgrade
                        ? 'Workspace Upgrade Needed'
                        : 'Waiting for Input',
            subtitle: [e.title, e.config_name].filter(Boolean).join(' · '),
            jobId: null,
            session: {threadId: e.thread_id, event: e},
        };
    }
}
