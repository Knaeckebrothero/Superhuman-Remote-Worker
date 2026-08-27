import {computed, signal, ɵresolveComponentResources} from '@angular/core';
import {ComponentFixture, TestBed} from '@angular/core/testing';
import {TranslocoTestingModule} from '@jsverse/transloco';
import {afterEach, beforeAll, describe, expect, it, vi} from 'vitest';
import {Job} from '../../core/models/api.model';
import {ToolCardView} from '../../core/models/tool-card.model';
import {ApiService} from '../../core/services/api.service';
import {JobWatchService} from '../../core/services/job-watch.service';
import {JobBatchCardComponent} from './job-batch-card.component';

/**
 * The fan-out card. What matters here is the header staying honest about live
 * state and a failed dispatch not rendering as an empty panel.
 *
 * The child panel is stubbed: its `entity` is a REQUIRED SIGNAL input read in a
 * constructor `effect()`, and this pipeline drops signal-input metadata, so the
 * real one throws NG0950 and takes this render down with it. See the stub's
 * docstring; the panel has its own spec and a k3d gate.
 *
 * Design: knowledge-base/knowledge/features/unified_tool_cards.md (slice 4, batch grouping).
 */
vi.mock('./job-tool-card-panel.component', () => import('./job-tool-card-panel.stub'));

function view(jobId: string | null, subtitle = 'do the thing', error?: string): ToolCardView {
    return {
        tool: 'create_job',
        title: 'Schedule job',
        icon: 'rocket_launch',
        subtitle,
        status: error ? 'error' : 'ok',
        params: [],
        details: [],
        error,
        entity: jobId ? {kind: 'job', id: jobId} : undefined,
    } as ToolCardView;
}

class FakeWatcher {
    private readonly rows = signal(new Map<string, Job>());
    readonly snapshot = computed(() => this.rows());
    readonly watch = vi.fn();
    readonly refresh = vi.fn(async () => {});
    job(id: string): Job | null {
        return this.rows().get(id) ?? null;
    }
    put(id: string, status: string): void {
        this.rows.update((m) => new Map(m).set(id, {id, status} as unknown as Job));
    }
}

describe('JobBatchCardComponent', () => {
    let watcher: FakeWatcher;
    let fixture: ComponentFixture<JobBatchCardComponent>;

    beforeAll(async () => {
        await ɵresolveComponentResources(() => Promise.resolve(''));
    });

    afterEach(() => TestBed.resetTestingModule());

    async function render(views: ToolCardView[], rows: Array<[string, string]> = []) {
        watcher = new FakeWatcher();
        for (const [id, status] of rows) watcher.put(id, status);

        TestBed.configureTestingModule({
            imports: [
                JobBatchCardComponent,
                TranslocoTestingModule.forRoot({
                    langs: {
                        en: {
                            toolCard: {
                                jobBatch: {
                                    title: '{{count}} jobs dispatched',
                                    finished: '{{done}}/{{total}} finished',
                                    failed: '{{count}} failed',
                                    review: '{{count}} awaiting review',
                                    notCreated: 'Job was not created',
                                },
                                job: {approve: 'Approve', cancel: 'Cancel job', openDiff: 'Open diff'},
                            },
                        },
                    },
                    translocoConfig: {availableLangs: ['en'], defaultLang: 'en'},
                }),
            ],
            providers: [
                {provide: JobWatchService, useValue: watcher},
                {provide: ApiService, useValue: {resumeJob: vi.fn(), approveJob: vi.fn(), cancelJob: vi.fn()}},
            ],
        });
        await TestBed.compileComponents();
        fixture = TestBed.createComponent(JobBatchCardComponent);
        // Assigned, not setInput — this pipeline drops signal-input metadata.
        // See job-tool-card-panel.spec.ts.
        (fixture.componentInstance as unknown as {views: () => ToolCardView[]}).views =
            signal<ToolCardView[]>(views);
        fixture.detectChanges();
        await fixture.whenStable();
        fixture.detectChanges();
    }

    const root = () => fixture.nativeElement as HTMLElement;
    const text = (sel: string) => root().querySelector(sel)?.textContent?.trim();
    const rows = () => root().querySelectorAll('.jb__row');

    it('counts only jobs it has actually seen', async () => {
        // b/c never polled -> null. The header must not claim they are done.
        await render([view('a'), view('b'), view('c')], [['a', 'completed']]);
        expect(text('.jb__title')).toBe('3 jobs dispatched');
        expect(text('.jb__meta')).toBe('1/3 finished');
        expect(root().querySelector('.jb__review')).toBeNull();
    });

    it('says finished, not done, and names the failures separately', async () => {
        // "2/2 done" for one failed and one cancelled job is a lie about the
        // outcome. Finished is true of all three terminal states; the failure
        // count carries the bad news instead of averaging it away.
        await render([view('a'), view('b')], [['a', 'failed'], ['b', 'cancelled']]);
        expect(text('.jb__meta')).toBe('2/2 finished');
        expect(text('.jb__failedChip')).toBe('1 failed');
    });

    it('calls out jobs waiting on the user', async () => {
        await render([view('a'), view('b')], [['a', 'pending_review'], ['b', 'completed']]);
        expect(text('.jb__review')).toBe('1 awaiting review');
        // pending_review is not terminal, so it is not counted as done.
        expect(text('.jb__meta')).toBe('1/2 finished');
    });

    it('renders one row per call, with the dispatch description', async () => {
        await render([view('a', 'survey the docs'), view('b', 'draft the brief')]);
        expect(rows()).toHaveLength(2);
        expect(root().querySelectorAll('.jb__label')[0].textContent?.trim()).toBe('survey the docs');
        expect(root().querySelectorAll('.jb__label')[1].textContent?.trim()).toBe('draft the brief');
    });

    it('says so when a dispatch never produced a job, instead of an empty panel', async () => {
        await render([view('a'), view(null, 'denied one', 'Grant denied')]);
        expect(root().querySelectorAll('app-job-tool-card-panel')).toHaveLength(1);
        expect(text('.jb__notCreated')).toContain('Grant denied');
    });

    it('falls back to a generic line when the failed call carries no error text', async () => {
        await render([view('a'), view(null, 'nameless')]);
        expect(text('.jb__notCreated')).toContain('Job was not created');
    });

    it('opens by default and collapses on demand — never the other way round', async () => {
        // Auto-collapsing would re-create the bug this card fixes: hiding a job
        // that is waiting on the user.
        await render([view('a'), view('b')], [['a', 'completed'], ['b', 'completed']]);
        expect(rows()).toHaveLength(2);
        expect(root().querySelector('.jb__head')?.getAttribute('aria-expanded')).toBe('true');

        (root().querySelector('.jb__head') as HTMLButtonElement).click();
        fixture.detectChanges();
        await fixture.whenStable();
        fixture.detectChanges();

        expect(rows()).toHaveLength(0);
        expect(root().querySelector('.jb__head')?.getAttribute('aria-expanded')).toBe('false');
    });
});
