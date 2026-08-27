import {describe, it, expect} from 'vitest';
import {TestBed} from '@angular/core/testing';
import {NEVER, of} from 'rxjs';
import {CapabilitiesService} from './capabilities.service';
import {ApiService} from './api.service';
import type {GrantCatalog, UserCapabilities} from '../models/api.model';

const CATALOG: GrantCatalog = {
  permission_mode: {
    type: 'enum',
    default: 'supervised',
    restrict_only: true,
    order: ['supervised', 'auto_accept', 'autonomous'],
  },
};

function make(caps: UserCapabilities | null): CapabilitiesService {
  TestBed.configureTestingModule({
    providers: [
      CapabilitiesService,
      {provide: ApiService, useValue: {getMyCapabilities: () => of(caps)}},
    ],
  });
  return TestBed.inject(CapabilitiesService);
}

describe('CapabilitiesService', () => {
  it('non-admin, supervised ceiling ⇒ only supervised is selectable', () => {
    const svc = make({is_admin: false, grants: {permission_mode: 'supervised'}, catalog: CATALOG});
    expect(svc.permissionModes()).toEqual(['supervised']);
    expect(svc.allowsPermissionMode('auto_accept')).toBe(false);
    expect(svc.allowsPermissionMode('autonomous')).toBe(false);
    expect(svc.permissionRestricted()).toBe(true);
  });

  it('non-admin, auto_accept ceiling ⇒ supervised + auto_accept (autonomous gated)', () => {
    const svc = make({is_admin: false, grants: {permission_mode: 'auto_accept'}, catalog: CATALOG});
    expect(svc.permissionModes()).toEqual(['supervised', 'auto_accept']);
    expect(svc.allowsPermissionMode('autonomous')).toBe(false);
    expect(svc.permissionRestricted()).toBe(true);
  });

  it('admin (grants null) ⇒ all modes, not restricted', () => {
    const svc = make({is_admin: true, grants: null, catalog: CATALOG});
    expect(svc.permissionModes()).toEqual(['supervised', 'auto_accept', 'autonomous']);
    expect(svc.permissionRestricted()).toBe(false);
  });

  it('API unavailable (null) ⇒ fails open to all modes (Phase 1 backstops)', () => {
    const svc = make(null);
    expect(svc.permissionModes()).toEqual(['supervised', 'auto_accept', 'autonomous']);
    expect(svc.permissionRestricted()).toBe(false);
  });
});

describe('CapabilitiesService.canPublishDatasources', () => {
  it('is true for admins (grants === null)', () => {
    const svc = make({is_admin: true, grants: null, catalog: CATALOG});
    expect(svc.canPublishDatasources()).toBe(true);
  });

  it('is true when the grant resolves true', () => {
    const svc = make({
      is_admin: false,
      grants: {public_datasources: true},
      catalog: CATALOG,
    });
    expect(svc.canPublishDatasources()).toBe(true);
  });

  it('is false when the grant is absent (deny-by-default)', () => {
    const svc = make({is_admin: false, grants: {}, catalog: CATALOG});
    expect(svc.canPublishDatasources()).toBe(false);
  });

  it('fails CLOSED on fetch error, unlike the fail-open mode helpers', () => {
    // Publishing exposes credentials org-wide; on error the section hides
    // (the server gate is the backstop either way).
    const svc = make(null);
    expect(svc.canPublishDatasources()).toBe(false);
  });

  it('fails closed while loading', () => {
    TestBed.configureTestingModule({
      providers: [
        CapabilitiesService,
        {provide: ApiService, useValue: {getMyCapabilities: () => NEVER}},
      ],
    });
    const svc = TestBed.inject(CapabilitiesService);
    expect(svc.canPublishDatasources()).toBe(false);
  });
});

describe('CapabilitiesService.protectedCloudAvailable', () => {
  it('is true when the deployment feature flag is on', () => {
    const svc = make({
      is_admin: false,
      grants: {},
      catalog: CATALOG,
      features: {protected_cloud: true},
    });
    expect(svc.protectedCloudAvailable()).toBe(true);
  });

  it('is false when the flag is off', () => {
    const svc = make({
      is_admin: false,
      grants: {},
      catalog: CATALOG,
      features: {protected_cloud: false},
    });
    expect(svc.protectedCloudAvailable()).toBe(false);
  });

  it('is false when features is absent from the payload', () => {
    const svc = make({is_admin: true, grants: null, catalog: CATALOG});
    expect(svc.protectedCloudAvailable()).toBe(false);
  });

  it('is false while loading / on fetch error', () => {
    const svc = make(null);
    expect(svc.protectedCloudAvailable()).toBe(false);
  });
});

describe('CapabilitiesService.datasourceScopeAutoAttachAvailable', () => {
  it('is true only when the deployment explicitly advertises v1', () => {
    const svc = make({
      is_admin: false,
      grants: {},
      catalog: CATALOG,
      features: {datasource_scope_auto_attach_v1: true},
    });
    expect(svc.datasourceScopeAutoAttachAvailable()).toBe(true);
  });

  it('is false when the flag is false', () => {
    const svc = make({
      is_admin: false,
      grants: {},
      catalog: CATALOG,
      features: {datasource_scope_auto_attach_v1: false},
    });
    expect(svc.datasourceScopeAutoAttachAvailable()).toBe(false);
  });

  it('is false when the flag is omitted', () => {
    const svc = make({
      is_admin: false,
      grants: {},
      catalog: CATALOG,
    });
    expect(svc.datasourceScopeAutoAttachAvailable()).toBe(false);
  });

  it('fails closed when the capabilities request fails', () => {
    expect(make(null).datasourceScopeAutoAttachAvailable()).toBe(false);
  });

  it('fails closed while loading', () => {
    TestBed.configureTestingModule({
      providers: [
        CapabilitiesService,
        {provide: ApiService, useValue: {getMyCapabilities: () => NEVER}},
      ],
    });
    expect(TestBed.inject(CapabilitiesService).datasourceScopeAutoAttachAvailable()).toBe(false);
  });
});
