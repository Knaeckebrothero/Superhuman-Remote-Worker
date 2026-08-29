import {signal} from '@angular/core';
import {TestBed} from '@angular/core/testing';
import {ActivatedRoute, convertToParamMap, Router} from '@angular/router';
import {BehaviorSubject, of} from 'rxjs';
import {afterEach, describe, expect, it, vi} from 'vitest';
import {CanvasService} from '../../core/services/canvas.service';
import {BrowserCapability, CanvasState} from '../../core/models/canvas.model';
import {ErrorMessageService} from '../../core/services/error-message.service';
import {PersistentChatService} from '../../core/services/persistent-chat.service';
import type {ConfigDriftItem} from '../../core/services/resume-error';
import {ViewportService} from '../../core/services/viewport.service';
import {AppToastService} from '../../ui/toast';
import {ApiService} from '../../core/services/api.service';
import {
  browserReplacementTargetMatches,
  ChatPageComponent,
} from './chat-page.component';

function createFixture(options: {
  draft?: boolean;
  threadId?: string;
  thread?: Record<string, unknown>;
} = {}): {
  component: ChatPageComponent;
  params: BehaviorSubject<ReturnType<typeof convertToParamMap>>;
  chat: {
    threadId: ReturnType<typeof signal<string | null>>;
    isConnected: ReturnType<typeof signal<boolean>>;
    isStartingSession: ReturnType<typeof signal<boolean>>;
    pendingDrift: ReturnType<typeof signal<ConfigDriftItem[] | null>>;
    connect: ReturnType<typeof vi.fn>;
    enterDraftSession: ReturnType<typeof vi.fn>;
    createAndConnect: ReturnType<typeof vi.fn>;
  };
  canvas: {
    threadId: ReturnType<typeof signal<string | null>>;
    state: ReturnType<typeof signal<CanvasState | null>>;
    loadStatus: ReturnType<typeof signal<'idle' | 'loading' | 'ready' | 'error'>>;
    browserCapability: ReturnType<typeof signal<BrowserCapability | null>>;
    browserCapabilityStatus: ReturnType<typeof signal<'idle' | 'loading' | 'ready' | 'error'>>;
    browserOpenStatus: ReturnType<typeof signal<'idle' | 'workspace' | 'browser' | 'error'>>;
    browserOpenError: ReturnType<typeof signal<string | null>>;
    selectThread: ReturnType<typeof vi.fn>;
    reconcile: ReturnType<typeof vi.fn>;
    openBrowser: ReturnType<typeof vi.fn>;
    retryOpenBrowser: ReturnType<typeof vi.fn>;
  };
  viewport: {isMobile: ReturnType<typeof signal<boolean>>};
  router: {navigate: ReturnType<typeof vi.fn>};
  api: {
    getPersistentThread: ReturnType<typeof vi.fn>;
    getPersistentThreadHistory: ReturnType<typeof vi.fn>;
  };
} {
  const params = new BehaviorSubject(
    convertToParamMap(options.threadId ? {threadId: options.threadId} : {}),
  );
  const chat = {
    threadId: signal<string | null>(null),
    isConnected: signal(false),
    isStartingSession: signal(false),
    pendingDrift: signal<ConfigDriftItem[] | null>(null),
    connect: vi.fn().mockResolvedValue(undefined),
    enterDraftSession: vi.fn(),
    createAndConnect: vi.fn().mockResolvedValue('created-thread'),
  };
  const canvas = {
    threadId: signal<string | null>(null),
    state: signal<CanvasState | null>(null),
    loadStatus: signal<'idle' | 'loading' | 'ready' | 'error'>('idle'),
    browserCapability: signal<BrowserCapability | null>(null),
    browserCapabilityStatus: signal<'idle' | 'loading' | 'ready' | 'error'>('idle'),
    browserOpenStatus: signal<'idle' | 'workspace' | 'browser' | 'error'>('idle'),
    browserOpenError: signal<string | null>(null),
    selectThread: vi.fn(),
    reconcile: vi.fn(),
    openBrowser: vi.fn(),
    retryOpenBrowser: vi.fn(),
  };
  canvas.selectThread.mockImplementation((threadId: string | null) => {
    canvas.threadId.set(threadId);
    canvas.browserCapability.set(null);
    canvas.browserCapabilityStatus.set(threadId ? 'loading' : 'idle');
    canvas.browserOpenStatus.set('idle');
    canvas.browserOpenError.set(null);
    if (threadId === null) canvas.state.set(null);
  });
  const viewport = {isMobile: signal(false)};
  const router = {navigate: vi.fn().mockResolvedValue(true)};
  const api = {
    getPersistentThread: vi.fn().mockReturnValue(of(options.thread ?? {
      id: options.threadId ?? 'thread-1',
      kind: 'session',
      status: 'active',
    })),
    getPersistentThreadHistory: vi.fn().mockReturnValue(of({
      thread_id: options.threadId ?? 'thread-1',
      messages: [],
      total: 0,
      has_more: false,
    })),
  };

  TestBed.configureTestingModule({
    providers: [
      ChatPageComponent,
      {
        provide: ActivatedRoute,
        useValue: {
          snapshot: {data: {draft: options.draft === true}},
          paramMap: params.asObservable(),
        },
      },
      {provide: Router, useValue: router},
      {provide: PersistentChatService, useValue: chat},
      {provide: ApiService, useValue: api},
      {provide: CanvasService, useValue: canvas},
      {provide: ViewportService, useValue: viewport},
      {provide: AppToastService, useValue: {danger: vi.fn()}},
      {provide: ErrorMessageService, useValue: {translate: vi.fn()}},
    ],
  });
  return {
    component: TestBed.inject(ChatPageComponent),
    params,
    chat,
    canvas,
    viewport,
    router,
    api,
  };
}

