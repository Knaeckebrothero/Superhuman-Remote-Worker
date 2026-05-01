import {inject, Injectable, signal} from '@angular/core';
import {HttpClient} from '@angular/common/http';
import {catchError, of, tap} from 'rxjs';
import {environment} from '../environment';

/**
 * Cockpit-side mirror of the orchestrator's readiness payload. Drives the
 * three-step onboarding gate: provider configured → models added →
 * defaults pinned. Required capabilities are ``chat``, ``embedding``,
 * ``auxiliary``; the optional ones are surfaced separately so the UI can
 * show "vision falls back to chat" hints without blocking.
 */
export interface SystemReadiness {
  ready: boolean;
  missing_providers: string[];
  missing_capabilities: string[];
  missing_defaults: string[];
  optional_capability_fallbacks: Record<string, string | null>;
}

const EMPTY_READINESS: SystemReadiness = {
  ready: false,
  missing_providers: [],
  missing_capabilities: [],
  missing_defaults: [],
  optional_capability_fallbacks: {},
};

/**
 * Reads ``GET /api/system/readiness`` and exposes a signal-cached snapshot.
 * The onboarding banner consumes ``readiness()`` to decide whether to render
 * the three-step checklist, and the dispatcher's 503 surfaces the same
 * shape on ``POST /api/jobs`` / ``POST /api/persistent/threads`` — keeping
 * one vocabulary across cockpit + backend.
 */
@Injectable({providedIn: 'root'})
export class ReadinessService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.apiUrl;

  readonly readiness = signal<SystemReadiness>({...EMPTY_READINESS});
  readonly loaded = signal(false);

  load(): void {
    this.http
      .get<SystemReadiness>(`${this.baseUrl}/system/readiness`)
      .pipe(
        catchError(() => of<SystemReadiness>({...EMPTY_READINESS})),
        tap((res) => {
          this.readiness.set(res);
          this.loaded.set(true);
        }),
      )
      .subscribe();
  }
}
