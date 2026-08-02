import {provideHttpClient} from '@angular/common/http';
import {HttpTestingController, provideHttpClientTesting} from '@angular/common/http/testing';
import {Injector, runInInjectionContext, signal} from '@angular/core';
import {TestBed} from '@angular/core/testing';
import {Router} from '@angular/router';
import {TranslocoService} from '@jsverse/transloco';
import {afterEach, describe, expect, it, vi} from 'vitest';
import {CapabilitiesService} from '../../core/services/capabilities.service';
import {ErrorMessageService} from '../../core/services/error-message.service';
import {ModelService} from '../../core/services/model.service';
import {UserService} from '../../core/services/user.service';
import {ModelGroupComponent} from '../agent-settings/model-group.component';
import {protectedCloudToggleVisible, SessionCreateComponent} from './session-create.component';

describe('protectedCloudToggleVisible', () => {
  it('hidden when feature off', () => {
    expect(protectedCloudToggleVisible(false, [{ main_cloud_backend: 'nextcloud' }])).toBe(false);
  });
  it('hidden when only default projects selected', () => {
    expect(protectedCloudToggleVisible(true, [{ is_default: true, main_cloud_backend: 'nextcloud' }])).toBe(false);
  });
  it('hidden for non-nextcloud backends', () => {
    expect(protectedCloudToggleVisible(true, [{ main_cloud_backend: 'opencloud' }])).toBe(false);
  });
  it('visible for a selected non-default nextcloud project', () => {
    expect(protectedCloudToggleVisible(true, [
      { is_default: true, main_cloud_backend: 'nextcloud' },
      { is_default: false, main_cloud_backend: 'nextcloud' },
    ])).toBe(true);
  });
});

describe('SessionCreateComponent framework defaults', () => {
  afterEach(() => TestBed.resetTestingModule());

  it('loads persistent session defaults instead of worker defaults', () => {
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        {provide: Router, useValue: {navigate: vi.fn()}},
        {provide: UserService, useValue: {currentUserId: signal<string | null>(null)}},
        {provide: ModelService, useValue: {load: vi.fn()}},
        {
          provide: ErrorMessageService,
          useValue: {translate: (_e: unknown, fallback: string) => fallback},
        },
        {
          provide: CapabilitiesService,
          useValue: {
            protectedCloudAvailable: signal(false),
            grants: signal(null),
          },
        },
      ],
    });
    TestBed.overrideComponent(SessionCreateComponent, {
      set: {imports: [], template: ''},
    });

    const fixture = TestBed.createComponent(SessionCreateComponent);
    const http = TestBed.inject(HttpTestingController);
    fixture.detectChanges();
    const prefillTools = vi.fn();
    fixture.componentInstance.agentSettings = {
      toolsGroup: {prefillFromConfig: prefillTools},
    } as any;

    http.expectOne(
      (request) => request.urlWithParams.endsWith('/experts?type=session'),
    ).flush([]);
    http.expectOne((request) => request.url.endsWith('/expert-defaults')).flush({
      personal_defaults_allowed: true,
      defaults: {
        worker: {application: null, personal: null, effective: null, source: 'application'},
        session: {application: null, personal: null, effective: null, source: 'application'},
      },
    });
    http.expectOne((request) => request.url.endsWith('/datasources/eligible')).flush([]);
    const persistentConfig = {
      agent_id: 'session_base',
      tools: {communication: [], delegation: []},
    };
    // account_defaults=true is load-bearing, not decoration: it is what puts
    // the account's workspace.backend (`virtual`) into the config the form
    // resolves. Without it the form reads session_base's `sandbox`, treats the
    // session as non-lite, and submits repository connectors that create_thread
    // rejects with 400.
    const baseRequest = http.expectOne(
      (request) => request.urlWithParams.endsWith(
        '/experts/session_base?type=session&account_defaults=true',
      ),
    );
    baseRequest.flush({config: persistentConfig});

    expect(fixture.componentInstance.frameworkDefaults()?.['agent_id']).toBe('session_base');
    expect(prefillTools).toHaveBeenCalledWith(persistentConfig);
    http.verify();
  });
});

