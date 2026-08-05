import {Component, computed, effect, inject, OnInit, signal, ViewChild} from '@angular/core';
import {Router} from '@angular/router';
import {HttpClient} from '@angular/common/http';
import {firstValueFrom} from 'rxjs';
import {environment} from '../../core/environment';
import {UserService} from '../../core/services/user.service';
import {AgentSettingsComponent} from '../../views/agent-settings/agent-settings.component';
import {ApiService, SessionToolGroupsResponse} from '../../core/services/api.service';
import {resolveEffectiveModels} from '../../views/agent-settings/agent-settings.types';
import {ModelService} from '../../core/services/model.service';
import {CapabilitiesService} from '../../core/services/capabilities.service';
import {ErrorMessageService} from '../../core/services/error-message.service';
import {SidebarToggleComponent} from '../../shell/sidebar-toggle/sidebar-toggle.component';
import {TranslocoPipe} from '@jsverse/transloco';
import {AppButtonComponent} from '../../ui/button';
import {AppInputComponent} from '../../ui/input';
import {AppChipComponent} from '../../ui/chip';
import {AppIconComponent} from '../../ui/icon';
import {AppFormFieldComponent} from '../../ui/form-field';
import {EffectiveModels, EligibleDatasource, ExpertDefaultsResponse} from '../../core/models/api.model';

interface Project {
  id: string;
  name: string;
  status: string;
  description?: string;
  is_default?: boolean;
  main_cloud_backend?: string | null;
}

/**
 * Whether the protected-cloud session-create checkbox should render.
 *
 * Visible only when the deployment flag is on AND at least one selected
 * project is a non-default Nextcloud project (spec §2/§4: default projects
 * excluded in v1; Nextcloud-only per design §9.2).
 */
export function protectedCloudToggleVisible(
  featureOn: boolean,
  selected: Array<{ is_default?: boolean; main_cloud_backend?: string | null }>,
): boolean {
  return featureOn && selected.some(
    (p) => !p.is_default && p.main_cloud_backend === 'nextcloud',
  );
}

interface Expert {
  id: string;
  display_name: string;
  description: string;
  icon: string;
  color: string;
  tags: string[];
  /** 'bundled' (disk) | 'user' | 'global' (DB). DB experts → expert_id. */
  source?: string;
  storage_kind?: 'bundled' | 'db';
  expert_type?: string;
}

interface ExpertDetail extends Expert {
  config: Record<string, unknown>;
  instructions: string | null;
  settings_matrix?: Record<string, Record<string, unknown>>;
  /** Effective model + provenance per slot (server-resolved). */
  effective_models?: EffectiveModels | null;
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

        <!-- Protected cloud toggle: only for non-default Nextcloud projects,
             gated on the deployment feature flag (Slice C). -->
        @if (protectedCloudVisible()) {
          <label class="protected-cloud-toggle">
            <input type="checkbox" [checked]="protectedCloud()"
                   [disabled]="creating()"
                   (change)="protectedCloud.set($any($event.target).checked)" />
            {{ 'sessions.create.protectedCloud' | transloco }}
          </label>
          <span class="field-hint">{{ 'sessions.create.protectedCloudHint' | transloco }}</span>
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
          @if (selectedExpert()) {
            <span class="field-hint">
              {{ 'sessions.create.expertSelectedPrefix' | transloco }} {{ selectedExpert()!.display_name }}
              @if (selectedExpertSource()) {
                · {{ ('settings.expertDefaults.source.' + selectedExpertSource()) | transloco }}
              }
            </span>
          }
        </app-form-field>

        <!-- Agent Settings (horizontal tabs: Settings / Advanced) -->
        <app-agent-settings
          mode="session"
          [config]="expertDetail()?.config ?? frameworkDefaults() ?? {}"
          [resolvedToolset]="toolPreview()"
          [readsResolvedToolset]="true"
          [disabled]="creating()"
          [settingsMatrix]="expertDetail()?.settings_matrix ?? frameworkSettingsMatrix()"
          [effectiveModels]="resolvedEffectiveModels()"
          [datasources]="datasources()"
          [loadingDatasources]="loadingDatasources()"
          [datasourceLoadError]="datasourceLoadError()"
          [datasourceContextKey]="datasourceContextKey()"
          [datasourceDefaultsEnabled]="capabilities.datasourceScopeAutoAttachAvailable()"
          [loadingExpert]="loadingExpert()"
          [gatedCapabilities]="capabilities.grants() ?? null"
          (retryDatasources)="loadDatasourcesList()"
        />

        <!-- A rejected config is correctable, so the error lands here and the
             form keeps every selection instead of unmounting into the chat
             view and bouncing back to the sessions list. -->
        @if (createError()) {
          <div class="create-error" role="alert">
            <app-icon size="md">error</app-icon>
            <span>{{ createError() }}</span>
          </div>
        }

        <!-- Footer -->
        <div class="form-actions">
          <app-button variant="secondary" (clicked)="cancel()" [disabled]="creating()">
            {{ 'sessions.create.cancel' | transloco }}
          </app-button>
          <app-button
            variant="primary"
            [loading]="creating()"
            [disabled]="loadingDatasources() || datasourceLoadError()"
            (clicked)="createSession()"
          >
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
      background: var(--panel-header-bg);
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
      display: flex;
      flex-direction: column;
      gap: 20px;
      padding: 20px;
      max-width: var(--content-max-width);
      width: 100%;
      margin: 0 auto;
    }
    /* The settings block is a solid bordered panel; without this it butts
       flush against the expert card grid and the cards look like they tuck
       under it. The gap above gives every section consistent separation. */
    app-agent-settings {
      display: block;
    }
    .create-error {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 10px 14px;
      margin-top: 16px;
      border-radius: var(--radius-control);
      background: var(--danger-tint);
      border: 1px solid var(--danger-tint);
      color: var(--danger-color);
      font-size: 13px;
    }

