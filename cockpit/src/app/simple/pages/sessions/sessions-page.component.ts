import {Component, computed, inject, OnInit, signal} from '@angular/core';
import {Router} from '@angular/router';
import {FormsModule} from '@angular/forms';
import {HttpClient} from '@angular/common/http';
import {DatePipe, TitleCasePipe} from '@angular/common';
import {firstValueFrom} from 'rxjs';
import {environment} from '../../../core/environment';
import {PersistentChatService} from '../../../core/services/persistent-chat.service';
import {SettingsService} from '../../../core/services/settings.service';
import {ToastService} from '../../../core/services/toast.service';
import {UserService} from '../../../core/services/user.service';
import {Thread} from '../../../core/models/api.model';
import {SidebarToggleComponent} from '../../layout/sidebar-toggle/sidebar-toggle.component';

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
    imports: [FormsModule, DatePipe, TitleCasePipe, SidebarToggleComponent],
    template: `
    <div class="page-toggle">
      <app-sidebar-toggle />
    </div>
    <div class="sessions-page">
      <div class="page-header">
        <h2>Sessions</h2>
        <div class="header-actions">
          <button class="btn btn-primary" (click)="goToCreate()">
            <span class="icon">add</span> New Session
          </button>
        </div>
      </div>

      <!-- Active session banner (hidden when filtering to ended sessions) -->
      @if (chat.isConnected() && statusFilter() !== 'ended') {
        <div class="active-banner" (click)="returnToActive()">
          <span class="active-dot"></span>
          <span>Active session in progress</span>
          <span class="active-action">Return to chat</span>
        </div>
      }

      <!-- Create dialog -->
      @if (showCreate) {
        <div class="create-dialog">
          <h3>New Session</h3>
          <p class="dialog-hint">Creates a thread via the orchestrator and connects through its WebSocket proxy. Requires a persistent agent pod to be registered and bound to the thread.</p>
          <div class="form-group">
            <label>Title</label>
            <input type="text" [(ngModel)]="newTitle" placeholder="Untitled Session" />
          </div>
          <div class="form-group">
            <label>Config</label>
            <select [(ngModel)]="newConfig">
              <option value="persistent_defaults">Default</option>
              <option value="developer">Developer</option>
              <option value="scholar">Scholar</option>
            </select>
          </div>
          <div class="form-group">
            <label>Model</label>
            <select [(ngModel)]="newModel">
              <option value="">Config default</option>
              <optgroup label="Local">
                <option value="openai/gpt-oss-120b">GPT-OSS 120B (local)</option>
              </optgroup>
              <optgroup label="OpenAI">
                <option value="gpt-5.4">GPT-5.4</option>
                <option value="codex/gpt-5.3-codex">Codex (coding)</option>
                <option value="codex/gpt-5.3-codex-spark">Codex Spark (ultra-fast)</option>
              </optgroup>
              <optgroup label="Anthropic">
                <option value="claude-opus-4-6">Claude Opus 4.6</option>
              </optgroup>
              <optgroup label="OpenRouter">
                <option value="openrouter/minimax/minimax-m2.7">MiniMax M2.7</option>
              </optgroup>
            </select>
          </div>
          <div class="form-group">
            <label>Projects</label>
            <div class="project-chips">
              @if (projects().length === 0) {
                <span class="chip-hint">No projects available</span>
              } @else {
                @for (project of projects(); track project.id) {
                  <button
                    type="button"
                    class="project-chip"
                    [class.selected]="isProjectSelected(project.id)"
                    (click)="toggleProject(project.id)"
                    [title]="project.description || project.name"
                  >{{ project.name }}</button>
                }
              }
            </div>
            <span class="chip-hint">Select projects for shared knowledge access</span>
          </div>
          <div class="form-group">
            <label>Permission Mode</label>
            <select [(ngModel)]="newPermission">
              <option value="supervised">Supervised</option>
              <option value="auto_accept">Auto-accept</option>
              <option value="autonomous">Autonomous</option>
            </select>
          </div>
          <div class="dialog-actions">
            <button class="btn btn-primary" (click)="createSession()" [disabled]="creating()">
              {{ creating() ? 'Creating...' : 'Create' }}
            </button>
            <button class="btn btn-secondary" (click)="showCreate = false">Cancel</button>
          </div>
        </div>
      }

      <!-- Session list -->
      <div class="session-list">
        @if (loading()) {
          <div class="loading">Loading sessions...</div>
        } @else if (threads().length === 0) {
          <div class="empty-state">
            <span class="empty-icon">chat_bubble_outline</span>
            <p>No sessions yet. Create one to get started.</p>
          </div>
        } @else {
          <!-- Filter tabs -->
          <div class="filter-tabs">
            <button
              class="filter-tab"
              [class.active]="statusFilter() === null"
              (click)="statusFilter.set(null)"
            >All ({{ threads().length }})</button>
            <button
              class="filter-tab"
              [class.active]="statusFilter() === 'active'"
              (click)="statusFilter.set('active')"
            >Active ({{ activeCount() }})</button>
            <button
              class="filter-tab"
              [class.active]="statusFilter() === 'ended'"
              (click)="statusFilter.set('ended')"
            >Ended ({{ endedCount() }})</button>
          </div>

          @if (filteredThreads().length === 0) {
            <div class="filter-empty">
              @if (statusFilter() === 'active') {
                <span class="empty-icon">check_circle</span>
                <p>No active sessions.</p>
              } @else if (statusFilter() === 'ended') {
                <span class="empty-icon">history</span>
                <p>No ended sessions.</p>
              }
            </div>
          }

          @for (thread of filteredThreads(); track thread.id) {
            <div class="session-card" [class.ended]="thread.status === 'ended'">
              <div class="session-main" (click)="resumeSession(thread)">
                <div class="session-info">
                  <span class="session-status-dot" [class]="thread.status"></span>
                  <span class="session-title">{{ thread.title || 'Untitled Session' }}</span>
                  <span class="session-config">{{ thread.config_name | titlecase }}</span>
                </div>
                <div class="session-meta">
                  <span class="meta-item">{{ thread.total_turns || 0 }} {{ (thread.total_turns || 0) === 1 ? 'turn' : 'turns' }}</span>
                  <span class="meta-item">{{ thread.last_activity | date:'short' }}</span>
                </div>
              </div>
              <div class="session-actions">
                @if (thread.nc_session_folder) {
                  <button class="icon-btn" title="Session Files" (click)="openSessionFiles(thread)">
                    <span class="icon">cloud</span>
                  </button>
                }
                <button class="icon-btn" title="Resume" (click)="resumeSession(thread)">
                  <span class="icon">play_arrow</span>
                </button>
                @if (thread.status !== 'ended') {
                  <button class="icon-btn" title="End" (click)="endSession(thread)">
                    <span class="icon">stop</span>
                  </button>
                }
                <button class="icon-btn danger" title="Delete" (click)="deleteSession(thread)">
                  <span class="icon">delete</span>
                </button>
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
      color: var(--text-primary, #cdd6f4);
      margin: 0;
    }

    .header-actions {
      display: flex;
      gap: 8px;
    }

    .btn {
      display: flex;
      align-items: center;
      gap: 4px;
      padding: 6px 14px;
      border-radius: 6px;
      border: 1px solid var(--border-color, #313244);
      font-size: 12px;
      font-family: inherit;
      cursor: pointer;
      transition: all 0.15s ease;
    }

    .btn-sm { padding: 4px 10px; }

    .btn-primary {
      background: var(--accent-color, #cba6f7);
      color: var(--timeline-bg, #11111b);
      border-color: var(--accent-color, #cba6f7);
    }

    .btn-secondary {
      background: transparent;
      color: var(--text-muted, #6c7086);
    }

    .btn:disabled { opacity: 0.5; cursor: not-allowed; }

    .icon {
      font-family: 'Material Symbols Outlined';
      font-size: 16px;
    }

    /* Active session banner */
    .active-banner {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 10px 14px;
      background: rgba(166, 227, 161, 0.08);
      border: 1px solid #a6e3a1;
      border-radius: 8px;
      margin-bottom: 16px;
      cursor: pointer;
      font-size: 13px;
      color: #a6e3a1;
      transition: background 0.15s ease;
    }

    .active-banner:hover { background: rgba(166, 227, 161, 0.14); }

    .active-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: #a6e3a1;
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
      background: var(--panel-bg, #181825);
      border: 1px solid var(--border-color, #313244);
      border-radius: 8px;
      margin-bottom: 16px;
    }

    .create-dialog h3 {
      font-size: 14px;
      font-weight: 600;
      color: var(--text-primary, #cdd6f4);
      margin: 0 0 12px;
    }

    .form-group {
      margin-bottom: 10px;
    }

    .form-group label {
      display: block;
      font-size: 11px;
      color: var(--text-muted, #6c7086);
      margin-bottom: 4px;
    }

    .form-group input, .form-group select {
      width: 100%;
      padding: 6px 10px;
      border-radius: 4px;
      border: 1px solid var(--border-color, #313244);
      background: var(--surface-0, #313244);
      color: var(--text-primary, #cdd6f4);
      font-size: 12px;
      font-family: inherit;
    }

    .project-chips {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-bottom: 4px;
    }

    .project-chip {
      padding: 4px 10px;
      border-radius: 12px;
      border: 1px solid var(--border-color, #313244);
      background: transparent;
      color: var(--text-muted, #6c7086);
      font-size: 11px;
      font-family: inherit;
      cursor: pointer;
      transition: all 0.15s;
    }

    .project-chip:hover {
      border-color: var(--text-color, #cdd6f4);
      color: var(--text-color, #cdd6f4);
    }

    .project-chip.selected {
      background: var(--accent-color, #a6e3a1);
      border-color: var(--accent-color, #a6e3a1);
      color: var(--bg-color, #1e1e2e);
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

    /* Filter tabs */
    .filter-tabs {
      display: flex;
      gap: 4px;
      margin-bottom: 12px;
    }

    .filter-tab {
      padding: 4px 12px;
      border-radius: 4px;
      border: 1px solid var(--border-color, #313244);
      background: transparent;
      color: var(--text-muted, #6c7086);
      font-size: 11px;
      font-family: inherit;
      cursor: pointer;
    }

    .filter-tab.active {
      background: var(--surface-0, #313244);
      color: var(--text-primary, #cdd6f4);
      border-color: var(--accent-color, #cba6f7);
    }

    /* Session cards */
    .session-card {
      display: flex;
      align-items: center;
      padding: 12px;
      border: 1px solid var(--border-color, #313244);
      border-radius: 8px;
      background: var(--panel-bg, #181825);
      margin-bottom: 8px;
      transition: border-color 0.15s ease;
    }

    .session-card:hover { border-color: var(--accent-color, #cba6f7); }
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

    .session-status-dot.active, .session-status-dot.created { background: #a6e3a1; }
    .session-status-dot.idle { background: #f9e2af; }
    .session-status-dot.ended { background: var(--surface-2, #585b70); }

    .session-title {
      font-size: 13px;
      font-weight: 600;
      color: var(--text-primary, #cdd6f4);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .session-config {
      font-size: 10px;
      padding: 1px 6px;
      border-radius: 3px;
      background: var(--surface-0, #313244);
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

    .icon-btn {
      display: flex;
      align-items: center;
      justify-content: center;
      width: 30px;
      height: 30px;
      border-radius: 4px;
      border: none;
      background: transparent;
      color: var(--text-muted, #6c7086);
      cursor: pointer;
    }

    .icon-btn:hover { background: var(--surface-0, #313244); color: var(--text-primary, #cdd6f4); }
    .icon-btn.danger:hover { color: #f38ba8; }

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
      font-family: 'Material Symbols Outlined';
      font-size: 48px;
      display: block;
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
    private readonly toast = inject(ToastService);
    private readonly userService = inject(UserService);
    private readonly settingsService = inject(SettingsService);
    readonly chat = inject(PersistentChatService);

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
                this.toast.error(e?.error?.detail || 'Failed to resume session');
                return;
            }
        }
        this.router.navigate(['/sessions', thread.id]);
    }

    openSessionFiles(thread: Thread): void {
        if (!thread.nc_session_folder || !environment.cloudUrl) return;
        const folderName = thread.nc_session_folder.split('/').pop();
        window.open(`${environment.cloudUrl}/apps/files/?dir=/${folderName}`, '_blank');
    }

    async endSession(thread: Thread): Promise<void> {
        if (!confirm('End this session? Work will be saved but the agent will stop.')) return;
        try {
            await firstValueFrom(
                this.http.delete(`${environment.apiUrl}/persistent/threads/${thread.id}`)
            );
            this.loadThreads();
        } catch (e: any) {
            this.toast.error(e?.error?.detail || 'Failed to end session');
        }
    }

    async deleteSession(thread: Thread): Promise<void> {
        if (!confirm('Delete this session permanently?')) return;
        try {
            await firstValueFrom(
                this.http.delete(`${environment.apiUrl}/persistent/threads/${thread.id}?permanent=true`)
            );
            this.loadThreads();
        } catch (e: any) {
            this.toast.error(e?.error?.detail || 'Failed to delete session');
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
