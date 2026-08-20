import {Component, computed, effect, inject, OnInit, signal} from '@angular/core';
import {Router} from '@angular/router';
import {ApiService} from '../../core/services/api.service';
import {ErrorMessageService} from '../../core/services/error-message.service';
import {UserService} from '../../core/services/user.service';
import {SessionService} from '../../core/services/session.service';
import {Datasource, Project, ProjectCreateRequest, ProjectStatus} from '../../core/models/api.model';
import {SidebarToggleComponent} from '../../shell/sidebar-toggle/sidebar-toggle.component';
import {TranslocoPipe} from '@jsverse/transloco';
import {AppSpinnerComponent} from '../../ui/spinner';
import {AppInlineEditableTextComponent} from '../../ui/inline-editable-text';
import {AppTabBarComponent, AppTabComponent} from '../../ui/tab-bar';
import {ViewportService} from '../../core/services/viewport.service';

/** The API's own explanation, when it gave one. Connector refusals name the
 *  fix ("unlink it first", "set its note root to knowledge"), which no generic
 *  message can. */
function errorDetail(err: unknown): string {
  const detail = (err as {error?: {detail?: unknown}} | null)?.error?.detail;
  return typeof detail === 'string' ? detail : '';
}

@Component({
  selector: 'app-project-list-page',
  standalone: true,
  imports: [
    SidebarToggleComponent,
    TranslocoPipe,
    AppSpinnerComponent,
    AppInlineEditableTextComponent,
    AppTabBarComponent,
    AppTabComponent,
  ],
  template: `
    <div class="page-container">
      <!-- Header -->
      <div class="page-header">
        <div class="header-left">
          <app-sidebar-toggle />
          <h1 class="page-title">{{ 'projects.title' | transloco }}</h1>
        </div>
        <div class="header-actions">
          <button class="btn btn-primary" (click)="toggleCreateForm()">
            {{ (showCreateForm() ? 'projects.cancel' : 'projects.newProject') | transloco }}
          </button>
          <!-- Refresh is desktop-only: on mobile the list reloads on navigation
               and pull-to-refresh works, so the button is dropped (matches the
               Jobs header) — and removing it keeps the header to one tidy row. -->
          @if (!viewport.isMobile()) {
            <button class="btn btn-ghost" (click)="refresh()" [disabled]="isLoading()">
              {{ 'projects.refresh' | transloco }}
            </button>
          }
        </div>
      </div>

      <!-- Create Form -->
      @if (showCreateForm()) {
        <div class="create-form">
          <div class="form-row">
            <input
              class="form-input"
              [placeholder]="'projects.namePlaceholder' | transloco"
              [value]="formName()"
              (input)="formName.set(asInputValue($event))"
            />
          </div>
          <div class="form-row">
            <input
              class="form-input"
              [placeholder]="'projects.descriptionPlaceholder' | transloco"
              [value]="formDescription()"
              (input)="formDescription.set(asInputValue($event))"
            />
          </div>
          <div class="form-row">
            <input
              class="form-input"
              [placeholder]="'projects.goalPlaceholder' | transloco"
              [value]="formGoal()"
              (input)="formGoal.set(asInputValue($event))"
            />
          </div>
          <!-- External knowledge base (opt-in): point the project's writable
               vault at a connector the user created for a private GitHub repo,
               instead of the internal forge. Collapsed by default — the default
               path is unchanged and sends no external_kb key at all. No
               credential is collected here: the connector already holds it. -->
          <div class="form-row">
            <label class="form-toggle">
              <input
                type="checkbox"
                [checked]="useExternalKb()"
                (change)="setUseExternalKb(asCheckedValue($event))"
              />
              <span>{{ 'projects.externalKb.toggle' | transloco }}</span>
            </label>
            @if (useExternalKb()) {
              <p class="form-hint">{{ 'projects.externalKb.hint' | transloco }}</p>
            }
          </div>
          @if (useExternalKb()) {
            <div class="form-row">
              @if (isLoadingKbConnectors()) {
                <p class="form-hint">{{ 'projects.externalKb.loading' | transloco }}</p>
              } @else if (kbConnectors().length === 0) {
                <p class="form-hint">{{ 'projects.externalKb.noConnectors' | transloco }}</p>
                <button class="btn btn-ghost kb-connector-link" (click)="openConnectors()">
                  {{ 'projects.externalKb.createConnector' | transloco }}
                </button>
              } @else {
                <select
                  class="form-input"
                  (change)="formKbDatasourceId.set(asInputValue($event))"
                >
                  <option value="" [selected]="!formKbDatasourceId()">
                    {{ 'projects.externalKb.selectPlaceholder' | transloco }}
                  </option>
                  @for (connector of kbConnectors(); track connector.id) {
                    <option [value]="connector.id" [selected]="connector.id === formKbDatasourceId()">
                      {{ connector.name }}
                    </option>
                  }
                </select>
                <p class="form-hint">{{ 'projects.externalKb.adoptHint' | transloco }}</p>
              }
            </div>
          }
          @if (createFailed()) {
            <div class="form-error" role="alert">
              {{ createErrorDetail() || ('projects.createFailed' | transloco) }}
            </div>
          }
          <div class="form-actions">
            <button
              class="btn btn-primary"
              [disabled]="isCreating() || !canCreate()"
              (click)="createProject()"
            >
              {{ (isCreating() ? 'projects.creating' : 'projects.createProject') | transloco }}
            </button>
          </div>
        </div>
      }

      <!-- Lifecycle filter. Archived is a real server-side state, not a badge:
           the tab drives ?status=, so an archived project is not merely hidden
           here, it is absent from the response. The count is what keeps that
           from being a silent omission. -->
      <app-tab-bar
        class="filter-tabs"
        [value]="statusFilter()"
        (valueChange)="onStatusFilterChange($event)"
      >
        <app-tab [value]="'active'">
          {{ 'projects.filter.active' | transloco:{ count: counts().active } }}
        </app-tab>
        <app-tab [value]="'archived'">
          {{ 'projects.filter.archived' | transloco:{ count: counts().archived } }}
        </app-tab>
      </app-tab-bar>

      @if (statusFilter() === 'archived') {
        <p class="list-notice">{{ 'projects.archivedNotice' | transloco }}</p>
      }

      @if (loadError(); as message) {
        <div class="form-error list-error" role="alert">{{ message }}</div>
      }

      @if (actionError(); as message) {
        <div class="form-error list-error" role="alert">{{ message }}</div>
      }

      <!-- Loading -->
      @if (isLoading() && projects().length === 0) {
        <div class="loading-state">
          <app-spinner size="lg" tone="accent" />
          <span>{{ 'projects.loading' | transloco }}</span>
        </div>
      }

      <!-- Empty -->
      @if (!isLoading() && !loadError() && projects().length === 0) {
        <div class="empty-state">
          <span class="empty-icon material-symbols-outlined">folder_off</span>
          @if (statusFilter() === 'archived') {
            <span>{{ 'projects.emptyArchived' | transloco }}</span>
            <span class="empty-hint">{{ 'projects.emptyArchivedHint' | transloco }}</span>
          } @else {
            <span>{{ 'projects.empty' | transloco }}</span>
            <span class="empty-hint">{{ 'projects.emptyHint' | transloco }}</span>
          }
        </div>
      }

      <!-- Project Cards Grid -->
      @if (projects().length > 0) {
        <div class="projects-grid">
          @for (project of projects(); track project.id) {
            <div class="project-card" (click)="openProject(project.id)">
              <div class="card-header">
                <span class="card-name">
                  @if (statusOf(project) === 'archived') {
                    <!-- An archived project takes a status-only PATCH, so a
                         rename is refused whole. The name stays readable; the
                         control that could only ever fail is not offered.
                         Unarchive (below) is the way back to editing it. -->
                    {{ project.name }}
                  } @else {
                    <app-inline-editable-text
                      [value]="project.name"
                      [ariaLabel]="'common.rename' | transloco"
                      (save)="onRenameProject(project, $event)"
                    />
                  }
                </span>
                <div class="card-badges">
                  @if (project.is_default) {
                    <span class="badge badge-personal">{{ 'projects.badgePersonal' | transloco }}</span>
                  }
                  <span class="badge" [class]="'badge-' + statusOf(project)">
                    {{ 'projects.status.' + statusOf(project) | transloco }}
                  </span>
                </div>
              </div>
              @if (project.description) {
                <p class="card-desc">{{ truncate(project.description, 120) }}</p>
              }
              <div class="card-footer">
                <span class="chip">{{ ((project.job_count ?? 0) === 1 ? 'projects.jobsCountOne' : 'projects.jobsCount') | transloco:{ count: project.job_count ?? 0 } }}</span>
                <span class="chip">{{ ((project.repo_count ?? 0) === 1 ? 'projects.reposCountOne' : 'projects.reposCount') | transloco:{ count: project.repo_count ?? 0 } }}</span>
                <span class="chip">{{ ((project.member_count ?? 0) === 1 ? 'projects.membersCountOne' : 'projects.membersCount') | transloco:{ count: project.member_count ?? 0 } }}</span>
                @if (statusOf(project) === 'archived') {
                  <!-- The way back out. Opening the card still works, so this is
                       a shortcut rather than the only route. -->
                  <button
                    class="btn btn-ghost card-action"
                    [attr.title]="'projects.tooltip.unarchive' | transloco"
                    [disabled]="unarchivingId() === project.id"
                    (click)="unarchiveProject(project, $event)"
                  >
                    {{ (unarchivingId() === project.id ? 'projects.action.unarchiving' : 'projects.action.unarchive') | transloco }}
                  </button>
                }
              </div>
            </div>
          }
        </div>
      }
    </div>
  `,
  styles: [`
    :host { display: block; height: 100%; overflow: auto; }

    .page-container {
      padding: 24px;
      max-width: var(--content-max-width);
      margin: 0 auto;
    }

    .page-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 20px;
      flex-wrap: wrap;
      gap: 12px;
    }

    .header-left {
      display: flex;
      align-items: center;
      gap: 12px;
      min-width: 0;
    }

    .page-title {
      font-size: 22px;
      font-weight: 700;
      color: var(--text-primary, var(--text-primary));
      margin: 0;
    }

    .header-actions { display: flex; gap: 8px; }

    .btn {
      padding: 8px 16px;
      border-radius: var(--radius-control);
      font-size: 13px;
      font-family: inherit;
      cursor: pointer;
      border: 1px solid var(--border-color, var(--surface-1));
      transition: all 0.15s ease;
    }

    .btn-primary {
      background: var(--accent-color, var(--accent-color));
      color: var(--on-accent, var(--timeline-bg));
      border-color: var(--accent-color, var(--accent-color));
      font-weight: 600;
    }

    .btn-primary:hover:not(:disabled) { opacity: 0.9; }
    .btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }

    .btn-ghost {
      background: transparent;
      color: var(--text-secondary, var(--text-secondary));
    }

    .btn-ghost:hover:not(:disabled) { background: var(--surface-0, var(--surface-0)); }

    /* Create Form */
    .create-form {
      background: var(--panel-bg, var(--panel-bg));
      border: 1px solid var(--border-color, var(--surface-0));
      border-radius: var(--radius-surface);
      padding: 16px;
      margin-bottom: 20px;
    }

    .form-row { margin-bottom: 10px; }

    .form-input {
      width: 100%;
      padding: 10px 12px;
      background: var(--surface-0, var(--surface-0));
      border: 1px solid var(--border-color, var(--surface-1));
      border-radius: var(--radius-control);
      color: var(--text-primary, var(--text-primary));
      font-size: 13px;
      font-family: inherit;
      outline: none;
      box-sizing: border-box;
    }

    .form-input:focus { border-color: var(--accent-color, var(--accent-color)); }

    select.form-input { cursor: pointer; }

    .kb-connector-link { margin: 8px 0 0 24px; }

    .form-toggle {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 13px;
      color: var(--text-secondary, var(--text-secondary));
      cursor: pointer;
    }

    .form-toggle input { cursor: pointer; }

    .form-hint {
      margin: 6px 0 0 24px;
      font-size: 12px;
      color: var(--text-muted, var(--text-muted));
      line-height: 1.4;
    }

    .form-error {
      margin-bottom: 10px;
      padding: 8px 12px;
      border-radius: var(--radius-control);
      background: var(--danger-tint);
      color: var(--danger);
      font-size: 12px;
    }

    /* Filter + notices */
    .filter-tabs { margin-bottom: 16px; }

    .list-notice {
      margin: 0 0 16px;
      font-size: 12px;
      color: var(--text-muted, var(--text-muted));
      line-height: 1.4;
    }

    .list-error { margin-bottom: 16px; }

    .form-actions { display: flex; justify-content: flex-end; }

    /* Loading & Empty */
    .loading-state, .empty-state {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 12px;
      padding: 60px 20px;
      color: var(--text-muted, var(--text-muted));
    }

    .empty-icon { font-size: 48px; opacity: 0.5; }
    .empty-hint { font-size: 12px; opacity: 0.6; }

    /* Grid */
    .projects-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
      gap: 16px;
    }

    .project-card {
      background: var(--panel-bg, var(--panel-bg));
      border: 1px solid var(--border-color, var(--surface-0));
      border-radius: var(--radius-surface);
      padding: 16px;
      cursor: pointer;
      transition: border-color 0.15s ease, transform 0.1s ease;
    }

    .project-card:hover {
      border-color: var(--accent-color, var(--accent-color));
      transform: translateY(-1px);
    }

    .card-header {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 8px;
    }

    .card-name {
      font-size: 15px;
      font-weight: 600;
      color: var(--text-primary, var(--text-primary));
    }

    .card-badges { display: flex; gap: 6px; flex-shrink: 0; }

    .badge {
      padding: 2px 8px;
      border-radius: var(--radius-tag);
      font-size: 10px;
      font-weight: 500;
      text-transform: capitalize;
    }

    .badge-active { background: var(--success-tint); color: var(--success); }
    .badge-archived { background: color-mix(in srgb, var(--text-muted) 25%, transparent); color: var(--text-muted); }
    .badge-personal { background: color-mix(in srgb, var(--accent-color) 20%, transparent); color: var(--accent-color); }

    .card-desc {
      font-size: 12px;
      color: var(--text-secondary, var(--text-secondary));
      margin: 0 0 12px;
      line-height: 1.4;
    }

    .card-footer { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }

    .card-action {
      margin-left: auto;
      padding: 4px 10px;
      font-size: 11px;
    }

    .chip {
      padding: 3px 8px;
      background: var(--surface-0, var(--surface-0));
      border-radius: var(--radius-tag);
      font-size: 11px;
      color: var(--text-muted, var(--text-muted));
    }

    @media (max-width: 768px) {
      .page-container { padding: 12px; }
      .projects-grid { grid-template-columns: 1fr; }
    }
  `],
})
export class ProjectListPageComponent implements OnInit {
  private readonly api = inject(ApiService);
  private readonly errors = inject(ErrorMessageService);
  private readonly userService = inject(UserService);
  private readonly session = inject(SessionService);
  private readonly router = inject(Router);
  protected readonly viewport = inject(ViewportService);