    .field-hint {
      display: block;
      margin-top: 4px;
      font-size: 11px;
      color: var(--text-muted);
    }
    .project-chips {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }
    .protected-cloud-toggle {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 13px;
      color: var(--text-primary, var(--text-primary));
      cursor: pointer;
    }
    .loading-hint {
      font-size: 12px;
      color: var(--text-muted);
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
      color: var(--text-muted);
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
  private readonly errorMessages = inject(ErrorMessageService);
  private readonly api = inject(ApiService);
  readonly capabilities = inject(CapabilitiesService);

  @ViewChild(AgentSettingsComponent) agentSettings!: AgentSettingsComponent;

  title = '';
  readonly creating = signal(false);
  /** Server rejection of the submitted config, rendered in-form so the user
   *  can correct it without losing the rest of their selections. */
  readonly createError = signal<string | null>(null);
  readonly projects = signal<Project[]>([]);
  readonly selectedProjectIds = signal<Set<string>>(new Set());
  readonly selectedProjects = computed(() =>
    this.projects().filter(p => this.selectedProjectIds().has(p.id)),
  );
  readonly protectedCloud = signal(false);
  readonly protectedCloudVisible = computed(() =>
    protectedCloudToggleVisible(this.capabilities.protectedCloudAvailable(), this.selectedProjects()),
  );
  readonly experts = signal<Expert[]>([]);
  readonly selectedExpert = signal<Expert | null>(null);
  private readonly effectiveDefaultExpertId = signal<string | null>(null);
  readonly selectedExpertSource = signal<'project' | 'user' | 'application' | 'explicit' | null>(null);
  private expertSelectionTouched = false;
  private defaultRequestSerial = 0;
  readonly expertDetail = signal<ExpertDetail | null>(null);
  readonly frameworkDefaults = signal<Record<string, unknown> | null>(null);
  readonly frameworkSettingsMatrix = signal<Record<string, Record<string, unknown>>>({});
  // Server-resolved effective models for the framework "defaults" expert — the
  // floor used when no expert is selected, so the model picker's "Default"
  // option shows the resolved chat pin instead of the config-literal placeholder.
  readonly frameworkEffectiveModels = signal<EffectiveModels | null>(null);
  readonly resolvedEffectiveModels = computed(() =>
    resolveEffectiveModels(this.expertDetail()?.effective_models, this.frameworkEffectiveModels()),
  );
  /**
   * What a session created with the CURRENT selection would bind — a
   * prediction, and labelled as one by the endpoint that produces it.
   *
   * The form has no agent to ask, so it cannot do better; what it must not do
   * is present the forecast as fact. The route it calls structurally cannot
   * return a measurement, which is what keeps that honest without relying on
   * anyone remembering to check a flag.
   */
  readonly toolPreview = signal<SessionToolGroupsResponse | null>(null);
  private toolPreviewSerial = 0;
  readonly loadingExperts = signal(false);
  readonly loadingExpert = signal(false);
  readonly datasources = signal<EligibleDatasource[]>([]);
  readonly loadingDatasources = signal(false);
  readonly datasourceLoadError = signal(false);
  readonly datasourceContextKey = computed(() => {
    const ids = Array.from(this.selectedProjectIds()).sort();
    return ids.length > 0 ? `projects:${ids.join(',')}` : 'standalone';
  });
  private datasourceRequestSerial = 0;

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
    // account_defaults: the session account layer (notably workspace.backend,
    // `virtual` by default — NOT session_base's `sandbox`) has to be in the
    // resolved config the form renders, or controls keyed off it disagree with
    // what create_thread resolves. That mismatch is what made the datasource
    // picker offer clone-based repository connectors on a lite tier and 400
    // every create.
    this.http.get<ExpertDetail>(
      `${environment.apiUrl}/experts/session_base?type=session&account_defaults=true`,
    ).subscribe({
      next: (d) => {
        if (d?.config) {
          this.frameworkDefaults.set(d.config);
          // Tool toggles keep their own override state rather than deriving it
          // directly from the config input. Synchronize the asynchronously
          // loaded session base so empty persistent categories render disabled.
          if (!this.expertDetail()) {
            this.agentSettings?.toolsGroup?.prefillFromConfig(d.config);
          }
        }
        if (d?.settings_matrix) this.frameworkSettingsMatrix.set(d.settings_matrix);
        if (d?.effective_models) this.frameworkEffectiveModels.set(d.effective_models);
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
        this.loadEffectiveDefault();
      },
    });
  }

