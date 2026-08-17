import {DestroyRef, Injectable, computed, inject, signal} from '@angular/core';
import {takeUntilDestroyed} from '@angular/core/rxjs-interop';
import {Subscription, timer} from 'rxjs';
import {switchMap} from 'rxjs/operators';
import {ApiService} from './api.service';
import {Job} from '../models/api.model';
import {isTerminalJobStatus} from '../util/job-status';

/**
 * Live status for the jobs a transcript's tool cards point at.
 *
 * **Why a separate map rather than fields on `ToolCardView`.** Tool card views
 * are memoized per event object in an identity-keyed `WeakMap`
 * (`persistent-chat.component.ts`), so a card only rebuilds when its underlying
 * call changes. Writing live status into the view would mean busting that cache
 * on every poll — turning a stable OnPush input into a churning one for every
 * card on screen. This copies the `citation.verdict` pattern instead
 * (`persistent-chat.service.ts` `citationsByCid`): patch an already-rendered
 * element through a signal map keyed by id, leaving the view model untouched.
 *
 * **Why polling.** No job-status push channel exists. A future `job.status`
 * frame would need no transport work — decoded frames already reach
 * `PersistentThreadTransportBridge.events$`, which `CanvasService` consumes as
 * a refetch trigger — but with `replicas: 2` such a frame must go through the
 * NATS→SSE bridge rather than a local broadcast, so it is not free either.
 * Polling is bounded by what is on screen and is the honest v1.
 *
 * Design: knowledge-base/knowledge/features/unified_tool_cards.md (slice 4).
 */

/** Poll cadence for a non-terminal job. Slow on purpose — jobs run for minutes. */
const POLL_MS = 10_000;

@Injectable({providedIn: 'root'})
export class JobWatchService {
    private readonly api = inject(ApiService);
    private readonly destroyRef = inject(DestroyRef);

    /** jobId -> last known row. The only live state; cards read through it. */
    private readonly jobsById = signal<Map<string, Job>>(new Map());

    /** Active pollers, so `watch()` is idempotent per id. */
    private readonly pollers = new Map<string, Subscription>();

    /**
     * Start (or join) watching a job. Idempotent: several cards pointing at the
     * same job share one poller.
     *
     * Returns nothing — callers read state via {@link job}. Deliberately not a
     * per-caller Observable: a months-old transcript can hold dozens of job
     * cards, and one poller per *card* rather than per *job* is how you get N
     * duplicate requests on a fan-out where several cards share a job.
     */
    watch(jobId: string): void {
        if (!jobId || this.pollers.has(jobId)) return;

        const sub = timer(0, POLL_MS)
            .pipe(
                switchMap(() => this.api.getJob(jobId)),
                takeUntilDestroyed(this.destroyRef),
            )
            .subscribe((job) => {
                if (!job) return;
                this.jobsById.update((m) => {
                    const next = new Map(m);
                    next.set(jobId, job);
                    return next;
                });
                // Stop at a terminal status. Without this every job card ever
                // rendered polls forever; a long transcript would issue a
                // request per card every 10s for the life of the tab.
                //
                // 'pending_review' is NOT terminal (see isTerminalJobStatus) —
                // an approval flips it to completed, and catching that
                // transition is precisely what makes the card's Approve button
                // resolve itself.
                if (isTerminalJobStatus(job.status)) this.stop(jobId);
            });

        this.pollers.set(jobId, sub);
    }

    /** Current row for a job, or null until the first poll lands. */
    job(jobId: string): Job | null {
        return this.jobsById().get(jobId) ?? null;
    }

    /** Signal-friendly accessor for template `computed()`s. */
    readonly snapshot = computed(() => this.jobsById());

    /**
     * Force an immediate refresh — used after an action (approve/cancel) so the
     * card reflects the new status without waiting out the poll interval, and
     * so a job that had already gone terminal (poller stopped) still updates.
     */
    async refresh(jobId: string): Promise<void> {
        const job = await this.fetchOnce(jobId);
        if (!job) return;
        this.jobsById.update((m) => {
            const next = new Map(m);
            next.set(jobId, job);
            return next;
        });
        if (!isTerminalJobStatus(job.status)) this.watch(jobId);
    }

    private fetchOnce(jobId: string): Promise<Job | null> {
        return new Promise((resolve) => {
            this.api
                .getJob(jobId)
                .pipe(takeUntilDestroyed(this.destroyRef))
                .subscribe({
                    next: (job) => resolve(job),
                    error: () => resolve(null),
                });
        });
    }

    private stop(jobId: string): void {
        this.pollers.get(jobId)?.unsubscribe();
        this.pollers.delete(jobId);
    }
}
