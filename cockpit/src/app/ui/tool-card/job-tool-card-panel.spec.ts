import {computed, signal, ɵresolveComponentResources} from '@angular/core';
import {ComponentFixture, TestBed} from '@angular/core/testing';
import {TranslocoTestingModule} from '@jsverse/transloco';
import {Observable, of} from 'rxjs';
import {afterEach, beforeAll, describe, expect, it, vi} from 'vitest';
import {Job} from '../../core/models/api.model';
import {ToolCardEntity} from '../../core/models/tool-card.model';
import {ApiService} from '../../core/services/api.service';
import {JobWatchService} from '../../core/services/job-watch.service';
import {canResumeJobStatus} from '../../core/util/job-status';
import {JobToolCardPanelComponent} from './job-tool-card-panel.component';

/**
 * Resume-with-feedback on the job tool card.
 *
 * The two things worth pinning are the ones a live gate would not reliably
 * reproduce: which statuses offer the action, and that a *rejected* resume
 * keeps the user's typed draft. Everything else the card does was gated on dev.
 *
 * Design: knowledge-base/knowledge/features/unified_tool_cards.md (slice 4).
 */

const JOB_ID = 'job-1234-5678';

function jobRow(status: string): Job {
    return {id: JOB_ID, status} as unknown as Job;
}

/** Stands in for the poller: the card only ever reads a current row from it. */
class FakeWatcher {
    private readonly rows = signal(new Map<string, Job>());
    readonly snapshot = computed(() => this.rows());
    readonly watch = vi.fn();
    readonly refresh = vi.fn(async () => {});

    job(jobId: string): Job | null {
        return this.rows().get(jobId) ?? null;
    }

    put(row: Job): void {
        this.rows.update((m) => new Map(m).set(row.id, row));
    }
}

describe('canResumeJobStatus', () => {
    it('offers a hand-back only where the job has stopped and waits on a human', () => {
        expect(['pending_review', 'failed', 'cancelled'].every(canResumeJobStatus)).toBe(true);

        // completed: the server 400s (main.py:13658). paused: the dispatcher
        // re-picks it and the card is showing a spinner. The rest are live.
        for (const s of ['completed', 'paused', 'processing', 'created', 'waiting', 'reviewing']) {
            expect(canResumeJobStatus(s)).toBe(false);
        }
        expect(canResumeJobStatus(null)).toBe(false);
        expect(canResumeJobStatus(undefined)).toBe(false);
    });
});

