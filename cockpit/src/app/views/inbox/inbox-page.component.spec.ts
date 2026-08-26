import {afterEach, describe, expect, it, vi} from 'vitest';
import {DestroyRef, Injector, NgZone, runInInjectionContext, signal} from '@angular/core';
import {ActivatedRoute, Router} from '@angular/router';
import {of} from 'rxjs';
import {TranslocoService} from '@jsverse/transloco';

import {InboxPageComponent} from './inbox-page.component';
import {ActionCenterService} from '../../core/services/action-center.service';
import {NotificationService} from '../../core/services/notification.service';
import {SudoService} from '../../core/services/sudo.service';
import {ApiService} from '../../core/services/api.service';
import {ViewportService} from '../../core/services/viewport.service';
import {AppNotification} from '../../core/models/api.model';
import {EMPTY_NOTIFICATION_COUNTS, Notification} from '../../core/models/notification.model';

/**
 * Officer-page card in the action center (F4 addendum,
 * knowledge-base/knowledge/issues/officer_conference_live_fire_findings.md): a page from a
 * persistent session has NO job behind it — the notification row is keyed by
 * the session thread UUID (REST rows carry job_id NULL, live SSE frames carry
 * job_id === thread_id). The card must render from the row alone, never fire
 * the job-scoped thread lookup (the 404 that blanked the pane), and route its
 * primary action to /sessions/{thread_id}.
 *
 * Construction mirrors automations-page.component.spec.ts: minimal injection
 * context, no TestBed.createComponent — the spec asserts the data the
 * template binds (title/preview/branch flag) and the component behavior;
 * the rendering path is exercised on the dev cluster.
 */

const THREAD_ID = 'a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d';

function makeNotification(overrides: Partial<AppNotification> = {}): AppNotification {
  return {
    id: 'n-1',
    job_id: null,
    thread_id: THREAD_ID,
    subject: 'Your centurion needs you',
    message: 'The promised durable direction still is not present.',
    job_description: '',
    config_name: null,
    status: 'sent',
    read_at: null,
    created_at: '2026-07-31T06:00:00Z',
    ...overrides,
  };
}

function feedRow(id: string): Notification {
  return {
    id,
    category: 'review_queue',
    severity: 'normal',
    subject: 'Job abc completed — review required',
    body: '**Job** …',
    source_ref: {kind: 'job', id: 'job-abc'},
    actions: [
      {type: 'approve', label_key: 'notifications.actions.approve', style: 'primary', params: {job_id: 'job-abc'}},
    ],
    payload: {job_id: 'job-abc', config_name: 'worker_base'},
    created_at: '2026-08-26T09:00:00Z',
    seen_at: null,
    read_at: null,
    interacted_at: null,
    resolved_at: null,
    resolved_by: null,
    archived_at: null,
  };
}

