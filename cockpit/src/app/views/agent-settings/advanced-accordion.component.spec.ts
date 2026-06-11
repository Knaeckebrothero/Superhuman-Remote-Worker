import {describe, expect, it, vi} from 'vitest';
import {signal} from '@angular/core';
import {TestBed} from '@angular/core/testing';
import {TranslocoService} from '@jsverse/transloco';
import {of} from 'rxjs';
import {AdvancedAccordionComponent} from './advanced-accordion.component';
import {UserService} from '../../core/services/user.service';
import {User} from '../../core/models/api.model';

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

describe('AdvancedAccordionComponent — lite workspace gating', () => {
  it('isLiteBackend / isNoneBackend track the selected backend', () => {
    const {component} = createComponent({is_admin: true});
    // Empty config resolves to the 'sandbox' default.
    expect(component.isLiteBackend()).toBe(false);
    expect(component.isNoneBackend()).toBe(false);

    component.workspaceBackend.set('vm');
    expect(component.isLiteBackend()).toBe(false);

    component.workspaceBackend.set('virtual');
    expect(component.isLiteBackend()).toBe(true);
    expect(component.isNoneBackend()).toBe(false);

    component.workspaceBackend.set('none');
    expect(component.isLiteBackend()).toBe(true);
    expect(component.isNoneBackend()).toBe(true);
  });

  it('omits shell/git/browser overrides for a virtual backend but keeps file limits', () => {
    const {component} = createComponent({is_admin: true});
    component.workspaceBackend.set('virtual');
    component.gitVersioning.set(true);
    component.shellMode.set('persistent');
    component.browserVision.set(true);
    component.maxReadWords.set(5000);

    const o = component.getOverrides() as Record<string, any>;
    expect(o['workspace'].backend).toBe('virtual');
    expect(o['workspace'].git_versioning).toBeUndefined();
    expect(o['workspace'].max_read_words).toBe(5000); // virtual keeps file tools
    expect(o['shell']).toBeUndefined();
    expect(o['browser']).toBeUndefined();
  });

  it('omits file-size limits for a none backend', () => {
    const {component} = createComponent({is_admin: true});
    component.workspaceBackend.set('none');
    component.maxReadWords.set(5000);
    component.maxWriteWords.set(2000);

    const o = component.getOverrides() as Record<string, any>;
    expect(o['workspace'].backend).toBe('none');
    expect(o['workspace'].max_read_words).toBeUndefined();
    expect(o['workspace'].max_write_words).toBeUndefined();
  });

  it('keeps shell and git overrides for a sandbox backend', () => {
    const {component} = createComponent({is_admin: true});
    component.workspaceBackend.set('sandbox');
    component.shellMode.set('persistent');
    component.gitVersioning.set(true);

    const o = component.getOverrides() as Record<string, any>;
    expect(o['shell'].mode).toBe('persistent');
    expect(o['workspace'].git_versioning).toBe(true);
  });
});
