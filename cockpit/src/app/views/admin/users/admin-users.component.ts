import {ChangeDetectionStrategy, Component, computed, inject, OnInit} from '@angular/core';
import {SidebarToggleComponent} from '../../../shell/sidebar-toggle/sidebar-toggle.component';
import {AdminUsersService} from '../../../core/services/admin-users.service';
import {UserService} from '../../../core/services/user.service';
import {User} from '../../../core/models/api.model';
import {AppCheckboxComponent} from '../../../ui/checkbox';
import {AppBadgeComponent} from '../../../ui/badge';

@Component({
  selector: 'app-admin-users',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [SidebarToggleComponent, AppCheckboxComponent, AppBadgeComponent],
  template: `
    <div class="admin-page">
      <div class="admin-container">
        <div class="page-header">
          <app-sidebar-toggle />
          <h1 class="page-title">Users &amp; VM Workspaces</h1>
        </div>
        <p class="page-desc">
          Manage user roles and grant access to VM workspaces.
        </p>

        <!-- Kill-switch -->
        <section class="admin-section">
          <h2 class="section-title">Global VM Workspaces</h2>
          <p class="section-desc">
            When disabled, every VM workspace request is rejected — including
            those from admin users. Already-running VMs are not torn down.
          </p>
          <app-checkbox
            [checked]="admin.vmWorkspaces().enabled"
            (changed)="setKillSwitch($event)"
          >
            <span class="toggle-label">
              VM workspaces enabled
              @if (!admin.vmWorkspaces().enabled) {
                <app-badge tone="danger" size="xs" appearance="solid">disabled</app-badge>
              }
            </span>
          </app-checkbox>
          @if (admin.vmWorkspaces().updated_at) {
            <p class="muted small">
              Last changed {{ formatDate(admin.vmWorkspaces().updated_at) }}
              by {{ admin.vmWorkspaces().updated_by || 'unknown' }}
            </p>
          }
        </section>

        <!-- Users -->
        <section class="admin-section">
          <h2 class="section-title">Users</h2>
          <p class="section-desc">
            Admins always have VM access. Non-admins need an explicit grant.
          </p>

          @if (admin.users().length > 0) {
            <div class="user-table">
              <div class="user-header">
                <span class="col-name">Name</span>
                <span class="col-email">Email</span>
                <span class="col-flag">Admin</span>
                <span class="col-flag">VM</span>
              </div>
              @for (u of admin.users(); track u.id) {
                <div class="user-row">
                  <span class="col-name">
                    <span class="dot" [style.background]="u.avatar_color"></span>
                    {{ u.display_name }}
                  </span>
                  <span class="col-email mono">{{ u.email || '-' }}</span>
                  <span class="col-flag">
                    <app-checkbox
                      size="sm"
                      [checked]="!!u.is_admin"
                      [disabled]="isSelf(u)"
                      [ariaLabel]="'Admin: ' + u.display_name"
                      [title]="isSelf(u) ? 'You cannot clear your own admin flag' : ''"
                      (changed)="toggleAdmin(u, $event)"
                    />
                  </span>
                  <span class="col-flag">
                    <app-checkbox
                      size="sm"
                      [checked]="!!(u.is_admin || u.can_use_vm)"
                      [disabled]="!!u.is_admin"
                      [ariaLabel]="'VM access: ' + u.display_name"
                      [title]="u.is_admin ? 'Admins always have VM access' : ''"
                      (changed)="toggleVm(u, $event)"
                    />
                  </span>
                </div>
              }
            </div>
          } @else {
            <p class="empty-state">No users loaded.</p>
          }
        </section>
      </div>
    </div>
  `,
  styles: [`
    :host {
      display: block;
      height: 100%;
      overflow: auto;
    }
    .admin-page {
      padding: 32px;
      max-width: 900px;
      margin: 0 auto;
      color: var(--text-primary);
    }
    .page-header {
      display: flex;
      align-items: center;
      gap: 12px;
      margin-bottom: 8px;
    }
    .page-title {
      font-size: 24px;
      font-weight: 700;
      margin: 0;
      color: var(--text-primary);
    }
    .page-desc {
      font-size: 13px;
      color: var(--text-muted);
      margin: 0 0 32px 0;
    }
    .admin-section {
      background: var(--panel-bg);
      border: 1px solid var(--border-color);
      border-radius: var(--radius-lg);
      padding: 24px;
      margin-bottom: 24px;
    }
    .section-title {
      font-size: 18px;
      font-weight: 600;
      margin-bottom: 4px;
      color: var(--text-primary);
    }
    .section-desc {
      font-size: 13px;
      color: var(--text-muted);
      margin-bottom: 20px;
    }
    .toggle-label {
      display: inline-flex;
      align-items: center;
      gap: 8px;
    }
    .small {
      font-size: 12px;
      margin-top: 8px;
    }
    .muted {
      color: var(--text-muted);
    }
    .mono {
      font-family: 'JetBrains Mono', 'Fira Code', monospace;
      font-size: 12px;
      color: var(--text-muted);
    }
    .user-table {
      border: 1px solid var(--border-color);
      border-radius: var(--radius-surface);
      overflow: hidden;
    }
    .user-header,
    .user-row {
      display: grid;
      grid-template-columns: 1.5fr 1.8fr 80px 80px;
      padding: 10px 14px;
      gap: 8px;
      align-items: center;
      font-size: 13px;
    }
    .user-header {
      background: var(--surface-0);
      font-weight: 600;
      font-size: 12px;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }
    .user-row {
      border-top: 1px solid var(--border-color);
    }
    .dot {
      display: inline-block;
      width: 10px;
      height: 10px;
      border-radius: 50%;
      margin-right: 8px;
    }
    .empty-state {
      text-align: center;
      color: var(--text-muted);
      padding: 20px;
      font-size: 13px;
    }
  `],
})
export class AdminUsersComponent implements OnInit {
  protected readonly admin = inject(AdminUsersService);
  private readonly users = inject(UserService);

  readonly selfId = computed(() => this.users.currentUser()?.id ?? null);

  ngOnInit(): void {
    this.admin.loadUsers();
    this.admin.loadVmWorkspaces();
  }

  isSelf(u: User): boolean {
    return u.id === this.selfId();
  }

  setKillSwitch(enabled: boolean): void {
    this.admin.setVmWorkspacesEnabled(enabled).subscribe({
      error: () => this.admin.loadVmWorkspaces(),
    });
  }

  toggleAdmin(u: User, checked: boolean): void {
    if (this.isSelf(u) && !checked) {
      this.admin.loadUsers();
      return;
    }
    this.admin.patchUser(u.id, {is_admin: checked}).subscribe({
      error: () => this.admin.loadUsers(),
    });
  }

  toggleVm(u: User, checked: boolean): void {
    if (u.is_admin) {
      return;
    }
    this.admin.patchUser(u.id, {can_use_vm: checked}).subscribe({
      error: () => this.admin.loadUsers(),
    });
  }

  formatDate(iso: string | null): string {
    if (!iso) return '';
    try {
      return new Date(iso).toLocaleString();
    } catch {
      return iso;
    }
  }
}
