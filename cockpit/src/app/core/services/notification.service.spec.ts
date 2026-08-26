import {describe, expect, it, vi} from 'vitest';
import {Injector, NgZone, runInInjectionContext} from '@angular/core';
import {HttpClient} from '@angular/common/http';
import {TranslocoService} from '@jsverse/transloco';
import {of} from 'rxjs';
import {NotificationService} from './notification.service';
import {AppToastService} from '../../ui/toast';

function createService() {
  const http = {
    get: vi.fn().mockReturnValue(of({notifications: [], unread_count: 0})),
    patch: vi.fn().mockReturnValue(of({status: 'ok'})),
    post: vi.fn().mockReturnValue(of({})),
  };
  const toast = {info: vi.fn(), warning: vi.fn(), success: vi.fn(), danger: vi.fn()};
  const transloco = {translate: vi.fn((key: string) => key)};

  const injector = Injector.create({
    providers: [
      {provide: HttpClient, useValue: http},
      {provide: AppToastService, useValue: toast},
      {provide: TranslocoService, useValue: transloco},
      {provide: NgZone, useValue: {run: (fn: () => void) => fn()}},
    ],
  });

  const service = runInInjectionContext(injector, () => new NotificationService());
  return {service, http, toast, transloco};
}

describe('NotificationService.handleSseEvent', () => {
  it('user_registered → info toast + adminUserRegistered signal', () => {
    const {service, toast, transloco} = createService();

    service.handleSseEvent({
      type: 'user_registered',
      user_id: 'u-1',
      display_name: 'pending4',
      email: 'p4@test.local',
    });

    expect(service.adminUserRegistered()).toEqual({
      user_id: 'u-1',
      display_name: 'pending4',
      email: 'p4@test.local',
    });
    expect(transloco.translate).toHaveBeenCalledWith('toasts.admin.userRegistered', {
      name: 'pending4',
    });
    expect(toast.info).toHaveBeenCalledOnce();
  });

  it('user_registered falls back to email when display_name missing', () => {
    const {service, transloco} = createService();

    service.handleSseEvent({type: 'user_registered', user_id: 'u-2', email: 'x@y.z'});

    expect(transloco.translate).toHaveBeenCalledWith('toasts.admin.userRegistered', {
      name: 'x@y.z',
    });
  });

  it('automation_auto_disabled → warning toast', () => {
    const {service, toast, transloco} = createService();

    service.handleSseEvent({
      type: 'automation_auto_disabled',
      automation_id: 'a-1',
      automation_name: 'Nightly digest',
      reason: 'max_fires_per_day',
    });

    expect(transloco.translate).toHaveBeenCalledWith('toasts.automations.autoDisabled', {
      name: 'Nightly digest',
    });
    expect(toast.warning).toHaveBeenCalledOnce();
  });

  it('new_message still increments unread count (extraction regression guard)', () => {
    const {service, toast} = createService();

    service.handleSseEvent({type: 'new_message', id: 'n-1', job_id: 'j-1', subject: 'Hi'});

    expect(service.unreadCount()).toBe(1);
    expect(service.notifications()).toHaveLength(1);
    expect(toast.info).not.toHaveBeenCalled();
  });

  it('unknown frame types are ignored without toasting', () => {
    const {service, toast} = createService();

    service.handleSseEvent({type: 'something_else', foo: 'bar'});

    expect(toast.info).not.toHaveBeenCalled();
    expect(toast.warning).not.toHaveBeenCalled();
    expect(service.adminUserRegistered()).toBeNull();
  });

  it('loop_user_question → bell entry with the server thread key + info toast', () => {
    const {service, toast} = createService();

    service.handleSseEvent({
      type: 'loop_user_question',
      loop_id: '942ef046-1234-5678-9abc-def012345678',
      project_id: 'p-1',
      job_id: 'j-1',
      subject: 'Loop question: Should the dice roller support D20 notation?',
      message: 'A loop agent filed a question for you.',
    });

    expect(service.unreadCount()).toBe(1);
    expect(service.notifications()).toHaveLength(1);
    const n = service.notifications()[0];
    // Mirrors the persisted message_log row's thread key ("loop-" + 6 hex)
    // so a REST refresh dedupes against the live-prepended entry.
    expect(n.thread_id).toBe('loop-942ef0');
    expect(n.job_id).toBe('j-1');
    expect(n.subject).toContain('D20');
    expect(toast.info).toHaveBeenCalledWith(
      'Loop question: Should the dice roller support D20 notation?',
    );
  });

  it('loop_campaign_disposition → bell entry + toast', () => {
    const {service, toast} = createService();

    service.handleSseEvent({
      type: 'loop_campaign_disposition',
      loop_id: '942ef046-1234-5678-9abc-def012345678',
      job_id: 'j-2',
      subject: 'Loop campaign ship: Dice roller',
      message: "The critic disposed campaign 'Dice roller' as SHIP.",
    });

    expect(service.unreadCount()).toBe(1);
    expect(service.notifications()[0].message).toContain('SHIP');
    expect(toast.info).toHaveBeenCalledOnce();
  });

  it('loop event without loop_id still lands, with a null thread', () => {
    const {service} = createService();

    service.handleSseEvent({type: 'loop_user_question', subject: 'Q'});

    expect(service.notifications()[0].thread_id).toBeNull();
    expect(service.notifications()[0].job_id).toBeNull();
  });
});

