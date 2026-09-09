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
  /** SidebarService.collapsed's initial value. Defaults to false (expanded) —
   *  the ⌘K collapse-then-focus test sets it explicitly. */
  collapsed?: boolean;
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
  const sidebarService = {
    collapse: vi.fn(),
    expand: vi.fn(),
    collapsed: signal(opts.collapsed ?? false),
  };
  const injector = Injector.create({
    providers: [
      {provide: Router, useValue: router},
      {provide: SessionListService, useValue: sessions},
      {provide: UserService, useValue: {
        currentUser: signal({is_admin: opts.isAdmin ?? false}),
        logout: vi.fn(),
      }},
      {provide: SidebarService, useValue: sidebarService},
      {provide: ViewportService, useValue: {isMobile: signal(false)}},
      {provide: LayoutService, useValue: {}},
      {provide: PersistentChatService, useValue: {threadId: signal(null)}},
    ],
  });
  const component = runInInjectionContext(injector, () => new SidebarComponent());
  return {component, router, sessions, sidebarService};
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
  // Chat, even when the app boots somewhere else — so this doesn't gate on
  // the starting *route*. (It does gate on Router.navigated — see the
  // cold-boot pair of tests below; this test relies on create()'s default
  // of navigated: true.)
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

describe('SidebarComponent session filter', () => {
  it('filters the session list case-insensitively', () => {
    const {component} = create({url: '/', threads: [
      {id: 'a', title: 'Comparing take-home pay', last_activity: new Date().toISOString()},
      {id: 'b', title: 'Kubernetes manifest review', last_activity: new Date().toISOString()},
    ]});
    component.filterText.set('TAKE-HOME');
    expect(component.sessionGroups()[0].threads.map((t) => t.id)).toEqual(['a']);
  });

  it('an empty filter shows every session', () => {
    const {component} = create({url: '/', threads: [
      {id: 'a', title: 'Comparing take-home pay', last_activity: new Date().toISOString()},
      {id: 'b', title: 'Kubernetes manifest review', last_activity: new Date().toISOString()},
    ]});
    component.filterText.set('');
    expect(component.sessionGroups()[0].threads).toHaveLength(2);
  });

  it('drops a group whose every thread was filtered out', () => {
    const {component} = create({url: '/', threads: [
      {id: 'a', title: 'Comparing take-home pay', last_activity: new Date().toISOString()},
    ]});
    component.filterText.set('nothing matches this');
    expect(component.sessionGroups()).toEqual([]);
  });

  // Fix round 1: the filter box itself must not render outside chat mode —
  // it offers to search a list that isn't on screen there. Same allowlist
  // trap as sessionGroups() and the NavigationEnd refresh gate: a `!==
  // 'jobs'` rewrite would also show the box on /admin/users.
  it('shows the rail filter only in chat mode', () => {
    expect(create({url: '/'}).component.showFilter()).toBe(true);
    expect(create({url: '/sessions/abc'}).component.showFilter()).toBe(true);
    expect(create({url: '/jobs'}).component.showFilter()).toBe(false);
    expect(create({url: '/projects'}).component.showFilter()).toBe(false);
    expect(create({url: '/admin/users'}).component.showFilter()).toBe(false);
  });
});

