import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import {DestroyRef, Injector, runInInjectionContext, signal} from '@angular/core';
import {of} from 'rxjs';

import {ActionCenterService} from './action-center.service';
import {NotificationService} from './notification.service';
import {SudoService} from './sudo.service';
import {ApiService} from './api.service';
import {AppNotification, Job} from '../models/api.model';
import {
  EMPTY_NOTIFICATION_COUNTS,
  Notification,
  NotificationCounts,
} from '../models/notification.model';

/**
 * The action center over the unified feed (slice 1): feed rows become
 * first-class items, a feed row hides the legacy twin of the same source,
 * `seen` is batched, and the bell badge is unseen-driven. No TestBed —
 * the real service runs over signal mocks, like inbox-page.component.spec.
 */

const THREAD_ID = 'a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d';
const JOB_ID = 'f0e1d2c3-b4a5-4697-8877-665544332211';

function feedRow(overrides: Partial<Notification> = {}): Notification {
  return {
    id: 'n-1',
    category: 'review_queue',
    severity: 'normal',
    subject: 'Job f0e1d2c3 completed — review required',
    body: '**Job** …',
    source_ref: {kind: 'job', id: JOB_ID},
    actions: [
      {type: 'approve', label_key: 'notifications.actions.approve', style: 'primary', params: {job_id: JOB_ID}},
      {type: 'open', label_key: 'notifications.actions.openJob', style: 'default', params: {job_id: JOB_ID}},
    ],
    payload: {job_id: JOB_ID, config_name: 'worker_base', job_description: 'Publish the demo'},
    created_at: '2026-08-26T09:00:00Z',
    seen_at: null,
    read_at: null,
    interacted_at: null,
    resolved_at: null,
    resolved_by: null,
    archived_at: null,
    ...overrides,
  };
}

function legacyMessage(overrides: Partial<AppNotification> = {}): AppNotification {
  return {
    id: 'm-1',
    job_id: null,
    thread_id: THREAD_ID,
    subject: 'Your centurion needs you',
    message: 'old ring row',
    job_description: '',
    config_name: null,
    status: 'sent',
    read_at: null,
    created_at: '2026-08-25T06:00:00Z',
    ...overrides,
  };
}

function create(opts: {
  feed?: Notification[];
  counts?: NotificationCounts;
  legacy?: AppNotification[];
  reviewJobs?: Job[];
} = {}) {
  const feed = signal<Notification[]>(opts.feed ?? []);
  const feedCounts = signal<NotificationCounts>(opts.counts ?? EMPTY_NOTIFICATION_COUNTS);
  const notificationsMock = {
    feed,
    feedCounts,
    feedNextBefore: signal<string | null>(null),
    notifications: signal<AppNotification[]>(opts.legacy ?? []),
    sessionEvents: signal([]),
    loadNotifications: vi.fn(),
    loadMoreFeed: vi.fn(),
    connectSSE: vi.fn(),
    disconnectSSE: vi.fn(),
    markSeen: vi.fn().mockReturnValue(of({updated: []})),
    markReadV2: vi.fn().mockImplementation((id: string) =>
      of({notification: feedRow({id, read_at: '2026-08-26T09:05:00Z', seen_at: '2026-08-26T09:05:00Z'})}),
    ),
    act: vi.fn().mockReturnValue(
      of({status: 'ok', result: {navigate: `/jobs/${JOB_ID}`}, notification: feedRow({resolved_at: '2026-08-26T09:06:00Z'})}),
    ),
    getNotification: vi.fn().mockReturnValue(of(null)),
    patchFeedRow: vi.fn((update: {id: string; seen_at?: string | null}) => {
      feed.update((rows) => rows.map((r) => (r.id === update.id ? {...r, ...update} : r)));
    }),
    upsertFeedRow: vi.fn((row: Notification) => {
      feed.update((rows) =>
        rows.some((r) => r.id === row.id) ? rows.map((r) => (r.id === row.id ? row : r)) : [row, ...rows],
      );
    }),
  };
  const sudoMock = {
    requests: signal([]),
    connectSSE: vi.fn(),
    disconnectSSE: vi.fn(),
    loadRequests: vi.fn(),
  };
  const apiMock = {
    getJobs: vi.fn().mockReturnValue(of(opts.reviewJobs ?? [])),
  };
  const injector = Injector.create({
    providers: [
      {provide: SudoService, useValue: sudoMock},
      {provide: NotificationService, useValue: notificationsMock},
      {provide: ApiService, useValue: apiMock},
      {provide: DestroyRef, useValue: {onDestroy: vi.fn()}},
    ],
  });
  const service = runInInjectionContext(injector, () => new ActionCenterService());
  return {service, notificationsMock, apiMock, feed};
}

