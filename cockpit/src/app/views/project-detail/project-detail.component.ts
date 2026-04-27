import {ChangeDetectionStrategy, Component, computed, inject, OnDestroy, OnInit, signal} from '@angular/core';
import {ActivatedRoute, Router} from '@angular/router';
import {TranslocoPipe, TranslocoService} from '@jsverse/transloco';
import {ApiService} from '../../core/services/api.service';
import {UserService} from '../../core/services/user.service';
import {SidebarToggleComponent} from '../../shell/sidebar-toggle/sidebar-toggle.component';
import {AppIconComponent} from '../../ui/icon';
import {AppSpinnerComponent} from '../../ui/spinner';
import {AppButtonComponent} from '../../ui/button';
import {AppIconButtonComponent} from '../../ui/icon-button';
import {AppInputComponent} from '../../ui/input';
import {AppTextareaComponent} from '../../ui/textarea';
import {AppSelectComponent} from '../../ui/select';
import {AppCheckboxComponent} from '../../ui/checkbox';
import {AppBadgeComponent} from '../../ui/badge';
import {AppFormFieldComponent} from '../../ui/form-field';
import {
    Datasource,
    Expert,
    Job,
    KnowledgeNote,
    KnowledgeNoteDetail,
    KnowledgeSummary,
    Project,
    ProjectDatasource,
    ProjectMember,
    ProjectMemberRole,
    ProjectRepoRole,
    ProjectRepository,
    User,
} from '../../core/models/api.model';

type Tab = 'overview' | 'jobs' | 'knowledge' | 'datasources' | 'repos' | 'experts' | 'members' | 'settings';