describe('JobToolCardPanelComponent — resume with feedback', () => {
    let watcher: FakeWatcher;
    let resumeJob: ReturnType<typeof vi.fn>;
    let fixture: ComponentFixture<JobToolCardPanelComponent>;

    // The badge/icon children declare `styleUrl`, which JIT cannot fetch under
    // jsdom. Same preamble as markdown-tool-card.spec.ts.
    beforeAll(async () => {
        await ɵresolveComponentResources(() => Promise.resolve(''));
    });

    /** Renders the card against `status`, with `resumeJob` returning `emits`. */
    async function render(status: string, emits: Observable<unknown> = of({status: 'resumed'})) {
        watcher = new FakeWatcher();
        watcher.put(jobRow(status));
        resumeJob = vi.fn(() => emits);

        TestBed.configureTestingModule({
            imports: [
                JobToolCardPanelComponent,
                TranslocoTestingModule.forRoot({
                    langs: {
                        en: {
                            jobs: {status: {pending_review: 'Pending Review', failed: 'Failed'}},
                            toolCard: {
                                job: {
                                    approve: 'Approve',
                                    cancel: 'Cancel job',
                                    openDiff: 'Open diff',
                                    resumeWithFeedback: 'Continue with feedback',
                                    feedbackPlaceholder: 'What should it do differently?',
                                    feedbackSubmit: 'Send & continue',
                                    feedbackDismiss: 'Dismiss',
                                },
                            },
                        },
                    },
                    translocoConfig: {availableLangs: ['en'], defaultLang: 'en'},
                }),
            ],
            providers: [
                {provide: JobWatchService, useValue: watcher},
                {provide: ApiService, useValue: {resumeJob, approveJob: vi.fn(), cancelJob: vi.fn()}},
            ],
        });
        await TestBed.compileComponents();
        fixture = TestBed.createComponent(JobToolCardPanelComponent);
        // Assigned, not `setInput`: this vitest pipeline drops signal-input
        // metadata, so the binding never lands and the constructor effect
        // throws NG0950 on `entity()`. Same workaround as
        // markdown-tool-card.spec.ts. Must precede the first detectChanges().
        (fixture.componentInstance as unknown as {entity: () => ToolCardEntity}).entity =
            signal<ToolCardEntity>({kind: 'job', id: JOB_ID});
        fixture.detectChanges();
        await fixture.whenStable();
        fixture.detectChanges();
    }

    const root = () => fixture.nativeElement as HTMLElement;
    const buttons = () => Array.from(root().querySelectorAll<HTMLButtonElement>('.jc__btn'));
    const byLabel = (label: string) => buttons().find((b) => b.textContent?.trim() === label);
    const textarea = () => root().querySelector<HTMLTextAreaElement>('.jc__input');

    async function type(text: string) {
        const el = textarea()!;
        el.value = text;
        el.dispatchEvent(new Event('input'));
        fixture.detectChanges();
        await fixture.whenStable();
        fixture.detectChanges();
    }

    async function click(el: HTMLButtonElement) {
        el.click();
        // The handlers are async and not zone-tracked here, so whenStable()
        // alone returns before they settle; drain the task queue first.
        await new Promise((resolve) => setTimeout(resolve, 0));
        fixture.detectChanges();
        await fixture.whenStable();
        fixture.detectChanges();
    }

    afterEach(() => TestBed.resetTestingModule());

    it('shows the status in the product\'s words, not the database enum', async () => {
        await render('pending_review');
        // The harness below translates jobs.status.pending_review; the card used
        // to print the raw `pending_review`, underscore and all, next to a Jobs
        // page that said "Pending Review" for the same row.
        expect(root().querySelector('app-badge')?.textContent?.trim()).toBe('Pending Review');
    });

    it('falls back to the raw status rather than printing a bare i18n key', async () => {
        // waiting_for_reply is in the jobs.status CHECK constraint; a status with
        // no translation must degrade to something a human can still act on.
        await render('some_new_status');
        expect(root().querySelector('app-badge')?.textContent?.trim()).toBe('some_new_status');
    });

    it('offers the action on a job frozen for review', async () => {
        await render('pending_review');
        expect(byLabel('Continue with feedback')).toBeTruthy();
    });

    it('does not offer it on a job that is still running', async () => {
        await render('processing');
        expect(byLabel('Continue with feedback')).toBeUndefined();
    });

    it('replaces the action row while composing, so the only cancel means cancel-writing', async () => {
        await render('pending_review');
        expect(byLabel('Cancel job')).toBeTruthy();

        await click(byLabel('Continue with feedback')!);
        expect(textarea()).toBeTruthy();
        // "Cancel job" is gone — next to "Dismiss" it is a one-click accident.
        expect(byLabel('Cancel job')).toBeUndefined();
        expect(byLabel('Approve')).toBeUndefined();
        expect(byLabel('Dismiss')).toBeTruthy();
    });

    it('will not send an empty or whitespace-only draft', async () => {
        await render('pending_review');
        await click(byLabel('Continue with feedback')!);

        expect(byLabel('Send & continue')!.disabled).toBe(true);
        await type('   ');
        expect(byLabel('Send & continue')!.disabled).toBe(true);
        expect(resumeJob).not.toHaveBeenCalled();
    });

    it('sends the trimmed draft, then closes and refreshes', async () => {
        await render('pending_review');
        await click(byLabel('Continue with feedback')!);
        await type('  tighten the summary  ');
        await click(byLabel('Send & continue')!);

        expect(resumeJob).toHaveBeenCalledWith(JOB_ID, 'tighten the summary');
        expect(watcher.refresh).toHaveBeenCalledWith(JOB_ID);
        // Composer closed; reopening starts empty.
        expect(textarea()).toBeNull();
        await click(byLabel('Continue with feedback')!);
        expect(textarea()!.value).toBe('');
    });

    it('submits on Ctrl+Enter', async () => {
        // Also the control for the negative case below: if this binding did not
        // fire under jsdom at all, that assertion would pass vacuously.
        await render('pending_review');
        await click(byLabel('Continue with feedback')!);
        await type('ship it but rename the file');

        textarea()!.dispatchEvent(
            new KeyboardEvent('keydown', {key: 'Enter', ctrlKey: true, bubbles: true}),
        );
        await new Promise((resolve) => setTimeout(resolve, 0));

        expect(resumeJob).toHaveBeenCalledWith(JOB_ID, 'ship it but rename the file');
    });

    it('disables send if a poll takes the job somewhere unresumable mid-typing', async () => {
        await render('pending_review');
        await click(byLabel('Continue with feedback')!);
        await type('add the caveat');
        expect(byLabel('Send & continue')!.disabled).toBe(false);

        // The agent approves it while the user is still writing.
        watcher.put(jobRow('completed'));
        fixture.detectChanges();
        await fixture.whenStable();
        fixture.detectChanges();

        expect(byLabel('Send & continue')!.disabled).toBe(true);
        // The draft is still on screen — only the dead action is blocked.
        expect(textarea()!.value).toBe('add the caveat');
        // Ctrl+Enter bypasses the disabled attribute, so the handler guards too.
        textarea()!.dispatchEvent(
            new KeyboardEvent('keydown', {key: 'Enter', ctrlKey: true, bubbles: true}),
        );
        await new Promise((resolve) => setTimeout(resolve, 0));
        expect(resumeJob).not.toHaveBeenCalled();
    });

    it('keeps the draft when the resume is rejected', async () => {
        // ApiService maps a 403 from the resume PEP (main.py:13597) to null after
        // toasting. Clearing here would throw away what the user wrote.
        await render('pending_review', of(null));
        await click(byLabel('Continue with feedback')!);
        await type('use the staging bucket');
        await click(byLabel('Send & continue')!);

        expect(resumeJob).toHaveBeenCalledOnce();
        expect(textarea()).toBeTruthy();
        expect(textarea()!.value).toBe('use the staging bucket');
    });
});
