import {describe, expect, it, vi} from 'vitest';
import {Injector, computed, runInInjectionContext, signal} from '@angular/core';
import {NavigationEnd, Router} from '@angular/router';
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
function create(opts: {
  url: string;
  threads?: Partial<Thread>[];
  isAdmin?: boolean;
  /** Router.navigated: true once a first navigation has occurred. Defaults to
   *  true (steady state) — tests exercising the cold-boot distinction set it
   *  explicitly. */
  navigated?: boolean;
}) {
  const threads = signal((opts.threads ?? []) as Thread[]);
  const router = {
    url: opts.url,
    navigated: opts.navigated ?? true,
    navigate: vi.fn(),
    events: new Subject(),
  };
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

  // Allowlist, not a fallback: fix round 1. mode() used to default to 'chat'
  // for anything unmatched, which lit the Chat tab on /experts, /admin/*,
  // /settings, etc. — routes that have no mode of their own and are reached
  // via the More/avatar menus (Task 8). Those routes must light no tab.
  it('lights no tab for routes outside the three modes', () => {
    expect(create({url: '/experts'}).component.mode()).toBeNull();
    expect(create({url: '/admin/models'}).component.mode()).toBeNull();
    expect(create({url: '/settings'}).component.mode()).toBeNull();
  });

  it('treats the landing page as chat even with a query string', () => {
    // router.url carries the query string and fragment; a naive `=== '/'`
    // check breaks on '/?foo=bar' and would wrongly null out the tab on the
    // landing page itself.
    expect(create({url: '/?foo=bar'}).component.mode()).toBe('chat');
  });

  it('treats a session thread url as chat, using the id from the hijack test', () => {
    expect(create({url: '/sessions/abc-123'}).component.mode()).toBe('chat');
  });
});

describe('SidebarComponent session list', () => {
  it('lists sessions grouped by recency when the mode is chat', () => {
    const {component} = create({url: '/', threads: [
      {id: 'a', title: 'Comparing take-home pay', last_activity: new Date().toISOString()},
    ]});
    expect(component.sessionGroups().map((g) => g.label)).toEqual(['today']);
  });

  it('renders no session groups outside chat mode', () => {
    const {component} = create({url: '/jobs', threads: [
      {id: 'a', title: 'Comparing take-home pay', last_activity: new Date().toISOString()},
    ]});
    expect(component.sessionGroups()).toEqual([]);
  });

  // mode() is an allowlist that returns null for routes outside the three
  // modes (/admin/*, /experts, /settings, ...). A `!== 'jobs'` rewrite of the
  // sessionGroups gate would also pass the two tests above yet show sessions
  // on an admin page — guard that case explicitly.
  it('renders no session groups when the mode is null', () => {
    const {component} = create({url: '/admin/users', threads: [
      {id: 'a', title: 'Comparing take-home pay', last_activity: new Date().toISOString()},
    ]});
    expect(component.sessionGroups()).toEqual([]);
  });

  // The rail must have sessions ready the instant the user is looking at
  // Chat, even when the app boots somewhere else — so the initial refresh is
  // unconditional, not gated on the starting route.
  it('refreshes the session list once on construction, regardless of the starting route', () => {
    const {sessions} = create({url: '/jobs'});
    expect(sessions.refresh).toHaveBeenCalledTimes(1);
  });

  it('refreshes the session list again on navigating into chat mode', () => {
    const {router, sessions} = create({url: '/jobs'});
    expect(sessions.refresh).toHaveBeenCalledTimes(1); // the construction-time call
    router.events.next(new NavigationEnd(1, '/', '/'));
    expect(sessions.refresh).toHaveBeenCalledTimes(2);
  });

  // Refreshing on every navigation regardless of mode would fetch threads
  // while the user is in Jobs or Admin, for nothing.
  it('does not refresh the session list on navigating to a non-chat route', () => {
    const {router, sessions} = create({url: '/'});
    expect(sessions.refresh).toHaveBeenCalledTimes(1); // the construction-time call
    router.events.next(new NavigationEnd(1, '/jobs', '/jobs'));
    expect(sessions.refresh).toHaveBeenCalledTimes(1);
  });

  // Same allowlist trap as sessionGroups: a `!== 'jobs'` gate on the
  // navigation subscription would also pass the two tests above yet refetch
  // threads while navigating around in the admin section.
  it('does not refresh the session list on navigating to a route with no mode', () => {
    const {router, sessions} = create({url: '/'});
    expect(sessions.refresh).toHaveBeenCalledTimes(1); // the construction-time call
    router.events.next(new NavigationEnd(1, '/admin/users', '/admin/users'));
    expect(sessions.refresh).toHaveBeenCalledTimes(1);
  });

  // Cold-boot double fetch (fix round 1): under Angular's default
  // enabledNonBlocking initial navigation, the sidebar is constructed BEFORE
  // the first navigation completes, so Router.navigated is still false. An
  // unconditional refresh() here would double up with the NavigationEnd
  // handler below firing for that same first navigation.
  it('does not refresh on construction when the router has not navigated yet, but the subsequent NavigationEnd does', () => {
    const {router, sessions} = create({url: '/', navigated: false});
    expect(sessions.refresh).toHaveBeenCalledTimes(0);
    router.events.next(new NavigationEnd(1, '/', '/'));
    expect(sessions.refresh).toHaveBeenCalledTimes(1);
  });

  // The other side of the guard: enabledBlocking, SSR, or a remount can
  // construct the sidebar AFTER the first navigation already resolved — no
  // NavigationEnd is coming for it, so construction must fetch directly.
  it('refreshes on construction when the router has already navigated', () => {
    const {sessions} = create({url: '/', navigated: true});
    expect(sessions.refresh).toHaveBeenCalledTimes(1);
  });
});