@Component({
  selector: 'app-project-detail-page',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    SidebarToggleComponent,
    TranslocoPipe,
    AppIconComponent,
    AppSpinnerComponent,
    AppButtonComponent,
    AppIconButtonComponent,
    AppInputComponent,
    AppTextareaComponent,
    AppSelectComponent,
    AppCheckboxComponent,
    AppBadgeComponent,
    AppFormFieldComponent,
  ],
  template: `
    <div class="page-container">
      @if (isLoading() && !project()) {
        <div class="loading-state">
          <app-spinner size="lg" tone="accent" />
          <span>{{ 'projectDetail.loading' | transloco }}</span>
        </div>
      }

      @if (project(); as proj) {
        <!-- Header -->
        <div class="page-header">
          <app-sidebar-toggle />
          <app-icon-button
            variant="ghost"
            size="sm"
            [ariaLabel]="'projectDetail.back' | transloco"
            (clicked)="goBack()"
          >
            <app-icon size="sm">arrow_back</app-icon>
          </app-icon-button>
          <div class="header-info">
            <h1 class="page-title">{{ proj.name }}</h1>
            <div class="header-badges">
              @if (proj.is_default) {
                <app-badge tone="accent" size="xs" [uppercase]="true">
                  {{ 'projectDetail.badgePersonal' | transloco }}
                </app-badge>
              }
              <app-badge [tone]="statusTone(proj.status)" size="xs" [uppercase]="true">
                {{ proj.status }}
              </app-badge>
            </div>
          </div>
        </div>

        <!-- Tabs -->
        <div class="tab-bar">
          @for (t of tabList; track t.id) {
            <button
              class="tab-btn"
              [class.active]="activeTab() === t.id"
              (click)="activeTab.set(t.id)"
            >
              {{ t.labelKey | transloco }}
            </button>
          }
        </div>

        <!-- Tab Content -->
        <div class="tab-content">
          <!-- OVERVIEW TAB -->
          @if (activeTab() === 'overview') {
            <div class="overview-section">
              <div class="detail-grid">
                <div class="detail-card">
                  <label>{{ 'projectDetail.overview.description' | transloco }}</label>
                  @if (isEditingOverview()) {
                    <app-textarea
                      [rows]="3"
                      [value]="editDescription()"
                      (changed)="editDescription.set($event)"
                    />
                  } @else {
                    <p class="detail-value">{{ proj.description || ('projectDetail.overview.noDescription' | transloco) }}</p>
                  }
                </div>
                <div class="detail-card">
                  <label>{{ 'projectDetail.overview.goal' | transloco }}</label>
                  @if (isEditingOverview()) {
                    <app-textarea
                      [rows]="3"
                      [value]="editGoal()"
                      (changed)="editGoal.set($event)"
                    />
                  } @else {
                    <p class="detail-value">{{ proj.goal || ('projectDetail.overview.noGoal' | transloco) }}</p>
                  }
                </div>
              </div>
              <div class="stats-row">
                <div class="stat-card">
                  <span class="stat-value">{{ jobs().length }}</span>
                  <span class="stat-label">{{ 'projectDetail.overview.statsJobs' | transloco }}</span>
                </div>
                <div class="stat-card">
                  <span class="stat-value">{{ projectDatasources().length }}</span>
                  <span class="stat-label">{{ 'projectDetail.overview.statsDatasources' | transloco }}</span>
                </div>
                <div class="stat-card">
                  <span class="stat-value">{{ repos().length }}</span>
                  <span class="stat-label">{{ 'projectDetail.overview.statsRepos' | transloco }}</span>
                </div>
                <div class="stat-card">
                  <span class="stat-value">{{ members().length }}</span>
                  <span class="stat-label">{{ 'projectDetail.overview.statsMembers' | transloco }}</span>
                </div>
              </div>
              @if (proj.default_config_name) {
                <div class="detail-card">
                  <label>{{ 'projectDetail.overview.defaultConfig' | transloco }}</label>
                  <p class="detail-value mono">{{ proj.default_config_name }}</p>
                </div>
              }
              <div class="detail-card">
                <label>{{ 'projectDetail.overview.created' | transloco }}</label>
                <p class="detail-value mono">{{ formatDate(proj.created_at) }}</p>
              </div>
              <div class="overview-actions">
                @if (isEditingOverview()) {
                  <app-button variant="primary" size="md" (clicked)="saveOverview()">
                    {{ 'projectDetail.overview.save' | transloco }}
                  </app-button>
                  <app-button variant="ghost" size="md" (clicked)="cancelEditOverview()">
                    {{ 'projectDetail.overview.cancel' | transloco }}
                  </app-button>
                } @else {
                  <app-button variant="ghost" size="md" (clicked)="startEditOverview()">
                    {{ 'projectDetail.overview.edit' | transloco }}
                  </app-button>
                  @if (proj.cloud_storage_url) {
                    <a class="ghost-link" [href]="proj.cloud_storage_url" target="_blank" rel="noopener">
                      {{ 'projectDetail.overview.openFolder' | transloco }}
                    </a>
                  }
                }
              </div>
            </div>
          }

          <!-- JOBS TAB -->
          @if (activeTab() === 'jobs') {
            <div class="table-section">
              <div class="tab-toolbar">
                <app-button variant="primary" size="sm" (clicked)="createJobInProject()">
                  {{ 'projectDetail.jobs.new' | transloco }}
                </app-button>
              </div>
              @if (jobs().length === 0) {
                <div class="empty-inline">{{ 'projectDetail.jobs.empty' | transloco }}</div>
              } @else {
                <div class="table-scroll">
                <table class="data-table">
                  <thead>
                    <tr>
                      <th>{{ 'projectDetail.jobs.colStatus' | transloco }}</th>
                      <th>{{ 'projectDetail.jobs.colDescription' | transloco }}</th>
                      <th>{{ 'projectDetail.jobs.colConfig' | transloco }}</th>
                      <th>{{ 'projectDetail.jobs.colBranch' | transloco }}</th>
                      <th>{{ 'projectDetail.jobs.colMerge' | transloco }}</th>
                    </tr>
                  </thead>
                  <tbody>
                    @for (job of jobs(); track job.id) {
                      <tr>
                        <td>
                          <span class="status-badge" [class]="'status-' + job.status">
                            {{ job.status }}
                          </span>
                        </td>
                        <td class="desc-cell">{{ truncate(job.description, 60) }}</td>
                        <td class="mono">{{ job.config_name }}</td>
                        <td class="mono">{{ job.branch_name || '-' }}</td>
                        <td>
                          @if (job.merge_status) {
                            <span class="merge-badge" [class]="'merge-' + job.merge_status">
                              {{ job.merge_status }}
                            </span>
                          } @else {
                            <span class="text-muted">-</span>
                          }
                        </td>
                      </tr>
                    }
                  </tbody>
                </table>
                </div>
              }
            </div>
          }

          <!-- KNOWLEDGE TAB -->
          @if (activeTab() === 'knowledge') {
            <div class="kb-section">
              <!-- Summary Stats -->
              @if (kbSummary(); as summary) {
                <div class="kb-stats-row">
                  <div class="kb-stat">
                    <span class="kb-stat-value">{{ summary.total }}</span>
                    <span class="kb-stat-label">{{ 'projectDetail.knowledge.totalLabel' | transloco }}</span>
                  </div>
                  @for (entry of kbTypeEntries(summary); track entry[0]) {
                    <div class="kb-stat">
                      <span class="kb-stat-value">{{ entry[1] }}</span>
                      <span class="kb-stat-label">{{ entry[0] }}</span>
                    </div>
                  }
                </div>
              }

              <!-- Filters + Search -->
              <div class="kb-toolbar">
                <div class="kb-search-wrap">
                  <app-input
                    size="sm"
                    [placeholder]="'projectDetail.knowledge.searchPlaceholder' | transloco"
                    [value]="kbSearchQuery()"
                    (changed)="kbSearchQuery.set($event)"
                    (keydown.enter)="searchKB()"
                  />
                </div>
                <app-button
                  variant="primary"
                  size="sm"
                  [disabled]="!kbSearchQuery()"
                  (clicked)="searchKB()"
                >
                  {{ 'projectDetail.knowledge.search' | transloco }}
                </app-button>
                <app-select
                  size="sm"
                  [fullWidth]="false"
                  [value]="kbFilterType()"
                  (changed)="onKbFilterTypeChange($event ?? '')"
                >
                  <option value="">{{ 'projectDetail.knowledge.filterAllTypes' | transloco }}</option>
                  <option value="decision">{{ 'projectDetail.knowledge.typeDecision' | transloco }}</option>
                  <option value="learning">{{ 'projectDetail.knowledge.typeLearning' | transloco }}</option>
                  <option value="goal">{{ 'projectDetail.knowledge.typeGoal' | transloco }}</option>
                  <option value="plan">{{ 'projectDetail.knowledge.typePlan' | transloco }}</option>
                  <option value="code">{{ 'projectDetail.knowledge.typeCode' | transloco }}</option>
                  <option value="question">{{ 'projectDetail.knowledge.typeQuestion' | transloco }}</option>
                  <option value="state">{{ 'projectDetail.knowledge.typeState' | transloco }}</option>
                  <option value="source">{{ 'projectDetail.knowledge.typeSource' | transloco }}</option>
                  <option value="retrospective">{{ 'projectDetail.knowledge.typeRetrospective' | transloco }}</option>
                </app-select>
                <app-select
                  size="sm"
                  [fullWidth]="false"
                  [value]="kbFilterStatus()"
                  (changed)="onKbFilterStatusChange($event ?? '')"
                >
                  <option value="">{{ 'projectDetail.knowledge.filterAllStatuses' | transloco }}</option>
                  <option value="active">{{ 'projectDetail.knowledge.statusActive' | transloco }}</option>
                  <option value="resolved">{{ 'projectDetail.knowledge.statusResolved' | transloco }}</option>
                  <option value="superseded">{{ 'projectDetail.knowledge.statusSuperseded' | transloco }}</option>
                  <option value="archived">{{ 'projectDetail.knowledge.statusArchived' | transloco }}</option>
                </app-select>
                <app-button variant="ghost" size="sm" (clicked)="clearKBFilters()">
                  {{ 'projectDetail.knowledge.clear' | transloco }}
                </app-button>
                <span class="grow-spacer"></span>
                <app-button variant="ghost" size="sm" (clicked)="exportKB()">
                  {{ 'projectDetail.knowledge.export' | transloco }}
                </app-button>
              </div>

              <!-- Note Detail View -->
              @if (kbSelectedNote(); as note) {
                <div class="kb-detail">
                  <div class="kb-detail-header">
                    <app-button variant="ghost" size="sm" (clicked)="kbSelectedNote.set(null)">
                      <app-icon size="sm">arrow_back</app-icon>
                      {{ 'projectDetail.knowledge.back' | transloco }}
                    </app-button>
                    <span class="grow-spacer"></span>
                    <app-select
                      size="sm"
                      [fullWidth]="false"
                      [value]="note.status"
                      (changed)="updateNoteStatus(note.note_id, $event ?? '')"
                    >
                      <option value="active">{{ 'projectDetail.knowledge.statusActive' | transloco }}</option>
                      <option value="resolved">{{ 'projectDetail.knowledge.statusResolved' | transloco }}</option>
                      <option value="superseded">{{ 'projectDetail.knowledge.statusSuperseded' | transloco }}</option>
                      <option value="archived">{{ 'projectDetail.knowledge.statusArchived' | transloco }}</option>
                    </app-select>
                    <app-button variant="danger" size="sm" (clicked)="deleteNote(note.note_id)">
                      {{ 'projectDetail.knowledge.delete' | transloco }}
                    </app-button>
                  </div>
                  <div class="kb-detail-meta">
                    <span class="kb-type-badge" [attr.data-type]="note.note_type">{{ note.note_type }}</span>
                    @if (note.confidence) {
                      <span class="kb-confidence">{{ note.confidence }}</span>
                    }
                    @if (note.phase) {
                      <span class="text-muted">{{ 'projectDetail.knowledge.phaseLabel' | transloco:{ phase: note.phase } }}</span>
                    }
                    <span class="text-muted">{{ formatDate(note.modified_at) }}</span>
                  </div>
                  <h2 class="kb-detail-title">{{ note.title }}</h2>
                  @if (note.tags && note.tags.length > 0) {
                    <div class="kb-tags">
                      @for (tag of note.tags; track tag) {
                        <span class="kb-tag">{{ tag }}</span>
                      }
                    </div>
                  }
                  <div class="kb-detail-content">{{ note.content }}</div>
                  @if (note.relationships && note.relationships.length > 0) {
                    <div class="kb-relationships">
                      <h4>{{ 'projectDetail.knowledge.relationships' | transloco }}</h4>
                      @for (rel of note.relationships; track rel.target_id) {
                        <div class="kb-rel-item">
                          <span class="kb-rel-type">{{ rel.type }}</span>
                          <button class="kb-rel-link" (click)="openNote(rel.target_id)">
                            {{ rel.target_title || rel.target_id }}
                          </button>
                        </div>
                      }
                    </div>
                  }
                </div>
              } @else {
                <!-- Note List -->
                @if (kbIsLoading()) {
                  <div class="loading-state">
                    <app-spinner size="lg" tone="accent" />
                    <span>{{ 'projectDetail.knowledge.loading' | transloco }}</span>
                  </div>
                } @else if (kbNotes().length === 0) {
                  <div class="empty-inline">{{ 'projectDetail.knowledge.empty' | transloco }}</div>
                } @else {
                  <div class="kb-note-list">
                    @for (note of kbNotes(); track note.note_id) {
                      <div class="kb-note-card" (click)="openNote(note.note_id)">
                        <div class="kb-note-header">
                          <span class="kb-type-badge" [attr.data-type]="note.note_type">{{ note.note_type }}</span>
                          <span class="kb-note-title">{{ note.title }}</span>
                          <span class="kb-note-status" [attr.data-status]="note.status">{{ note.status }}</span>
                        </div>
                        @if (note.content_preview) {
                          <div class="kb-note-preview">{{ truncate(note.content_preview, 180) }}</div>
                        }
                        <div class="kb-note-footer">
                          @if (note.tags && note.tags.length > 0) {
                            @for (tag of note.tags.slice(0, 4); track tag) {
                              <span class="kb-tag-sm">{{ tag }}</span>
                            }
                            @if (note.tags.length > 4) {
                              <span class="text-muted">+{{ note.tags.length - 4 }}</span>
                            }
                          }
                          @if (note.confidence) {
                            <span class="kb-confidence-sm">{{ note.confidence }}</span>
                          }
                          <span class="text-muted footer-date">{{ formatDate(note.modified_at) }}</span>
                        </div>
                      </div>
                    }
                  </div>
                  @if (kbTotal() > kbNotes().length) {
                    <div class="kb-pagination">
                      <span class="text-muted">{{ 'projectDetail.knowledge.pagination' | transloco:{ current: kbNotes().length, total: kbTotal() } }}</span>
                      <app-button variant="ghost" size="sm" (clicked)="loadMoreKBNotes()">
                        {{ 'projectDetail.knowledge.loadMore' | transloco }}
                      </app-button>
                    </div>
                  }
                }
              }
            </div>
          }

          <!-- DATASOURCES TAB -->
          @if (activeTab() === 'datasources') {
            <div class="table-section">
              <!-- Link Datasource Form -->
              <div class="inline-form">
                <app-select
                  size="sm"
                  [value]="dsLinkId()"
                  (changed)="dsLinkId.set($event ?? '')"
                >
                  <option value="">{{ 'projectDetail.datasources.selectPlaceholder' | transloco }}</option>
                  @for (ds of availableDatasources(); track ds.id) {
                    <option [value]="ds.id">{{ ds.name }} ({{ ds.type }})</option>
                  }
                </app-select>
                <app-button
                  variant="primary"
                  size="sm"
                  [disabled]="!dsLinkId()"
                  (clicked)="linkDatasource()"
                >
                  {{ 'projectDetail.datasources.link' | transloco }}
                </app-button>
              </div>

              @if (projectDatasources().length === 0) {
                <div class="empty-inline">{{ 'projectDetail.datasources.empty' | transloco }}</div>
              } @else {
                <div class="table-scroll">
                <table class="data-table">
                  <thead>
                    <tr>
                      <th>{{ 'projectDetail.datasources.colName' | transloco }}</th>
                      <th>{{ 'projectDetail.datasources.colType' | transloco }}</th>
                      <th>{{ 'projectDetail.datasources.colDescription' | transloco }}</th>
                      <th>{{ 'projectDetail.datasources.colAccess' | transloco }}</th>
                      <th>{{ 'projectDetail.datasources.colActions' | transloco }}</th>
                    </tr>
                  </thead>
                  <tbody>
                    @for (ds of projectDatasources(); track ds.id) {
                      <tr>
                        <td>{{ ds.name }}</td>
                        <td>
                          <span class="role-badge" [class]="'role-' + ds.type">{{ ds.type }}</span>
                        </td>
                        <td>
                          <app-input
                            size="sm"
                            [value]="ds.project_description || ds.description || ''"
                            [placeholder]="'projectDetail.datasources.descriptionPlaceholder' | transloco"
                            (blurred)="updateDatasourceDescription(ds.id, $any($event.target).value)"
                          />
                        </td>
                        <td>
                          <app-select
                            size="sm"
                            [value]="boolToText(ds.project_read_only)"
                            (changed)="updateDatasourceReadOnly(ds.id, $event ?? '')"
                          >
                            <option value="">{{ 'projectDetail.datasources.accessDefault' | transloco }}</option>
                            <option value="true">{{ 'projectDetail.datasources.accessReadOnly' | transloco }}</option>
                            <option value="false">{{ 'projectDetail.datasources.accessReadWrite' | transloco }}</option>
                          </app-select>
                        </td>
                        <td>
                          <app-button variant="danger" size="sm" (clicked)="unlinkDatasource(ds.id)">
                            {{ 'projectDetail.datasources.unlink' | transloco }}
                          </app-button>
                        </td>
                      </tr>
                    }
                  </tbody>
                </table>
                </div>
              }
            </div>
          }

          <!-- REPOS TAB -->
          @if (activeTab() === 'repos') {
            <div class="table-section">
              <!-- Add Repo Form -->
              <div class="inline-form">
                <app-input
                  size="sm"
                  [placeholder]="'projectDetail.repos.namePlaceholder' | transloco"
                  [value]="repoName()"
                  (changed)="repoName.set($event)"
                />
                <app-input
                  size="sm"
                  [placeholder]="'projectDetail.repos.urlPlaceholder' | transloco"
                  [value]="repoUrl()"
                  (changed)="repoUrl.set($event)"
                />
                <app-select
                  size="sm"
                  [fullWidth]="false"
                  [value]="repoRole()"
                  (changed)="onRepoRoleChange($event)"
                >
                  <option value="jobs">{{ 'projectDetail.repos.roleJobs' | transloco }}</option>
                  <option value="source">{{ 'projectDetail.repos.roleSource' | transloco }}</option>
                  <option value="reference">{{ 'projectDetail.repos.roleReference' | transloco }}</option>
                </app-select>
                <app-checkbox
                  size="sm"
                  [checked]="repoReadOnly()"
                  (changed)="repoReadOnly.set($event)"
                >
                  {{ 'projectDetail.repos.readOnlyLabel' | transloco }}
                </app-checkbox>
                <app-checkbox
                  size="sm"
                  [checked]="repoCreateManaged()"
                  (changed)="repoCreateManaged.set($event)"
                >
                  {{ 'projectDetail.repos.managedLabel' | transloco }}
                </app-checkbox>
                <app-button
                  variant="primary"
                  size="sm"
                  [disabled]="!repoName().trim()"
                  (clicked)="addRepo()"
                >
                  {{ 'projectDetail.repos.add' | transloco }}
                </app-button>
              </div>

              @if (repos().length === 0) {
                <div class="empty-inline">{{ 'projectDetail.repos.empty' | transloco }}</div>
              } @else {
                <div class="table-scroll">
                <table class="data-table">
                  <thead>
                    <tr>
                      <th>{{ 'projectDetail.repos.colRole' | transloco }}</th>
                      <th>{{ 'projectDetail.repos.colName' | transloco }}</th>
                      <th>{{ 'projectDetail.repos.colUrl' | transloco }}</th>
                      <th>{{ 'projectDetail.repos.colReadOnly' | transloco }}</th>
                      <th>{{ 'projectDetail.repos.colManaged' | transloco }}</th>
                      <th>{{ 'projectDetail.repos.colActions' | transloco }}</th>
                    </tr>
                  </thead>
                  <tbody>
                    @for (repo of repos(); track repo.id) {
                      <tr>
                        <td>
                          <span class="role-badge" [class]="'role-' + repo.role">
                            {{ repo.role }}
                          </span>
                        </td>
                        <td>{{ repo.name }}</td>
                        <td class="mono url-cell">{{ repo.repo_url || '-' }}</td>
                        <td>{{ (repo.read_only ? 'projectDetail.repos.yes' : 'projectDetail.repos.no') | transloco }}</td>
                        <td>{{ (repo.is_managed ? 'projectDetail.repos.yes' : 'projectDetail.repos.no') | transloco }}</td>
                        <td>
                          <app-button variant="danger" size="sm" (clicked)="removeRepo(repo.id)">
                            {{ 'projectDetail.repos.remove' | transloco }}
                          </app-button>
                        </td>
                      </tr>
                    }
                  </tbody>
                </table>
                </div>
              }
            </div>
          }

          <!-- EXPERTS TAB -->
          @if (activeTab() === 'experts') {
            <div class="table-section">
              @if (isLoadingExperts()) {
                <div class="empty-inline">
                  <app-spinner size="sm" tone="accent" />
                  {{ 'projectDetail.experts.loading' | transloco }}
                </div>
              } @else if (projectExperts().length === 0) {
                <div class="empty-inline">
                  {{ 'projectDetail.experts.emptyPrefix' | transloco }}
                  <code>experts/</code> {{ 'projectDetail.experts.emptySuffix' | transloco }}
                </div>
              } @else {
                <div class="expert-grid">
                  @for (expert of projectExperts(); track expert.id) {
                    <div
                      class="expert-card"
                      [style.--expert-color]="expert.color"
                    >
                      <app-icon size="inherit" class="expert-icon" [style.color]="expert.color">{{ expert.icon }}</app-icon>
                      <span class="expert-name">{{ expert.display_name }}</span>
                      <span class="expert-desc">{{ expert.description }}</span>
                      @if (expert.tags.length > 0) {
                        <div class="expert-tags">
                          @for (tag of expert.tags; track tag) {
                            <span class="expert-tag">{{ tag }}</span>
                          }
                        </div>
                      }
                    </div>
                  }
                </div>
              }
            </div>
          }

          <!-- MEMBERS TAB -->
          @if (activeTab() === 'members') {
            <div class="table-section">
              <!-- Add Member Form -->
              <div class="inline-form">
                <app-select
                  size="sm"
                  [value]="memberUserId()"
                  (changed)="memberUserId.set($event ?? '')"
                >
                  <option value="">{{ 'projectDetail.members.selectUser' | transloco }}</option>
                  @for (user of availableUsers(); track user.id) {
                    <option [value]="user.id">{{ user.display_name }}</option>
                  }
                </app-select>
                <app-select
                  size="sm"
                  [fullWidth]="false"
                  [value]="memberRole()"
                  (changed)="onMemberRoleSelectChange($event)"
                >
                  <option value="editor">{{ 'projectDetail.members.roleEditor' | transloco }}</option>
                  <option value="viewer">{{ 'projectDetail.members.roleViewer' | transloco }}</option>
                  <option value="owner">{{ 'projectDetail.members.roleOwner' | transloco }}</option>
                </app-select>
                <app-button
                  variant="primary"
                  size="sm"
                  [disabled]="!memberUserId()"
                  (clicked)="addMember()"
                >
                  {{ 'projectDetail.members.add' | transloco }}
                </app-button>
              </div>

              @if (members().length === 0) {
                <div class="empty-inline">{{ 'projectDetail.members.empty' | transloco }}</div>
              } @else {
                <div class="table-scroll">
                <table class="data-table">
                  <thead>
                    <tr>
                      <th>{{ 'projectDetail.members.colUser' | transloco }}</th>
                      <th>{{ 'projectDetail.members.colRole' | transloco }}</th>
                      <th>{{ 'projectDetail.members.colJoined' | transloco }}</th>
                      <th>{{ 'projectDetail.members.colActions' | transloco }}</th>
                    </tr>
                  </thead>
                  <tbody>
                    @for (member of members(); track member.user_id) {
                      <tr>
                        <td>
                          <div class="user-cell">
                            <span
                              class="user-avatar-sm"
                              [style.background]="member.avatar_color || 'var(--accent-color)'"
                            >{{ getInitials(member.display_name || '?') }}</span>
                            <span>{{ member.display_name || member.user_id.slice(0, 8) }}</span>
                          </div>
                        </td>
                        <td>
                          <app-select
                            size="sm"
                            [fullWidth]="false"
                            [value]="member.role"
                            (changed)="onRowMemberRoleChange(member.user_id, $event)"
                          >
                            <option value="owner">{{ 'projectDetail.members.roleOwnerLower' | transloco }}</option>
                            <option value="editor">{{ 'projectDetail.members.roleEditorLower' | transloco }}</option>
                            <option value="viewer">{{ 'projectDetail.members.roleViewerLower' | transloco }}</option>
                          </app-select>
                        </td>
                        <td class="mono">{{ formatDate(member.joined_at) }}</td>
                        <td>
                          <app-button
                            variant="danger"
                            size="sm"
                            [disabled]="member.role === 'owner' && ownerCount() <= 1"
                            (clicked)="removeMember(member.user_id)"
                          >
                            {{ 'projectDetail.members.remove' | transloco }}
                          </app-button>
                        </td>
                      </tr>
                    }
                  </tbody>
                </table>
                </div>
              }
            </div>
          }

          <!-- SETTINGS TAB -->
          @if (activeTab() === 'settings') {
            <div class="settings-section">
              <!-- General -->
              <div class="settings-group">
                <h3 class="settings-heading">{{ 'projectDetail.settings.general' | transloco }}</h3>
                <app-form-field [label]="'projectDetail.settings.projectName' | transloco">
                  <app-input
                    [value]="settingsName()"
                    [disabled]="proj.is_default"
                    (changed)="settingsName.set($event)"
                  />
                </app-form-field>
                <app-form-field [label]="'projectDetail.settings.defaultConfig' | transloco">
                  <app-input
                    [placeholder]="'projectDetail.settings.configPlaceholder' | transloco"
                    [value]="settingsConfigName()"
                    (changed)="settingsConfigName.set($event)"
                  />
                </app-form-field>
                <div class="settings-actions">
                  <app-button
                    variant="primary"
                    size="sm"
                    [loading]="isSavingSettings()"
                    [disabled]="isSavingSettings()"
                    (clicked)="saveSettings()"
                  >
                    {{ 'projectDetail.settings.save' | transloco }}
                  </app-button>
                </div>
              </div>

              <!-- Memory -->
              <div class="settings-group">
                <h3 class="settings-heading">{{ 'projectDetail.settings.memory' | transloco }}</h3>
                <app-checkbox
                  [checked]="projectMemoryShared()"
                  (changed)="toggleProjectMemory($event)"
                >
                  {{ 'projectDetail.settings.shareMemories' | transloco }}
                </app-checkbox>
                <p class="text-muted setting-desc">
                  {{ 'projectDetail.settings.shareMemoriesDesc' | transloco }}
                </p>
              </div>

              <!-- Cloud Storage -->
              <div class="settings-group">
                <h3 class="settings-heading">{{ 'projectDetail.settings.cloudStorage' | transloco }}</h3>
                @if (proj.cloud_storage_url) {
                  <div class="cloud-folder-row">
                    <label class="form-label-sm">{{ 'projectDetail.settings.folder' | transloco }}</label>
                    <a [href]="proj.cloud_storage_url" target="_blank" rel="noopener" class="cloud-folder-link">
                      {{ proj.name }}
                      <app-icon size="xs">open_in_new</app-icon>
                    </a>
                  </div>
                  <app-checkbox
                    [checked]="settingsCloudReadOnly()"
                    (changed)="toggleCloudReadOnly($event)"
                  >
                    {{ 'projectDetail.settings.readOnlyAgent' | transloco }}
                  </app-checkbox>
                  <p class="text-muted setting-desc">
                    {{ 'projectDetail.settings.readOnlyAgentDesc' | transloco }}
                  </p>
                } @else {
                  <p class="text-muted setting-desc">
                    {{ 'projectDetail.settings.cloudUnavailable' | transloco }}
                  </p>
                }
              </div>

              <!-- Danger Zone -->
              <div class="settings-group danger-zone">
                <h3 class="settings-heading danger-heading">{{ 'projectDetail.settings.dangerZone' | transloco }}</h3>
                @if (proj.is_default) {
                  <p class="text-muted setting-desc">
                    {{ 'projectDetail.settings.defaultProjectWarning' | transloco }}
                  </p>
                } @else {
                  <div class="danger-actions">
                    @if (proj.status === 'active') {
                      <div class="danger-row">
                        <div class="danger-info">
                          <span class="danger-title">{{ 'projectDetail.settings.archiveTitle' | transloco }}</span>
                          <span class="danger-desc">
                            {{ 'projectDetail.settings.archiveDesc' | transloco }}
                          </span>
                        </div>
                        <app-button variant="ghost" size="sm" (clicked)="archiveProject()">
                          {{ 'projectDetail.settings.archive' | transloco }}
                        </app-button>
                      </div>
                    }
                    <div class="danger-row">
                      <div class="danger-info">
                        <span class="danger-title">{{ 'projectDetail.settings.deleteTitle' | transloco }}</span>
                        <span class="danger-desc">
                          {{ 'projectDetail.settings.deleteDesc' | transloco }}
                        </span>
                      </div>
                      <app-button variant="danger" size="sm" (clicked)="deleteProject()">
                        {{ 'projectDetail.settings.delete' | transloco }}
                      </app-button>
                    </div>
                  </div>
                }
              </div>
            </div>
          }
        </div>
      }
    </div>
  `,
  styles: [`
    :host { display: block; height: 100%; overflow: auto; overflow-x: hidden; }

    .page-container {
      padding: 24px;
      max-width: 1200px;
      margin: 0 auto;
      overflow-x: hidden;
    }

    .page-header {
      display: flex;
      align-items: center;
      gap: 12px;
      margin-bottom: 20px;
    }

    .header-info { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }

    .page-title {
      font-size: 22px;
      font-weight: 700;
      color: var(--text-primary);
      margin: 0;
    }

    .header-badges { display: flex; gap: 6px; }

    /* Tabs */
    .tab-bar {
      display: flex;
      gap: 2px;
      border-bottom: 1px solid var(--border-color);
      margin-bottom: 20px;
      overflow-x: auto;
      -webkit-overflow-scrolling: touch;
      scrollbar-width: none;
    }

    .tab-bar::-webkit-scrollbar { display: none; }

    .tab-btn {
      padding: 10px 20px;
      background: none;
      border: none;
      border-bottom: 2px solid transparent;
      color: var(--text-secondary);
      font-size: 13px;
      font-family: inherit;
      cursor: pointer;
      transition: all 0.15s ease;
      white-space: nowrap;
      flex-shrink: 0;
    }

    .tab-btn:hover { color: var(--text-primary); }

    .tab-btn.active {
      color: var(--accent-color);
      border-bottom-color: var(--accent-color);
    }

    .ghost-link {
      display: inline-flex;
      align-items: center;
      padding: 8px 16px;
      border-radius: 6px;
      font-size: 13px;
      color: var(--text-secondary);
      text-decoration: none;
      border: 1px solid transparent;
    }
    .ghost-link:hover { background: var(--surface-0); }

    .loading-state, .empty-state {
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 12px;
      padding: 60px 20px;
      color: var(--text-muted);
    }

    /* Overview */
    .overview-section { display: flex; flex-direction: column; gap: 16px; }

    .detail-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 16px;
    }

    .detail-card {
      background: var(--panel-bg);
      border: 1px solid var(--border-color);
      border-radius: 8px;
      padding: 14px;
    }

    .detail-card label {
      display: block;
      font-size: 11px;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.5px;
      margin-bottom: 6px;
    }

    .detail-value {
      margin: 0;
      font-size: 13px;
      color: var(--text-primary);
      line-height: 1.4;
    }

    .stats-row { display: flex; gap: 16px; }

    .stat-card {
      flex: 1;
      background: var(--panel-bg);
      border: 1px solid var(--border-color);
      border-radius: 8px;
      padding: 16px;
      text-align: center;
    }

    .stat-value {
      display: block;
      font-size: 28px;
      font-weight: 700;
      color: var(--accent-color);
    }

    .stat-label {
      display: block;
      font-size: 11px;
      color: var(--text-muted);
      margin-top: 4px;
    }

    .overview-actions { display: flex; gap: 8px; align-items: center; }

    .mono { font-family: 'JetBrains Mono', monospace; font-size: 12px; }

    /* Tab toolbar */
    .tab-toolbar {
      display: flex;
      justify-content: flex-end;
      margin-bottom: 4px;
    }

    /* Tables */
    .table-section { display: flex; flex-direction: column; gap: 12px; }

    .data-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 12px;
    }

    .data-table th {
      text-align: left;
      padding: 10px 12px;
      background: var(--surface-0);
      color: var(--text-muted);
      font-weight: 500;
      text-transform: uppercase;
      font-size: 10px;
      letter-spacing: 0.5px;
      border-bottom: 1px solid var(--border-color);
    }

    .data-table td {
      padding: 10px 12px;
      border-bottom: 1px solid var(--border-color);
      color: var(--text-primary);
      vertical-align: middle;
    }

    .desc-cell { max-width: 250px; }
    .url-cell { max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

    .empty-inline {
      padding: 20px;
      text-align: center;
      color: var(--text-muted);
      font-size: 13px;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
    }

    /* Status badges (kept custom — semantic palette per status) */
    .status-badge {
      display: inline-block;
      padding: 3px 8px;
      border-radius: 4px;
      font-size: 11px;
      font-weight: 500;
      text-transform: capitalize;
    }

    .status-created,
    .status-pending { background: var(--accent-color); color: var(--app-bg); opacity: 0.85; }
    .status-processing { background: var(--warning-tint); color: var(--warning); }
    .status-completed { background: var(--success-tint); color: var(--success); }
    .status-failed { background: var(--danger-tint); color: var(--danger); }
    .status-cancelled { background: var(--surface-0); color: var(--text-muted); }
    .status-pending_review { background: var(--warning-tint); color: var(--warning); }

    /* Merge badges */
    .merge-badge {
      display: inline-block;
      padding: 3px 8px;
      border-radius: 4px;
      font-size: 11px;
      font-weight: 500;
    }

    .merge-merged { background: var(--success-tint); color: var(--success); }
    .merge-conflict { background: var(--danger-tint); color: var(--danger); }
    .merge-skipped { background: var(--surface-0); color: var(--text-muted); }
    .merge-pending { background: var(--warning-tint); color: var(--warning); }

    /* Role badges */
    .role-badge {
      display: inline-block;
      padding: 3px 8px;
      border-radius: 4px;
      font-size: 11px;
      font-weight: 500;
      background: var(--surface-0);
      color: var(--text-secondary);
    }

    .role-jobs { background: var(--accent-color); color: var(--app-bg); }
    .role-source { background: var(--success-tint); color: var(--success); }
    .role-reference { background: var(--warning-tint); color: var(--warning); }

    /* Inline form */
    .inline-form {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 12px;
      background: var(--panel-bg);
      border: 1px solid var(--border-color);
      border-radius: 8px;
      flex-wrap: wrap;
    }

    /* User cell */
    .user-cell {
      display: flex;
      align-items: center;
      gap: 8px;
    }

    .user-avatar-sm {
      width: 24px;
      height: 24px;
      border-radius: 50%;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 10px;
      font-weight: 600;
      color: var(--app-bg);
      flex-shrink: 0;
    }

    .text-muted { color: var(--text-muted); }
    .grow-spacer { flex: 1; }

    /* Expert grid */
    .expert-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
      gap: 12px;
    }

    .expert-card {
      display: flex;
      flex-direction: column;
      gap: 6px;
      padding: 14px;
      border: 1px solid var(--border-color);
      border-radius: 8px;
      background: var(--panel-bg);
    }

    .expert-icon { font-size: 28px; }

    .expert-name {
      font-size: 13px;
      font-weight: 600;
      color: var(--text-primary);
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

    .expert-tags { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 2px; }

    .expert-tag {
      font-size: 10px;
      padding: 1px 6px;
      border-radius: 3px;
      background: var(--surface-0);
      color: var(--text-muted);
    }

    /* Settings */
    .settings-section { display: flex; flex-direction: column; gap: 20px; }

    .settings-group {
      background: var(--panel-bg);
      border: 1px solid var(--border-color);
      border-radius: 8px;
      padding: 16px;
    }

    .settings-heading {
      font-size: 14px;
      font-weight: 600;
      color: var(--text-primary);
      margin: 0 0 14px 0;
    }

    .form-label-sm {
      display: block;
      font-size: 11px;
      color: var(--text-muted);
      margin-bottom: 4px;
    }

    .settings-actions { display: flex; gap: 8px; margin-top: 8px; }
    .setting-desc { font-size: 12px; margin-top: 4px; }

    .cloud-folder-row {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-bottom: 12px;
    }
    .cloud-folder-link {
      color: var(--accent-color);
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      gap: 4px;
    }
    .cloud-folder-link:hover { text-decoration: underline; }

    /* Danger Zone */
    .danger-zone { border-color: var(--danger); }
    .danger-heading { color: var(--danger); }

    .danger-actions { display: flex; flex-direction: column; gap: 12px; }

    .danger-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 12px;
      border: 1px solid var(--border-color);
      border-radius: 6px;
    }

    .danger-info { flex: 1; min-width: 0; }

    .danger-title {
      display: block;
      font-size: 13px;
      font-weight: 500;
      color: var(--text-primary);
    }

    .danger-desc {
      display: block;
      font-size: 11px;
      color: var(--text-muted);
      margin-top: 2px;
    }

    /* Knowledge Base */
    .kb-section { display: flex; flex-direction: column; gap: 16px; }

    .kb-stats-row {
      display: flex; gap: 12px; flex-wrap: wrap;
    }

    .kb-stat {
      flex: 1; min-width: 70px;
      background: var(--panel-bg);
      border: 1px solid var(--border-color);
      border-radius: 8px;
      padding: 12px;
      text-align: center;
    }

    .kb-stat-value {
      display: block; font-size: 22px; font-weight: 700;
      color: var(--accent-color);
    }

    .kb-stat-label {
      display: block; font-size: 10px;
      color: var(--text-muted);
      text-transform: capitalize; margin-top: 2px;
    }

    .kb-toolbar {
      display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
    }

    .kb-search-wrap { flex: 1; min-width: 180px; }

    .kb-note-list { display: flex; flex-direction: column; gap: 8px; }

    .kb-note-card {
      background: var(--panel-bg);
      border: 1px solid var(--border-color);
      border-radius: 8px;
      padding: 14px;
      cursor: pointer;
      transition: border-color 0.15s ease;
    }

    .kb-note-card:hover { border-color: var(--accent-color); }

    .kb-note-header {
      display: flex; align-items: center; gap: 8px; margin-bottom: 6px;
    }

    .kb-note-title {
      font-size: 14px; font-weight: 600;
      color: var(--text-primary);
      flex: 1; min-width: 0;
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }

    .kb-note-status {
      font-size: 10px; padding: 2px 6px; border-radius: 3px;
      font-weight: 500; text-transform: capitalize;
    }

    .kb-note-status[data-status="active"] { background: var(--success-tint); color: var(--success); }
    .kb-note-status[data-status="resolved"] { background: var(--accent-color); color: var(--app-bg); }
    .kb-note-status[data-status="superseded"] { background: var(--warning-tint); color: var(--warning); }
    .kb-note-status[data-status="archived"] { background: var(--surface-0); color: var(--text-muted); }

    .kb-type-badge {
      font-size: 10px; padding: 2px 8px; border-radius: 4px;
      font-weight: 600; text-transform: capitalize; white-space: nowrap;
      background: var(--surface-0); color: var(--text-secondary);
    }

    .kb-type-badge[data-type="decision"] { background: rgba(203, 166, 247, 0.2); color: var(--accent-color); }
    .kb-type-badge[data-type="learning"] { background: var(--success-tint); color: var(--success); }
    .kb-type-badge[data-type="goal"] { background: var(--warning-tint); color: var(--warning); }

    .kb-note-preview {
      font-size: 12px; color: var(--text-secondary);
      line-height: 1.5; margin-bottom: 8px;
      display: -webkit-box; -webkit-line-clamp: 2;
      -webkit-box-orient: vertical; overflow: hidden;
    }

    .kb-note-footer {
      display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
    }
    .kb-note-footer .footer-date { margin-left: auto; }

    .kb-tag-sm {
      font-size: 10px; padding: 1px 5px; border-radius: 3px;
      background: var(--surface-0); color: var(--text-muted);
    }

    .kb-confidence-sm {
      font-size: 10px; padding: 1px 5px; border-radius: 3px;
      background: var(--surface-0); color: var(--accent-color);
      text-transform: capitalize;
    }

    .kb-pagination {
      display: flex; align-items: center; justify-content: center; gap: 12px;
      padding: 12px;
    }

    /* Detail view */
    .kb-detail {
      background: var(--panel-bg);
      border: 1px solid var(--border-color);
      border-radius: 8px;
      padding: 20px;
    }

    .kb-detail-header {
      display: flex; align-items: center; gap: 8px; margin-bottom: 12px;
    }

    .kb-detail-meta {
      display: flex; align-items: center; gap: 8px; margin-bottom: 8px;
      font-size: 12px;
    }

    .kb-detail-title {
      font-size: 20px; font-weight: 700; margin: 0 0 12px 0;
      color: var(--text-primary);
    }

    .kb-tags { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 14px; }

    .kb-tag {
      font-size: 11px; padding: 3px 8px; border-radius: 4px;
      background: var(--surface-0); color: var(--accent-color);
    }

    .kb-confidence {
      font-size: 12px; padding: 2px 6px; border-radius: 3px;
      background: var(--surface-0); color: var(--accent-color);
      text-transform: capitalize;
    }

    .kb-detail-content {
      font-size: 13px; line-height: 1.7;
      color: var(--text-primary);
      white-space: pre-wrap; word-wrap: break-word;
    }

    .kb-relationships {
      margin-top: 16px; padding-top: 16px;
      border-top: 1px solid var(--border-color);
    }

    .kb-relationships h4 {
      font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px;
      color: var(--text-muted); margin: 0 0 8px 0;
    }

    .kb-rel-item {
      display: flex; align-items: center; gap: 8px;
      font-size: 12px; margin-bottom: 4px;
    }

    .kb-rel-type {
      font-size: 10px; padding: 2px 6px; border-radius: 3px;
      background: var(--surface-0); color: var(--text-secondary);
      font-weight: 500;
    }

    .kb-rel-link {
      background: none; border: none; padding: 0;
      color: var(--accent-color);
      cursor: pointer; font-size: 12px; font-family: inherit;
      text-decoration: underline;
    }

    .kb-rel-link:hover { opacity: 0.8; }

    .table-scroll {
      overflow-x: auto;
      -webkit-overflow-scrolling: touch;
    }

    @media (max-width: 768px) {
      .page-container { padding: 12px; }

      .page-header { flex-wrap: wrap; gap: 8px; }
      .page-title { font-size: 18px; }
      .header-info { gap: 8px; }

      .tab-bar {
        position: relative;
        mask-image: linear-gradient(to right, black calc(100% - 24px), transparent);
        -webkit-mask-image: linear-gradient(to right, black calc(100% - 24px), transparent);
      }

      .tab-btn { padding: 10px 14px; font-size: 12px; }

      .detail-grid { grid-template-columns: 1fr; }

      .stats-row {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 8px;
      }
      .stat-card { padding: 12px; }
      .stat-value { font-size: 22px; }

      .data-table { min-width: 500px; }

      .inline-form { flex-direction: column; align-items: stretch; }
      .kb-stats-row { flex-direction: column; }
      .kb-toolbar { flex-direction: column; gap: 8px; }
    }
  `],
})
export class ProjectDetailPageComponent implements OnInit, OnDestroy {
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly api = inject(ApiService);
  private readonly userService = inject(UserService);
  private readonly transloco = inject(TranslocoService);