function createComponent(
  notifications: AppNotification[],
  queryParams: Record<string, string> = {},
  feed: Notification[] = [],
) {
  const sudoMock = {
    requests: signal([]),
    rules: signal([]),
    isConnected: signal(false),
    connectSSE: vi.fn(),
    disconnectSSE: vi.fn(),
    loadRequests: vi.fn(),
    loadRules: vi.fn(),
    approve: vi.fn(),
    deny: vi.fn(),
    approveVmUpgrade: vi.fn(),
    resumeWithoutVm: vi.fn(),
    createRule: vi.fn(),
    deleteRule: vi.fn(),
  };

  const feedSignal = signal<Notification[]>(feed);
  const notificationsMock = {
    notifications: signal<AppNotification[]>(notifications),
    sessionEvents: signal([]),
    feed: feedSignal,
    feedCounts: signal(EMPTY_NOTIFICATION_COUNTS),
    feedNextBefore: signal<string | null>(null),
    loadNotifications: vi.fn(),
    loadMoreFeed: vi.fn(),
    connectSSE: vi.fn(),
    disconnectSSE: vi.fn(),
    markRead: vi.fn(),
    markSeen: vi.fn().mockReturnValue(of({updated: []})),
    markReadV2: vi.fn().mockImplementation((id: string) =>
      of({notification: {...feedRow(id), read_at: '2026-08-26T09:05:00Z', seen_at: '2026-08-26T09:05:00Z'}}),
    ),
    act: vi.fn().mockReturnValue(of({status: 'ok', result: {}, notification: feedRow('n-1')})),
    getNotification: vi.fn().mockReturnValue(of(null)),
    patchFeedRow: vi.fn(),
    upsertFeedRow: vi.fn((row: Notification) => {
      feedSignal.update((rows) =>
        rows.some((r) => r.id === row.id) ? rows.map((r) => (r.id === row.id ? row : r)) : [row, ...rows],
      );
    }),
  };

  const apiMock = {
    getJobs: vi.fn().mockReturnValue(of([])),
    getThreadMessages: vi.fn().mockReturnValue(of({messages: []})),
    replyToThread: vi.fn().mockReturnValue(of({})),
    approveJob: vi.fn().mockReturnValue(of({})),
    resumeJob: vi.fn().mockReturnValue(of({})),
    upgradeJobToVm: vi.fn().mockReturnValue(of({})),
    getFrozenJobData: vi.fn().mockReturnValue(of(null)),
  };

  const baseInjector = Injector.create({
    providers: [
      {provide: SudoService, useValue: sudoMock},
      {provide: NotificationService, useValue: notificationsMock},
      {provide: ApiService, useValue: apiMock},
      {provide: DestroyRef, useValue: {onDestroy: vi.fn()}},
    ],
  });
  const actionCenter = runInInjectionContext(
    baseInjector,
    () => new ActionCenterService(),
  );

  const routerMock = {navigate: vi.fn()};
  const zoneMock = {
    run: (fn: () => unknown) => fn(),
    runOutsideAngular: (fn: () => unknown) => fn(),
  };

  const injector = Injector.create({
    providers: [
      {provide: ActionCenterService, useValue: actionCenter},
      {provide: SudoService, useValue: sudoMock},
      {provide: ApiService, useValue: apiMock},
      {provide: ActivatedRoute, useValue: {queryParams: of(queryParams)}},
      {provide: Router, useValue: routerMock},
      {provide: NgZone, useValue: zoneMock},
      {provide: TranslocoService, useValue: {translate: (key: string) => key, getActiveLang: () => 'en'}},
      {provide: ViewportService, useValue: {isMobile: signal(false)}},
    ],
  });

  const component = runInInjectionContext(injector, () => new InboxPageComponent());
  return {component, actionCenter, apiMock, routerMock, notificationsMock};
}

describe('InboxPageComponent officer-page card', () => {
  let cleanup: (() => void) | null = null;

  afterEach(() => {
    cleanup?.();
    cleanup = null;
  });

  it('maps a persisted officer page (job_id NULL) to a session-page card with subject and preview', () => {
    const {actionCenter} = createComponent([makeNotification()]);

    const items = actionCenter.items();
    expect(items).toHaveLength(1);
    const item = items[0];
    expect(item.type).toBe('message');
    expect(item.title).toBe('Your centurion needs you');
    expect(item.message?.lastMessage).toBe(
      'The promised durable direction still is not present.',
    );
    expect(item.message?.sessionThreadId).toBe(THREAD_ID);
    // No resolvable job — the card must not carry one.
    expect(item.jobId).toBeNull();
    // Converges with the email deep link ?job={thread}&thread={thread}.
    expect(item.id).toBe(`msg:${THREAD_ID}:${THREAD_ID}`);
  });

  it('maps the live SSE variant (job_id === thread_id) to the same card', () => {
    const {actionCenter} = createComponent([
      makeNotification({job_id: THREAD_ID, message: ''}),
    ]);

    const item = actionCenter.items()[0];
    expect(item.message?.sessionThreadId).toBe(THREAD_ID);
    expect(item.jobId).toBeNull();
    expect(item.id).toBe(`msg:${THREAD_ID}:${THREAD_ID}`);
  });

  it('selecting the card does not fire the job thread lookup and does not blow up', () => {
    const {component, actionCenter, apiMock} = createComponent([makeNotification()]);
    cleanup = () => component.ngOnDestroy();
    component.ngOnInit();

    component.selectItem(actionCenter.items()[0]);

    expect(component.selectedItem()?.message?.sessionThreadId).toBe(THREAD_ID);
    // Today's bug: the pane tried GET /api/jobs/{thread_id}/threads/... and
    // blanked on the 404. With no jobId there is nothing to fetch.
    expect(apiMock.getThreadMessages).not.toHaveBeenCalled();
    expect(component.threadLoading()).toBe(false);
  });

  it('routes the primary action to /sessions/{thread_id}', () => {
    const {component, actionCenter, routerMock} = createComponent([makeNotification()]);
    cleanup = () => component.ngOnDestroy();
    component.ngOnInit();
    component.selectItem(actionCenter.items()[0]);

    component.goToSession(component.selectedItem()!.message!.sessionThreadId!);

    expect(routerMock.navigate).toHaveBeenCalledWith(['/sessions', THREAD_ID]);
  });

  it('resolves the email deep link ?job={thread}&thread={thread} to the card', () => {
    const {component} = createComponent([makeNotification()], {
      job: THREAD_ID,
      thread: THREAD_ID,
    });
    cleanup = () => component.ngOnDestroy();

    component.ngOnInit();

    expect(component.selectedItem()?.id).toBe(`msg:${THREAD_ID}:${THREAD_ID}`);
    expect(component.selectedItem()?.message?.sessionThreadId).toBe(THREAD_ID);
  });

  it('keeps job-keyed notifications on the regular message path', () => {
    const jobId = 'f0e1d2c3-b4a5-4697-8877-665544332211';
    const {component, actionCenter, apiMock} = createComponent([
      makeNotification({job_id: jobId, thread_id: 'ab12cd', config_name: 'scholar'}),
    ]);
    cleanup = () => component.ngOnDestroy();
    component.ngOnInit();

    const item = actionCenter.items()[0];
    expect(item.jobId).toBe(jobId);
    expect(item.message?.sessionThreadId).toBeNull();
    expect(item.id).toBe(`msg:${jobId}:ab12cd`);

    component.selectItem(item);
    expect(apiMock.getThreadMessages).toHaveBeenCalledWith(jobId, 'ab12cd');
  });

  it('still hides job-less rows whose thread key is not a session UUID', () => {
    const {actionCenter} = createComponent([
      makeNotification({thread_id: 'loop-ab12cd'}),
    ]);

    expect(actionCenter.items()).toHaveLength(0);
  });
});