  readonly projects = signal<Project[]>([]);
  readonly isLoading = signal(false);
  readonly showCreateForm = signal(false);
  readonly isCreating = signal(false);

  /** Which lifecycle the grid is showing — and, more to the point, which
   *  `?status=` the request carries. */
  readonly statusFilter = signal<ProjectStatus>('active');
  /** Per-tab totals. The archived one is the whole reason this control has
   *  counts: a project that left the grid must still be findable, and a number
   *  on the tab is what says "there are five over here" without a click. */
  readonly counts = signal<Record<ProjectStatus, number>>({active: 0, archived: 0});
  /** The list load's own failure. Errors used to be swallowed into an empty
   *  array by `ApiService.getProjects`, which is indistinguishable from "you
   *  have no projects" — the one reading a user must never be given wrongly. */
  readonly loadError = signal<string | null>(null);
  /** A refused unarchive (409 on an archived project is the whole point of
   *  phase 1, so it has to be legible). */
  readonly actionError = signal<string | null>(null);
  readonly unarchivingId = signal<string | null>(null);
  /** Two requests are in flight per load (the shown tab, plus the other tab's
   *  count); a fast double tab-switch must not let a stale one win. */
  private loadSerial = 0;

  readonly formName = signal('');
  readonly formDescription = signal('');
  readonly formGoal = signal('');