describe('NotificationService — unified feed frames', () => {
  const row = {
    id: 'n-1',
    category: 'review_queue',
    severity: 'normal',
    subject: 's',
    body: 'b',
    source_ref: {kind: 'job', id: 'j-1'},
    actions: [],
    payload: {},
    created_at: '2026-08-26T09:00:00Z',
    seen_at: null,
    read_at: null,
    interacted_at: null,
    resolved_at: null,
    resolved_by: null,
    archived_at: null,
  };

  it('notification frame prepends the row once and bumps the counts', () => {
    const {service, toast} = createService();

    service.handleSseEvent({type: 'notification', notification: row});
    service.handleSseEvent({type: 'notification', notification: row}); // a replay never re-broadcasts, but be safe

    expect(service.feed().map((n) => n.id)).toEqual(['n-1']);
    expect(service.feedCounts()).toMatchObject({unseen: 1, unread: 1, pending: 1});
    expect(toast.info).not.toHaveBeenCalled();
  });

  it('notification.updated patches engagement and decrements the counts', () => {
    const {service} = createService();
    service.handleSseEvent({type: 'notification', notification: row});

    service.handleSseEvent({type: 'notification.updated', id: 'n-1', seen_at: '2026-08-26T09:01:00Z'});
    expect(service.feed()[0].seen_at).toBe('2026-08-26T09:01:00Z');
    expect(service.feedCounts().unseen).toBe(0);

    service.handleSseEvent({
      type: 'notification.updated',
      id: 'n-1',
      resolved_at: '2026-08-26T09:02:00Z',
      resolved_by: 'officer:t-1',
    });
    expect(service.feed()[0].resolved_by).toBe('officer:t-1');
    expect(service.feedCounts().pending).toBe(0);
    // Unknown id is ignored, not crashed on.
    service.handleSseEvent({type: 'notification.updated', id: 'ghost', read_at: 'x'});
    expect(service.feed()).toHaveLength(1);
  });

  it('a feed load carries both the feed page and the legacy rows', () => {
    const {service, http} = createService();
    http.get.mockReturnValue(
      of({
        items: [row],
        next_before: 'cursor',
        counts: {unseen: 1, unread: 1, pending: 1, by_category: {review_queue: {pending: 1, unseen: 1}}},
        notifications: [],
        unread_count: 0,
      }),
    );
    service.loadNotifications();
    expect(service.feed()).toHaveLength(1);
    expect(service.feedNextBefore()).toBe('cursor');
    expect(service.feedCounts().by_category['review_queue'].pending).toBe(1);
    expect(service.notifications()).toEqual([]);
  });

  it('session.lifecycle and user_registered still ride the same stream', () => {
    const {service, toast} = createService();
    service.handleSseEvent({type: 'session.lifecycle', thread_id: 't-1', state: 'booting', backend: 'vm'});
    expect(service.lifecycleEvent()).toEqual({thread_id: 't-1', state: 'booting', reason: undefined, backend: 'vm'});
    service.handleSseEvent({type: 'user_registered', user_id: 'u-9', display_name: 'nine'});
    expect(service.adminUserRegistered()?.user_id).toBe('u-9');
    expect(toast.info).toHaveBeenCalledOnce();
  });
});
