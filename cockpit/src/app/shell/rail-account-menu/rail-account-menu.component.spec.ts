import {describe, expect, it, vi} from 'vitest';
import {Injector, runInInjectionContext, signal} from '@angular/core';
import {Router} from '@angular/router';
import {RailAccountMenuComponent} from './rail-account-menu.component';
import {UserService} from '../../core/services/user.service';
import type {User} from '../../core/models/api.model';

/**
 * Builds the component directly with stub providers — no TestBed, matching
 * the sidebar's own spec (see sidebar.component.spec.ts). `showAdmin()` is
 * pure signal logic; it needs no rendered DOM.
 */
function create(user: {is_admin: boolean} | null) {
  const router = {navigate: vi.fn()};
  const userService = {
    currentUser: signal(user as User | null),
    logout: vi.fn(),
  };
  const injector = Injector.create({
    providers: [
      {provide: Router, useValue: router},
      {provide: UserService, useValue: userService},
    ],
  });
  const component = runInInjectionContext(injector, () => new RailAccountMenuComponent());
  return {component, router, userService};
}

describe('RailAccountMenuComponent', () => {
  it('hides Admin from a non-admin', () => {
    const {component} = create({is_admin: false});
    expect(component.showAdmin()).toBe(false);
  });

  it('shows Admin to an admin', () => {
    const {component} = create({is_admin: true});
    expect(component.showAdmin()).toBe(true);
  });

  it('never shows Admin when there is no user', () => {
    const {component} = create(null);
    expect(component.showAdmin()).toBe(false);
  });
});
