import {computed, inject, Injectable, signal} from '@angular/core';
import {ApiService} from './api.service';
import type {GrantCatalog} from '../models/api.model';
import {allowedEnumOptions} from '../../views/agent-settings/capability-gates';

/** Permission modes, lowest→highest autonomy (mirrors the backend
 * `_PERMISSION_ORDER` in src/core/capability_grants.py). */
export const PERMISSION_MODES = ['supervised', 'auto_accept', 'autonomous'];

/**
 * The caller's resolved capability grants, fetched once and shared. Drives
 * permission-mode dropdown greying so a user is never offered a mode the
 * backend will deny at provisioning — the UX half of
 * docs/issues/session_permission_mode_grant_denied_ready_timeout.md.
 *
 * Fails open (grants ⇒ all modes) while loading, for admins, and on error:
 * Phase 1's provisioning pre-flight is the authoritative backstop, so this
 * layer only needs to hide options the user demonstrably cannot use.
 */
@Injectable({providedIn: 'root'})
export class CapabilitiesService {
  private readonly api = inject(ApiService);

  // undefined = loading; null = admin / unrestricted; else the resolved grants dict.
  readonly grants = signal<Record<string, unknown> | null | undefined>(undefined);
  readonly catalog = signal<GrantCatalog>({});

  constructor() {
    this.load();
  }

  load(): void {
    this.api.getMyCapabilities().subscribe((c) => {
      this.grants.set(c ? c.grants : null);
      if (c?.catalog) this.catalog.set(c.catalog);
    });
  }

  /** permission_mode options at/below the user's ceiling (admin/loading ⇒ all). */
  readonly permissionModes = computed(() =>
    allowedEnumOptions(this.grants() ?? null, 'permission_mode', PERMISSION_MODES, this.catalog()),
  );

  /** True if `mode` is selectable for this user. */
  allowsPermissionMode(mode: string): boolean {
    return this.permissionModes().includes(mode);
  }

  /** True when at least one permission mode is gated (drives the lock hint). */
  readonly permissionRestricted = computed(
    () => this.permissionModes().length < PERMISSION_MODES.length,
  );
}
