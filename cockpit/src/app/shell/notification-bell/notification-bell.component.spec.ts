import {describe, expect, it, vi} from 'vitest';
import {Injector, runInInjectionContext, signal} from '@angular/core';
import {Router} from '@angular/router';
import {TranslocoService} from '@jsverse/transloco';

import {NotificationBellComponent} from './notification-bell.component';
import {ActionCenterService} from '../../core/services/action-center.service';

function create(counts: {notifications: number; unseen: number; total: number}, badge: number) {
  const actionCenter = {
    counts: signal({...counts, byCategory: {}}),
    badgeCount: signal(badge),
  };
  const router = {navigate: vi.fn()};
  const transloco = {
    translate: vi.fn((key: string, params?: Record<string, unknown>) =>
      params ? `${key}:${params['n']}` : key,
    ),
  };
  const injector = Injector.create({
    providers: [
      {provide: ActionCenterService, useValue: actionCenter},
      {provide: Router, useValue: router},
      {provide: TranslocoService, useValue: transloco},
    ],
  });
  const component = runInInjectionContext(injector, () => new NotificationBellComponent());
  return {component, router};
}

describe('NotificationBellComponent', () => {
  it('leads the tooltip with the server unseen count and adds the pending total when it differs', () => {
    const {component} = create({notifications: 3, unseen: 2, total: 3}, 2);
    expect(component.tooltipText()).toBe('notificationBell.unseenPlural:2, notificationBell.pendingPlural:3');
  });

  it('singular unseen copy, no pending suffix when every pending row is the unseen one', () => {
    const {component} = create({notifications: 1, unseen: 1, total: 1}, 1);
    expect(component.tooltipText()).toBe('notificationBell.unseenSingle:1');
  });

  it('falls back to the title when nothing is unseen (the badge is unseen-driven)', () => {
    const {component} = create({notifications: 4, unseen: 0, total: 4}, 0);
    expect(component.tooltipText()).toBe('notificationBell.title');
  });

  it('routes to the inbox', () => {
    const {component, router} = create({notifications: 0, unseen: 0, total: 0}, 0);
    component.goToInbox();
    expect(router.navigate).toHaveBeenCalledWith(['/inbox']);
  });
});