  /** External-KB opt-in (see knowledge-base/knowledge/features/external_forge_knowledge_base.md).
   *  Off ⇒ the create body carries no `external_kb` key at all and the backend
   *  provisions the internal forge repo exactly as before. */
  readonly useExternalKb = signal(false);
  /** The chosen connector. The repository URL, branch and PAT all live on it
   *  already, so this form never handles a credential of its own. */
  readonly formKbDatasourceId = signal('');
  readonly kbConnectors = signal<Datasource[]>([]);
  readonly isLoadingKbConnectors = signal(false);

  /** `createProject` on ApiService reports a failure as `null`, so a failure is
   *  otherwise indistinguishable from a no-op. Surface it. */
  readonly createFailed = signal(false);
  /** The server's own words when it has better ones than "create failed" —
   *  every connector refusal says what to change. */
  readonly createErrorDetail = signal('');

  /** A name is always required; enabling the external KB additionally requires
   *  a connector to attach. */
  readonly canCreate = computed(() => {
    if (!this.formName().trim()) return false;
    if (!this.useExternalKb()) return true;
    return !!this.formKbDatasourceId();
  });

    constructor() {
        // Load projects reactively — waits for currentUserId on F5 refresh, and
        // re-runs when the lifecycle tab changes, because the tab IS the query.
        effect(() => {
            const userId = this.userService.currentUserId();
            const status = this.statusFilter();
            if (userId) {
                this.fetchProjects(userId, status);
            }
        });
    }

