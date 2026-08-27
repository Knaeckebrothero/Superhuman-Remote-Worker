import {Injectable, inject, signal} from '@angular/core';
import {isPlatformBrowser} from '@angular/common';
import {PLATFORM_ID} from '@angular/core';
import {UserService} from '../../core/services/user.service';
import {DEFAULT_PAGE_SIZE, PAGE_SIZE_OPTIONS} from './job-filters';

/**
 * The jobs list's page-size preference, persisted per user.
 *
 * A stated preference should survive the session — the computer asking again
 * next time is the thing NN/g calls out. Storage follows the house key
 * convention `srw.<camelCase>.<userId>`, guarded for SSR and wrapped in
 * try/catch because a browser with storage disabled must degrade to the
 * default rather than throw on every read.
 */
@Injectable({providedIn: 'root'})
export class JobPageSizePreference {
  private readonly platformId = inject(PLATFORM_ID);
  private readonly userService = inject(UserService);

  private readonly _value = signal<number>(DEFAULT_PAGE_SIZE);
  readonly value = this._value.asReadonly();

  private get storageKey(): string | null {
    const userId = this.userService.currentUser()?.id;
    return userId ? `srw.jobsPageSize.${userId}` : null;
  }

  restore(): void {
    if (!isPlatformBrowser(this.platformId)) return;
    const key = this.storageKey;
    if (!key) return;
    try {
      const raw = localStorage.getItem(key);
      const parsed = Number(raw);
      if (PAGE_SIZE_OPTIONS.includes(parsed)) {
        this._value.set(parsed);
      }
    } catch {
      // Storage disabled or full — the default is a fine answer.
    }
  }

  set(pageSize: number): void {
    if (!PAGE_SIZE_OPTIONS.includes(pageSize)) return;
    this._value.set(pageSize);
    if (!isPlatformBrowser(this.platformId)) return;
    const key = this.storageKey;
    if (!key) return;
    try {
      localStorage.setItem(key, String(pageSize));
    } catch {
      // Non-fatal: the preference simply will not survive the reload.
    }
  }
}