  readonly project = signal<Project | null>(null);
  readonly jobs = signal<Job[]>([]);
  readonly repos = signal<ProjectRepository[]>([]);
  readonly members = signal<ProjectMember[]>([]);
  readonly isLoading = signal(false);
  readonly activeTab = signal<Tab>('overview');

  // Overview editing
  readonly isEditingOverview = signal(false);
  readonly editDescription = signal('');
  readonly editGoal = signal('');

  // Repos tab
  readonly repoName = signal('');
  readonly repoUrl = signal('');
  readonly repoRole = signal<ProjectRepoRole>('jobs');
  readonly repoReadOnly = signal(false);
  readonly repoCreateManaged = signal(false);

  // Members tab
  readonly memberUserId = signal('');
  readonly memberRole = signal<ProjectMemberRole>('editor');
  readonly allUsers = signal<User[]>([]);

  readonly ownerCount = computed(() =>
    this.members().filter((m) => m.role === 'owner').length,
  );

  readonly availableUsers = computed(() => {
    const memberIds = new Set(this.members().map((m) => m.user_id));
    return this.allUsers().filter((u) => !memberIds.has(u.id));
  });

  // Datasources tab
  readonly projectDatasources = signal<ProjectDatasource[]>([]);
  readonly allDatasources = signal<Datasource[]>([]);
  readonly dsLinkId = signal('');