  ngOnInit(): void {
      // Projects are loaded via effect() in the constructor (waits for currentUserId)
  }

  refresh(): void {
      const userId = this.userService.currentUserId();
      if (userId) {
          this.fetchProjects(userId, this.statusFilter());
      }
  }

  onStatusFilterChange(status: ProjectStatus | null): void {
    // The tab bar's value model is nullable; these tabs always carry one.
    if (!status || status === this.statusFilter()) return;
    this.actionError.set(null);
    this.statusFilter.set(status);
  }

    private fetchProjects(userId: string, status: ProjectStatus): void {
    const serial = ++this.loadSerial;
    this.isLoading.set(true);
    this.loadError.set(null);
        this.api.getProjects(userId, [status]).subscribe({
      next: (projects) => {
        if (serial !== this.loadSerial) return;
        this.projects.set(projects);
        this.counts.update((counts) => ({...counts, [status]: projects.length}));
        this.isLoading.set(false);
      },
      error: (err: unknown) => {
        if (serial !== this.loadSerial) return;
        this.projects.set([]);
        this.loadError.set(this.errors.translate(err, 'errors.projects.loadFailed'));
        this.isLoading.set(false);
      },
    });
    this.fetchCount(userId, status === 'active' ? 'archived' : 'active', serial);
  }

