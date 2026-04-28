import {Component, computed, inject, OnInit, signal} from '@angular/core';
import {Router} from '@angular/router';
import {HttpClient} from '@angular/common/http';
import {TitleCasePipe} from '@angular/common';
import {TranslocoDatePipe} from '@jsverse/transloco-locale';
import {firstValueFrom} from 'rxjs';
import {environment} from '../../core/environment';
import {PersistentChatService} from '../../core/services/persistent-chat.service';
import {ModelService} from '../../core/services/model.service';
import {SettingsService} from '../../core/services/settings.service';
import {AppToastService} from '../../ui/toast';
import {ErrorMessageService} from '../../core/services/error-message.service';
import {UserService} from '../../core/services/user.service';
import {Thread} from '../../core/models/api.model';
import {SidebarToggleComponent} from '../../shell/sidebar-toggle/sidebar-toggle.component';
import {TranslocoPipe, TranslocoService} from '@jsverse/transloco';
import {AppButtonComponent} from '../../ui/button';
import {AppIconButtonComponent} from '../../ui/icon-button';
import {AppTabBarComponent, AppTabComponent} from '../../ui/tab-bar';
import {AppInputComponent} from '../../ui/input';
import {AppSelectComponent} from '../../ui/select';
import {AppChipComponent} from '../../ui/chip';
import {AppIconComponent} from '../../ui/icon';
import {AppFormFieldComponent} from '../../ui/form-field';

interface Project {
    id: string;
    name: string;
    status: string;
    description?: string;
    is_default?: boolean;
}