  readonly availableDatasources = computed(() => {
    const linked = new Set(this.projectDatasources().map((d) => d.id));
    return this.allDatasources().filter((d) => !linked.has(d.id));
  });

  // Experts tab
  readonly projectExperts = signal<Expert[]>([]);
  readonly isLoadingExperts = signal(false);

  // Settings tab
  readonly settingsName = signal('');
  readonly settingsConfigName = signal('');
  readonly settingsCloudReadOnly = signal(false);
  readonly isSavingSettings = signal(false);
  /** Framework defaults from GET /api/experts/defaults — used as fallback for toggles. */
  private readonly frameworkDefaults = signal<Record<string, unknown> | null>(null);
  readonly projectMemoryShared = computed(() => {
    const p = this.project();
    const val = (p?.default_config_override as any)?.memory?.project_scoped;
    if (typeof val === 'boolean') return val;
    // Fall back to framework default from defaults.yaml
    const defaults = this.frameworkDefaults();
    const defaultVal = (defaults?.['memory'] as any)?.['project_scoped'];
    return typeof defaultVal === 'boolean' ? defaultVal : true;
  });

  // Knowledge tab
  readonly kbSummary = signal<KnowledgeSummary | null>(null);
  readonly kbNotes = signal<KnowledgeNote[]>([]);
  readonly kbTotal = signal(0);
  readonly kbSelectedNote = signal<KnowledgeNoteDetail | null>(null);
  readonly kbIsLoading = signal(false);
  readonly kbSearchQuery = signal('');
  readonly kbFilterType = signal('');
  readonly kbFilterStatus = signal('');