  /** The other tab's label. A count is not load-bearing — if it fails the tab
   *  keeps its last number rather than taking the page with it. */
  private fetchCount(userId: string, status: ProjectStatus, serial: number): void {
    this.api.getProjects(userId, [status]).subscribe({
      next: (projects) => {
        if (serial !== this.loadSerial) return;
        this.counts.update((counts) => ({...counts, [status]: projects.length}));
      },
      error: () => {},
    });
  }

  /** Anything that is not explicitly archived is treated as active, including
   *  a NULL the database still permits — a row with an unrecognised status
   *  must stay visible and usable rather than vanish into an unnamed state. */
  statusOf(project: Project): ProjectStatus {
    return project.status === 'archived' ? 'archived' : 'active';
  }

  unarchiveProject(project: Project, event: Event): void {
    // The card itself navigates; this button must not.
    event.stopPropagation();
    if (this.unarchivingId()) return;
    this.actionError.set(null);
    this.unarchivingId.set(project.id);
    this.api.setProjectStatus(project.id, 'active').subscribe({
      next: () => {
        this.unarchivingId.set(null);
        // Both tab counts move, so reload rather than splicing the row out.
        this.refresh();
      },
      error: (err: unknown) => {
        this.unarchivingId.set(null);
        this.actionError.set(this.errors.translate(err, 'errors.projects.unarchiveFailed'));
      },
    });
  }

  toggleCreateForm(): void {
    const next = !this.showCreateForm();
    this.showCreateForm.set(next);
    if (!next) {
      this.createFailed.set(false);
      this.createErrorDetail.set('');
      this.resetExternalKbForm();
    }
  }

