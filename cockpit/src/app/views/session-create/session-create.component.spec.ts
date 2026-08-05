import {provideHttpClient} from '@angular/common/http';
import {HttpTestingController, provideHttpClientTesting} from '@angular/common/http/testing';
import {Injector, runInInjectionContext, signal} from '@angular/core';
import {TestBed} from '@angular/core/testing';
import {Router} from '@angular/router';
import {of} from 'rxjs';
import {TranslocoService} from '@jsverse/transloco';
import {afterEach, describe, expect, it, vi} from 'vitest';
import {CapabilitiesService} from '../../core/services/capabilities.service';
import {ErrorMessageService} from '../../core/services/error-message.service';
import {ModelService} from '../../core/services/model.service';
import {UserService} from '../../core/services/user.service';
import {ApiService} from '../../core/services/api.service';
import {ModelGroupComponent} from '../agent-settings/model-group.component';
import {protectedCloudToggleVisible, SessionCreateComponent} from './session-create.component';

/**
 * The tool-groups preview is the only thing the form uses ApiService for, and
 * it is deliberately stubbed in the existing setups: those tests drive raw
 * HttpTestingController expectations and a real ApiService would drag the
 * whole transloco provider tree in behind it. The preview wiring gets its own
 * describe at the bottom, where the stub IS the subject.
 */