describe('InboxPageComponent unified feed (slice 1)', () => {
  let cleanup: (() => void) | null = null;

  afterEach(() => {
    cleanup?.();
    cleanup = null;
  });

  it('a feed row renders as a notification item and selecting it marks it read', () => {
    const {component, actionCenter, notificationsMock} = createComponent([], {}, [feedRow('n-1')]);
    cleanup = () => component.ngOnDestroy();
    component.ngOnInit();

    const item = actionCenter.items()[0];
    expect(item.type).toBe('notification');
    expect(item.id).toBe('ntf:n-1');

    component.selectItem(item);
    expect(component.selectedItem()?.id).toBe('ntf:n-1');
    expect(notificationsMock.markReadV2).toHaveBeenCalledWith('n-1');
    // The pane owns its source fetch; the legacy job/thread lookups stay quiet.
    expect(component.threadLoading()).toBe(false);
    expect(component.reviewLoading()).toBe(false);
  });

  it('resolves the email deep link ?n=<id> to the feed row', () => {
    const {component} = createComponent([], {n: 'n-1'}, [feedRow('n-1')]);
    cleanup = () => component.ngOnDestroy();

    component.ngOnInit();

    expect(component.selectedItem()?.id).toBe('ntf:n-1');
  });

  it('fetches a deep-linked row that is not in the loaded page', () => {
    const {component, notificationsMock} = createComponent([], {n: 'deep'}, []);
    notificationsMock.getNotification.mockReturnValue(of({notification: feedRow('deep'), source: null}));
    cleanup = () => component.ngOnDestroy();

    component.ngOnInit();

    expect(notificationsMock.getNotification).toHaveBeenCalledWith('deep');
    expect(component.selectedItem()?.id).toBe('ntf:deep');
  });

  it('the notification filter and its keyboard shortcut narrow the list', () => {
    const {component} = createComponent([makeNotification()], {}, [feedRow('n-1')]);
    cleanup = () => component.ngOnDestroy();
    component.ngOnInit();

    expect(component.filteredItems()).toHaveLength(2);
    component.setFilter('notification');
    expect(component.filteredItems().map((i) => i.id)).toEqual(['ntf:n-1']);
  });

  it('severity drives the urgency bar colour', () => {
    const {component, actionCenter} = createComponent([], {}, [
      feedRow('crit'),
    ]);
    const item = {...actionCenter.items()[0]};
    item.notification = {...item.notification!, severity: 'critical'};
    expect(component.urgencyColor(item)).toBe('red');
    item.notification = {...item.notification, severity: 'low'};
    expect(component.urgencyColor(item)).toBe('muted');
    item.status = 'resolved';
    expect(component.urgencyColor(item)).toBe('muted');
  });
});
