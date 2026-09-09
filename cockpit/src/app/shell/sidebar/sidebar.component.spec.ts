import {describe, expect, it, vi} from 'vitest';
import {Injector, computed, runInInjectionContext, signal} from '@angular/core';
import {Router} from '@angular/router';
import {Subject} from 'rxjs';
import {SidebarComponent} from './sidebar.component';
import {UserService} from '../../core/services/user.service';
import {SidebarService} from '../../core/services/sidebar.service';
import {ViewportService} from '../../core/services/viewport.service';
import {LayoutService} from '../../workbench/services/layout.service';
import {PersistentChatService} from '../../core/services/persistent-chat.service';
import {SessionListService} from '../../core/services/session-list.service';
import type {Thread} from '../../core/models/api.model';

/**
 * Builds the component directly with stub providers — no TestBed. Every
 * dependency is a plain object exposing only what the component reads.
 */
function create(opts: {url: string; threads?: Partial<Thread>[]; isAdmin?: boolean}) {
  const threads = signal((opts.threads ?? []) as Thread[]);
  const router = {url: opts.url, navigate: vi.fn(), events: new Subject()};
  const sessions = {
    threads,
    loading: signal(false),
    refresh: vi.fn(),
    grouped: computed(() =>
      threads().length ? [{label: 'today' as const, threads: threads()}] : [],
    ),
  };
  const injector = Injector.create({
    providers: [
      {provide: Router, useValue: router},
      {provide: SessionListService, useValue: sessions},
      {provide: UserService, useValue: {
        currentUser: signal({is_admin: opts.isAdmin ?? false}),
        logout: vi.fn(),
      }},
      {provide: SidebarService, useValue: {collapse: vi.fn(), collapsed: signal(false)}},
      {provide: ViewportService, useValue: {isMobile: signal(false)}},
      {provide: LayoutService, useValue: {}},
      {provide: PersistentChatService, useValue: {threadId: signal(null)}},
    ],
  });
  const component = runInInjectionContext(injector, () => new SidebarComponent());
  return {component, router, sessions};
}

describe('SidebarComponent mode switcher', () => {
  it('selecting Chat navigates to the draft landing, never to a thread', () => {
    const {component, router} = create({url: '/sessions/abc-123'});
    component.selectMode('chat');
    // Regression guard: sessionsLink was hardcoded in April 2026 because this
    // used to resume whatever session was active. A fresh draft is not a
    // resumed session.
    expect(router.navigate).toHaveBeenCalledWith(['/']);
    expect(router.navigate).not.toHaveBeenCalledWith(['/sessions', 'abc-123']);
  });

  it('selecting Jobs navigates to /jobs', () => {
    const {component, router} = create({url: '/'});
    component.selectMode('jobs');
    expect(router.navigate).toHaveBeenCalledWith(['/jobs']);
  });

  it('derives the active mode from the current url', () => {
    expect(create({url: '/jobs'}).component.mode()).toBe('jobs');
    expect(create({url: '/projects/p-1'}).component.mode()).toBe('projects');
    expect(create({url: '/sessions/abc'}).component.mode()).toBe('chat');
    expect(create({url: '/'}).component.mode()).toBe('chat');
  });
});