  private loadExperts(): void {
    this.loadingExperts.set(true);
    this.http.get<Expert[]>(`${environment.apiUrl}/experts?type=session`).subscribe({
      next: (experts) => {
        this.experts.set(experts);
        this.applyEffectiveDefault();
        this.loadingExperts.set(false);
      },
      error: () => this.loadingExperts.set(false),
    });
    this.loadEffectiveDefault();
  }

  private loadEffectiveDefault(): void {
    if (this.expertSelectionTouched) return;
    const serial = ++this.defaultRequestSerial;
    const projectIds = Array.from(this.selectedProjectIds());
    const projectId = projectIds.length === 1 ? projectIds[0] : undefined;
    const suffix = projectId ? `?project_id=${encodeURIComponent(projectId)}` : '';
    this.http.get<ExpertDefaultsResponse>(`${environment.apiUrl}/expert-defaults${suffix}`).subscribe({
      next: (response) => {
        if (serial !== this.defaultRequestSerial || this.expertSelectionTouched) return;
        this.effectiveDefaultExpertId.set(response?.defaults?.session?.effective?.id ?? null);
        this.selectedExpertSource.set(response?.defaults?.session?.source ?? null);
        this.applyEffectiveDefault();
      },
    });
  }

  private applyEffectiveDefault(): void {
    if (this.expertSelectionTouched) return;
    const id = this.effectiveDefaultExpertId();
    const expert = id ? this.experts().find(item => item.id === id) : undefined;
    if (expert && this.selectedExpert()?.id !== expert.id) {
      this.selectedExpert.set(expert);
      this.fetchExpertDetail(expert.id);
    }
  }