@Component({
    selector: 'app-sessions-page',
    standalone: true,
    imports: [
        TranslocoDatePipe,
        TitleCasePipe,
        SidebarToggleComponent,
        TranslocoPipe,
        AppButtonComponent,
        AppIconButtonComponent,
        AppTabBarComponent,
        AppTabComponent,
        AppInputComponent,
        AppSelectComponent,
        AppChipComponent,
        AppIconComponent,
        AppFormFieldComponent,
    ],
    template: `
    <div class="page-toggle">
      <app-sidebar-toggle />
    </div>
    <div class="sessions-page">
      <div class="page-header">
        <h2>{{ 'sessions.title' | transloco }}</h2>
        <div class="header-actions">
          <app-button variant="primary" size="sm" (clicked)="goToCreate()">
            <app-icon size="sm">add</app-icon> {{ 'sessions.newSession' | transloco }}
          </app-button>
        </div>
      </div>

      <!-- Active session banner (hidden when filtering to ended sessions) -->
      @if (chat.isConnected() && statusFilter() !== 'ended') {
        <div class="active-banner" (click)="returnToActive()">
          <span class="active-dot"></span>
          <span>{{ 'sessions.activeBanner' | transloco }}</span>
          <span class="active-action">{{ 'sessions.returnToChat' | transloco }}</span>
        </div>
      }

      <!-- Create dialog -->
      @if (showCreate) {
        <div class="create-dialog">
          <h3>{{ 'sessions.create.title' | transloco }}</h3>
          <p class="dialog-hint">{{ 'sessions.create.hint' | transloco }}</p>
          <app-form-field [label]="'sessions.create.titleLabel' | transloco">
            <app-input [(value)]="newTitle" [placeholder]="'sessions.create.titlePlaceholder' | transloco" />
          </app-form-field>
          <app-form-field [label]="'sessions.create.configLabel' | transloco">
            <app-select [(value)]="newConfig">
              <option value="persistent_defaults">{{ 'sessions.create.configDefault' | transloco }}</option>
              <option value="developer">{{ 'sessions.create.configDeveloper' | transloco }}</option>
              <option value="scholar">{{ 'sessions.create.configScholar' | transloco }}</option>
            </app-select>
          </app-form-field>
          <app-form-field [label]="'sessions.create.modelLabel' | transloco">
            <app-select [(value)]="newModel">
              <option value="">{{ 'sessions.create.modelConfigDefault' | transloco }}</option>
              @for (group of modelService.models(); track group.group) {
                <optgroup [label]="group.group">
                  @for (model of group.models; track model) {
                    <option [value]="model">{{ model }}</option>
                  }
                </optgroup>
              }
            </app-select>
          </app-form-field>
          <app-form-field [label]="'sessions.create.projectsLabel' | transloco" [hint]="'sessions.create.projectsHint' | transloco">
            <div class="project-chips">
              @if (projects().length === 0) {
                <span class="chip-hint">{{ 'sessions.create.projectsEmpty' | transloco }}</span>
              } @else {
                @for (project of projects(); track project.id) {
                  <app-chip
                    [selected]="isProjectSelected(project.id)"
                    [ariaLabel]="project.description || project.name"
                    (clicked)="toggleProject(project.id)"
                  >{{ project.name }}</app-chip>
                }
              }
            </div>
          </app-form-field>
          <app-form-field [label]="'sessions.create.permissionLabel' | transloco">
            <app-select [(value)]="newPermission">
              <option value="supervised">{{ 'sessions.create.permissionSupervised' | transloco }}</option>
              <option value="auto_accept">{{ 'sessions.create.permissionAutoAccept' | transloco }}</option>
              <option value="autonomous">{{ 'sessions.create.permissionAutonomous' | transloco }}</option>
            </app-select>
          </app-form-field>
          <div class="dialog-actions">
            <app-button variant="primary" size="sm" [loading]="creating()" (clicked)="createSession()">
              {{ 'sessions.create.create' | transloco }}
            </app-button>
            <app-button variant="secondary" size="sm" (clicked)="showCreate = false">
              {{ 'sessions.create.cancel' | transloco }}
            </app-button>
          </div>
        </div>
      }

      <!-- Session list -->
      <div class="session-list">
        @if (loading()) {
          <div class="loading">{{ 'sessions.loading' | transloco }}</div>
        } @else if (threads().length === 0) {
          <div class="empty-state">
            <app-icon size="inherit" class="empty-icon">chat_bubble_outline</app-icon>
            <p>{{ 'sessions.empty' | transloco }}</p>
          </div>
        } @else {
          <!-- Filter tabs -->
          <app-tab-bar class="filter-tabs" [value]="statusFilter()" (valueChange)="statusFilter.set($event)">
            <app-tab [value]="null">{{ 'sessions.filter.all' | transloco:{ count: threads().length } }}</app-tab>
            <app-tab [value]="'active'">{{ 'sessions.filter.active' | transloco:{ count: activeCount() } }}</app-tab>
            <app-tab [value]="'ended'">{{ 'sessions.filter.ended' | transloco:{ count: endedCount() } }}</app-tab>
          </app-tab-bar>

          @if (filteredThreads().length === 0) {
            <div class="filter-empty">
              @if (statusFilter() === 'active') {
                <app-icon size="inherit" class="empty-icon">check_circle</app-icon>
                <p>{{ 'sessions.emptyFilterActive' | transloco }}</p>
              } @else if (statusFilter() === 'ended') {
                <app-icon size="inherit" class="empty-icon">history</app-icon>
                <p>{{ 'sessions.emptyFilterEnded' | transloco }}</p>
              }
            </div>
          }

          @for (thread of filteredThreads(); track thread.id) {
            <div class="session-card" [class.ended]="thread.status === 'ended'">
              <div class="session-main" (click)="resumeSession(thread)">
                <div class="session-info">
                  <span class="session-status-dot" [class]="thread.status"></span>
                  <span class="session-title">{{ thread.title || ('sessions.untitledSession' | transloco) }}</span>
                  <span class="session-id" title="Session ID">{{ thread.id.slice(0, 8) }}</span>
                  <span class="session-config">{{ thread.config_name | titlecase }}</span>
                </div>
                <div class="session-meta">
                  <span class="meta-item">{{ thread.total_turns || 0 }} {{ ((thread.total_turns || 0) === 1 ? 'sessions.turnsOne' : 'sessions.turnsMany') | transloco }}</span>
                  <span class="meta-item">{{ thread.last_activity | translocoDate:{dateStyle:'short', timeStyle:'short'} }}</span>
                </div>
              </div>
              <div class="session-actions">
                @if (thread.cloud_session_url || thread.nc_session_folder) {
                  <app-icon-button
                    [ariaLabel]="'sessions.tooltip.sessionFiles' | transloco"
                    [tooltip]="'sessions.tooltip.sessionFiles' | transloco"
                    (clicked)="openSessionFiles(thread)"
                  >
                    <app-icon size="sm">cloud</app-icon>
                  </app-icon-button>
                }
                <app-icon-button
                  [ariaLabel]="'sessions.tooltip.resume' | transloco"
                  [tooltip]="'sessions.tooltip.resume' | transloco"
                  (clicked)="resumeSession(thread)"
                >
                  <app-icon size="sm">play_arrow</app-icon>
                </app-icon-button>
                @if (thread.status !== 'ended') {
                  <app-icon-button
                    [ariaLabel]="'sessions.tooltip.end' | transloco"
                    [tooltip]="'sessions.tooltip.end' | transloco"
                    (clicked)="endSession(thread)"
                  >
                    <app-icon size="sm">stop</app-icon>
                  </app-icon-button>
                }
                <app-icon-button
                  variant="danger"
                  [ariaLabel]="'sessions.tooltip.delete' | transloco"
                  [tooltip]="'sessions.tooltip.delete' | transloco"
                  (clicked)="deleteSession(thread)"
                >
                  <app-icon size="sm">delete</app-icon>
                </app-icon-button>
              </div>
            </div>
          }
        }
      </div>
    </div>
  `,
    styles: [`
    :host {
      display: block;
      height: 100%;
      overflow-y: auto;
      background: var(--app-bg, #1e1e2e);
    }

    .page-toggle {
      padding: 8px 12px;
      flex-shrink: 0;
    }

    .page-toggle:empty {
      display: none;
    }

    .sessions-page {
      max-width: 800px;
      margin: 0 auto;
      padding: 24px;
    }

    .page-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 20px;
    }

    .page-header h2 {
      font-size: 18px;
      font-weight: 600;
      color: var(--text-primary, var(--text-primary));
      margin: 0;
    }

    .header-actions {
      display: flex;
      gap: 8px;
    }

    /* Active session banner */
    .active-banner {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 10px 14px;
      background: var(--success-tint);
      border: 1px solid var(--success);
      border-radius: 8px;
      margin-bottom: 16px;
      cursor: pointer;
      font-size: 13px;
      color: var(--success);
      transition: background 0.15s ease;
    }

    .active-banner:hover { background: var(--success-tint); }

    .active-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--success);
      flex-shrink: 0;
      animation: pulse 1.5s infinite;
    }

    .active-action {
      margin-left: auto;
      font-size: 12px;
      font-weight: 600;
      text-decoration: underline;
    }

    .dialog-hint {
      font-size: 11px;
      color: var(--text-muted, #6c7086);
      line-height: 1.5;
      margin-bottom: 8px;
    }

    .dialog-hint { margin-top: -4px; }

    /* Create dialog */
    .create-dialog {
      padding: 16px;
      background: var(--panel-bg, var(--panel-bg));
      border: 1px solid var(--border-color, var(--surface-0));
      border-radius: 8px;
      margin-bottom: 16px;
    }

    .create-dialog h3 {
      font-size: 14px;
      font-weight: 600;
      color: var(--text-primary, var(--text-primary));
      margin: 0 0 12px;
    }


    .project-chips {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-bottom: 4px;
    }

    .chip-hint {
      font-size: 10px;
      color: var(--text-muted, #6c7086);
    }

    .dialog-actions {
      display: flex;
      gap: 8px;
      margin-top: 12px;
    }

    .filter-tabs {
      margin-bottom: 12px;
    }

    /* Session cards */
    .session-card {
      display: flex;
      align-items: center;
      padding: 12px;
      border: 1px solid var(--border-color, var(--surface-0));
      border-radius: 8px;
      background: var(--panel-bg, var(--panel-bg));
      margin-bottom: 8px;
      transition: border-color 0.15s ease;
    }

    .session-card:hover { border-color: var(--accent-color, var(--accent-color)); }
    .session-card.ended { opacity: 0.6; }

    .session-main {
      flex: 1;
      cursor: pointer;
      min-width: 0;
    }

    .session-info {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-bottom: 4px;
    }

    .session-status-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      flex-shrink: 0;
    }

    .session-status-dot.active, .session-status-dot.created { background: var(--success); }
    .session-status-dot.idle { background: var(--warning); }
    .session-status-dot.ended { background: var(--surface-2, #585b70); }

    .session-title {
      font-size: 13px;
      font-weight: 600;
      color: var(--text-primary, var(--text-primary));
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .session-config {
      font-size: 10px;
      padding: 1px 6px;
      border-radius: 3px;
      background: var(--surface-0, var(--surface-0));
      color: var(--text-muted, #6c7086);
      flex-shrink: 0;
    }

    .session-id {
      font-family: var(--font-mono, monospace);
      font-size: 10px;
      color: var(--text-muted, #6c7086);
      flex-shrink: 0;
    }

    .session-meta {
      display: flex;
      gap: 12px;
    }

    .meta-item {
      font-size: 11px;
      color: var(--text-muted, #6c7086);
    }

    .session-actions {
      display: flex;
      gap: 4px;
      flex-shrink: 0;
      margin-left: 8px;
    }

    /* Empty / loading */
    .loading, .empty-state {
      text-align: center;
      padding: 40px;
      color: var(--text-muted, #6c7086);
      font-size: 13px;
    }

    @keyframes pulse {
      0%, 100% { opacity: 1; }
      50% { opacity: 0.4; }
    }

    .empty-icon {
      display: block;
      font-size: 48px;
      margin-bottom: 12px;
      opacity: 0.3;
    }

    .filter-empty {
      text-align: center;
      padding: 32px;
      color: var(--text-muted, #6c7086);
      font-size: 13px;
    }

    @media (max-width: 768px) {
      .sessions-page {
        max-width: 100%;
        padding: 12px;
      }

      .sessions-header {
        flex-wrap: wrap;
        gap: 8px;
      }

      .filter-tabs {
        flex-wrap: wrap;
      }

      .session-card {
        padding: 10px;
      }

      .session-actions button {
        min-height: 36px;
        font-size: 11px;
      }
    }
  `],
})
export class SessionsPageComponent implements OnInit {
    private readonly http = inject(HttpClient);
    private readonly router = inject(Router);
    private readonly toast = inject(AppToastService);
    private readonly errors = inject(ErrorMessageService);
    private readonly userService = inject(UserService);
    private readonly settingsService = inject(SettingsService);
    readonly modelService = inject(ModelService);
    readonly chat = inject(PersistentChatService);
    private readonly transloco = inject(TranslocoService);

