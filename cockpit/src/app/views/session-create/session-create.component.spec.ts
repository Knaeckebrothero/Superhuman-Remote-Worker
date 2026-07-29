import {provideHttpClient} from '@angular/common/http';
import {HttpTestingController, provideHttpClientTesting} from '@angular/common/http/testing';
import {signal} from '@angular/core';
import {TestBed} from '@angular/core/testing';
import {Router} from '@angular/router';
import {afterEach, describe, expect, it, vi} from 'vitest';
import {CapabilitiesService} from '../../core/services/capabilities.service';
import {ErrorMessageService} from '../../core/services/error-message.service';
import {ModelService} from '../../core/services/model.service';
import {UserService} from '../../core/services/user.service';
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
