import {describe, expect, it, vi} from 'vitest';
import {signal} from '@angular/core';
import {TestBed} from '@angular/core/testing';
import {TranslocoService} from '@jsverse/transloco';
import {of} from 'rxjs';
import {AdvancedAccordionComponent} from './advanced-accordion.component';
import {UserService} from '../../../core/services/user.service';
import {User} from '../../../core/models/api.model';

/**
 * Create the component under TestBed (which supplies ChangeDetectionScheduler
 * for effect() in the constructor) with a mocked UserService signal so
 * canUseVm() resolves against controllable state.
 */
function createComponent(user: Partial<User> | null) {
  const currentUser = signal<User | null>(
    user
      ? ({
          id: 'u1',
          display_name: 'Test',
          avatar_color: '#fff',
          created_at: '2025-01-01T00:00:00Z',
          ...user,
        } as User)
      : null,
  );

  const mockUserService = {
    currentUser,
    users: signal([]),
    isAuthenticated: signal(true),
    sessionReady: signal(true),
    isApproved: signal(true),
    currentUserId: signal(user ? 'u1' : null),
    loadCurrentUser: vi.fn(),
    loadUsers: vi.fn(),
    logout: vi.fn(),
  };

  const mockTransloco = {
    translate: (key: string) => key,
    langChanges$: of('en'),
    getActiveLang: () => 'en',
  };

  TestBed.configureTestingModule({
    providers: [
      AdvancedAccordionComponent,
      {provide: UserService, useValue: mockUserService},
      {provide: TranslocoService, useValue: mockTransloco},
    ],
  });

  const component = TestBed.inject(AdvancedAccordionComponent);
  return {component, currentUser};
}

describe('AdvancedAccordionComponent — VM permission', () => {
  describe('canUseVm computed', () => {
    it('is false for a non-admin without can_use_vm', () => {
      const {component} = createComponent({is_admin: false, can_use_vm: false});
      expect(component.canUseVm()).toBe(false);
    });

    it('is true for an admin without explicit grant', () => {
      const {component} = createComponent({is_admin: true, can_use_vm: false});
      expect(component.canUseVm()).toBe(true);
    });

    it('is true for a non-admin with explicit grant', () => {
      const {component} = createComponent({is_admin: false, can_use_vm: true});
      expect(component.canUseVm()).toBe(true);
    });

    it('is false when the user signal is null', () => {
      const {component} = createComponent(null);
      expect(component.canUseVm()).toBe(false);
    });

    it('tracks user signal changes', () => {
      const {component, currentUser} = createComponent({
        is_admin: false,
        can_use_vm: false,
      });
      expect(component.canUseVm()).toBe(false);

      currentUser.set({
        id: 'u1',
        display_name: 'Test',
        avatar_color: '#fff',
        created_at: '2025-01-01T00:00:00Z',
        is_admin: false,
        can_use_vm: true,
      } as User);
      expect(component.canUseVm()).toBe(true);
    });
  });
});
