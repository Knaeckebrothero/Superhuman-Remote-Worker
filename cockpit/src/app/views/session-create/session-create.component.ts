import {Component, effect, inject, OnInit, signal, ViewChild} from '@angular/core';
import {Router} from '@angular/router';
import {HttpClient} from '@angular/common/http';
import {firstValueFrom} from 'rxjs';
import {environment} from '../../core/environment';
import {UserService} from '../../core/services/user.service';
import {AgentSettingsComponent} from '../../views/agent-settings/agent-settings.component';
import {ModelService} from '../../core/services/model.service';
import {SidebarToggleComponent} from '../../shell/sidebar-toggle/sidebar-toggle.component';
import {TranslocoPipe} from '@jsverse/transloco';
import {AppButtonComponent} from '../../ui/button';
import {AppInputComponent} from '../../ui/input';
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
  imports: [
    AgentSettingsComponent,
    SidebarToggleComponent,
    TranslocoPipe,
    AppButtonComponent,
    AppInputComponent,
    AppChipComponent,
    AppIconComponent,
    AppFormFieldComponent,
  ],
  template: `
    <div class="session-create-page">
      <div class="page-header">
        <app-sidebar-toggle />
        <h2>{{ 'sessions.create.title' | transloco }}</h2>
        <app-button variant="secondary" size="sm" (clicked)="cancel()">
          {{ 'sessions.create.cancel' | transloco }}
        </app-button>
      </div>

      <div class="form-container">
        <!-- Title -->
        <app-form-field [label]="'sessions.create.titleLabel' | transloco">
          <app-input
            [(value)]="title"
            [placeholder]="'sessions.create.titlePlaceholder' | transloco"
            [disabled]="creating()"
          />
        </app-form-field>

        <!-- Projects (multi-select chips) -->
        @if (projects().length > 0) {
          <app-form-field [label]="'sessions.create.projectsLabel' | transloco" [hint]="'sessions.create.projectsHint' | transloco">
            <div class="project-chips">
              @for (project of projects(); track project.id) {
                <app-chip
                  [selected]="selectedProjectIds().has(project.id)"
                  [disabled]="creating()"
                  [ariaLabel]="project.description || project.name"
                  (clicked)="toggleProject(project.id)"
                >{{ project.name }}</app-chip>
              }
            </div>
          </app-form-field>
        }

        <!-- Expert selector -->
        <app-form-field [label]="'sessions.create.expertLabel' | transloco">
          @if (loadingExperts()) {
            <div class="loading-hint">{{ 'sessions.create.expertLoading' | transloco }}</div>
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
                    <app-icon size="lg" class="expert-check">check_circle</app-icon>
                  }
                  <app-icon size="inherit" class="expert-icon" [style.color]="expert.color">{{ expert.icon }}</app-icon>
                  <span class="expert-name">{{ expert.display_name }}</span>
                  <span class="expert-desc">{{ expert.description }}</span>
                </button>
              }
            </div>
          }
        </app-form-field>

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
          <app-button variant="secondary" (clicked)="cancel()" [disabled]="creating()">
            {{ 'sessions.create.cancel' | transloco }}
          </app-button>
          <app-button variant="primary" [loading]="creating()" (clicked)="createSession()">
            {{ creating() ? ('sessions.create.creating' | transloco) : ('sessions.create.createSession' | transloco) }}
          </app-button>
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
      background: var(--panel-bg, var(--panel-bg));
    }
    .page-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 12px 16px;
      background: var(--panel-header-bg, #1e1e2e);
      border-bottom: 1px solid var(--border-color, var(--surface-0));
      flex-shrink: 0;
    }
    .page-header h2 {
      margin: 0;
      font-size: 16px;
      font-weight: 600;
      color: var(--text-primary, var(--text-primary));
    }
    .form-container {
      flex: 1;
      overflow: auto;
      padding: 20px;
      max-width: var(--content-max-width);
      width: 100%;
      margin: 0 auto;
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
      border: 1px solid var(--border-color, var(--surface-1));
      border-radius: var(--radius-surface);
      background: var(--surface-0, var(--surface-0));
      cursor: pointer;
      text-align: left;
      transition: all 0.15s;
      font-family: inherit;
      color: var(--text-primary, var(--text-primary));
    }
    .expert-card:hover:not(:disabled) {
      border-color: var(--expert-color, var(--accent-color));
      background: color-mix(in srgb, var(--accent-color) 20%, transparent);
    }
    .expert-card.selected {
      border-color: var(--expert-color, var(--accent-color));
      background: color-mix(in srgb, var(--accent-color) 20%, transparent);
      box-shadow: 0 0 0 1px var(--expert-color, var(--accent-color));
    }
    .expert-card:disabled {
      opacity: 0.6;
      cursor: not-allowed;
    }
    .expert-check {
      position: absolute;
      top: 8px;
      right: 8px;
      color: var(--expert-color, var(--accent-color));
    }
    .expert-icon {
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
      border-top: 1px solid var(--border-color, var(--surface-0));
    }

    @media (max-width: 768px) {
      .form-container {
        max-width: 100%;
        padding: 12px;
      }

      .page-header {
        padding: 8px 12px;
      }

      .expert-grid {
        grid-template-columns: 1fr;
      }

      .form-actions {
        flex-direction: column;
      }

      .form-actions app-button {
        width: 100%;
      }
    }
  `],
})
export class SessionCreateComponent implements OnInit {
  private readonly http = inject(HttpClient);
  private readonly router = inject(Router);
  private readonly userService = inject(UserService);
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
      next: (projects) => {
        this.projects.set(projects);
        const defaultProject = projects.find(p => p.is_default);
        if (defaultProject) {
          this.selectedProjectIds.set(new Set([defaultProject.id]));
          // Refresh eligible datasources now that a project is selected.
          this.loadDatasourcesList();
        }
      },
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
    // Eligible = owner + global + linked to any selected project. The picker
    // pre-selects these; explicit-only resolution attaches what stays checked.
    const qs = Array.from(this.selectedProjectIds())
      .map(id => `project_id=${encodeURIComponent(id)}`)
      .join('&');
    const url = `${environment.apiUrl}/datasources/eligible${qs ? '?' + qs : ''}`;
    this.http.get<any[]>(url).subscribe({
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
    // Refresh eligible datasources for the new project selection.
    this.loadDatasourcesList();
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

    // Extract permission_mode and model from overrides (session-specific handling).
    // Only send permission_mode when the user actually picked a per-session
    // override; omitting it lets the backend fall back to the user's saved
    // default, then the config default. Sending a hardcoded 'supervised' here
    // would clobber that saved default and forced every session to Supervised.
    const permissionMode = (configOverride['interactive'] as any)?.['permission_mode'] ?? null;
    const model = (configOverride['llm'] as any)?.['model'] ?? null;

    const body: Record<string, unknown> = {
      title: this.title || 'Untitled Session',
      config_name: configName,
      project_ids: projectIds.length > 0 ? projectIds : undefined,
    };

    if (permissionMode) body['permission_mode'] = permissionMode;
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
