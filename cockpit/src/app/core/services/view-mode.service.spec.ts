import {afterEach, beforeEach, describe, expect, it} from 'vitest';
import {signal} from '@angular/core';
import {TestBed} from '@angular/core/testing';
import {UserService} from './user.service';
import {ViewModeService} from './view-mode.service';
import {User} from '../models/api.model';

/**
 * Spec for `ViewModeService`. The service uses an `effect()` to rehydrate
 * from localStorage when the current user changes, so tests go through
 * `TestBed` and call `TestBed.tick()` to flush effects (mirrors
 * `i18n.service.spec.ts`).
 */

const ADMIN_USER: User = {
  id: 'admin-1',
  display_name: 'Admin',
  email: 'admin@example.test',
  is_admin: true,
  is_approved: true,
} as User;

const REGULAR_USER: User = {
  id: 'user-1',
  display_name: 'User',
  email: 'user@example.test',
  is_admin: false,
  is_approved: true,
} as User;

function buildUserServiceMock(initial: User | null = null) {
  const currentUser = signal<User | null>(initial);
  const currentUserId = signal<string | null>(initial?.id ?? null);
  // Mirror the real service: id signal stays in sync with the user signal.
  const setUser = (u: User | null) => {
    currentUser.set(u);
    currentUserId.set(u?.id ?? null);
  };
  return {currentUser, currentUserId, setUser};
}

function configure(userMock: ReturnType<typeof buildUserServiceMock>) {
  TestBed.configureTestingModule({
    providers: [
      ViewModeService,
      {provide: UserService, useValue: userMock},
    ],
  });
}

describe('ViewModeService', () => {
  beforeEach(() => {
    TestBed.resetTestingModule();
    localStorage.clear();
  });

  afterEach(() => {
    localStorage.clear();
  });

  describe('initial state', () => {
    it("defaults to 'all' for a fresh admin (silent rollout)", () => {
      const userMock = buildUserServiceMock(ADMIN_USER);
      configure(userMock);
      const service = TestBed.inject(ViewModeService);
      TestBed.tick();

      expect(service.viewMode()).toBe('all');
      expect(service.effectiveMode()).toBe('all');
    });

    it("defaults to 'all' when no user is logged in", () => {
      const userMock = buildUserServiceMock(null);
      configure(userMock);
      const service = TestBed.inject(ViewModeService);
      TestBed.tick();

      expect(service.viewMode()).toBe('all');
    });

    it('rehydrates a previously-saved preference for the same user', () => {
      localStorage.setItem('srw.viewMode.admin-1', 'me');
      const userMock = buildUserServiceMock(ADMIN_USER);
      configure(userMock);
      const service = TestBed.inject(ViewModeService);
      TestBed.tick();

      expect(service.viewMode()).toBe('me');
    });

    it("ignores a malformed stored value (falls back to default 'all')", () => {
      localStorage.setItem('srw.viewMode.admin-1', 'banana');
      const userMock = buildUserServiceMock(ADMIN_USER);
      configure(userMock);
      const service = TestBed.inject(ViewModeService);
      TestBed.tick();

      expect(service.viewMode()).toBe('all');
    });
  });

  describe('setMode', () => {
    it('updates the signal and persists per-user', () => {
      const userMock = buildUserServiceMock(ADMIN_USER);
      configure(userMock);
      const service = TestBed.inject(ViewModeService);
      TestBed.tick();

      service.setMode('me');

      expect(service.viewMode()).toBe('me');
      expect(localStorage.getItem('srw.viewMode.admin-1')).toBe('me');
    });

    it('isolates preferences per user id', () => {
      // Admin A saves 'me'.
      const userMock = buildUserServiceMock(ADMIN_USER);
      configure(userMock);
      const service = TestBed.inject(ViewModeService);
      TestBed.tick();
      service.setMode('me');

      // Switch to a different admin (same browser profile).
      userMock.setUser({...ADMIN_USER, id: 'admin-2'});
      TestBed.tick();

      // Admin B has no saved preference → falls back to default.
      expect(service.viewMode()).toBe('all');
      expect(localStorage.getItem('srw.viewMode.admin-1')).toBe('me');
      expect(localStorage.getItem('srw.viewMode.admin-2')).toBeNull();
    });

    it('keeps the signal in memory when no user is logged in', () => {
      const userMock = buildUserServiceMock(null);
      configure(userMock);
      const service = TestBed.inject(ViewModeService);
      TestBed.tick();

      service.setMode('me');

      expect(service.viewMode()).toBe('me');
      // Nothing keyed without a user.
      expect(localStorage.length).toBe(0);
    });
  });

  describe('effectiveMode', () => {
    it("returns 'me' for non-admins regardless of toggle state", () => {
      const userMock = buildUserServiceMock(REGULAR_USER);
      configure(userMock);
      const service = TestBed.inject(ViewModeService);
      TestBed.tick();

      // Even if the signal somehow gets set, the computed gates it.
      service.viewMode.set('all');
      expect(service.effectiveMode()).toBe('me');
    });

    it('mirrors viewMode for admins', () => {
      const userMock = buildUserServiceMock(ADMIN_USER);
      configure(userMock);
      const service = TestBed.inject(ViewModeService);
      TestBed.tick();

      service.setMode('me');
      expect(service.effectiveMode()).toBe('me');
      service.setMode('all');
      expect(service.effectiveMode()).toBe('all');
    });
  });

  describe('cross-user rehydration', () => {
    it('reloads from localStorage when the active user changes', () => {
      localStorage.setItem('srw.viewMode.admin-2', 'me');

      const userMock = buildUserServiceMock(ADMIN_USER);
      configure(userMock);
      const service = TestBed.inject(ViewModeService);
      TestBed.tick();
      expect(service.viewMode()).toBe('all');

      userMock.setUser({...ADMIN_USER, id: 'admin-2'});
      TestBed.tick();

      expect(service.viewMode()).toBe('me');
    });
  });
});