function stubApi(preview: unknown = null) {
  return {previewToolGroups: vi.fn().mockReturnValue(of(preview))};
}

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
        {provide: ApiService, useValue: stubApi()},
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
        {provide: ApiService, useValue: stubApi()},
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
    const request = http.expectOne((r) => r.url.endsWith('/persistent/threads') && r.method === 'POST');
    expect(request.request.body.datasource_ids).toEqual([]);
    request.flush({thread_id: 'thread-xyz'});
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
// have changed the model or switched expert didn't hold up: `prefillFromConfig`
// — the same sink the original inference already named — is also invoked by
// `SessionCreateComponent.applyEffectiveDefault()` (`:498`), which re-resolves
// the effective default expert and, when it differs from what's currently
// selected (the guard at `:502`, `expert && selectedExpert()?.id !== expert.id`),
// fetches its detail and prefills the form. That guard is what decides whether
// ANY post-load re-resolution can do anything at all — and it is gated only on
// `expertSelectionTouched`, which nothing in the Model/Reasoning controls ever
// sets, so it stays live for the component's whole lifetime unless the user
// clicks an expert card.
//
// Three call sites feed that guard: `loadExperts()`'s response landing (`:474`),
// `loadEffectiveDefault()`'s own response landing (`:493` — itself re-issued
// from `loadExperts()`'s tail call at `:479` AND from `loadProjects()`'s
// response handler at `:464`, once a default project auto-populates), and
// `toggleProject()` (`:531`).
//
// The first two settle in parallel at `ngOnInit`, typically within a few
// hundred ms — a near-zero real-world window for a human to have already
// filled in two dropdowns. The project path has no such bound: the user can
// take as long as they like, and a project chip is a perfectly ordinary,
// easily-overlooked thing to click after already configuring Model/Reasoning
// (it sits above the Settings tab, and re-visiting it is not unusual). Its
// default expert can differ from whatever auto-selected earlier (`source:
// 'project'` vs `'user'`/`'application'`), which is exactly what satisfies the
// `:502` guard on demand, any time, with zero expert-grid interaction. That is
// the best fit for "I don't recall changing the model or the expert" — the
// primary test below reproduces that path; the page-load race is kept as a
// secondary variant, since the mechanism is identical and it's worth having
// regression coverage for both.
describe('SessionCreateComponent — reasoning pick lost to an involuntary prefillFromConfig (Task 3)', () => {
  afterEach(() => {
    TestBed.resetTestingModule();
    localStorage.clear();
  });

  /** Builds a real `ModelGroupComponent` (the sink under test, constructed the
   *  same way model-group.component.spec.ts does) plus a `SessionCreateComponent`
   *  fixture wired so its `agentSettings` ViewChild forwards to that real
   *  instance — reproducing AgentSettingsComponent.prefillFromConfig's own
   *  first line (`this.modelGroup?.prefillFromConfig(config)`). Assertions
   *  made against `modelGroup` exercise the actual production clearing logic,
   *  not a stand-in that only proves a call happened. */
  function setupWithRealModelGroup() {
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
        {provide: ApiService, useValue: stubApi()},
      ],
    });
    TestBed.overrideComponent(SessionCreateComponent, {set: {imports: [], template: ''}});

    const fixture = TestBed.createComponent(SessionCreateComponent);
    const http = TestBed.inject(HttpTestingController);
    fixture.detectChanges(); // runs ngOnInit — issues every request below at once

    fixture.componentInstance.agentSettings = {
      prefillFromConfig: (config: Record<string, unknown>) => modelGroup.prefillFromConfig(config),
    } as any;

    return {fixture, http, modelGroup};
  }

  it('the primary path: a project-chip click, well after the pick, resolves a different project-scoped default expert', () => {
    const {fixture, http, modelGroup} = setupWithRealModelGroup();

    // The page loads and settles completely and quietly — expert list,
    // account defaults, datasources, and the (project-less) effective-default
    // lookup all resolve, auto-selecting expert-1. Nothing has been picked
    // yet, so this settling is inert.
    http.expectOne(
      (r) => r.urlWithParams.endsWith('/experts/session_base?type=session&account_defaults=true'),
    ).flush({config: {}});
    http.expectOne((r) => r.url.includes('/datasources/eligible')).flush([]);
    http.expectOne((r) => r.urlWithParams.endsWith('/experts?type=session')).flush([
      {id: 'expert-1', display_name: 'Coder', description: '', icon: 'code', color: '#fff', tags: []},
      {id: 'expert-2', display_name: 'Researcher', description: '', icon: 'science', color: '#fff', tags: []},
    ]);
    http.expectOne((r) => r.url.includes('/expert-defaults')).flush({
      personal_defaults_allowed: true,
      defaults: {
        worker: {application: null, personal: null, effective: null, source: 'application'},
        session: {application: {id: 'expert-1'}, personal: null, effective: {id: 'expert-1'}, source: 'application'},
      },
    });
    http.expectOne(
      (r) => r.urlWithParams.endsWith('/experts/expert-1?account_defaults=true'),
    ).flush({config: {llm: {}}});
    expect(fixture.componentInstance.selectedExpert()?.id).toBe('expert-1');
    expect(modelGroup.reasoningResetNotice()).toBe(false); // quiescent — nothing to lose yet

    // Unbounded time later, the user fills in the Settings tab: a model, then
    // Reasoning: Max. No pending requests, nothing racing — a plain, settled
    // page. No expert card is ever clicked.
    modelGroup.onSessionModelChange('gpt-5.6-sol');
    modelGroup.onSessionReasoningChange('max');
    expect(modelGroup.sessionReasoning()).toBe('max');

    // The user clicks a project chip — an ordinary action on this form, and
    // the only one taken besides the Model/Reasoning picks. toggleExpert()
    // never runs; expertSelectionTouched stays false throughout.
    fixture.componentInstance.toggleProject('project-x');

    http.expectOne((r) => r.url.includes('/datasources/eligible')).flush([]);
    // The project's own effective default expert differs from expert-1 —
    // exactly what the `:502` guard checks for, and exactly what a per-project
    // override (`source: 'project'`) ordinarily produces.
    http.expectOne((r) => r.url.includes('/expert-defaults')).flush({
      personal_defaults_allowed: true,
      defaults: {
        worker: {application: null, personal: null, effective: null, source: 'application'},
        session: {application: {id: 'expert-1'}, personal: null, effective: {id: 'expert-2'}, source: 'project'},
      },
    });
    // That resolution just auto-selected expert-2 and fetched its detail — a
    // request the user never asked for and a control (the expert grid) they
    // never touched.
    http.expectOne(
      (r) => r.urlWithParams.endsWith('/experts/expert-2?account_defaults=true'),
    ).flush({config: {llm: {}}});

    // The reasoning pick is gone, with no trace it ever reset — the bug as
    // reported — reached via a click on a project chip, minutes after the
    // pick, with no model change and no expert-card click at all.
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

  it('a secondary, narrow-window variant: the plain page-load default-expert resolution lands after an unusually fast pick', () => {
    // Kept for completeness — the mechanism (an involuntary prefillFromConfig)
    // is identical to the primary test above, but this variant's real-world
    // window is only as wide as the page's initial parallel requests take to
    // settle (typically well under a second), which is implausible for a
    // human to beat with two dropdown picks. The project-chip path above is
    // the better fit for the reported incident.
    const {fixture, http, modelGroup} = setupWithRealModelGroup();

    http.expectOne(
      (r) => r.urlWithParams.endsWith('/experts/session_base?type=session&account_defaults=true'),
    ).flush({config: {}});
    http.expectOne((r) => r.url.includes('/datasources/eligible')).flush([]);

    // The user acts before the plain expert-list/effective-default round
    // (already in flight since ngOnInit) has settled. No expert card is ever
    // clicked — expertSelectionTouched stays false for the rest of the test.
    modelGroup.onSessionModelChange('gpt-5.6-sol');
    modelGroup.onSessionReasoningChange('max');
    expect(modelGroup.sessionReasoning()).toBe('max');

    // Only now do the two requests that were already in flight before the
    // user acted — the plain expert list and the effective-default lookup
    // every session-create load issues, with no project or expert
    // interaction required — resolve.
    http.expectOne(
      (r) => r.urlWithParams.endsWith('/experts?type=session'),
    ).flush([
      {id: 'expert-1', display_name: 'Coder', description: '', icon: 'code', color: '#fff', tags: []},
    ]);
    http.expectOne((r) => r.url.includes('/expert-defaults')).flush({
      personal_defaults_allowed: true,
      defaults: {
        worker: {application: null, personal: null, effective: null, source: 'application'},
        session: {application: {id: 'expert-1'}, personal: null, effective: {id: 'expert-1'}, source: 'application'},
      },
    });
    http.expectOne(
      (r) => r.urlWithParams.endsWith('/experts/expert-1?account_defaults=true'),
    ).flush({config: {llm: {}}});

    expect(modelGroup.sessionReasoning()).toBeNull();
    expect(modelGroup.reasoningResetNotice()).toBe(true);
    expect(modelGroup.sessionModel()).toBe('gpt-5.6-sol');

    http.verify();
  });
});


