import {Component, effect, inject, OnInit, signal, ViewChild} from '@angular/core';
import {Router} from '@angular/router';
import {FormsModule} from '@angular/forms';
import {HttpClient} from '@angular/common/http';
import {firstValueFrom} from 'rxjs';
import {environment} from '../../../core/environment';
import {UserService} from '../../../core/services/user.service';
import {ToastService} from '../../../core/services/toast.service';
import {AgentSettingsComponent} from '../../../shared/components/agent-settings/agent-settings.component';
import {ModelService} from '../../../core/services/model.service';

interface Project {
  id: string;
  name: string;
  status: string;
  description?: string;
  is_default?: boolean;
}

interface Expert {
  id: string;
  display_name: string;
  description: string;
  icon: string;
  color: string;
  tags: string[];
}

interface ExpertDetail extends Expert {
  config: Record<string, unknown>;
  instructions: string | null;
  defaults_tools?: Record<string, string[]>;
  settings_matrix?: Record<string, Record<string, unknown>>;
}

/**
 * Full-page session creation component.
 * Uses the shared AgentSettingsComponent in session mode (horizontal tabs).
 */
@Component({
  selector: 'app-session-create',
  standalone: true,
  imports: [FormsModule, AgentSettingsComponent],
  template: `
    <div class="session-create-page">
      <div class="page-header">
        <h2>New Session</h2>
        <button class="btn btn-secondary" (click)="cancel()">Cancel</button>
      </div>

      <div class="form-container">
        <!-- Title -->
        <div class="form-group">
          <label class="form-label">Title</label>
          <input
            type="text"
            class="form-input"
            [(ngModel)]="title"
            placeholder="Untitled Session"
            [disabled]="creating()"
          >
        </div>

        <!-- Projects (multi-select chips) -->
        @if (projects().length > 0) {
          <div class="form-group">
            <label class="form-label">Projects</label>
            <div class="project-chips">
              @for (project of projects(); track project.id) {
                <button
                  type="button"
                  class="project-chip"
                  [class.selected]="selectedProjectIds().has(project.id)"
                  (click)="toggleProject(project.id)"
                  [title]="project.description || project.name"
                  [disabled]="creating()"
                >{{ project.name }}</button>
              }
            </div>
            <span class="field-hint">Select projects for shared knowledge access</span>
          </div>
        }

        <!-- Expert selector -->
        <div class="form-group">
          <label class="form-label">Expert</label>
          @if (loadingExperts()) {
            <div class="loading-hint">Loading experts...</div>
          } @else if (experts().length > 0) {
            <div class="expert-grid">
              @for (expert of experts(); track expert.id) {
                <button
                  type="button"
                  class="expert-card"
                  [class.selected]="selectedExpert()?.id === expert.id"
                  [style.--expert-color]="expert.color"
                  (click)="toggleExpert(expert)"
                  [disabled]="creating()"
                >
                  @if (selectedExpert()?.id === expert.id) {
                    <span class="expert-check">check_circle</span>
                  }
                  <span class="expert-icon" [style.color]="expert.color">{{ expert.icon }}</span>
                  <span class="expert-name">{{ expert.display_name }}</span>
                  <span class="expert-desc">{{ expert.description }}</span>
                </button>
              }
            </div>
          }
        </div>

        <!-- Agent Settings (horizontal tabs: Settings / Advanced) -->
        <app-agent-settings
          mode="session"
          [config]="expertDetail()?.config ?? frameworkDefaults() ?? {}"
          [disabled]="creating()"
          [defaultsTools]="expertDetail()?.defaults_tools ?? {}"
          [settingsMatrix]="expertDetail()?.settings_matrix ?? frameworkSettingsMatrix()"
          [datasources]="datasources()"
          [loadingDatasources]="loadingDatasources()"
          [loadingExpert]="loadingExpert()"
        />

        <!-- Footer -->
        <div class="form-actions">
          <button class="btn btn-secondary" (click)="cancel()" [disabled]="creating()">
            Cancel
          </button>
          <button class="btn btn-primary" (click)="createSession()" [disabled]="creating()">
            {{ creating() ? 'Creating...' : 'Create Session' }}
          </button>
        </div>
      </div>
    </div>
  `,
  styles: [`
    :host {
      display: block;
      height: 100%;
      overflow: hidden;
    }
    .session-create-page {
      display: flex;
      flex-direction: column;
      height: 100%;
      background: var(--panel-bg, #181825);
    }
    .page-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 12px 16px;
      background: var(--panel-header-bg, #1e1e2e);
      border-bottom: 1px solid var(--border-color, #313244);
      flex-shrink: 0;
    }
    .page-header h2 {
      margin: 0;
      font-size: 16px;
      font-weight: 600;
      color: var(--text-primary, #cdd6f4);
    }
    .form-container {
      flex: 1;
      overflow: auto;
      padding: 20px;
      max-width: 800px;
      width: 100%;
      margin: 0 auto;
    }
    .form-group {
      margin-bottom: 16px;
    }
    .form-label {
      display: block;
      margin-bottom: 6px;
      font-size: 12px;
      font-weight: 500;
      color: var(--text-primary, #cdd6f4);
    }
    .form-input {
      width: 100%;
      padding: 10px 12px;
      border: 1px solid var(--border-color, #45475a);
      border-radius: 6px;
      background: var(--surface-0, #313244);
      color: var(--text-primary, #cdd6f4);
      font-family: inherit;
      font-size: 13px;
    }
    .form-input:focus {
      outline: none;
      border-color: var(--accent-color, #cba6f7);
    }
    .form-input:disabled {
      opacity: 0.6;
      cursor: not-allowed;
    }
    .field-hint {
      display: block;
      margin-top: 4px;
      font-size: 11px;
      color: var(--text-muted, #6c7086);
    }
    .project-chips {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }
    .project-chip {
      padding: 6px 14px;
      border: 1px solid var(--border-color, #45475a);
      border-radius: 16px;
      background: transparent;
      color: var(--text-primary, #cdd6f4);
      font-size: 12px;
      cursor: pointer;
      transition: all 0.15s;
    }
    .project-chip:hover:not(:disabled) {
      border-color: var(--accent-color, #cba6f7);
    }
    .project-chip.selected {
      border-color: var(--accent-color, #cba6f7);
      background: rgba(203, 166, 247, 0.15);
      color: var(--accent-color, #cba6f7);
    }
    .project-chip:disabled {
      opacity: 0.5;
      cursor: not-allowed;
    }
    .loading-hint {
      font-size: 12px;
      color: var(--text-muted, #6c7086);
      padding: 8px 0;
    }
    .expert-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
      gap: 10px;
    }
    .expert-card {
      position: relative;
      display: flex;
      flex-direction: column;
      align-items: flex-start;
      gap: 6px;
      padding: 14px;
      border: 1px solid var(--border-color, #45475a);
      border-radius: 8px;
      background: var(--surface-0, #313244);
      cursor: pointer;
      text-align: left;
      transition: all 0.15s;
      font-family: inherit;
      color: var(--text-primary, #cdd6f4);
    }
    .expert-card:hover:not(:disabled) {
      border-color: var(--expert-color, #cba6f7);
      background: rgba(203, 166, 247, 0.05);
    }
    .expert-card.selected {
      border-color: var(--expert-color, #cba6f7);
      background: rgba(203, 166, 247, 0.08);
      box-shadow: 0 0 0 1px var(--expert-color, #cba6f7);
    }
    .expert-card:disabled {
      opacity: 0.6;
      cursor: not-allowed;
    }
    .expert-check {
      position: absolute;
      top: 8px;
      right: 8px;
      font-family: 'Material Symbols Outlined';
      font-size: 18px;
      color: var(--expert-color, #cba6f7);
    }
    .expert-icon {
      font-family: 'Material Symbols Outlined';
      font-size: 28px;
    }
    .expert-name {
      font-size: 13px;
      font-weight: 600;
    }
    .expert-desc {
      font-size: 11px;
      color: var(--text-muted, #6c7086);
      line-height: 1.4;
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }
    .form-actions {
      display: flex;
      justify-content: flex-end;
      gap: 10px;
      margin-top: 24px;
      padding-top: 16px;
      border-top: 1px solid var(--border-color, #313244);
    }
    .btn {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 10px 20px;
      border: none;
      border-radius: 6px;
      font-size: 13px;
      font-weight: 500;
      cursor: pointer;
      transition: all 0.15s;
    }
    .btn:disabled {
      opacity: 0.6;
      cursor: not-allowed;
    }
    .btn-secondary {
      background: var(--surface-0, #313244);
      color: var(--text-secondary, #a6adc8);
    }
    .btn-secondary:hover:not(:disabled) {
      background: var(--panel-header-bg, #1e1e2e);
    }
    .btn-primary {
      background: var(--accent-color, #cba6f7);
      color: var(--timeline-bg, #11111b);
    }
    .btn-primary:hover:not(:disabled) {
      filter: brightness(1.1);
    }

    /* Select dropdown (reuse from job-create) */
    select.form-input {
      cursor: pointer;
      -webkit-appearance: none;
      appearance: none;
      background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%236c7086' d='M6 8L1 3h10z'/%3E%3C/svg%3E");
      background-repeat: no-repeat;
      background-position: right 12px center;
      padding-right: 32px;
    }
  `],
})
export class SessionCreateComponent implements OnInit {
  private readonly http = inject(HttpClient);
  private readonly router = inject(Router);
  private readonly userService = inject(UserService);
  private readonly toast = inject(ToastService);
  private readonly modelService = inject(ModelService);