describe('SessionCreateComponent submit flow', () => {
  afterEach(() => TestBed.resetTestingModule());

  function setup() {
    const navigate = vi.fn().mockResolvedValue(true);
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        {provide: Router, useValue: {navigate}},
        {provide: UserService, useValue: {currentUserId: signal<string | null>(null)}},
        {provide: ModelService, useValue: {load: vi.fn()}},
        {
          provide: ErrorMessageService,
          useValue: {translate: (_e: unknown, fallback: string) => fallback},
        },
        {
          provide: CapabilitiesService,
          useValue: {protectedCloudAvailable: signal(false), grants: signal(null)},
        },
      ],
    });
    TestBed.overrideComponent(SessionCreateComponent, {set: {imports: [], template: ''}});
    const fixture = TestBed.createComponent(SessionCreateComponent);
    const http = TestBed.inject(HttpTestingController);
    return {fixture, http, navigate};
  }

  // The form used to hand its body to a `_creating` route and unmount, so a
  // rejected config destroyed every selection and dropped the user on
  // /sessions with a toast. It now creates first and only navigates on success.
  it('navigates to the created thread once the server accepts the config', async () => {
    const {fixture, http, navigate} = setup();
    const component = fixture.componentInstance;

    const pending = component.createSession();
    http.expectOne((r) => r.url.endsWith('/persistent/threads') && r.method === 'POST')
      .flush({thread_id: 'thread-xyz'});
    await pending;

    expect(navigate).toHaveBeenCalledWith(['/sessions', 'thread-xyz']);
    expect(component.createError()).toBeNull();
  });

  it('stays on the form with the error when the server rejects the config', async () => {
    const {fixture, http, navigate} = setup();
    const component = fixture.componentInstance;
    component.title = 'Keep my settings';

    const pending = component.createSession();
    http.expectOne((r) => r.url.endsWith('/persistent/threads') && r.method === 'POST')
      .flush(
        {detail: 'Lite session backends cannot attach repository connectors'},
        {status: 400, statusText: 'Bad Request'},
      );
    await pending;

    expect(navigate).not.toHaveBeenCalled();
    expect(component.createError()).toBeTruthy();
    expect(component.creating()).toBe(false);
    // Nothing was reset — the user can fix the tier and resubmit.
    expect(component.title).toBe('Keep my settings');
  });
});

