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
});
