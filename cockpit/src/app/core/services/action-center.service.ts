import {computed, DestroyRef, inject, Injectable, signal} from '@angular/core';
import {SudoRequest, SudoService} from './sudo.service';
import {NotificationService, SessionEvent} from './notification.service';
import {ApiService} from './api.service';
import {AppNotification, Job} from '../models/api.model';
import {ActionItem, ActionItemStatus, MessageActionData, ReviewActionData,} from '../models/action.model';

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
    const sudoItems = this.sudo.requests().map((r) => this.mapSudo(r));
    const messageItems = this.deduplicateThreads(this.notifications.notifications());
    const reviewItems = this.reviewJobs().map((j) => this.mapReview(j));
      const sessionItems = this.notifications.sessionEvents().map((e) => this.mapSession(e));
      return [...sudoItems, ...messageItems, ...reviewItems, ...sessionItems].sort(actionItemComparator);
  });

  readonly counts = computed(() => {
    const pending = this.items().filter((i) => i.status === 'pending');
    return {
      sudo: pending.filter((i) => i.type === 'sudo').length,
      messages: pending.filter((i) => i.type === 'message').length,
      reviews: pending.filter((i) => i.type === 'review').length,
        sessions: pending.filter((i) => i.type === 'session').length,
      total: pending.length,
    };
  });

  // --- Actions ---

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

  private deduplicateThreads(notifications: AppNotification[]): ActionItem[] {
    const threadMap = new Map<string, AppNotification>();

    for (const n of notifications) {
      if (!n.thread_id || !n.job_id) continue;
      const key = `${n.job_id}:${n.thread_id}`;
      const existing = threadMap.get(key);
      if (!existing || new Date(n.created_at) > new Date(existing.created_at)) {
        threadMap.set(key, n);
      }
    }

    return Array.from(threadMap.values()).map((n) => {
      const isUnread = !n.read_at;
      // Determine blocking status from notification subject/status
      const isBlocking = n.status === 'waiting_for_reply';
      const status: ActionItemStatus = isUnread || isBlocking ? 'pending' : 'resolved';

      let urgency: number;
      if (isBlocking) urgency = 80;
      else if (isUnread) urgency = 40;
      else urgency = 20;

      return {
        id: `msg:${n.job_id}:${n.thread_id}`,
        type: 'message' as const,
        status,
        urgency,
        timestamp: n.created_at,
        title: n.subject || 'Message',
        subtitle: [n.config_name || 'agent', n.job_description].filter(Boolean).join(' \u00B7 '),
        jobId: n.job_id,
        message: {
          threadId: n.thread_id!,
          subject: n.subject,
          mode: isBlocking ? 'blocking' : 'async',
          lastMessage: n.message?.slice(0, 200) || '',
          configName: n.config_name,
          jobDescription: n.job_description,
          unread: isUnread,
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
      ? [req.vm_name, 'sudo in container'].filter(Boolean).join(' \u00B7 ')
      : [req.vm_name, `${req.requesting_user} \u2192 ${req.target_user}`]
          .filter(Boolean)
          .join(' \u00B7 ');

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
      subtitle: [job.config_name, `job ${job.id.slice(0, 8)}`].filter(Boolean).join(' \u00B7 '),
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
            subtitle: [e.title, e.config_name].filter(Boolean).join(' \u00B7 '),
            jobId: null,
            session: {threadId: e.thread_id, event: e},
        };
    }
}
