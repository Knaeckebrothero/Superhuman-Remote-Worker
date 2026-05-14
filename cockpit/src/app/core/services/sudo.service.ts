import { Injectable, inject, signal, computed, NgZone } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { environment } from '../environment';

export interface SudoRequest {
  id: string;
  job_id: string;
  vm_name: string;
  command: string;
  arguments: string[];
  working_directory: string;
  requesting_user: string;
  target_user: string;
  status: string;
  requested_at: string;
  decided_at: string | null;
  decided_by: string | null;
  decision_reason: string | null;
  ttl_seconds: number;
  expires_at: string;
  metadata: Record<string, unknown>;
  request_type: 'sudo_command' | 'vm_upgrade';
}

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

@Injectable({ providedIn: 'root' })
export class SudoService {
  private readonly http = inject(HttpClient);
  private readonly zone = inject(NgZone);
  private readonly baseUrl = environment.apiUrl;

  /** All known requests (populated from REST, updated from SSE). */
  readonly requests = signal<SudoRequest[]>([]);
  readonly rules = signal<SudoRule[]>([]);
  readonly isLoading = signal(false);
  readonly isConnected = signal(false);

  /** Derived: only pending requests. */
  readonly pendingRequests = computed(() =>
    this.requests().filter((r) => r.status === 'pending'),
  );

  /** Derived: count of pending requests (for badge). */
  readonly pendingCount = computed(() => this.pendingRequests().length);

  private eventSource: EventSource | null = null;

  /** Load requests from REST API. */
  loadRequests(jobId?: string, status?: string): void {
    this.isLoading.set(true);
    const params: Record<string, string> = {};
    if (jobId) params['job_id'] = jobId;
    if (status) params['status'] = status;

    this.http
      .get<SudoRequest[]>(`${this.baseUrl}/sudo/requests`, { params })
      .subscribe({
        next: (data) => {
          this.requests.set(data);
          this.isLoading.set(false);
        },
        error: () => this.isLoading.set(false),
      });
  }

  /** Load auto-approval rules. */
  loadRules(): void {
    this.http.get<SudoRule[]>(`${this.baseUrl}/sudo/rules`).subscribe({
      next: (data) => this.rules.set(data),
    });
  }

  /** Approve a pending request. */
  approve(requestId: string, reason = ''): void {
    this.http
      .post<{ id: string; status: string }>(
        `${this.baseUrl}/sudo/requests/${requestId}/approve`,
        { reason },
      )
      .subscribe({
        next: () => this.updateRequestStatus(requestId, 'approved'),
      });
  }

  /** Deny a pending request. */
  deny(requestId: string, reason: string): void {
    this.http
      .post<{ id: string; status: string }>(
        `${this.baseUrl}/sudo/requests/${requestId}/deny`,
        { reason },
      )
      .subscribe({
        next: () => this.updateRequestStatus(requestId, 'denied'),
      });
  }

  /** Approve a VM upgrade request (provision VM and resume job). */
  approveVmUpgrade(requestId: string): void {
    this.http
      .post(`${this.baseUrl}/sudo/requests/${requestId}/approve-upgrade`, {})
      .subscribe({
        next: () => this.updateRequestStatus(requestId, 'approved'),
      });
  }

  /** Resume a VM upgrade request without provisioning a VM. */
  resumeWithoutVm(requestId: string): void {
    this.http
      .post(`${this.baseUrl}/sudo/requests/${requestId}/resume-without-vm`, {})
      .subscribe({
        next: () => this.updateRequestStatus(requestId, 'approved'),
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

  /** Connect to the SSE stream for real-time updates. */
  connectSSE(): void {
    if (this.eventSource) return;

    this.eventSource = new EventSource(`${this.baseUrl}/sudo/events`, {
      withCredentials: true,
    });

    this.eventSource.addEventListener('new_request', (e: MessageEvent) => {
      this.zone.run(() => {
        const data = JSON.parse(e.data) as SudoRequest;
        this.requests.update((reqs) => [
          {
            ...data,
            status: 'pending',
            decided_at: null,
            decided_by: null,
            decision_reason: null,
            ttl_seconds: data.ttl_seconds ?? 300,
            expires_at: data.expires_at ?? '',
            metadata: data.metadata ?? {},
            request_type: data.request_type ?? 'sudo_command',
          },
          ...reqs,
        ]);
      });
    });

    this.eventSource.addEventListener('request_decided', (e: MessageEvent) => {
      this.zone.run(() => {
        const data = JSON.parse(e.data) as {
          id: string;
          status: string;
          decided_by?: string;
          reason?: string;
        };
        this.updateRequestStatus(data.id, data.status);
      });
    });

    this.eventSource.onopen = () => {
      this.zone.run(() => this.isConnected.set(true));
    };

    this.eventSource.onerror = () => {
      this.zone.run(() => this.isConnected.set(false));
    };
  }

  /** Disconnect from the SSE stream. */
  disconnectSSE(): void {
    if (this.eventSource) {
      this.eventSource.close();
      this.eventSource = null;
      this.isConnected.set(false);
    }
  }

  private updateRequestStatus(id: string, status: string): void {
    this.requests.update((reqs) =>
      reqs.map((r) => (r.id === id ? { ...r, status } : r)),
    );
  }
}