describe('ActionCenterService — unified feed', () => {
  it('maps a feed row to a first-class item with server identity and severity urgency', () => {
    const {service} = create({feed: [feedRow()]});
    const items = service.items();
    expect(items).toHaveLength(1);
    const item = items[0];
    expect(item.id).toBe('ntf:n-1');
    expect(item.type).toBe('notification');
    expect(item.category).toBe('review_queue');
    expect(item.status).toBe('pending');
    expect(item.urgency).toBe(45);
    expect(item.jobId).toBe(JOB_ID);
    expect(item.title).toContain('review required');
    expect(item.subtitle).toBe('worker_base · Publish the demo');
    expect(item.notification?.actions.map((a) => a.type)).toEqual(['approve', 'open']);
  });

  it('a resolved row sorts below pending ones and carries no urgency', () => {
    const {service} = create({
      feed: [feedRow({id: 'done', resolved_at: '2026-08-26T09:10:00Z'}), feedRow({id: 'open', severity: 'low'})],
    });
    const [first, second] = service.items();
    expect(first.id).toBe('ntf:open');
    expect(second.id).toBe('ntf:done');
    expect(second.status).toBe('resolved');
    expect(second.urgency).toBe(0);
  });

  it('hides the legacy review twin of a job the feed already covers', () => {
    const job = {id: JOB_ID, status: 'pending_review', description: 'd', config_name: 'c', created_at: 'x'} as unknown as Job;
    const other = {id: 'other-job', status: 'pending_review', description: 'e', config_name: 'c', created_at: 'x'} as unknown as Job;
    const {service} = create({feed: [feedRow()], reviewJobs: [job, other]});
    service.loadReviewJobs();
    const ids = service.items().map((i) => i.id);
    expect(ids).toContain('ntf:n-1');
    expect(ids).toContain('rev:other-job');
    expect(ids).not.toContain(`rev:${JOB_ID}`);
  });

  it('hides the legacy officer session-page row when the feed carries the thread', () => {
    const {service} = create({
      feed: [feedRow({id: 'p-1', category: 'officer_question', source_ref: {kind: 'thread', id: THREAD_ID}, payload: {}})],
      legacy: [legacyMessage()],
    });
    const ids = service.items().map((i) => i.id);
    expect(ids).toEqual(['ntf:p-1']);
  });

  it('keeps legacy items whose source the feed does not cover', () => {
    const {service} = create({feed: [feedRow()], legacy: [legacyMessage()]});
    const ids = service.items().map((i) => i.id);
    expect(ids).toContain('ntf:n-1');
    expect(ids).toContain(`msg:${THREAD_ID}:${THREAD_ID}`);
  });

  it('counts feed items and exposes the server unseen count; the badge adds legacy pending', () => {
    const {service} = create({
      feed: [feedRow(), feedRow({id: 'n-2', seen_at: '2026-08-26T09:01:00Z'})],
      counts: {...EMPTY_NOTIFICATION_COUNTS, unseen: 1, unread: 2, pending: 2},
      legacy: [legacyMessage()],
    });
    const c = service.counts();
    expect(c.notifications).toBe(2);
    expect(c.messages).toBe(1);
    expect(c.unseen).toBe(1);
    expect(c.total).toBe(3);
    // 1 unseen feed row + 1 pending legacy message; the seen feed row no longer nags.
    expect(service.badgeCount()).toBe(2);
  });

  describe('seen batching', () => {
    beforeEach(() => vi.useFakeTimers());
    afterEach(() => vi.useRealTimers());

    it('collapses N rendered rows into one POST after the debounce and stamps optimistically', () => {
      const {service, notificationsMock} = create({
        feed: [feedRow({id: 'a'}), feedRow({id: 'b'}), feedRow({id: 'c', seen_at: '2026-08-26T09:00:00Z'})],
      });
      service.noteSeen('a');
      service.noteSeen('b');
      service.noteSeen('b'); // duplicate
      service.noteSeen('c'); // already seen — skipped
      expect(notificationsMock.markSeen).not.toHaveBeenCalled();

      vi.advanceTimersByTime(800);

      expect(notificationsMock.markSeen).toHaveBeenCalledTimes(1);
      expect(notificationsMock.markSeen.mock.calls[0][0].sort()).toEqual(['a', 'b']);
      expect(notificationsMock.patchFeedRow).toHaveBeenCalledTimes(2);
      expect(service.items().find((i) => i.id === 'ntf:a')?.notification?.seen_at).toBeTruthy();
    });
  });

  it('markRead posts once and upserts the returned row; read rows are left alone', () => {
    const {service, notificationsMock} = create({
      feed: [feedRow({id: 'r-1'}), feedRow({id: 'r-2', read_at: '2026-08-26T08:00:00Z', seen_at: '2026-08-26T08:00:00Z'})],
    });
    service.markRead('r-1');
    service.markRead('r-2');
    expect(notificationsMock.markReadV2).toHaveBeenCalledTimes(1);
    expect(notificationsMock.markReadV2).toHaveBeenCalledWith('r-1');
    expect(notificationsMock.upsertFeedRow).toHaveBeenCalledTimes(1);
  });

  it('act posts {action_type, params} and replaces the row from the response', () => {
    const {service, notificationsMock} = create({feed: [feedRow()]});
    let result: unknown = null;
    service.act('n-1', 'approve', {notes: 'ok'}).subscribe((r) => (result = r));
    expect(notificationsMock.act).toHaveBeenCalledWith('n-1', 'approve', {notes: 'ok'});
    expect(notificationsMock.upsertFeedRow).toHaveBeenCalledTimes(1);
    expect((result as {result: {navigate: string}}).result.navigate).toBe(`/jobs/${JOB_ID}`);
    expect(service.items()[0].status).toBe('resolved');
  });

  it('fetchNotification upserts a row that was not in the loaded page', () => {
    const {service, notificationsMock} = create({feed: []});
    notificationsMock.getNotification.mockReturnValue(of({notification: feedRow({id: 'deep'}), source: null}));
    let row: Notification | null = null;
    service.fetchNotification('deep').subscribe((r) => (row = r));
    expect(row).not.toBeNull();
    expect(service.items().map((i) => i.id)).toEqual(['ntf:deep']);
  });
});
