import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import {signal} from '@angular/core';
import {TestBed} from '@angular/core/testing';
import {of} from 'rxjs';
import {UserService} from './user.service';
import {SettingsService} from './settings.service';
import {ViewModeService} from './view-mode.service';
import {User, UserSettings} from '../models/api.model';

/**
 * Spec for `ViewModeService`. The service uses effects to (1) rehydrate from
 * localStorage when the current user changes and (2) reconcile to the
 * server-persisted `admin_view_mode` once preferences load, so tests go
 * through `TestBed` and call `TestBed.tick()` to flush effects (mirrors
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

function buildSettingsServiceMock() {
  // Only the surface ViewModeService touches: the `preferences` signal it
  // reconciles against, and `updatePreferences` it calls on every setMode.
  const preferences = signal<UserSettings>({});
  const updatePreferences = vi.fn((_s: Partial<UserSettings>) => of({status: 'updated'}));
  return {preferences, updatePreferences};
}

function configure(
  userMock: ReturnType<typeof buildUserServiceMock>,
  settingsMock: ReturnType<typeof buildSettingsServiceMock> = buildSettingsServiceMock(),
) {
  TestBed.configureTestingModule({
    providers: [
      ViewModeService,
      {provide: UserService, useValue: userMock},
      {provide: SettingsService, useValue: settingsMock},
    ],
  });
  return settingsMock;
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

  describe('server-side persistence', () => {
    it('persists the choice to the server on setMode', () => {
      const userMock = buildUserServiceMock(ADMIN_USER);
      const settingsMock = configure(userMock);
      const service = TestBed.inject(ViewModeService);
      TestBed.tick();

      service.setMode('me');

      expect(settingsMock.updatePreferences).toHaveBeenCalledWith({admin_view_mode: 'me'});
    });

    it('adopts the server value once preferences load and refreshes the mirror', () => {
      const userMock = buildUserServiceMock(ADMIN_USER);
      const settingsMock = configure(userMock);
      const service = TestBed.inject(ViewModeService);
      TestBed.tick();
      expect(service.viewMode()).toBe('all'); // localStorage empty → default

      // Server preferences arrive (initial load, or a login on another device).
      settingsMock.preferences.set({admin_view_mode: 'me'});
      TestBed.tick();

      expect(service.viewMode()).toBe('me');
      expect(localStorage.getItem('srw.viewMode.admin-1')).toBe('me');
    });

    it('server value overrides a stale localStorage mirror', () => {
      localStorage.setItem('srw.viewMode.admin-1', 'all');
      const userMock = buildUserServiceMock(ADMIN_USER);
      const settingsMock = configure(userMock);
      const service = TestBed.inject(ViewModeService);
      TestBed.tick();
      expect(service.viewMode()).toBe('all');

      settingsMock.preferences.set({admin_view_mode: 'me'});
      TestBed.tick();

      expect(service.viewMode()).toBe('me');
    });

    it('ignores a missing or invalid server value (keeps the local choice)', () => {
      localStorage.setItem('srw.viewMode.admin-1', 'me');
      const userMock = buildUserServiceMock(ADMIN_USER);
      const settingsMock = configure(userMock);
      const service = TestBed.inject(ViewModeService);
      TestBed.tick();
      expect(service.viewMode()).toBe('me');

      // Preferences load but carry no admin_view_mode → must not clobber.
      settingsMock.preferences.set({admin_view_mode: undefined});
      TestBed.tick();
      expect(service.viewMode()).toBe('me');

      // A bad value is likewise ignored.
      settingsMock.preferences.set({admin_view_mode: 'banana' as unknown as 'me'});
      TestBed.tick();
      expect(service.viewMode()).toBe('me');
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