  @ViewChild(AgentSettingsComponent) agentSettings!: AgentSettingsComponent;

  title = '';
  readonly creating = signal(false);
  readonly projects = signal<Project[]>([]);
  readonly selectedProjectIds = signal<Set<string>>(new Set());
  readonly experts = signal<Expert[]>([]);
  readonly selectedExpert = signal<Expert | null>(null);
  readonly expertDetail = signal<ExpertDetail | null>(null);
  readonly frameworkDefaults = signal<Record<string, unknown> | null>(null);
  readonly frameworkSettingsMatrix = signal<Record<string, Record<string, unknown>>>({});
  readonly loadingExperts = signal(false);
  readonly loadingExpert = signal(false);
  readonly datasources = signal<any[]>([]);
  readonly loadingDatasources = signal(false);

  constructor() {
    effect(() => {
      const userId = this.userService.currentUserId();
      if (userId) this.loadProjects(userId);
    });
  }

  ngOnInit(): void {
    this.modelService.load();
    this.loadExperts();
    this.loadDatasourcesList();
    this.http.get<ExpertDetail>(`${environment.apiUrl}/experts/defaults`).subscribe({
      next: (d) => {
        if (d?.config) this.frameworkDefaults.set(d.config);
        if (d?.settings_matrix) this.frameworkSettingsMatrix.set(d.settings_matrix);
      },
    });
  }

