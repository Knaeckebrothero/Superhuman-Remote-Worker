import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import {Injector, runInInjectionContext, signal} from '@angular/core';
import {of} from 'rxjs';

import {ActionCenterService} from './action-center.service';
import {NotificationService} from './notification.service';
import {
  EMPTY_NOTIFICATION_COUNTS,
  Notification,
  NotificationCounts,
} from '../models/notification.model';

/**
 * The action center over the unified feed (slice 3): the feed is the only
 * source of items, counts are the server's, the bell badge is `unseen`,
 * `seen` is batched, and the legacy email deep links resolve by source.
 * No TestBed — the real service runs over signal mocks.
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

function create(opts: {feed?: Notification[]; counts?: NotificationCounts} = {}) {
  const feed = signal<Notification[]>(opts.feed ?? []);
  const feedCounts = signal<NotificationCounts>(opts.counts ?? EMPTY_NOTIFICATION_COUNTS);
  const notificationsMock = {
    feed,
    feedCounts,
    feedNextBefore: signal<string | null>(null),
    loadNotifications: vi.fn(),
    loadMoreFeed: vi.fn(),
    listBySource: vi.fn().mockReturnValue(of(null)),
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
  // `inject(DestroyRef)` inside an R3Injector context resolves to the
  // injector itself (NG_ENV_ID), so destroying the injector is the hook.
  const injector = Injector.create({
    providers: [{provide: NotificationService, useValue: notificationsMock}],
  }) as Injector & {destroy(): void};
  const service = runInInjectionContext(injector, () => new ActionCenterService());
  return {service, notificationsMock, feed, injector};
}

describe('ActionCenterService — unified feed', () => {
  it('maps a feed row to an item with server identity and severity urgency', () => {
    const {service} = create({feed: [feedRow()]});
    const items = service.items();
    expect(items).toHaveLength(1);
    const item = items[0];
    expect(item.id).toBe('ntf:n-1');
    expect(item.category).toBe('review_queue');
    expect(item.status).toBe('pending');
    expect(item.urgency).toBe(45);
    expect(item.jobId).toBe(JOB_ID);
    expect(item.title).toContain('review required');
    expect(item.subtitle).toBe('worker_base · Publish the demo');
    expect(item.notification.actions.map((a) => a.type)).toEqual(['approve', 'open']);
  });

  it('sorts pending above resolved, then by severity, then newest first', () => {
    const {service} = create({
      feed: [
        feedRow({id: 'done', resolved_at: '2026-08-26T09:10:00Z'}),
        feedRow({id: 'low-new', severity: 'low', created_at: '2026-08-26T10:00:00Z'}),
        feedRow({id: 'crit', severity: 'critical', category: 'sudo_request', created_at: '2026-08-26T08:00:00Z'}),
        feedRow({id: 'low-old', severity: 'low', created_at: '2026-08-26T09:00:00Z'}),
      ],
    });
    expect(service.items().map((i) => i.id)).toEqual(['ntf:crit', 'ntf:low-new', 'ntf:low-old', 'ntf:done']);
    expect(service.items()[3].urgency).toBe(0);
  });

  it('items come from the feed alone — no legacy sudo/message/review/session join', () => {
    const {service, notificationsMock} = create({feed: [feedRow()]});
    expect(service.items().map((i) => i.id)).toEqual(['ntf:n-1']);
    service.refreshAll();
    expect(notificationsMock.loadNotifications).toHaveBeenCalledTimes(1);
  });

  it('initSSE opens only the notification stream and closes it on destroy', () => {
    const {service, notificationsMock, injector} = create();
    service.initSSE();
    service.initSSE();
    expect(notificationsMock.connectSSE).toHaveBeenCalledTimes(1);
    injector.destroy();
    expect(notificationsMock.disconnectSSE).toHaveBeenCalledTimes(1);
  });

  it('counts and the bell badge are the server\'s: pending, unseen, by_category', () => {
    const {service} = create({
      feed: [feedRow(), feedRow({id: 'n-2', seen_at: '2026-08-26T09:01:00Z'})],
      counts: {
        unseen: 1,
        unread: 2,
        pending: 7,
        by_category: {review_queue: {pending: 5, unseen: 1}, sudo_request: {pending: 2, unseen: 0}},
      },
    });
    const c = service.counts();
    expect(c.notifications).toBe(7);
    expect(c.total).toBe(7);
    expect(c.unseen).toBe(1);
    expect(c.byCategory['sudo_request'].pending).toBe(2);
    expect(service.badgeCount()).toBe(1);
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
      expect(service.items().find((i) => i.id === 'ntf:a')?.notification.seen_at).toBeTruthy();
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

  describe('fetchBySource (legacy email deep links)', () => {
    it('answers from the loaded page without a request', () => {
      const {service, notificationsMock} = create({
        feed: [feedRow({id: 's-1', category: 'sudo_request', source_ref: {kind: 'sudo_request', id: 'req-9'}})],
      });
      let row: Notification | null = null;
      service.fetchBySource({kind: 'sudo_request', id: 'req-9'}).subscribe((r) => (row = r));
      expect(row).toMatchObject({id: 's-1'});
      expect(notificationsMock.listBySource).not.toHaveBeenCalled();
    });

    it('asks the server for the newest row about the source and upserts it', () => {
      const {service, notificationsMock} = create({feed: []});
      notificationsMock.listBySource.mockReturnValue(
        of({
          items: [feedRow({id: 'p-1', category: 'officer_question', source_ref: {kind: 'thread', id: THREAD_ID}})],
          next_before: null,
          counts: EMPTY_NOTIFICATION_COUNTS,
        }),
      );
      let row: Notification | null = null;
      service.fetchBySource({kind: 'thread', id: THREAD_ID}).subscribe((r) => (row = r));
      expect(notificationsMock.listBySource).toHaveBeenCalledWith('thread', THREAD_ID, 1);
      expect(row).toMatchObject({id: 'p-1'});
      expect(service.items().map((i) => i.id)).toEqual(['ntf:p-1']);
    });

    it('yields null when nothing was ever recorded about the source', () => {
      const {service, notificationsMock} = create({feed: []});
      notificationsMock.listBySource.mockReturnValue(of({items: [], next_before: null, counts: EMPTY_NOTIFICATION_COUNTS}));
      let row: Notification | null | undefined;
      service.fetchBySource({kind: 'job', id: 'ghost'}).subscribe((r) => (row = r));
      expect(row).toBeNull();
      expect(service.items()).toHaveLength(0);
    });
  });
});