describe('SessionCreateComponent tool preview', () => {
  afterEach(() => TestBed.resetTestingModule());

  function setup(preview: unknown) {
    const api = stubApi(preview);
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        {provide: Router, useValue: {navigate: vi.fn()}},
        {provide: UserService, useValue: {currentUserId: signal<string | null>(null)}},
        {provide: ModelService, useValue: {load: vi.fn()}},
        {provide: ErrorMessageService, useValue: {translate: (_e: unknown, f: string) => f}},
        {
          provide: CapabilitiesService,
          useValue: {protectedCloudAvailable: signal(false), grants: signal(null)},
        },
        {provide: ApiService, useValue: api},
      ],
    });
    TestBed.overrideComponent(SessionCreateComponent, {set: {imports: [], template: ''}});
    const fixture = TestBed.createComponent(SessionCreateComponent);
    const http = TestBed.inject(HttpTestingController);
    return {fixture, http, api};
  }

  const ANSWER = {
    source: 'resolved',
    origin: 'prediction',
    observed_at: null,
    prediction_reason: 'no agent exists for an unsaved session',
    enumerate_only: {shell: ['run_command']},
    tool_groups: {},
    categories: {
      canvas: {state: 'on', settable: true, reason: null, decided_by: 'base', tools: ['get_canvas']},
      shell: {
        state: 'unavailable',
        settable: false,
        reason: 'requires the shell_tools capability grant',
        decided_by: 'grant',
        tools: [],
      },
    },
  };

  it('routes the preview EXACTLY as createSession routes the create', () => {
    // A preview that resolved a different expert layer from the create it
    // previews would be this series' defect rebuilt in the surface built to
    // prevent it. `source: 'user'` marks a DB expert, which must go by
    // expert_id with config_name held at the persistent base.
    const {fixture, api} = setup(ANSWER);
    const component = fixture.componentInstance;
    component.toggleExpert({
      id: 'db-expert-1',
      display_name: 'Coder',
      description: '',
      icon: 'code',
      color: '#fff',
      tags: [],
      source: 'user',
    } as never);

    expect(api.previewToolGroups).toHaveBeenCalledWith({
      config_name: 'session_base',
      expert_id: 'db-expert-1',
      project_id: null,
    });
  });

  it('a bundled expert previews by config_name, matching create', () => {
    const {fixture, api} = setup(ANSWER);
    fixture.componentInstance.toggleExpert({
      id: 'scholar',
      display_name: 'Scholar',
      description: '',
      icon: 'school',
      color: '#fff',
      tags: [],
      source: 'bundled',
    } as never);

    expect(api.previewToolGroups).toHaveBeenCalledWith({
      config_name: 'scholar',
      expert_id: null,
      project_id: null,
    });
  });

  it('the answer reaches the settings surface and re-anchors the tool switches', () => {
    const {fixture, api} = setup(ANSWER);
    const component = fixture.componentInstance;
    const prefill = vi.fn();
    component.agentSettings = {
      prefillFromConfig: vi.fn(),
      prefillFromResolvedToolset: prefill,
      hasToolEdits: () => false,
    } as never;

    component.toggleExpert({
      id: 'scholar', display_name: 'S', description: '', icon: 'x', color: '#fff', tags: [],
      source: 'bundled',
    } as never);

    expect(component.toolPreview()).toEqual(ANSWER);
    expect(prefill).toHaveBeenCalledWith(ANSWER.categories);
    expect(api.previewToolGroups).toHaveBeenCalled();
  });

  it('a late answer never clobbers a switch the user has already flipped', () => {
    // Same class of bug as the live pane's, on the other surface: the config
    // prefill has already run by the time this lands.
    const {fixture} = setup(ANSWER);
    const component = fixture.componentInstance;
    const prefill = vi.fn();
    component.agentSettings = {
      prefillFromConfig: vi.fn(),
      prefillFromResolvedToolset: prefill,
      hasToolEdits: () => true,
    } as never;

    component.toggleExpert({
      id: 'scholar', display_name: 'S', description: '', icon: 'x', color: '#fff', tags: [],
      source: 'bundled',
    } as never);

    // The prediction still renders — it is what the rows read — but the
    // baseline is left alone.
    expect(component.toolPreview()).toEqual(ANSWER);
    expect(prefill).not.toHaveBeenCalled();
  });

  it('the project selection moves the prediction, because the project layer can', () => {
    const {fixture, api} = setup(ANSWER);
    fixture.componentInstance.toggleProject('proj-1');

    expect(api.previewToolGroups).toHaveBeenLastCalledWith({
      config_name: 'session_base',
      expert_id: null,
      project_id: 'proj-1',
    });
  });

  it('a failed preview leaves the surface with no answer rather than a wrong one', () => {
    const {fixture} = setup(null);
    const component = fixture.componentInstance;
    const prefill = vi.fn();
    component.agentSettings = {
      prefillFromConfig: vi.fn(),
      prefillFromResolvedToolset: prefill,
      hasToolEdits: () => false,
    } as never;

    component.toggleProject('proj-1');

    expect(component.toolPreview()).toBeNull();
    expect(prefill).not.toHaveBeenCalled();
  });
});