  private loadProjects(userId: string): void {
    this.http.get<Project[]>(`${environment.apiUrl}/projects?user_id=${userId}`).subscribe({
      next: (projects) => this.projects.set(projects),
    });
  }

  private loadExperts(): void {
    this.loadingExperts.set(true);
    this.http.get<Expert[]>(`${environment.apiUrl}/experts`).subscribe({
      next: (experts) => { this.experts.set(experts); this.loadingExperts.set(false); },
      error: () => this.loadingExperts.set(false),
    });
  }

  private loadDatasourcesList(): void {
    this.loadingDatasources.set(true);
    this.http.get<any[]>(`${environment.apiUrl}/datasources?scope=global`).subscribe({
      next: (ds) => { this.datasources.set(ds); this.loadingDatasources.set(false); },
      error: () => this.loadingDatasources.set(false),
    });
  }

  toggleProject(id: string): void {
    this.selectedProjectIds.update(current => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  toggleExpert(expert: Expert): void {
    if (this.selectedExpert()?.id === expert.id) {
      this.selectedExpert.set(null);
      this.expertDetail.set(null);
      this.agentSettings?.resetAll();
    } else {
      this.selectedExpert.set(expert);
      this.fetchExpertDetail(expert.id);
    }
  }

  private fetchExpertDetail(expertId: string): void {
    this.loadingExpert.set(true);
    this.http.get<ExpertDetail>(`${environment.apiUrl}/experts/${expertId}`).subscribe({
      next: (detail) => {
        this.expertDetail.set(detail);
        if (detail?.config) this.agentSettings?.prefillFromConfig(detail.config);
        this.loadingExpert.set(false);
      },
      error: () => this.loadingExpert.set(false),
    });
  }

  async createSession(): Promise<void> {
    this.creating.set(true);

    const expert = this.selectedExpert();
    const configName = expert?.id ?? 'persistent_defaults';
    const projectIds = Array.from(this.selectedProjectIds());

    // Build config_override from settings component
    const configOverride = this.agentSettings?.getOverrides() ?? {};

    // Extract permission_mode and model from overrides (session-specific handling)
    const permissionMode = (configOverride['interactive'] as any)?.['permission_mode'] ?? 'supervised';
    const model = (configOverride['llm'] as any)?.['model'] ?? null;

    const body: Record<string, unknown> = {
      title: this.title || 'Untitled Session',
      config_name: configName,
      permission_mode: permissionMode,
      project_ids: projectIds.length > 0 ? projectIds : undefined,
    };

    if (model) body['model'] = model;

    // Include full config_override if there are non-model overrides
    const hasNonTrivialOverrides = Object.keys(configOverride).some(
      k => k !== 'interactive' && k !== 'llm'
    ) || (configOverride['llm'] && Object.keys(configOverride['llm'] as any).some(k => k !== 'model'));
    if (hasNonTrivialOverrides) {
      body['config_override'] = configOverride;
    }

    // Datasource IDs
    const dsIds = this.agentSettings?.getSelectedDatasourceIds() ?? [];
    if (dsIds.length > 0) body['datasource_ids'] = dsIds;

    // Navigate immediately to chat view with spinner, create thread in background
    this.router.navigate(['/sessions', '_creating'], {state: {createBody: body}});
  }

  cancel(): void {
    this.router.navigate(['/sessions']);
  }
}
