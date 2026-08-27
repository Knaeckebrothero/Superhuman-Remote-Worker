import {inject, Injectable, signal} from '@angular/core';
import {HttpClient} from '@angular/common/http';
import {catchError, of, tap} from 'rxjs';
import {environment} from '../environment';

/**
 * Cockpit-side mirror of the orchestrator's readiness payload. Drives the
 * onboarding gate: provider configured → models added → model defaults pinned
 * → application experts selected. Required capabilities are ``chat``, ``embedding``,
 * ``auxiliary``; the optional ones are surfaced separately so the UI can
 * show "vision falls back to chat" hints without blocking.
 */
export interface SystemReadiness {
  ready: boolean;
  missing_providers: string[];
  missing_capabilities: string[];
  missing_defaults: string[];
  /** Absent only during a rolling upgrade against a pre-feature backend. */
  missing_expert_defaults?: string[];
  optional_capability_fallbacks: Record<string, string | null>;
}

const EMPTY_READINESS: SystemReadiness = {
  ready: false,
  missing_providers: [],
  missing_capabilities: [],
  missing_defaults: [],
  missing_expert_defaults: [],
  optional_capability_fallbacks: {},
};

/**
 * Reads ``GET /api/system/readiness`` and exposes a signal-cached snapshot.
 * The onboarding banner consumes ``readiness()`` to decide whether to render
 * the setup checklist, and the dispatcher's 503 surfaces the same
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
