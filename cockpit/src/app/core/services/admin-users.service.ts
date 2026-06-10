import {inject, Injectable, signal} from '@angular/core';
import {HttpClient} from '@angular/common/http';
import {catchError, Observable, of, tap} from 'rxjs';
import {User, VmWorkspacesSetting} from '../models/api.model';
import {environment} from '../environment';

export interface AdminUserPatch {
  is_admin?: boolean;
  can_use_vm?: boolean;
  is_approved?: boolean;
}

/** Result of POST /admin/users/approve (bulk admission). */
export interface AdminApproveResult {
  approved_count: number;
  results: {id: string; status: string}[];
}

/**
 * REST client for the admin user management + VM kill-switch surface.
 * Every call is gated by `is_admin=TRUE` server-side; the client does not
 * re-check.
 */
@Injectable({providedIn: 'root'})
export class AdminUsersService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.apiUrl;

  readonly users = signal<User[]>([]);
  readonly vmWorkspaces = signal<VmWorkspacesSetting>({
    enabled: true,
    updated_at: null,
    updated_by: null,
  });

  loadUsers(): void {
    this.http
      .get<User[]>(`${this.baseUrl}/admin/users`)
      .pipe(catchError(() => of([] as User[])))
      .subscribe((rows) => this.users.set(rows));
  }

  patchUser(userId: string, body: AdminUserPatch): Observable<{status: string}> {
    return this.http
      .patch<{status: string}>(`${this.baseUrl}/admin/users/${userId}`, body)
      .pipe(tap(() => this.loadUsers()));
  }

  /**
   * Bulk-approve pending users in a single server-side transaction. Reloads
   * the list on success so the Status column reflects the new state.
   */
  approveUsers(userIds: string[]): Observable<AdminApproveResult> {
    return this.http
      .post<AdminApproveResult>(`${this.baseUrl}/admin/users/approve`, {
        user_ids: userIds,
      })
      .pipe(tap(() => this.loadUsers()));
  }

  loadVmWorkspaces(): void {
    this.http
      .get<VmWorkspacesSetting>(`${this.baseUrl}/admin/system-settings/vm_workspaces`)
      .pipe(
        catchError(() =>
          of<VmWorkspacesSetting>({enabled: true, updated_at: null, updated_by: null}),
        ),
      )
      .subscribe((row) => this.vmWorkspaces.set(row));
  }

  setVmWorkspacesEnabled(enabled: boolean): Observable<VmWorkspacesSetting> {
    return this.http
      .put<VmWorkspacesSetting>(
        `${this.baseUrl}/admin/system-settings/vm_workspaces`,
        {enabled},
      )
      .pipe(tap((row) => this.vmWorkspaces.set(row)));
  }
}