// Task 3 — root cause of the vanishing Reasoning pick (dev thread
// 1930dec9-181d-4fd5-a030-90b3d0b363d6). The inference that the user must
// have changed the model or switched expert didn't hold up: this reproduces
// the actual trigger. `applyEffectiveDefault()` re-resolves the effective
// default expert (and, once found, fetches its detail and prefills the form)
// from THREE places — the plain `/experts` list landing, the `/expert-defaults`
// lookup landing, and a project selection resolving — every one of them gated
// only on `expertSelectionTouched`, which nothing in the Model/Reasoning
// controls ever sets. So the very same `prefillFromConfig` sink the brief
// already knew about (model-group.component.ts:589) fires on an involuntary,
// timing-dependent path too, not only on a deliberate expert switch — and it
// can land after the user has already picked a level in the Settings tab,
// which is interactive immediately off framework defaults, before any expert
// resolves. No project or expert interaction is needed to hit it: the plain
// project-less `/expert-defaults` round every session-create load already
// issues is sufficient.
describe('SessionCreateComponent — reasoning pick vs. an in-flight default-expert resolution (Task 3)', () => {
  afterEach(() => {
    TestBed.resetTestingModule();
    localStorage.clear();
  });

  it('silently wipes a Reasoning pick when the effective-default-expert lookup resolves after the user has already picked one', () => {
    // The real ModelGroupComponent — the sink under test — built the same way
    // model-group.component.spec.ts does, standing in for the ViewChild
    // SessionCreateComponent talks to through AgentSettingsComponent. This
    // keeps the assertions honest: they exercise the actual clearing logic,
    // not a stand-in that only proves the call happened.
    const modelServiceMock = {
      models: signal([]),
      auxiliaryModels: signal([]),
      visionModels: signal([]),
      whisperModels: signal([]),
      embeddingModels: signal([]),
      providers: signal([]),
      reasoningByModel: signal<Record<string, {method: string; default: string | null; options: string[]}>>({
        'gpt-5.6-sol': {method: 'effort_enum', default: 'high', options: ['low', 'medium', 'high', 'xhigh', 'max']},
      }),
      loading: signal(false),
      loaded: signal(true),
      load: vi.fn(),
    };
    const translocoMock = {translate: (key: string) => key, langChanges$: {subscribe: () => ({unsubscribe() {}})}, getActiveLang: () => 'en'};
    const modelGroupInjector = Injector.create({
      providers: [
        {provide: ModelService, useValue: modelServiceMock},
        {provide: TranslocoService, useValue: translocoMock},
      ],
    });
    const modelGroup = runInInjectionContext(modelGroupInjector, () => new ModelGroupComponent());

    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        {provide: Router, useValue: {navigate: vi.fn()}},
        {provide: UserService, useValue: {currentUserId: signal<string | null>(null)}},
        {provide: ModelService, useValue: modelServiceMock},
        {provide: ErrorMessageService, useValue: {translate: (_e: unknown, fallback: string) => fallback}},
        {
          provide: CapabilitiesService,
          useValue: {protectedCloudAvailable: signal(false), grants: signal(null)},
        },
      ],
    });
    TestBed.overrideComponent(SessionCreateComponent, {set: {imports: [], template: ''}});

    const fixture = TestBed.createComponent(SessionCreateComponent);
    const http = TestBed.inject(HttpTestingController);
    fixture.detectChanges(); // runs ngOnInit — issues every request below at once

    // Stand-in for the real `agentSettings` ViewChild. AgentSettingsComponent's
    // own prefillFromConfig forwards to the model group as its first line
    // (`this.modelGroup?.prefillFromConfig(config)`) — this fake reproduces
    // exactly that line so fetchExpertDetail()'s real callback runs against
    // the real ModelGroupComponent built above.
    fixture.componentInstance.agentSettings = {
      prefillFromConfig: (config: Record<string, unknown>) => modelGroup.prefillFromConfig(config),
    } as any;

    // The account-defaults fetch resolves — the Settings tab is now showing
    // and interactive off framework defaults. No expert is selected yet.
    http.expectOne(
      (r) => r.urlWithParams.endsWith('/experts/session_base?type=session&account_defaults=true'),
    ).flush({config: {}});
    http.expectOne((r) => r.url.endsWith('/datasources/eligible')).flush([]);

    // The user acts on the form exactly as reported: picks a model, then
    // Reasoning: Max. No expert card is ever clicked — toggleExpert() never
    // runs, so expertSelectionTouched stays false for the rest of the test.
    modelGroup.onSessionModelChange('gpt-5.6-sol');
    modelGroup.onSessionReasoningChange('max');
    expect(modelGroup.sessionReasoning()).toBe('max');

    // Only now do the two requests that were already in flight before the
    // user acted — the plain expert list and the effective-default lookup
    // that every session-create page load issues, with no project or expert
    // interaction required — resolve.
    http.expectOne(
      (r) => r.urlWithParams.endsWith('/experts?type=session'),
    ).flush([
      {id: 'expert-1', display_name: 'Coder', description: '', icon: 'code', color: '#fff', tags: []},
    ]);
    http.expectOne((r) => r.url.endsWith('/expert-defaults')).flush({
      personal_defaults_allowed: true,
      defaults: {
        worker: {application: null, personal: null, effective: null, source: 'application'},
        session: {
          application: {id: 'expert-1'},
          personal: null,
          effective: {id: 'expert-1'},
          source: 'application',
        },
      },
    });

    // That lookup just auto-selected expert-1 and fetched its detail — a
    // request the user never asked for.
    http.expectOne(
      (r) => r.urlWithParams.endsWith('/experts/expert-1?account_defaults=true'),
    ).flush({config: {llm: {}}});

    // The user never touched the model or reasoning selects again and never
    // clicked an expert card — yet the reasoning pick is gone, and there is
    // no trace that it ever reset (the bug as reported).
    expect(modelGroup.sessionReasoning()).toBeNull();
    // The fix (task 3): the reset itself is correct and stays — reasoning
    // vocabularies don't translate across families — but it must no longer
    // be silent.
    expect(modelGroup.reasoningResetNotice()).toBe(true);
    // The model pick "survives" only by coincidence: prefillFromConfig's
    // no-base-model branch restores from the very localStorage key
    // onSessionModelChange had just written, so it silently re-lands on the
    // identical value — indistinguishable from "my pick stuck." This is why
    // the user only noticed the Reasoning field, not the Model field.
    expect(modelGroup.sessionModel()).toBe('gpt-5.6-sol');

    http.verify();
  });
});
