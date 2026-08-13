import {Component, computed, effect, inject, OnInit, signal} from '@angular/core';
import {Router} from '@angular/router';
import {ApiService} from '../../core/services/api.service';
import {UserService} from '../../core/services/user.service';
import {SessionService} from '../../core/services/session.service';
import {Project, ProjectCreateRequest} from '../../core/models/api.model';
import {SidebarToggleComponent} from '../../shell/sidebar-toggle/sidebar-toggle.component';
import {TranslocoPipe} from '@jsverse/transloco';
import {AppSpinnerComponent} from '../../ui/spinner';
import {AppInlineEditableTextComponent} from '../../ui/inline-editable-text';
import {ViewportService} from '../../core/services/viewport.service';
@Component({
  selector: 'app-project-list-page',
  standalone: true,
  imports: [SidebarToggleComponent, TranslocoPipe, AppSpinnerComponent, AppInlineEditableTextComponent],
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
               vault at an existing private GitHub repo instead of the internal
               forge. Collapsed by default — the default path is unchanged and
               sends no external_kb key at all. -->
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
              <input
                class="form-input"
                [placeholder]="'projects.externalKb.repoUrlPlaceholder' | transloco"
                [value]="formKbRepoUrl()"
                (input)="formKbRepoUrl.set(asInputValue($event))"
              />
            </div>
            <div class="form-row">
              <input
                class="form-input"
                [placeholder]="'projects.externalKb.branchPlaceholder' | transloco"
                [value]="formKbBranch()"
                (input)="formKbBranch.set(asInputValue($event))"
              />
            </div>
            <div class="form-row">
              <input
                class="form-input"
                type="password"
                autocomplete="off"
                spellcheck="false"
                [placeholder]="'projects.externalKb.tokenPlaceholder' | transloco"
                [value]="formKbToken()"
                (input)="formKbToken.set(asInputValue($event))"
              />
            </div>
          }
          @if (createFailed()) {
            <div class="form-error" role="alert">{{ 'projects.createFailed' | transloco }}</div>
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

      <!-- Loading -->
      @if (isLoading() && projects().length === 0) {
        <div class="loading-state">
          <app-spinner size="lg" tone="accent" />
          <span>{{ 'projects.loading' | transloco }}</span>
        </div>
      }

      <!-- Empty -->
      @if (!isLoading() && projects().length === 0) {
        <div class="empty-state">
          <span class="empty-icon material-symbols-outlined">folder_off</span>
          <span>{{ 'projects.empty' | transloco }}</span>
          <span class="empty-hint">{{ 'projects.emptyHint' | transloco }}</span>
        </div>
      }

      <!-- Project Cards Grid -->
      @if (projects().length > 0) {
        <div class="projects-grid">
          @for (project of projects(); track project.id) {
            <div class="project-card" (click)="openProject(project.id)">
              <div class="card-header">
                <span class="card-name">
                  <app-inline-editable-text
                    [value]="project.name"
                    [ariaLabel]="'common.rename' | transloco"
                    (save)="onRenameProject(project, $event)"
                  />
                </span>
                <div class="card-badges">
                  @if (project.is_default) {
                    <span class="badge badge-personal">{{ 'projects.badgePersonal' | transloco }}</span>
                  }
                  <span class="badge" [class]="'badge-' + project.status">
                    {{ 'projects.status.' + project.status | transloco }}
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
    .badge-deleted { background: var(--danger-tint); color: var(--danger); }
    .badge-personal { background: color-mix(in srgb, var(--accent-color) 20%, transparent); color: var(--accent-color); }

    .card-desc {
      font-size: 12px;
      color: var(--text-secondary, var(--text-secondary));
      margin: 0 0 12px;
      line-height: 1.4;
    }

    .card-footer { display: flex; gap: 8px; }

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
  private readonly userService = inject(UserService);
  private readonly session = inject(SessionService);
  private readonly router = inject(Router);
  protected readonly viewport = inject(ViewportService);

  readonly projects = signal<Project[]>([]);
  readonly isLoading = signal(false);
  readonly showCreateForm = signal(false);
  readonly isCreating = signal(false);

  readonly formName = signal('');
  readonly formDescription = signal('');
  readonly formGoal = signal('');

  /** External-KB opt-in (see docs/features/external_forge_knowledge_base.md).
   *  Off ⇒ the create body carries no `external_kb` key at all and the backend
   *  provisions the internal forge repo exactly as before. */
  readonly useExternalKb = signal(false);
  readonly formKbRepoUrl = signal('');
  readonly formKbBranch = signal('');
  /** The PAT. Lives only here, only while the form is open: never persisted,
   *  never logged, never in a URL — and cleared on success, on collapsing the
   *  section, and on closing the form. */
  readonly formKbToken = signal('');

  /** `createProject` on ApiService swallows HTTP errors into `null`, so a
   *  failure is otherwise indistinguishable from a no-op. Surface it. */
  readonly createFailed = signal(false);

  /** A name is always required; enabling the external KB additionally requires
   *  a repo URL and a token (branch is optional — the backend defaults it). */
  readonly canCreate = computed(() => {
    if (!this.formName().trim()) return false;
    if (!this.useExternalKb()) return true;
    return !!this.formKbRepoUrl().trim() && !!this.formKbToken().trim();
  });

    constructor() {
        // Load projects reactively — waits for currentUserId on F5 refresh
        effect(() => {
            const userId = this.userService.currentUserId();
            if (userId) {
                this.fetchProjects(userId);
            }
        });
    }

  ngOnInit(): void {
      // Projects are loaded via effect() in the constructor (waits for currentUserId)
  }

  refresh(): void {
      const userId = this.userService.currentUserId();
      if (userId) {
          this.fetchProjects(userId);
      }
  }

    private fetchProjects(userId: string): void {
    this.isLoading.set(true);
        this.api.getProjects(userId).subscribe((projects) => {
      this.projects.set(projects);
      this.isLoading.set(false);
    });
  }

  /** Header button. Closing the form drops the PAT from memory — the secret
   *  never outlives the form that collected it. */
  toggleCreateForm(): void {
    const next = !this.showCreateForm();
    this.showCreateForm.set(next);
    if (!next) {
      this.createFailed.set(false);
      this.resetExternalKbForm();
    }
  }

  /** Collapsing the section also discards what was typed into it, so a hidden
   *  URL/token pair can never be submitted by accident. */
  setUseExternalKb(enabled: boolean): void {
    this.useExternalKb.set(enabled);
    if (!enabled) this.resetExternalKbForm();
  }

  private resetExternalKbForm(): void {
    this.useExternalKb.set(false);
    this.formKbRepoUrl.set('');
    this.formKbBranch.set('');
    this.formKbToken.set('');
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
      const branch = this.formKbBranch().trim();
      body.external_kb = {
        repo_url: this.formKbRepoUrl().trim(),
        token: this.formKbToken().trim(),
        // Omitted rather than nulled when blank: the API's `branch` is a
        // non-nullable string that defaults to `main`, so an explicit null
        // would be a 422.
        ...(branch ? {branch} : {}),
      };
    }

    this.createFailed.set(false);
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
          this.refresh();
        } else {
          // ApiService maps HTTP failures to `null` for every caller; keep
          // that contract and report it here instead of failing silently.
          this.createFailed.set(true);
        }
      },
      error: () => {
        this.isCreating.set(false);
        this.createFailed.set(true);
      },
    });
  }

  openProject(id: string): void {
    this.router.navigate(['/projects', id]);
  }

  onRenameProject(project: Project, name: string): void {
    if (!name || name === project.name) return;
    const previous = project.name;
    // Optimistic: update the card immediately, revert if the PATCH fails.
    this.projects.update((list) =>
      list.map((p) => (p.id === project.id ? {...p, name} : p)),
    );
    this.api.updateProject(project.id, {name}).subscribe({
      next: (res) => {
        if (!res) {
          this.projects.update((list) =>
            list.map((p) => (p.id === project.id ? {...p, name: previous} : p)),
          );
        }
      },
      error: () => {
        this.projects.update((list) =>
          list.map((p) => (p.id === project.id ? {...p, name: previous} : p)),
        );
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