    threads = signal<Thread[]>([]);
    projects = signal<Project[]>([]);
    loading = signal(true);
    creating = signal(false);
    statusFilter = signal<string | null>(null);
    selectedProjectIds = signal<string[]>([]);

    showCreate = false;
    newTitle = '';
    newConfig = 'persistent_defaults';
    newModel = this.loadSavedSessionModel();
    newPermission = 'supervised';

    filteredThreads = () => {
        const filter = this.statusFilter();
        const all = this.threads();
        if (!filter) return all;
        return all.filter(t => t.status === filter);
    };

    readonly activeCount = computed(() => this.threads().filter(t => t.status !== 'ended').length);
    readonly endedCount = computed(() => this.threads().filter(t => t.status === 'ended').length);

    ngOnInit(): void {
        this.loadThreads();
        this.loadProjects();
        this.modelService.load();
    }

    async loadThreads(): Promise<void> {
        this.loading.set(true);
        try {
            const data = await firstValueFrom(
                this.http.get<{ threads: Thread[] }>(`${environment.apiUrl}/persistent/threads`)
            );
            this.threads.set(data.threads || []);
        } catch (e) {
            // Silent — sessions not available
        }
        this.loading.set(false);
    }

    async loadProjects(): Promise<void> {
        try {
            const userId = this.userService.currentUserId();
            const params = userId ? `?user_id=${userId}` : '';
            const data = await firstValueFrom(
                this.http.get<Project[]>(`${environment.apiUrl}/projects${params}`)
            );
            this.projects.set(data || []);
            const defaultProject = (data || []).find(p => p.is_default);
            if (defaultProject) {
                this.selectedProjectIds.set([defaultProject.id]);
            }
        } catch (e) {
            // Silent — projects not available
        }
    }