  readonly tabList: { id: Tab; labelKey: string }[] = [
    { id: 'overview', labelKey: 'projectDetail.tabs.overview' },
    { id: 'jobs', labelKey: 'projectDetail.tabs.jobs' },
    { id: 'knowledge', labelKey: 'projectDetail.tabs.knowledge' },
    { id: 'datasources', labelKey: 'projectDetail.tabs.datasources' },
    { id: 'repos', labelKey: 'projectDetail.tabs.repos' },
    { id: 'experts', labelKey: 'projectDetail.tabs.experts' },
    { id: 'members', labelKey: 'projectDetail.tabs.members' },
    { id: 'settings', labelKey: 'projectDetail.tabs.settings' },
  ];

  private projectId = '';
  private refreshInterval: ReturnType<typeof setInterval> | null = null;

  ngOnInit(): void {
    this.route.paramMap.subscribe((params) => {
      this.projectId = params.get('id') ?? '';
      if (this.projectId) this.loadAll();
    });
    this.api.getUsers().subscribe((users) => this.allUsers.set(users));
    // Load framework defaults so toggles reflect the actual base config
    this.api.getExpertDetail('defaults').subscribe((d) => {
      if (d?.config) this.frameworkDefaults.set(d.config);
    });

    this.refreshInterval = setInterval(() => {
      if (this.activeTab() === 'jobs' && this.projectId) {
        this.loadJobs();
      }
    }, 30000);
  }