function presentedState(revision: number, path = 'output/report.md'): CanvasState {
  return {
    canvas_id: 'main',
    source: {type: 'workspace_file', path},
    title: 'Report',
    renderer: 'markdown',
    editable: false,
    alt_text: null,
    presentation_revision: revision,
    source_version: `sha256:${revision}`,
    content_url: null,
    status: 'ready',
    capabilities: {can_edit: false, can_pop_out: false, can_take_control: false},
    updated_at: `2026-07-13T10:00:0${revision}Z`,
  };
}

describe('ChatPageComponent Canvas route selection', () => {
  afterEach(() => TestBed.resetTestingModule());

  it('switches chat and Canvas when Angular reuses the component for a new thread', () => {
    const {component, params, chat, canvas} = createFixture({threadId: 'thread-1'});
    component.ngOnInit();
    TestBed.tick();

    expect(canvas.selectThread).toHaveBeenLastCalledWith('thread-1');
    expect(chat.connect).toHaveBeenLastCalledWith('thread-1');

    params.next(convertToParamMap({threadId: 'thread-2'}));
    expect(canvas.selectThread).toHaveBeenLastCalledWith('thread-2');
    expect(chat.connect).toHaveBeenLastCalledWith('thread-2');

    // takeUntilDestroyed prevents a reused route stream from acting after the
    // host view has gone away.
    TestBed.resetTestingModule();
    params.next(convertToParamMap({threadId: 'thread-3'}));
    expect(canvas.selectThread).toHaveBeenCalledTimes(2);
    expect(chat.connect).toHaveBeenCalledTimes(2);
  });

  it('keeps the root route as a Canvas-free instant draft', () => {
    const {component, params, chat, canvas} = createFixture({draft: true});
    component.ngOnInit();
    TestBed.tick();

    expect(canvas.selectThread).toHaveBeenCalledOnce();
    expect(canvas.selectThread).toHaveBeenCalledWith(null);
    expect(chat.enterDraftSession).toHaveBeenCalledOnce();
    expect(chat.connect).not.toHaveBeenCalled();

    params.next(convertToParamMap({threadId: 'ignored-on-draft-route'}));
    expect(canvas.selectThread).toHaveBeenCalledOnce();
  });

  it('loads a subagent transcript without connecting or selecting live Canvas state', () => {
    const thread = {
      id: 'child-1',
      title: 'Subagent tester-7f3a',
      kind: 'subagent',
      status: 'active',
      parent_job_id: 'job-1',
      subagent_handle: 'tester-7f3a',
      subagent_type: 'tester',
      subagent_status: 'running',
    };
    const {component, chat, canvas, api} = createFixture({threadId: 'child-1', thread});

    component.ngOnInit();
    TestBed.tick();

    expect(chat.connect).not.toHaveBeenCalled();
    expect(chat.enterDraftSession).not.toHaveBeenCalled();
    expect(canvas.selectThread).toHaveBeenCalledWith(null);
    expect(canvas.selectThread).not.toHaveBeenCalledWith('child-1');
    expect(api.getPersistentThreadHistory).toHaveBeenCalledWith('child-1');
    expect(component.subagentThread()).toMatchObject(thread);

    component.refreshSubagentTranscript();
    expect(api.getPersistentThreadHistory).toHaveBeenCalledTimes(2);
  });

  it('opens a new source but does not reopen a locally closed same-source refresh', () => {
    const {component, canvas} = createFixture({threadId: 'thread-1'});
    component.ngOnInit();
    TestBed.tick();

    canvas.state.set(presentedState(1));
    TestBed.tick();
    expect(component.canvasOpen()).toBe(true);

    component.closeCanvas();
    canvas.state.set(presentedState(2));
    TestBed.tick();
    expect(component.canvasOpen()).toBe(false);

    canvas.state.set(presentedState(3, 'output/other.md'));
    TestBed.tick();
    expect(component.canvasOpen()).toBe(true);
  });

  it('does not strand mobile focus in inert chat when a new Canvas arrives', () => {
    const {component, canvas, viewport} = createFixture({threadId: 'thread-1'});
    viewport.isMobile.set(true);
    component.ngOnInit();
    TestBed.tick();
    canvas.state.set(presentedState(1));
    TestBed.tick();

    expect(component.canvasOpen()).toBe(true);
    expect(component.canvasFocus()).toBe(false);
    expect(component.chatAreaHidden()).toBe(false);

    component.openCanvas(true);
    expect(component.canvasFocus()).toBe(true);
    expect(component.chatAreaHidden()).toBe(true);
    component.returnToChat();
    expect(component.canvasOpen()).toBe(true);
    expect(component.canvasFocus()).toBe(false);
    expect(component.canvasAreaHidden()).toBe(true);

    component.closeCanvas();
    expect(component.canvasOpen()).toBe(false);
  });

  it('restores the desktop opener when Canvas closes', async () => {
    const {component, canvas} = createFixture({threadId: 'thread-1'});
    component.ngOnInit();
    TestBed.tick();
    canvas.state.set(presentedState(1));
    TestBed.tick();

    const opener = document.createElement('button');
    document.body.appendChild(opener);
    opener.focus();
    component.openCanvas(true);
    component.closeCanvas();
    await Promise.resolve();

    expect(document.activeElement).toBe(opener);
    opener.remove();
  });

  it('restores focus when the active Canvas is cleared remotely', async () => {
    const {component, canvas} = createFixture({threadId: 'thread-1'});
    component.ngOnInit();
    TestBed.tick();
    canvas.state.set(presentedState(1));
    TestBed.tick();

    const opener = document.createElement('button');
    const canvasPanel = document.createElement('div');
    const canvasButton = document.createElement('button');
    canvasPanel.id = 'canvas-panel';
    canvasPanel.appendChild(canvasButton);
    document.body.append(opener, canvasPanel);
    opener.focus();
    component.openCanvas(true);
    canvasButton.focus();

    canvas.state.set({...presentedState(2), source: null, status: 'cleared'});
    TestBed.tick();
    await Promise.resolve();

    expect(component.canvasOpen()).toBe(false);
    expect(document.activeElement).toBe(opener);
    opener.remove();
    canvasPanel.remove();
  });

  it('keeps a cleared Canvas reachable while the editor owns unsaved bytes', () => {
    const {component, canvas} = createFixture({threadId: 'thread-1'});
    component.ngOnInit();
    TestBed.tick();
    canvas.state.set(presentedState(1));
    TestBed.tick();

    component.canvasDirty.set(true);
    canvas.state.set({...presentedState(2), source: null, status: 'cleared'});
    TestBed.tick();
    expect(component.canvasAvailable()).toBe(true);
    expect(component.canvasOpen()).toBe(true);

    component.canvasDirty.set(false);
    TestBed.tick();
    expect(component.canvasAvailable()).toBe(false);
    expect(component.canvasOpen()).toBe(false);
  });

  it('does not reopen a locally hidden dirty editor when the Canvas is cleared', () => {
    const {component, canvas} = createFixture({threadId: 'thread-1'});
    component.ngOnInit();
    TestBed.tick();
    canvas.state.set(presentedState(1));
    TestBed.tick();
    component.canvasDirty.set(true);
    component.closeCanvas();

    canvas.state.set({...presentedState(2), source: null, status: 'cleared'});
    TestBed.tick();

    expect(component.canvasAvailable()).toBe(true);
    expect(component.canvasOpen()).toBe(false);
  });

  it('keeps an explicitly opened empty browser host without auto-opening on discovery', () => {
    const {component, canvas} = createFixture({threadId: 'thread-1'});
    component.ngOnInit();
    TestBed.tick();

    canvas.browserCapability.set({
      feature_enabled: true,
      can_open_browser: true,
      workspace_ready: false,
      reason: null,
    });
    canvas.browserCapabilityStatus.set('ready');
    TestBed.tick();
    expect(component.canvasAvailable()).toBe(true);
    expect(component.canvasOpen()).toBe(false);

    component.openCanvas();
    TestBed.tick();
    expect(component.canvasOpen()).toBe(true);

    canvas.browserCapabilityStatus.set('loading');
    TestBed.tick();
    expect(component.canvasOpen()).toBe(true);
  });

  it('closes an empty browser host on an authoritative cleared state', () => {
    const {component, canvas} = createFixture({threadId: 'thread-1'});
    component.ngOnInit();
    TestBed.tick();
    canvas.browserCapability.set({
      feature_enabled: true,
      can_open_browser: true,
      workspace_ready: true,
      reason: null,
    });
    component.openCanvas();
    canvas.state.set({...presentedState(2), source: null, status: 'cleared'});
    TestBed.tick();

    expect(component.canvasOpen()).toBe(false);
  });

  it('opens and starts the browser from chat but never replaces a dirty stage', () => {
    const {component, canvas} = createFixture({threadId: 'thread-1'});
    component.ngOnInit();
    TestBed.tick();
    canvas.browserCapability.set({
      feature_enabled: true,
      can_open_browser: true,
      workspace_ready: true,
      reason: null,
    });

    component.openSharedBrowser();
    expect(component.canvasOpen()).toBe(true);
    expect(canvas.openBrowser).toHaveBeenCalledOnce();

    component.canvasDirty.set(true);
    expect(component.browserActionDisabled()).toBe(true);
    expect(component.browserActionTooltipKey()).toBe('canvas.browser.open.dirty');
    component.openSharedBrowser();
    expect(canvas.openBrowser).toHaveBeenCalledOnce();
  });

  it('confirms before replacing a clean non-browser presentation', () => {
    const {component, canvas} = createFixture({threadId: 'thread-1'});
    component.ngOnInit();
    TestBed.tick();
    canvas.browserCapability.set({
      feature_enabled: true,
      can_open_browser: true,
      workspace_ready: true,
      reason: null,
    });
    canvas.state.set(presentedState(4));
    TestBed.tick();

    component.openSharedBrowser();

    expect(component.browserReplacementTarget()).toEqual({
      threadId: 'thread-1',
      presentationRevision: 4,
      sourceKey: 'workspace_file:output/report.md',
    });
    expect(canvas.openBrowser).not.toHaveBeenCalled();

    component.confirmSharedBrowserReplacement();

    expect(component.browserReplacementTarget()).toBeNull();
    expect(component.canvasOpen()).toBe(true);
    expect(canvas.openBrowser).toHaveBeenCalledOnce();
    expect(canvas.openBrowser).toHaveBeenCalledWith(undefined, 4);
  });

  it('cancels replacement when the Canvas changes while confirmation is open', () => {
    const {component, canvas} = createFixture({threadId: 'thread-1'});
    component.ngOnInit();
    TestBed.tick();
    canvas.browserCapability.set({
      feature_enabled: true,
      can_open_browser: true,
      workspace_ready: true,
      reason: null,
    });
    canvas.state.set(presentedState(4));
    TestBed.tick();
    component.openSharedBrowser();

    canvas.state.set(presentedState(5));
    TestBed.tick();

    expect(component.browserReplacementTarget()).toBeNull();
    component.confirmSharedBrowserReplacement();
    expect(canvas.openBrowser).not.toHaveBeenCalled();
  });

  it('does not confirm when opening an already-presented browser', () => {
    const {component, canvas} = createFixture({threadId: 'thread-1'});
    component.ngOnInit();
    TestBed.tick();
    canvas.browserCapability.set({
      feature_enabled: true,
      can_open_browser: true,
      workspace_ready: true,
      reason: null,
    });
    canvas.state.set({
      ...presentedState(6),
      source: {type: 'browser'},
      title: 'Shared browser',
      renderer: 'auto',
      source_version: null,
      capabilities: {
        can_edit: false,
        can_pop_out: true,
        can_take_control: true,
        can_stream_browser: true,
      },
    });
    TestBed.tick();

    component.openSharedBrowser();

    expect(component.browserReplacementTarget()).toBeNull();
    expect(canvas.openBrowser).toHaveBeenCalledOnce();
    expect(canvas.openBrowser).toHaveBeenCalledWith(undefined, 6);
  });

  it('matches replacement approval to the exact thread, revision, and source', () => {
    const target = {
      threadId: 'thread-1',
      presentationRevision: 4,
      sourceKey: 'workspace_file:output/report.md',
    };

    expect(browserReplacementTargetMatches(target, 'thread-1', presentedState(4))).toBe(true);
    expect(browserReplacementTargetMatches(target, 'thread-2', presentedState(4))).toBe(false);
    expect(browserReplacementTargetMatches(target, 'thread-1', presentedState(5))).toBe(false);
    expect(browserReplacementTargetMatches(
      target,
      'thread-1',
      presentedState(4, 'output/other.md'),
    )).toBe(false);
  });

  it('keeps an unsupported feature-visible browser action disabled with its reason', () => {
    const {component, canvas} = createFixture({threadId: 'thread-1'});
    component.ngOnInit();
    TestBed.tick();
    canvas.browserCapability.set({
      feature_enabled: true,
      can_open_browser: false,
      workspace_ready: false,
      reason: 'workspace_required',
    });

    expect(component.browserActionVisible()).toBe(true);
    expect(component.browserActionDisabled()).toBe(true);
    expect(component.browserActionTooltipKey()).toBe(
      'canvas.browser.reason.workspace_required',
    );
  });
});