    toggleProject(id: string): void {
        const current = this.selectedProjectIds();
        if (current.includes(id)) {
            this.selectedProjectIds.set(current.filter(p => p !== id));
        } else {
            this.selectedProjectIds.set([...current, id]);
        }
    }

    isProjectSelected(id: string): boolean {
        return this.selectedProjectIds().includes(id);
    }

    async createSession(): Promise<void> {
        this.creating.set(true);
        const body: Record<string, any> = {
            title: this.newTitle || 'Untitled Session',
            config_name: this.newConfig,
            permission_mode: this.newPermission,
        };
        if (this.newModel) {
            body['model'] = this.newModel;
            this.persistSessionModel(this.newModel);
        }
        if (this.selectedProjectIds().length > 0) {
            body['project_ids'] = this.selectedProjectIds();
        }
        this.showCreate = false;
        this.newTitle = '';
        this.selectedProjectIds.set([]);
        // Navigate immediately to chat view with spinner, create thread in background
        this.router.navigate(['/sessions', '_creating'], {state: {createBody: body}});
        this.creating.set(false);
    }

    async resumeSession(thread: Thread): Promise<void> {
        if (thread.status === 'ended' || thread.status === 'idle') {
            try {
                await firstValueFrom(
                    this.http.post(`${environment.apiUrl}/persistent/threads/${thread.id}/resume`, {})
                );
                thread.status = 'created';
            } catch (e: any) {
                this.toast.danger(this.errors.translate(e, 'errors.sessions.resumeFailed'));
                return;
            }
        }
        this.router.navigate(['/sessions', thread.id]);
    }

