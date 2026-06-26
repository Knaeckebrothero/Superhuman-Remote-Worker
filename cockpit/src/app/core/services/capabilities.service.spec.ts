import {describe, it, expect} from 'vitest';
import {TestBed} from '@angular/core/testing';
import {of} from 'rxjs';
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