  ngOnDestroy(): void {
    if (this.refreshInterval) clearInterval(this.refreshInterval);
  }

  goBack(): void {
    this.router.navigate(['/projects']);
  }

  loadAll(): void {
    this.isLoading.set(true);
    this.api.getProject(this.projectId).subscribe((p) => {
      this.project.set(p);
      this.isLoading.set(false);
      // Initialize settings signals from project
      if (p) {
        this.settingsName.set(p.name);
        this.settingsConfigName.set(p.default_config_name ?? '');
        this.settingsCloudReadOnly.set(p.cloud_storage_read_only ?? false);
      }
    });
    this.loadJobs();
    this.loadRepos();
    this.loadMembers();
    this.loadExperts();
    this.loadProjectDatasources();
    this.loadKBSummary();
  }

  loadJobs(): void {
    this.api.getProjectJobs(this.projectId).subscribe((j) => this.jobs.set(j));
  }

  loadRepos(): void {
    this.api.getProjectRepositories(this.projectId).subscribe((r) => this.repos.set(r));
  }

  loadMembers(): void {
    this.api.getProjectMembers(this.projectId).subscribe((m) => this.members.set(m));
  }

  loadExperts(): void {
    this.isLoadingExperts.set(true);
    this.api.getProjectExperts(this.projectId).subscribe({
      next: (e) => {
        this.projectExperts.set(e);
        this.isLoadingExperts.set(false);
      },
      error: () => this.isLoadingExperts.set(false),
    });
  }