    openSessionFiles(thread: Thread): void {
        // Prefer the backend-computed URL (works for all backends).
        if (thread.cloud_session_url) {
            window.open(thread.cloud_session_url, '_blank');
            return;
        }
        // Legacy fallback for Nextcloud sessions without a computed URL.
        if (!thread.nc_session_folder || !environment.cloudUrl) return;
        const folderName = thread.nc_session_folder.split('/').pop();
        window.open(`${environment.cloudUrl}/apps/files/?dir=/${folderName}`, '_blank');
    }

    async endSession(thread: Thread): Promise<void> {
        if (!confirm(this.transloco.translate('sessions.confirmEnd'))) return;
        try {
            await firstValueFrom(
                this.http.delete(`${environment.apiUrl}/persistent/threads/${thread.id}`)
            );
            this.loadThreads();
        } catch (e: any) {
            this.toast.danger(this.errors.translate(e, 'errors.sessions.endFailed'));
        }
    }

    async deleteSession(thread: Thread): Promise<void> {
        if (!confirm(this.transloco.translate('sessions.confirmDelete'))) return;
        try {
            await firstValueFrom(
                this.http.delete(`${environment.apiUrl}/persistent/threads/${thread.id}?permanent=true`)
            );
            this.loadThreads();
        } catch (e: any) {
            this.toast.danger(this.errors.translate(e, 'errors.sessions.deleteFailed'));
        }
    }

    goToCreate(): void {
        this.router.navigate(['/sessions/new']);
    }

    returnToActive(): void {
        const threadId = this.chat.threadId();
        if (threadId) {
            this.router.navigate(['/sessions', threadId]);
        }
    }

    private loadSavedSessionModel(): string {
        try {
            return localStorage.getItem('default_session_model') ?? '';
        } catch {
            return '';
        }
    }

    private persistSessionModel(model: string): void {
        try {
            localStorage.setItem('default_session_model', model);
        } catch { /* localStorage may be unavailable */ }
        this.settingsService.updatePreferences({ default_session_model: model }).subscribe();
    }
}