  loadDatasourcesList(): void {
    const serial = ++this.datasourceRequestSerial;
    this.loadingDatasources.set(true);
    this.datasourceLoadError.set(false);
    // The server applies all-project scope matching and computes owner-specific
    // defaults for this exact session context.
    const qs = Array.from(this.selectedProjectIds())
      .map(id => `project_id=${encodeURIComponent(id)}`)
      .join('&');
    const url = `${environment.apiUrl}/datasources/eligible${qs ? '?' + qs : ''}`;
    this.http.get<EligibleDatasource[]>(url).subscribe({
      next: (ds) => {
        if (serial !== this.datasourceRequestSerial) return;
        this.datasources.set(ds);
        this.loadingDatasources.set(false);
      },
      error: () => {
        if (serial !== this.datasourceRequestSerial) return;
        // Preserve the last authorized rows and their explicit selection as
        // read-only context; the error state blocks create until retry wins.
        this.datasourceLoadError.set(true);
        this.loadingDatasources.set(false);
      },
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
    this.loadEffectiveDefault();
    // The project layer can override the expert's tools, so the prediction
    // moves with the selection.
    this.loadToolPreview();
  }

  toggleExpert(expert: Expert): void {
    this.expertSelectionTouched = true;
    this.selectedExpertSource.set('explicit');
    if (this.selectedExpert()?.id === expert.id) return;
    this.selectedExpert.set(expert);
    this.fetchExpertDetail(expert.id);
  }

  private fetchExpertDetail(expertId: string): void {
    this.loadingExpert.set(true);
    this.http.get<ExpertDetail>(
      `${environment.apiUrl}/experts/${expertId}?account_defaults=true`,
    ).subscribe({
      next: (detail) => {
        this.expertDetail.set(detail);
        if (detail?.config) this.agentSettings?.prefillFromConfig(detail.config);
        this.loadingExpert.set(false);
      },
      error: () => this.loadingExpert.set(false),
    });
    this.loadToolPreview();
  }

  /**
   * Ask what this configuration would bind, and re-anchor the tool switches to
   * the answer.
   *
   * Serial-guarded: a slower answer for an expert the user has already
   * switched away from must not paint over the current one. And it re-anchors
   * ONLY while the user has not touched a tool switch — the config prefill has
   * already run by the time this lands, so clobbering a click here would be
   * the same late-response bug the live pane's forkJoin exists to prevent,
   * rebuilt on the other surface.
   */
  private loadToolPreview(): void {
    const serial = ++this.toolPreviewSerial;
    const expert = this.selectedExpert();
    const projectIds = Array.from(this.selectedProjectIds());
    // Routed EXACTLY as createSession routes it. A preview that resolved a
    // different expert layer from the create it previews would be this
    // series' defect in the surface built to prevent it.
    const {configName, expertId} = this.expertRouting(expert);
    this.api.previewToolGroups({
      config_name: configName,
      expert_id: expertId ?? null,
      project_id: projectIds.length === 1 ? projectIds[0] : null,
    }).subscribe((preview) => {
      if (serial !== this.toolPreviewSerial) return;
      this.toolPreview.set(preview);
      const categories = preview?.categories;
      if (categories && !this.agentSettings?.hasToolEdits()) {
        this.agentSettings?.prefillFromResolvedToolset(categories);
      }
    });
  }

  /** How an expert selection maps onto the create/preview request.
   *
   *  DB-backed experts (source user/global/managed) go via `expert_id`;
   *  `config_name` stays the persistent base. Bundled experts keep the
   *  `config_name` path. Fixes the `config_name=<uuid>` conflation that
   *  crashed session boot — and shared with the tool preview so the two
   *  cannot resolve different expert layers. */
  private expertRouting(expert: Expert | null): {
    configName: string;
    expertId: string | undefined;
  } {
    const isDbExpert = expert?.storage_kind === 'db' ||
      ['user', 'global', 'managed'].includes(expert?.source ?? '');
    return {
      configName: expert && !isDbExpert ? expert.id : 'session_base',
      expertId: isDbExpert ? expert!.id : undefined,
    };
  }

  async createSession(): Promise<void> {
    if (this.loadingDatasources() || this.datasourceLoadError()) return;
    this.creating.set(true);

    const expert = this.selectedExpert();
    const {configName, expertId} = this.expertRouting(expert);
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
      expert_id: expertId,
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
    // Empty is an explicit opt-out; omission means "apply server defaults".
    body['datasource_ids'] = dsIds;

    if (this.protectedCloud() && this.protectedCloudVisible()) {
      body['protected_cloud'] = true;
    }

    // Create BEFORE navigating. The form used to hand the body to the chat
    // route and unmount, so a rejected config destroyed every selection and
    // dumped the user back on /sessions with nothing to correct. The POST is
    // fast (tens of ms — it returns as soon as the thread row exists; the slow
    // part is provisioning, which happens after); paying that here buys a
    // recoverable failure. On success we route to the real thread id and the
    // chat view runs its normal connect, showing the setup progress.
    this.createError.set(null);
    try {
      const resp = await firstValueFrom(
        this.http.post<{thread_id: string}>(
          `${environment.apiUrl}/persistent/threads`,
          body,
        ),
      );
      await this.router.navigate(['/sessions', resp.thread_id]);
    } catch (err) {
      this.createError.set(
        this.errorMessages.translate(err, 'sessions.create.failed'),
      );
      this.creating.set(false);
    }
  }

  cancel(): void {
    this.router.navigate(['/sessions']);
  }
}
