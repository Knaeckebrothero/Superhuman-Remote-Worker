import {Component, effect, inject, OnInit, signal} from '@angular/core';
import {Router} from '@angular/router';
import {ApiService} from '../../core/services/api.service';
import {UserService} from '../../core/services/user.service';
import {KeycloakService} from '../../core/services/keycloak.service';
import {Project} from '../../core/models/api.model';
import {SidebarToggleComponent} from '../../simple/layout/sidebar-toggle/sidebar-toggle.component';
import {TranslocoPipe} from '@jsverse/transloco';

@Component({
  selector: 'app-project-list-page',
  standalone: true,
  imports: [SidebarToggleComponent, TranslocoPipe],
  template: `
    <div class="page-container">
      <!-- Header -->
      <div class="page-header">
        <app-sidebar-toggle />
        <h1 class="page-title">{{ 'projects.title' | transloco }}</h1>
        <div class="header-actions">
          <button class="btn btn-primary" (click)="showCreateForm.set(!showCreateForm())">
            {{ (showCreateForm() ? 'projects.cancel' : 'projects.newProject') | transloco }}
          </button>
          <button class="btn btn-ghost" (click)="refresh()" [disabled]="isLoading()">
            {{ 'projects.refresh' | transloco }}
          </button>
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
          <div class="form-actions">
            <button
              class="btn btn-primary"
              [disabled]="isCreating() || !formName().trim()"
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
          <div class="spinner"></div>
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
                <span class="card-name">{{ project.name }}</span>
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
                <span class="chip">{{ 'projects.jobsCount' | transloco:{ count: project.job_count ?? 0 } }}</span>
                <span class="chip">{{ 'projects.reposCount' | transloco:{ count: project.repo_count ?? 0 } }}</span>
                <span class="chip">{{ 'projects.membersCount' | transloco:{ count: project.member_count ?? 0 } }}</span>
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
      max-width: 1200px;
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

    .page-title {
      font-size: 22px;
      font-weight: 700;
      color: var(--text-primary, #cdd6f4);
      margin: 0;
    }

    .header-actions { display: flex; gap: 8px; }

    .btn {
      padding: 8px 16px;
      border-radius: 6px;
      font-size: 13px;
      font-family: inherit;
      cursor: pointer;
      border: 1px solid var(--border-color, #45475a);
      transition: all 0.15s ease;
    }

    .btn-primary {
      background: var(--accent-color, #cba6f7);
      color: var(--timeline-bg, #11111b);
      border-color: var(--accent-color, #cba6f7);
      font-weight: 600;
    }

    .btn-primary:hover:not(:disabled) { opacity: 0.9; }
    .btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }

    .btn-ghost {
      background: transparent;
      color: var(--text-secondary, #a6adc8);
    }

    .btn-ghost:hover:not(:disabled) { background: var(--surface-0, #313244); }

    /* Create Form */
    .create-form {
      background: var(--panel-bg, #181825);
      border: 1px solid var(--border-color, #313244);
      border-radius: 8px;
      padding: 16px;
      margin-bottom: 20px;
    }

    .form-row { margin-bottom: 10px; }

    .form-input {
      width: 100%;
      padding: 10px 12px;
      background: var(--surface-0, #313244);
      border: 1px solid var(--border-color, #45475a);
      border-radius: 6px;
      color: var(--text-primary, #cdd6f4);
      font-size: 13px;
      font-family: inherit;
      outline: none;
      box-sizing: border-box;
    }

    .form-input:focus { border-color: var(--accent-color, #cba6f7); }

    .form-actions { display: flex; justify-content: flex-end; }

    /* Loading & Empty */
    .loading-state, .empty-state {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 12px;
      padding: 60px 20px;
      color: var(--text-muted, #6c7086);
    }

    .spinner {
      width: 32px; height: 32px;
      border: 3px solid var(--surface-0, #313244);
      border-top-color: var(--accent-color, #cba6f7);
      border-radius: 50%;
      animation: spin 0.8s linear infinite;
    }

    @keyframes spin { to { transform: rotate(360deg); } }

    .empty-icon { font-size: 48px; opacity: 0.5; }
    .empty-hint { font-size: 12px; opacity: 0.6; }

    /* Grid */
    .projects-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
      gap: 16px;
    }

    .project-card {
      background: var(--panel-bg, #181825);
      border: 1px solid var(--border-color, #313244);
      border-radius: 8px;
      padding: 16px;
      cursor: pointer;
      transition: border-color 0.15s ease, transform 0.1s ease;
    }

    .project-card:hover {
      border-color: var(--accent-color, #cba6f7);
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
      color: var(--text-primary, #cdd6f4);
    }

    .card-badges { display: flex; gap: 6px; flex-shrink: 0; }

    .badge {
      padding: 2px 8px;
      border-radius: 4px;
      font-size: 10px;
      font-weight: 500;
      text-transform: capitalize;
    }

    .badge-active { background: rgba(166, 227, 161, 0.2); color: #a6e3a1; }
    .badge-archived { background: rgba(108, 112, 134, 0.2); color: #6c7086; }
    .badge-deleted { background: rgba(243, 139, 168, 0.2); color: #f38ba8; }
    .badge-personal { background: rgba(203, 166, 247, 0.2); color: #cba6f7; }

    .card-desc {
      font-size: 12px;
      color: var(--text-secondary, #a6adc8);
      margin: 0 0 12px;
      line-height: 1.4;
    }

    .card-footer { display: flex; gap: 8px; }

    .chip {
      padding: 3px 8px;
      background: var(--surface-0, #313244);
      border-radius: 4px;
      font-size: 11px;
      color: var(--text-muted, #6c7086);
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
  private readonly keycloak = inject(KeycloakService);
  private readonly router = inject(Router);

  readonly projects = signal<Project[]>([]);
  readonly isLoading = signal(false);
  readonly showCreateForm = signal(false);
  readonly isCreating = signal(false);

  readonly formName = signal('');
  readonly formDescription = signal('');
  readonly formGoal = signal('');

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

  createProject(): void {
    const name = this.formName().trim();
    if (!name) return;

    const userId = this.userService.currentUserId();
    if (!userId) return;

    this.isCreating.set(true);
    this.api.createProject({
      name,
      description: this.formDescription().trim() || undefined,
      goal: this.formGoal().trim() || undefined,
      user_id: userId,
    }).subscribe((result) => {
      this.isCreating.set(false);
      if (result) {
        this.showCreateForm.set(false);
        this.formName.set('');
        this.formDescription.set('');
        this.formGoal.set('');
        // Refresh the Keycloak token so the new `project-{id}` group claim
        // reaches OpenCloud on the next request — otherwise the Space stays
        // invisible until the session expires.
        this.keycloak.forceRefreshToken();
        this.refresh();
      }
    });
  }

  openProject(id: string): void {
    this.router.navigate(['/projects', id]);
  }

  truncate(text: string, max: number): string {
    return text.length <= max ? text : text.slice(0, max) + '...';
  }

  asInputValue(event: Event): string {
    return (event.target as HTMLInputElement).value;
  }
}
