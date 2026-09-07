import { Injectable, inject, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { environment } from '../environment';

export interface SudoRule {
  id: string;
  pattern: string;
  action: 'approve' | 'deny' | 'review';
  priority: number;
  description: string;
  created_by: string;
  created_at: string;
  enabled: boolean;
}

/**
 * Sudo auto-approval rules (`/api/sudo/rules`). The requests themselves
 * reach the cockpit as `sudo_request` feed rows since slice 3 of the
 * unified notification system — approve/deny are the row's declared
 * actions, and `/api/sudo/events` is no longer consumed here.
 */
@Injectable({ providedIn: 'root' })
export class SudoService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.apiUrl;

  readonly rules = signal<SudoRule[]>([]);

  /** Load auto-approval rules. */
  loadRules(): void {
    this.http.get<SudoRule[]>(`${this.baseUrl}/sudo/rules`).subscribe({
      next: (data) => this.rules.set(data),
    });
  }

  /** Create an auto-approval rule. */
  createRule(
    pattern: string,
    action: string,
    priority = 100,
    description = '',
  ): void {
    this.http
      .post<SudoRule>(`${this.baseUrl}/sudo/rules`, {
        pattern,
        action,
        priority,
        description,
      })
      .subscribe({
        next: (rule) => this.rules.update((r) => [...r, rule]),
      });
  }

  /** Delete an auto-approval rule. */
  deleteRule(ruleId: string): void {
    this.http.delete(`${this.baseUrl}/sudo/rules/${ruleId}`).subscribe({
      next: () => this.rules.update((r) => r.filter((x) => x.id !== ruleId)),
    });
  }
}