  // Datasources
  loadProjectDatasources(): void {
    this.api.getProjectDatasources(this.projectId).subscribe((ds) => this.projectDatasources.set(ds));
    this.api.getDatasources().subscribe((ds) => this.allDatasources.set(ds));
  }

  linkDatasource(): void {
    const dsId = this.dsLinkId();
    if (!dsId) return;
    this.api.linkProjectDatasource(this.projectId, dsId).subscribe((res) => {
      if (res) {
        this.dsLinkId.set('');
        this.loadProjectDatasources();
      }
    });
  }

  unlinkDatasource(datasourceId: string): void {
    this.api.unlinkProjectDatasource(this.projectId, datasourceId).subscribe((res) => {
      if (res) {
        this.loadProjectDatasources();
      }
    });
  }

  updateDatasourceReadOnly(datasourceId: string, value: string): void {
    const readOnly = value === '' ? null : value === 'true';
    this.api.updateProjectDatasource(this.projectId, datasourceId, { read_only: readOnly }).subscribe((res) => {
      if (res) this.loadProjectDatasources();
    });
  }

  updateDatasourceDescription(datasourceId: string, value: string): void {
    const desc = value.trim() || null;
    this.api.updateProjectDatasource(this.projectId, datasourceId, { description: desc }).subscribe((res) => {
      if (res) this.loadProjectDatasources();
    });
  }

  // Overview
  startEditOverview(): void {
    const p = this.project();
    this.editDescription.set(p?.description ?? '');
    this.editGoal.set(p?.goal ?? '');
    this.isEditingOverview.set(true);
  }

  cancelEditOverview(): void {
    this.isEditingOverview.set(false);
  }

  saveOverview(): void {
    this.api.updateProject(this.projectId, {
      description: this.editDescription(),
      goal: this.editGoal(),
    }).subscribe((res) => {
      if (res) {
        this.isEditingOverview.set(false);
        this.api.getProject(this.projectId).subscribe((p) => this.project.set(p));
      }
    });
  }

  // Repos
  addRepo(): void {
    const name = this.repoName().trim();
    if (!name) return;

    this.api.addProjectRepository(this.projectId, {
      name,
      repo_url: this.repoUrl().trim() || undefined,
      role: this.repoRole(),
      read_only: this.repoReadOnly(),
      create_managed: this.repoCreateManaged(),
    }).subscribe((res) => {
      if (res) {
        this.repoName.set('');
        this.repoUrl.set('');
        this.loadRepos();
      }
    });
  }

