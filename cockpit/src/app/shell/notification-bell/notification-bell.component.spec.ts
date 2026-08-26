import {describe, expect, it, vi} from 'vitest';
import {Injector, runInInjectionContext, signal} from '@angular/core';
import {Router} from '@angular/router';
import {TranslocoService} from '@jsverse/transloco';

import {NotificationBellComponent} from './notification-bell.component';
import {ActionCenterService} from '../../core/services/action-center.service';

function create(counts: Record<string, number>, badge: number) {
  const actionCenter = {
    counts: signal(counts),
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
  it('reads the unseen-driven badge and leads the tooltip with it', () => {
    const {component} = create(
      {notifications: 2, sudo: 1, messages: 0, reviews: 0, sessions: 0, unseen: 2, total: 3},
      3,
    );
    expect(component.tooltipText()).toBe('notificationBell.unseenPlural:2, notificationBell.sudo:1');
  });

  it('singular unseen copy', () => {
    const {component} = create(
      {notifications: 1, sudo: 0, messages: 0, reviews: 0, sessions: 0, unseen: 1, total: 1},
      1,
    );
    expect(component.tooltipText()).toBe('notificationBell.unseenSingle:1');
  });

  it('falls back to the title when nothing needs attention', () => {
    const {component} = create(
      {notifications: 0, sudo: 0, messages: 0, reviews: 0, sessions: 0, unseen: 0, total: 0},
      0,
    );
    expect(component.tooltipText()).toBe('notificationBell.title');
  });

  it('routes to the inbox', () => {
    const {component, router} = create(
      {notifications: 0, sudo: 0, messages: 0, reviews: 0, sessions: 0, unseen: 0, total: 0},
      0,
    );
    component.goToInbox();
    expect(router.navigate).toHaveBeenCalledWith(['/inbox']);
  });
});
