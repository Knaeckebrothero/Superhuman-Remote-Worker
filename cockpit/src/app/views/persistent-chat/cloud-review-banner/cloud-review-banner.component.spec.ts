import { readFileSync } from 'node:fs';
import {
  Component,
  EventEmitter,
  Input,
  Output,
  ɵresolveComponentResources,
} from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { TranslocoPipe, TranslocoTestingModule } from '@jsverse/transloco';
import { afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';

import en from '../../../../assets/i18n/en.json';
import { writeReceipt } from '../../job-diff-review/cloud-review-receipt';
import {
  CloudReviewBannerComponent,
  cloudReviewBannerMode,
  cloudReviewBannerVisible,
} from './cloud-review-banner.component';

@Component({
  selector: 'app-button',
  standalone: true,
  template: '<button type="button" (click)="clicked.emit()"><ng-content /></button>',
})
class ButtonStub {
  @Input() variant = '';
  @Input() size = '';
  @Output() readonly clicked = new EventEmitter<void>();
}

@Component({ selector: 'app-icon', standalone: true, template: '<ng-content />' })
class IconStub {
  @Input() size = '';
}

describe('cloudReviewBannerVisible', () => {
  it('shows when the thread is protected and something is staged', () => {
    expect(cloudReviewBannerVisible(true, 3)).toBe(true);
  });

  it('hides when the thread is not protected, even with a nonzero count', () => {
    // Guards against a stale count surviving a switch to an unprotected thread.
    expect(cloudReviewBannerVisible(false, 3)).toBe(false);
  });

  it('hides when protected but nothing is staged yet', () => {
    expect(cloudReviewBannerVisible(true, 0)).toBe(false);
  });
});

describe('cloudReviewBannerMode', () => {
  const base = { protectedCloud: true, count: 0, probe: 'ready' as const, hasReceipt: false };

  it('is hidden for an unprotected thread whatever else is true', () => {
    expect(cloudReviewBannerMode({ ...base, protectedCloud: false, count: 4 })).toBe('hidden');
    expect(cloudReviewBannerMode({ ...base, protectedCloud: false, probe: 'error' })).toBe(
      'hidden',
    );
  });

  it('is pending whenever something is staged', () => {
    expect(cloudReviewBannerMode({ ...base, count: 1 })).toBe('pending');
  });

  it('says so when the count could not be checked, instead of showing nothing', () => {
    // The gap this closes: one failed probe on load left a protected ended
    // session with no entry point to the review and no way to ask again.
    expect(cloudReviewBannerMode({ ...base, probe: 'error' })).toBe('unknown');
  });

  it('never claims changes exist while the answer is unknown', () => {
    expect(cloudReviewBannerMode({ ...base, probe: 'error', count: 0 })).not.toBe('pending');
  });

  it('prefers the unanswered question to a stale receipt', () => {
    expect(cloudReviewBannerMode({ ...base, probe: 'error', hasReceipt: true })).toBe('unknown');
  });

  it('offers the last result only once a successful check found nothing pending', () => {
    expect(cloudReviewBannerMode({ ...base, hasReceipt: true })).toBe('receipt');
    expect(cloudReviewBannerMode({ ...base, hasReceipt: true, count: 2 })).toBe('pending');
    expect(cloudReviewBannerMode({ ...base, hasReceipt: true, probe: 'loading' })).toBe('hidden');
  });

  it('shows nothing while the first check is still in flight', () => {
    expect(cloudReviewBannerMode({ ...base, probe: 'loading' })).toBe('hidden');
    expect(cloudReviewBannerMode({ ...base, probe: 'idle' })).toBe('hidden');
  });
});

describe('CloudReviewBannerComponent', () => {
  let fixture: ComponentFixture<CloudReviewBannerComponent>;

  beforeAll(async () => {
    await ɵresolveComponentResources(() => Promise.resolve(''));
  });

  beforeEach(() => localStorage.clear());
  afterEach(() => {
    localStorage.clear();
    TestBed.resetTestingModule();
  });

  async function render(inputs: Record<string, unknown>): Promise<HTMLElement> {
    TestBed.configureTestingModule({
      imports: [
        CloudReviewBannerComponent,
        TranslocoTestingModule.forRoot({
          langs: { en },
          translocoConfig: { availableLangs: ['en'], defaultLang: 'en' },
        }),
      ],
    });
    TestBed.overrideComponent(CloudReviewBannerComponent, {
      // styleUrl cleared: overrideComponent otherwise re-queues it as a
      // pending resource and compileComponents() rejects the component.
      set: { styleUrl: undefined, styles: [''], imports: [TranslocoPipe, ButtonStub, IconStub] },
    });
    await TestBed.compileComponents();
    fixture = TestBed.createComponent(CloudReviewBannerComponent);
    // Assigned rather than setInput() — this vitest pipeline drops
    // signal-input metadata (see job-tool-card-panel.spec.ts).
    const inst = fixture.componentInstance as unknown as Record<string, unknown>;
    for (const [k, v] of Object.entries(inputs)) inst[k] = () => v;
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  const text = (root: HTMLElement) => (root.textContent ?? '').replace(/\s+/g, ' ').trim();

  it('renders a labelled, actionable call to action when changes are staged', async () => {
    const root = await render({
      protectedCloud: true,
      count: 4,
      probe: 'ready',
      folderName: 'Protected Docs',
      stagedAt: '2026-08-24T09:18:00.000Z',
    });
    expect(root.querySelector('[role="region"]')?.getAttribute('aria-label')).toBe(
      'Pending cloud review',
    );
    expect(text(root)).toContain('4 cloud changes are waiting for your review');
    expect(text(root)).toContain('Nothing has been written to your cloud yet');
    // The project's own name, not the workspace mount path — which is the
    // literal string "cloud" and names nothing a user recognises.
    expect(text(root)).toContain('Protected Docs');
    // A real button, not a role="button" badge with no tabindex.
    const action = root.querySelector<HTMLButtonElement>('button')!;
    expect(action.textContent?.trim()).toBe('Review changes');
  });

  it('uses the singular form for one change', async () => {
    const root = await render({ protectedCloud: true, count: 1, folderName: null, stagedAt: null });
    expect(text(root)).toContain('1 cloud change is waiting for your review');
  });

  it('emits a review request when activated', async () => {
    const root = await render({ protectedCloud: true, count: 2, folderName: null, stagedAt: null });
    let asked = 0;
    (fixture.componentInstance as unknown as { review: { subscribe(f: () => void): void } })
      .review.subscribe(() => asked++);
    root.querySelector<HTMLButtonElement>('button')!.click();
    expect(asked).toBe(1);
  });

  it('renders nothing when the thread is unprotected or nothing is staged', async () => {
    expect(text(await render({ protectedCloud: false, count: 4 }))).toBe('');
    TestBed.resetTestingModule();
    expect(text(await render({ protectedCloud: true, count: 0 }))).toBe('');
  });

  it('names the failure and keeps a way back in when the check failed', async () => {
    const root = await render({ protectedCloud: true, count: 0, probe: 'error' });
    expect(text(root)).toContain("Couldn't check for pending cloud changes");
    // It must not claim changes exist — nobody knows — but it must not be a
    // dead end either.
    expect(text(root)).not.toContain('waiting for your review');
    const labels = [...root.querySelectorAll('button')].map((b) => b.textContent?.trim());
    expect(labels).toEqual(['Check again', 'Open review']);
  });

  it('re-probes on demand rather than stranding the session', async () => {
    const root = await render({ protectedCloud: true, count: 0, probe: 'error' });
    let rechecks = 0;
    (
      fixture.componentInstance as unknown as { recheck: { subscribe(f: () => void): void } }
    ).recheck.subscribe(() => rechecks++);
    root.querySelector<HTMLButtonElement>('button')!.click();
    expect(rechecks).toBe(1);
  });

  it('offers the browser-local result once nothing is pending', async () => {
    // Without this the cached record had no entry point at all after a
    // decision, which made keeping it nearly pointless.
    writeReceipt('t1', {
      decision: 'applied',
      epoch: 5,
      applied: 3,
      deleted: 1,
      overlayReset: true,
      at: new Date(Date.now() - 60_000).toISOString(),
    });
    const root = await render({ protectedCloud: true, count: 0, probe: 'ready', threadId: 't1' });
    expect(text(root)).toContain('Last cloud review: applied');
    expect(text(root)).toContain('3 written, 1 deleted');
    // Labelled for what it is. PC-20 is not resolved by a browser cache.
    expect(text(root)).toContain('this browser only');
    expect(root.querySelector<HTMLButtonElement>('button')?.textContent?.trim()).toBe(
      'View result',
    );
  });

  it('keeps a pending diff in front of an older stored result', async () => {
    writeReceipt('t1', {
      decision: 'applied',
      epoch: 5,
      applied: 3,
      deleted: 1,
      overlayReset: true,
      at: new Date(Date.now() - 60_000).toISOString(),
    });
    const root = await render({ protectedCloud: true, count: 2, probe: 'ready', threadId: 't1' });
    expect(text(root)).toContain('waiting for your review');
    expect(text(root)).not.toContain('Last cloud review');
  });

  it('takes no connection input at all', () => {
    // PC-25 structurally: there is no way to gate this on isConnected(),
    // because the review API serves ended threads and the pending decision
    // must outlive the agent.
    const src = readFileSync(
      'src/app/views/persistent-chat/cloud-review-banner/cloud-review-banner.component.ts',
      'utf8',
    );
    const inputs = [...src.matchAll(/^\s{2}(\w+) = input</gm)].map((m) => m[1]);
    expect(inputs).not.toContain('isConnected');
    expect(inputs).not.toContain('connected');
    expect(inputs).toEqual([
      'protectedCloud',
      'count',
      'probe',
      'folderName',
      'stagedAt',
      'threadId',
    ]);
  });

  it('puts the explanation before the action on a phone', () => {
    // The previous rule gave the body order:2 while icon and button both sat
    // at 0, which put the Review button ABOVE the sentence explaining it.
    const styles = readFileSync(
      'src/app/views/persistent-chat/cloud-review-banner/cloud-review-banner.component.scss',
      'utf8',
    );
    const small = styles.slice(styles.indexOf('breakpoint-down(sm)'));
    const body = small.indexOf('.crb__body');
    const actions = small.indexOf('.crb__actions');
    expect(body).toBeGreaterThan(-1);
    expect(actions).toBeGreaterThan(body);
    expect(small.slice(body, actions)).toContain('order: 1');
    expect(small.slice(actions)).toContain('order: 2');
  });

  it('keeps body copy off the muted token, which fails AA over the tint', () => {
    const styles = readFileSync(
      'src/app/views/persistent-chat/cloud-review-banner/cloud-review-banner.component.scss',
      'utf8',
    );
    expect(styles).toContain('background: var(--warning-tint)');
    expect(styles).toContain('color: var(--text-primary)');
  });
});
