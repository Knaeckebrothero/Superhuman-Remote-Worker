import {provideHttpClient} from '@angular/common/http';
import {HttpTestingController, provideHttpClientTesting} from '@angular/common/http/testing';
import {signal} from '@angular/core';
import {TestBed} from '@angular/core/testing';
import {Router} from '@angular/router';
import {afterEach, describe, expect, it, vi} from 'vitest';
import {CapabilitiesService} from '../../core/services/capabilities.service';
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
    http.expectOne(
      (request) => request.urlWithParams.endsWith('/experts/session_base?type=session'),
    ).flush({
      config: persistentConfig,
    });

    expect(fixture.componentInstance.frameworkDefaults()?.['agent_id']).toBe('session_base');
    expect(prefillTools).toHaveBeenCalledWith(persistentConfig);
    http.verify();
  });
});