// Task 14, item B (session_config_drift_resume.md §8.3): "Start a new
// session" used to navigate with nothing carried over. It now hands off a
// single `from=<threadId>` query param — never a list of surviving ids, so
// session-create can re-derive what's still valid at load time instead of
// trusting a snapshot baked into the URL.
describe('ChatPageComponent onStartNewSession', () => {
  afterEach(() => TestBed.resetTestingModule());

  it('navigates to session-create with the current thread as ?from=', () => {
    const {component, chat, router} = createFixture({threadId: 'thread-1'});
    component.ngOnInit();
    TestBed.tick();
    // The mocked `connect()` has no side effect on `chat.threadId` the way
    // the real PersistentChatService does once attached — set it explicitly
    // to reproduce an actually-connected session.
    chat.threadId.set('thread-1');
    chat.pendingDrift.set([
      {id: 'connector:abc', kind: 'connector', reason: 'deleted', label: 'KurortEngine'},
    ]);

    component.onStartNewSession();

    expect(router.navigate).toHaveBeenCalledWith(
      ['/sessions/new'],
      {queryParams: {from: 'thread-1'}},
    );
    expect(chat.pendingDrift()).toBeNull();
  });

  it('falls back to a plain navigation if there is somehow no current thread', () => {
    const {component, chat, router} = createFixture({draft: true});
    component.ngOnInit();
    TestBed.tick();
    expect(chat.threadId()).toBeNull();

    component.onStartNewSession();

    expect(router.navigate).toHaveBeenCalledWith(['/sessions/new'], undefined);
  });
});