  removeRepo(repoId: string): void {
    this.api.removeProjectRepository(this.projectId, repoId).subscribe(() => {
      this.loadRepos();
    });
  }

  onRepoRoleChange(value: string | null): void {
    if (value) this.repoRole.set(value as ProjectRepoRole);
  }

  // Members
  addMember(): void {
    const userId = this.memberUserId();
    if (!userId) return;

    this.api.addProjectMember(this.projectId, {
      user_id: userId,
      role: this.memberRole(),
    }).subscribe((res) => {
      if (res) {
        this.memberUserId.set('');
        this.loadMembers();
      }
    });
  }

  onMemberRoleSelectChange(value: string | null): void {
    if (value) this.memberRole.set(value as ProjectMemberRole);
  }

  onRowMemberRoleChange(userId: string, role: string | null): void {
    if (role) this.changeMemberRole(userId, role as ProjectMemberRole);
  }

  changeMemberRole(userId: string, role: ProjectMemberRole): void {
    this.api.updateProjectMember(this.projectId, userId, { role }).subscribe(() => {
      this.loadMembers();
    });
  }

  removeMember(userId: string): void {
    this.api.removeProjectMember(this.projectId, userId).subscribe(() => {
      this.loadMembers();
    });
  }

  // Settings
  saveSettings(): void {
    const update: Record<string, string> = {};
    const p = this.project();
    const name = this.settingsName().trim();
    const config = this.settingsConfigName().trim();
    if (name && name !== p?.name) update['name'] = name;
    if (config !== (p?.default_config_name ?? '')) {
      update['default_config_name'] = config || '';
    }
    if (Object.keys(update).length === 0) return;
    this.isSavingSettings.set(true);
    this.api.updateProject(this.projectId, update).subscribe({
      next: (res) => {
        this.isSavingSettings.set(false);
        if (res) this.api.getProject(this.projectId).subscribe((p) => this.project.set(p));
      },
      error: () => this.isSavingSettings.set(false),
    });
  }

  toggleProjectMemory(checked: boolean): void {
    const p = this.project();
    if (!p) return;
    const existing = (p.default_config_override ?? {}) as Record<string, any>;
    const override = {
      ...existing,
      memory: { ...(existing['memory'] ?? {}), project_scoped: checked },
    };
    this.api.updateProject(this.projectId, { default_config_override: override }).subscribe((res) => {
      if (res) this.api.getProject(this.projectId).subscribe((proj) => this.project.set(proj));
    });
  }

  toggleCloudReadOnly(checked: boolean): void {
    this.settingsCloudReadOnly.set(checked);
    this.api.updateProject(this.projectId, { cloud_storage_read_only: checked }).subscribe((res) => {
      if (res) this.api.getProject(this.projectId).subscribe((proj) => this.project.set(proj));
    });
  }

  archiveProject(): void {
    if (!confirm(this.transloco.translate('projectDetail.settings.archiveConfirm'))) return;
    this.api.updateProject(this.projectId, { status: 'archived' }).subscribe((res) => {
      if (res) this.api.getProject(this.projectId).subscribe((p) => this.project.set(p));
    });
  }

  deleteProject(): void {
    if (!confirm(this.transloco.translate('projectDetail.settings.deleteConfirm'))) return;
    this.api.deleteProject(this.projectId).subscribe((res) => {
      if (res) this.router.navigate(['/projects']);
    });
  }

  createJobInProject(): void {
    this.router.navigate(['/'], { queryParams: { project: this.projectId } });
  }

  // Knowledge
  loadKBSummary(): void {
    this.api.getKnowledgeSummary(this.projectId).subscribe((s) => {
      this.kbSummary.set(s);
      if (s && s.total > 0) this.loadKBNotes();
    });
  }

  loadKBNotes(): void {
    this.kbIsLoading.set(true);
    this.api.getKnowledgeNotes(this.projectId, {
      type: this.kbFilterType() || undefined,
      status: this.kbFilterStatus() || undefined,
      limit: 50,
      offset: 0,
    }).subscribe({
      next: (res) => {
        this.kbNotes.set(res?.notes ?? []);
        this.kbTotal.set(res?.total ?? 0);
        this.kbIsLoading.set(false);
      },
      error: () => this.kbIsLoading.set(false),
    });
  }

  onKbFilterTypeChange(value: string): void {
    this.kbFilterType.set(value);
    this.loadKBNotes();
  }

  onKbFilterStatusChange(value: string): void {
    this.kbFilterStatus.set(value);
    this.loadKBNotes();
  }

  loadMoreKBNotes(): void {
    const current = this.kbNotes();
    this.api.getKnowledgeNotes(this.projectId, {
      type: this.kbFilterType() || undefined,
      status: this.kbFilterStatus() || undefined,
      limit: 50,
      offset: current.length,
    }).subscribe((res) => {
      if (res) this.kbNotes.set([...current, ...res.notes]);
    });
  }

  searchKB(): void {
    const q = this.kbSearchQuery().trim();
    if (!q) { this.loadKBNotes(); return; }
    this.kbIsLoading.set(true);
    this.api.searchKnowledge(this.projectId, q, 20).subscribe({
      next: (res) => {
        this.kbNotes.set(res?.notes ?? []);
        this.kbTotal.set(res?.total ?? 0);
        this.kbIsLoading.set(false);
      },
      error: () => this.kbIsLoading.set(false),
    });
  }

  clearKBFilters(): void {
    this.kbSearchQuery.set('');
    this.kbFilterType.set('');
    this.kbFilterStatus.set('');
    this.loadKBNotes();
  }

  openNote(noteId: string): void {
    this.api.getKnowledgeNote(this.projectId, noteId).subscribe((note) => {
      if (note) this.kbSelectedNote.set(note);
    });
  }

  updateNoteStatus(noteId: string, status: string): void {
    this.api.updateKnowledgeNote(this.projectId, noteId, { status }).subscribe((res) => {
      if (res) {
        this.openNote(noteId);
        this.loadKBSummary();
      }
    });
  }

  deleteNote(noteId: string): void {
    if (!confirm(this.transloco.translate('projectDetail.knowledge.deleteConfirm', {noteId}))) return;
    this.api.deleteKnowledgeNote(this.projectId, noteId).subscribe((res) => {
      if (res) {
        this.kbSelectedNote.set(null);
        this.loadKBNotes();
        this.loadKBSummary();
      }
    });
  }

  exportKB(): void {
    this.api.exportKnowledge(this.projectId).subscribe((res) => {
      if (res) alert(this.transloco.translate('projectDetail.knowledge.exportedAlert', {count: res.note_count, path: res.path}));
    });
  }

  kbTypeEntries(summary: KnowledgeSummary): [string, number][] {
    return Object.entries(summary.by_type).slice(0, 6);
  }

  // Helpers
  boolToText(v: boolean | null | undefined): string {
    if (v === null || v === undefined) return '';
    return v ? 'true' : 'false';
  }

  statusTone(status: string): 'success' | 'neutral' | 'danger' | 'warning' | 'accent' {
    switch (status) {
      case 'active': return 'success';
      case 'archived': return 'neutral';
      case 'deleted': return 'danger';
      default: return 'neutral';
    }
  }

  getInitials(name: string): string {
    return name.split(' ').map((w) => w[0]).join('').toUpperCase().slice(0, 2);
  }

  truncate(text: string | undefined, max: number): string {
    if (!text) return '';
    return text.length <= max ? text : text.slice(0, max) + '...';
  }

  formatDate(dateString: string): string {
    const date = new Date(dateString);
    return date.toLocaleDateString(this.transloco.getActiveLang(), {
      day: '2-digit',
      month: 'short',
      year: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    });
  }
}