describe('SidebarComponent ⌘K shortcut', () => {
  // filterInput is a @ViewChild, only ever resolved by rendering the real
  // template — this spec never does (see the dynamic-query note on the
  // field). Stand in for it directly; onKeydown only ever reads
  // `.nativeElement.focus()` off it.
  function stubFilterInput(component: ReturnType<typeof create>['component']) {
    const focus = vi.fn();
    (component as any).filterInput = {nativeElement: {focus}};
    return focus;
  }

  function cmdK(): KeyboardEvent {
    return new KeyboardEvent('keydown', {key: 'k', metaKey: true});
  }

  // Regression guard for F1: the rail can be collapsed (width: 0, overflow:
  // hidden) while the filter stays mounted — mobile's default state after
  // every navigation (SidebarService.collapsed defaults true at <=768px, and
  // the rail auto-collapses post-navigation on mobile). Focusing straight
  // into that would strand focus on an invisible control while having
  // already swallowed the browser's own Ctrl+K/⌘K. Asserting call ORDER
  // (not just that both were called) is deliberate: the fix expands before
  // it focuses, and a version that focused first would still pass a looser
  // "both were called" assertion.
  it('expands a collapsed rail before focusing the filter, and swallows the browser shortcut', () => {
    const {component, sidebarService} = create({url: '/', collapsed: true});
    const focus = stubFilterInput(component);
    const event = cmdK();
    const preventDefault = vi.spyOn(event, 'preventDefault');

    component.onKeydown(event);

    expect(preventDefault).toHaveBeenCalled();
    expect(sidebarService.expand).toHaveBeenCalledTimes(1);
    expect(focus).toHaveBeenCalledTimes(1);
    const expandOrder = sidebarService.expand.mock.invocationCallOrder[0];
    const focusOrder = focus.mock.invocationCallOrder[0];
    expect(expandOrder).toBeLessThan(focusOrder);
  });

  it('does not expand an already-expanded rail, but still focuses the filter', () => {
    const {component, sidebarService} = create({url: '/', collapsed: false});
    const focus = stubFilterInput(component);

    component.onKeydown(cmdK());

    expect(sidebarService.expand).not.toHaveBeenCalled();
    expect(focus).toHaveBeenCalledTimes(1);
  });

  // Unchanged pre-existing behavior: outside chat mode filterInput is never
  // set (the @if in the template), so the browser's own Ctrl+K must survive.
  it('leaves the browser shortcut alone when the filter is not on screen', () => {
    const {component, sidebarService} = create({url: '/jobs', collapsed: true});
    const event = cmdK();
    const preventDefault = vi.spyOn(event, 'preventDefault');

    component.onKeydown(event);

    expect(preventDefault).not.toHaveBeenCalled();
    expect(sidebarService.expand).not.toHaveBeenCalled();
  });
});

// F4/F5: the "See all sessions" row and the empty-state copy are template-
// only additions (a static routerLink and two @if branches on mode()/
// sessionGroups().length, both already covered by the mode() and
// sessionGroups() tests above) — this component's spec never renders the
// template (see the dynamic-query note on filterInput), so there is no
// rendered-DOM assertion to add for those beyond what mode()'s existing
// allowlist tests already prove about the gate they share. hasSessions()
// and clearFilter() are new component-level logic, so those get real cases.
describe('SidebarComponent rail empty state', () => {
  it('hasSessions reflects the UNFILTERED list, not the filtered one — this is what tells "no sessions yet" apart from "no matches"', () => {
    const {component} = create({url: '/', threads: [
      {id: 'a', title: 'Comparing take-home pay', last_activity: new Date().toISOString()},
    ]});
    component.filterText.set('nothing matches this');

    expect(component.sessionGroups()).toEqual([]);
    expect(component.hasSessions()).toBe(true);
  });

  it('hasSessions is false for a genuinely empty account', () => {
    const {component} = create({url: '/', threads: []});
    expect(component.hasSessions()).toBe(false);
  });
});

describe('SidebarComponent clearFilter', () => {
  it('resets filterText and returns focus to the input', () => {
    const {component} = create({url: '/'});
    component.filterText.set('something');
    const focus = vi.fn();
    (component as any).filterInput = {nativeElement: {focus}};

    component.clearFilter();

    expect(component.filterText()).toBe('');
    expect(focus).toHaveBeenCalledTimes(1);
  });

  it('does not throw when the filter input is not resolved', () => {
    const {component} = create({url: '/'});
    component.filterText.set('something');
    expect(() => component.clearFilter()).not.toThrow();
    expect(component.filterText()).toBe('');
  });
});