describe('ChatPageComponent settings pane (live_session_settings.md Slice A)', () => {
  afterEach(() => TestBed.resetTestingModule());

  it('settings takes the right pane over from the canvas and gives it back', () => {
    const {component, canvas} = createFixture({threadId: 'thread-1'});
    component.ngOnInit();
    TestBed.tick();
    canvas.state.set(presentedState(1));
    TestBed.tick();
    expect(component.canvasContentVisible()).toBe(true);

    component.openSettings();
    expect(component.settingsVisible()).toBe(true);
    expect(component.canvasContentVisible()).toBe(false);
    // Canvas stays "open" behind settings — closing settings restores it.
    expect(component.canvasOpen()).toBe(true);

    component.closeSettings();
    expect(component.settingsVisible()).toBe(false);
    expect(component.canvasContentVisible()).toBe(true);
  });

  it('a canvas push while settings holds the pane badges instead of stealing', () => {
    const {component, canvas} = createFixture({threadId: 'thread-1'});
    component.ngOnInit();
    TestBed.tick();
    component.openSettings();
    expect(component.settingsVisible()).toBe(true);

    canvas.state.set(presentedState(1));
    TestBed.tick();

    // Settings keeps the pane; the push is staged behind it with a badge.
    expect(component.settingsVisible()).toBe(true);
    expect(component.canvasContentVisible()).toBe(false);
    expect(component.canvasOpen()).toBe(true);
    expect(component.canvasPending()).toBe(true);

    // Closing settings delivers the pending canvas and clears the badge.
    component.closeSettings();
    expect(component.canvasContentVisible()).toBe(true);
    expect(component.canvasPending()).toBe(false);
  });

  it('explicitly opening the canvas reclaims the pane from settings', () => {
    const {component, canvas} = createFixture({threadId: 'thread-1'});
    component.ngOnInit();
    TestBed.tick();
    component.openSettings();
    canvas.state.set(presentedState(1));
    TestBed.tick();
    expect(component.canvasPending()).toBe(true);

    component.openCanvas(true);
    expect(component.settingsOpen()).toBe(false);
    expect(component.canvasContentVisible()).toBe(true);
    expect(component.canvasPending()).toBe(false);
  });

  it('mobile settings takes the full screen like a focused canvas', () => {
    const {component, viewport} = createFixture({threadId: 'thread-1'});
    viewport.isMobile.set(true);
    component.ngOnInit();
    TestBed.tick();

    component.openSettings();
    expect(component.settingsFocus()).toBe(true);
    expect(component.chatAreaHidden()).toBe(true);

    component.closeSettings();
    expect(component.chatAreaHidden()).toBe(false);
  });

  it('a thread switch resets the settings pane like the canvas', () => {
    const {component, canvas} = createFixture({threadId: 'thread-1'});
    component.ngOnInit();
    TestBed.tick();
    component.openSettings();

    canvas.selectThread('thread-2');
    TestBed.tick();

    expect(component.settingsOpen()).toBe(false);
    expect(component.canvasPending()).toBe(false);
  });
});