  /** Connectors are fetched only once the section is opened: the default path
   *  creates an internal vault and has no use for the list. Collapsing the
   *  section also drops the selection, so a hidden connector can never be
   *  attached by accident. */
  setUseExternalKb(enabled: boolean): void {
    this.useExternalKb.set(enabled);
    if (!enabled) {
      this.resetExternalKbForm();
      return;
    }
    this.loadKbConnectors();
  }

  private loadKbConnectors(): void {
    this.isLoadingKbConnectors.set(true);
    this.api.getDatasources(undefined, 'kb').subscribe({
      next: (connectors) => {
        // A connector carrying the server-owned marker is already some
        // project's knowledge base; attaching it again can only ever 409.
        this.kbConnectors.set(
          connectors.filter((connector) => !connector.config?.native_project_id),
        );
        this.isLoadingKbConnectors.set(false);
      },
      error: () => {
        this.kbConnectors.set([]);
        this.isLoadingKbConnectors.set(false);
      },
    });
  }

  openConnectors(): void {
    this.router.navigate(['/datasources']);
  }

  private resetExternalKbForm(): void {
    this.useExternalKb.set(false);
    this.formKbDatasourceId.set('');
  }

  createProject(): void {
    const name = this.formName().trim();
    if (!name) return;
    if (!this.canCreate()) return;

    const userId = this.userService.currentUserId();
    if (!userId) return;

    const body: ProjectCreateRequest = {
      name,
      description: this.formDescription().trim() || undefined,
      goal: this.formGoal().trim() || undefined,
      user_id: userId,
    };
    if (this.useExternalKb()) {
      // Nothing but the connector id: it already carries repository, branch,
      // forge and credential, and the API rejects a request that names the
      // vault twice.
      body.external_kb = {datasource_id: this.formKbDatasourceId()};
    }

    this.createFailed.set(false);
    this.createErrorDetail.set('');
    this.isCreating.set(true);
    this.api.createProject(body).subscribe({
      next: (result) => {
        this.isCreating.set(false);
        if (result) {
          this.showCreateForm.set(false);
          this.formName.set('');
          this.formDescription.set('');
          this.formGoal.set('');
          this.resetExternalKbForm();
          // Refresh the server-side access token so the new `project-{id}`
          // group claim propagates — otherwise downstream services that read
          // groups from the JWT stay out of sync until the next natural
          // refresh.
          void this.session.forceRefresh();
          if (this.statusFilter() === 'active') {
            this.refresh();
          } else {
            // A new project is active; staying on the archived tab would hide
            // the thing that was just created. Setting the tab reloads.
            this.statusFilter.set('active');
          }
        } else {
          // ApiService maps HTTP failures to `null` for every caller; keep
          // that contract and report it here instead of failing silently.
          this.createFailed.set(true);
        }
      },
      error: (err: unknown) => {
        this.isCreating.set(false);
        this.createFailed.set(true);
        this.createErrorDetail.set(errorDetail(err));
      },
    });
  }

  openProject(id: string): void {
    this.router.navigate(['/projects', id]);
  }

  /** A rename carries a field other than `status`, which an archived project
   *  refuses whole with a 409. The card does not offer the control while
   *  archived; this still goes through the propagating `updateProjectFields`
   *  because another tab can archive the project between load and rename, and
   *  a card that silently snaps back to its old name explains nothing. */
  onRenameProject(project: Project, name: string): void {
    if (!name || name === project.name || this.statusOf(project) === 'archived') return;
    const previous = project.name;
    this.actionError.set(null);
    // Optimistic: update the card immediately, revert if the PATCH fails.
    this.projects.update((list) =>
      list.map((p) => (p.id === project.id ? {...p, name} : p)),
    );
    this.api.updateProjectFields(project.id, {name}).subscribe({
      error: (err: unknown) => {
        this.projects.update((list) =>
          list.map((p) => (p.id === project.id ? {...p, name: previous} : p)),
        );
        this.actionError.set(this.errors.translate(err, 'errors.projects.renameFailed'));
      },
    });
  }

  truncate(text: string, max: number): string {
    return text.length <= max ? text : text.slice(0, max) + '...';
  }

  asInputValue(event: Event): string {
    return (event.target as HTMLInputElement).value;
  }

  asCheckedValue(event: Event): boolean {
    return (event.target as HTMLInputElement).checked;
  }
}
