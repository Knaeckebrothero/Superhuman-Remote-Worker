import {Component, computed, inject, OnDestroy, OnInit, signal} from '@angular/core';
import {ActivatedRoute, Router} from '@angular/router';
import {ApiService} from '../../core/services/api.service';
import {UserService} from '../../core/services/user.service';
import {SidebarToggleComponent} from '../../simple/layout/sidebar-toggle/sidebar-toggle.component';
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
  imports: [SidebarToggleComponent],
  template: `
    <div class="page-container">
      @if (isLoading() && !project()) {
        <div class="loading-state">
          <div class="spinner"></div>
          <span>Loading project...</span>
        </div>
      }

      @if (project(); as proj) {
        <!-- Header -->
        <div class="page-header">
          <app-sidebar-toggle />
          <button class="btn btn-ghost btn-back" (click)="goBack()">
            <span class="material-symbols-outlined">arrow_back</span>
          </button>
          <div class="header-info">
            <h1 class="page-title">{{ proj.name }}</h1>
            <div class="header-badges">
              @if (proj.is_default) {
                <span class="badge badge-personal">Personal</span>
              }
              <span class="badge" [class]="'badge-' + proj.status">{{ proj.status }}</span>
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
              {{ t.label }}
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
                  <label>Description</label>
                  @if (isEditingOverview()) {
                    <textarea
                      class="form-input"
                      rows="3"
                      [value]="editDescription()"
                      (input)="editDescription.set(asTextareaValue($event))"
                    ></textarea>
                  } @else {
                    <p class="detail-value">{{ proj.description || 'No description' }}</p>
                  }
                </div>
                <div class="detail-card">
                  <label>Goal</label>
                  @if (isEditingOverview()) {
                    <textarea
                      class="form-input"
                      rows="3"
                      [value]="editGoal()"
                      (input)="editGoal.set(asTextareaValue($event))"
                    ></textarea>
                  } @else {
                    <p class="detail-value">{{ proj.goal || 'No goal set' }}</p>
                  }
                </div>
              </div>
              <div class="stats-row">
                <div class="stat-card">
                  <span class="stat-value">{{ jobs().length }}</span>
                  <span class="stat-label">Jobs</span>
                </div>
                <div class="stat-card">
                  <span class="stat-value">{{ projectDatasources().length }}</span>
                  <span class="stat-label">Data Sources</span>
                </div>
                <div class="stat-card">
                  <span class="stat-value">{{ repos().length }}</span>
                  <span class="stat-label">Repos</span>
                </div>
                <div class="stat-card">
                  <span class="stat-value">{{ members().length }}</span>
                  <span class="stat-label">Members</span>
                </div>
              </div>
              @if (proj.default_config_name) {
                <div class="detail-card">
                  <label>Default Config</label>
                  <p class="detail-value mono">{{ proj.default_config_name }}</p>
                </div>
              }
              <div class="detail-card">
                <label>Created</label>
                <p class="detail-value mono">{{ formatDate(proj.created_at) }}</p>
              </div>
              <div class="overview-actions">
                @if (isEditingOverview()) {
                  <button class="btn btn-primary" (click)="saveOverview()">Save</button>
                  <button class="btn btn-ghost" (click)="cancelEditOverview()">Cancel</button>
                } @else {
                  <button class="btn btn-ghost" (click)="startEditOverview()">Edit</button>
                }
              </div>
            </div>
          }

          <!-- JOBS TAB -->
          @if (activeTab() === 'jobs') {
            <div class="table-section">
              <div class="tab-toolbar">
                <button class="btn btn-primary btn-sm" (click)="createJobInProject()">
                  + New Job
                </button>
              </div>
              @if (jobs().length === 0) {
                <div class="empty-inline">No jobs in this project</div>
              } @else {
                <table class="data-table">
                  <thead>
                    <tr>
                      <th>Status</th>
                      <th>Description</th>
                      <th>Config</th>
                      <th>Branch</th>
                      <th>Merge</th>
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
                    <span class="kb-stat-label">Total</span>
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
                <input
                  class="form-input-sm kb-search"
                  placeholder="Search knowledge..."
                  [value]="kbSearchQuery()"
                  (input)="kbSearchQuery.set(asInputValue($event))"
                  (keydown.enter)="searchKB()"
                />
                <button class="btn btn-primary btn-sm" (click)="searchKB()" [disabled]="!kbSearchQuery()">
                  Search
                </button>
                <select class="inline-select" [value]="kbFilterType()" (change)="kbFilterType.set(asSelectValue($event)); loadKBNotes()">
                  <option value="">All types</option>
                  <option value="decision">Decision</option>
                  <option value="learning">Learning</option>
                  <option value="goal">Goal</option>
                  <option value="plan">Plan</option>
                  <option value="code">Code</option>
                  <option value="question">Question</option>
                  <option value="state">State</option>
                  <option value="source">Source</option>
                  <option value="retrospective">Retrospective</option>
                </select>
                <select class="inline-select" [value]="kbFilterStatus()" (change)="kbFilterStatus.set(asSelectValue($event)); loadKBNotes()">
                  <option value="">All statuses</option>
                  <option value="active">Active</option>
                  <option value="resolved">Resolved</option>
                  <option value="superseded">Superseded</option>
                  <option value="archived">Archived</option>
                </select>
                <button class="btn btn-ghost btn-sm" (click)="clearKBFilters()">Clear</button>
                <div style="flex:1"></div>
                <button class="btn btn-ghost btn-sm" (click)="exportKB()">Export</button>
              </div>

              <!-- Note Detail View -->
              @if (kbSelectedNote(); as note) {
                <div class="kb-detail">
                  <div class="kb-detail-header">
                    <button class="btn btn-ghost btn-sm" (click)="kbSelectedNote.set(null)">
                      <span class="material-symbols-outlined" style="font-size:16px">arrow_back</span> Back
                    </button>
                    <div style="flex:1"></div>
                    <select
                      class="inline-select"
                      [value]="note.status"
                      (change)="updateNoteStatus(note.note_id, asSelectValue($event))"
                    >
                      <option value="active">Active</option>
                      <option value="resolved">Resolved</option>
                      <option value="superseded">Superseded</option>
                      <option value="archived">Archived</option>
                    </select>
                    <button class="action-btn delete" (click)="deleteNote(note.note_id)">Delete</button>
                  </div>
                  <div class="kb-detail-meta">
                    <span class="kb-type-badge" [attr.data-type]="note.note_type">{{ note.note_type }}</span>
                    @if (note.confidence) {
                      <span class="kb-confidence">{{ note.confidence }}</span>
                    }
                    @if (note.phase) {
                      <span class="text-muted">Phase {{ note.phase }}</span>
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
                      <h4>Relationships</h4>
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
                  <div class="loading-state"><div class="spinner"></div><span>Loading notes...</span></div>
                } @else if (kbNotes().length === 0) {
                  <div class="empty-inline">No knowledge notes found</div>
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
                          <span class="text-muted" style="margin-left:auto">{{ formatDate(note.modified_at) }}</span>
                        </div>
                      </div>
                    }
                  </div>
                  @if (kbTotal() > kbNotes().length) {
                    <div class="kb-pagination">
                      <span class="text-muted">{{ kbNotes().length }} of {{ kbTotal() }} notes</span>
                      <button class="btn btn-ghost btn-sm" (click)="loadMoreKBNotes()">Load more</button>
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
                <select
                  class="form-input-sm"
                  [value]="dsLinkId()"
                  (change)="dsLinkId.set(asInputValue($event))"
                >
                  <option value="">Select a datasource...</option>
                  @for (ds of availableDatasources(); track ds.id) {
                    <option [value]="ds.id">{{ ds.name }} ({{ ds.type }})</option>
                  }
                </select>
                <button
                  class="btn btn-primary btn-sm"
                  [disabled]="!dsLinkId()"
                  (click)="linkDatasource()"
                >
                  Link
                </button>
              </div>

              @if (projectDatasources().length === 0) {
                <div class="empty-inline">No datasources linked to this project</div>
              } @else {
                <table class="data-table">
                  <thead>
                    <tr>
                      <th>Name</th>
                      <th>Type</th>
                      <th>Project Description</th>
                      <th>Access</th>
                      <th>Actions</th>
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
                          <input
                            class="form-input-sm"
                            [value]="ds.project_description || ds.description || ''"
                            placeholder="Usage context for this project..."
                            (change)="updateDatasourceDescription(ds.id, asInputValue($event))"
                          />
                        </td>
                        <td>
                          <select
                            class="form-input-sm"
                            [value]="ds.project_read_only != null ? ds.project_read_only : ''"
                            (change)="updateDatasourceReadOnly(ds.id, asInputValue($event))"
                          >
                            <option value="">Default (Read-write)</option>
                            <option value="true">Read-only</option>
                            <option value="false">Read-write</option>
                          </select>
                        </td>
                        <td>
                          <button class="action-btn delete" (click)="unlinkDatasource(ds.id)">
                            Unlink
                          </button>
                        </td>
                      </tr>
                    }
                  </tbody>
                </table>
              }
            </div>
          }

          <!-- REPOS TAB -->
          @if (activeTab() === 'repos') {
            <div class="table-section">
              <!-- Add Repo Form -->
              <div class="inline-form">
                <input
                  class="form-input-sm"
                  placeholder="Repo name"
                  [value]="repoName()"
                  (input)="repoName.set(asInputValue($event))"
                />
                <input
                  class="form-input-sm"
                  placeholder="Repo URL"
                  [value]="repoUrl()"
                  (input)="repoUrl.set(asInputValue($event))"
                />
                <select
                  class="form-input-sm"
                  [value]="repoRole()"
                  (change)="setRepoRole($event)"
                >
                  <option value="jobs">jobs</option>
                  <option value="source">source</option>
                  <option value="reference">reference</option>
                </select>
                <label class="inline-checkbox">
                  <input type="checkbox" [checked]="repoReadOnly()" (change)="repoReadOnly.set(asChecked($event))" />
                  Read-only
                </label>
                <label class="inline-checkbox">
                  <input type="checkbox" [checked]="repoCreateManaged()" (change)="repoCreateManaged.set(asChecked($event))" />
                  Managed
                </label>
                <button
                  class="btn btn-primary btn-sm"
                  [disabled]="!repoName().trim()"
                  (click)="addRepo()"
                >
                  Add
                </button>
              </div>

              @if (repos().length === 0) {
                <div class="empty-inline">No repositories attached</div>
              } @else {
                <table class="data-table">
                  <thead>
                    <tr>
                      <th>Role</th>
                      <th>Name</th>
                      <th>URL</th>
                      <th>Read Only</th>
                      <th>Managed</th>
                      <th>Actions</th>
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
                        <td>{{ repo.read_only ? 'Yes' : 'No' }}</td>
                        <td>{{ repo.is_managed ? 'Yes' : 'No' }}</td>
                        <td>
                          <button class="action-btn delete" (click)="removeRepo(repo.id)">
                            Remove
                          </button>
                        </td>
                      </tr>
                    }
                  </tbody>
                </table>
              }
            </div>
          }

          <!-- EXPERTS TAB -->
          @if (activeTab() === 'experts') {
            <div class="table-section">
              @if (isLoadingExperts()) {
                <div class="empty-inline">
                  <div class="spinner-sm"></div>
                  Loading experts...
                </div>
              } @else if (projectExperts().length === 0) {
                <div class="empty-inline">
                  No project experts yet. Add expert configs to the
                  <code>experts/</code> directory in the jobs repo.
                </div>
              } @else {
                <div class="expert-grid">
                  @for (expert of projectExperts(); track expert.id) {
                    <div
                      class="expert-card"
                      [style.--expert-color]="expert.color"
                    >
                      <span class="expert-icon" [style.color]="expert.color">{{ expert.icon }}</span>
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
                <select
                  class="form-input-sm"
                  [value]="memberUserId()"
                  (change)="memberUserId.set(asSelectValue($event))"
                >
                  <option value="">Select user...</option>
                  @for (user of availableUsers(); track user.id) {
                    <option [value]="user.id">{{ user.display_name }}</option>
                  }
                </select>
                <select
                  class="form-input-sm"
                  [value]="memberRole()"
                  (change)="setMemberRole($event)"
                >
                  <option value="editor">Editor</option>
                  <option value="viewer">Viewer</option>
                  <option value="owner">Owner</option>
                </select>
                <button
                  class="btn btn-primary btn-sm"
                  [disabled]="!memberUserId()"
                  (click)="addMember()"
                >
                  Add
                </button>
              </div>

              @if (members().length === 0) {
                <div class="empty-inline">No members</div>
              } @else {
                <table class="data-table">
                  <thead>
                    <tr>
                      <th>User</th>
                      <th>Role</th>
                      <th>Joined</th>
                      <th>Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    @for (member of members(); track member.user_id) {
                      <tr>
                        <td>
                          <div class="user-cell">
                            <span
                              class="user-avatar-sm"
                              [style.background]="member.avatar_color || '#89b4fa'"
                            >{{ getInitials(member.display_name || '?') }}</span>
                            <span>{{ member.display_name || member.user_id.slice(0, 8) }}</span>
                          </div>
                        </td>
                        <td>
                          <select
                            class="inline-select"
                            [value]="member.role"
                            (change)="onMemberRoleChange(member.user_id, $event)"
                          >
                            <option value="owner">owner</option>
                            <option value="editor">editor</option>
                            <option value="viewer">viewer</option>
                          </select>
                        </td>
                        <td class="mono">{{ formatDate(member.joined_at) }}</td>
                        <td>
                          <button
                            class="action-btn delete"
                            [disabled]="member.role === 'owner' && ownerCount() <= 1"
                            (click)="removeMember(member.user_id)"
                          >
                            Remove
                          </button>
                        </td>
                      </tr>
                    }
                  </tbody>
                </table>
              }
            </div>
          }

          <!-- SETTINGS TAB -->
          @if (activeTab() === 'settings') {
            <div class="settings-section">
              <!-- General -->
              <div class="settings-group">
                <h3 class="settings-heading">General</h3>
                <div class="form-row">
                  <label class="form-label-sm">Project Name</label>
                  <input
                    class="form-input"
                    [value]="settingsName()"
                    (input)="settingsName.set(asInputValue($event))"
                    [disabled]="proj.is_default"
                  />
                </div>
                <div class="form-row">
                  <label class="form-label-sm">Default Config</label>
                  <input
                    class="form-input"
                    placeholder="e.g. developer, scholar"
                    [value]="settingsConfigName()"
                    (input)="settingsConfigName.set(asInputValue($event))"
                  />
                </div>
                <div class="settings-actions">
                  <button
                    class="btn btn-primary btn-sm"
                    [disabled]="isSavingSettings()"
                    (click)="saveSettings()"
                  >
                    Save Changes
                  </button>
                </div>
              </div>

              <!-- Memory -->
              <div class="settings-group">
                <h3 class="settings-heading">Memory</h3>
                <label class="toggle-row">
                  <input
                    type="checkbox"
                    [checked]="projectMemoryShared()"
                    (change)="toggleProjectMemory($event)"
                  />
                  <span class="toggle-label">Share memories across jobs</span>
                </label>
                <p class="text-muted" style="font-size: 12px; margin-top: 4px;">
                  When enabled, automatic memories from any job in this project are visible to all other jobs.
                </p>
              </div>

              <!-- Danger Zone -->
              <div class="settings-group danger-zone">
                <h3 class="settings-heading danger-heading">Danger Zone</h3>
                @if (proj.is_default) {
                  <p class="text-muted" style="font-size: 12px;">
                    This is your default personal project and cannot be archived or deleted.
                  </p>
                } @else {
                  <div class="danger-actions">
                    @if (proj.status === 'active') {
                      <div class="danger-row">
                        <div class="danger-info">
                          <span class="danger-title">Archive Project</span>
                          <span class="danger-desc">
                            Mark this project as archived. Jobs will no longer be created.
                          </span>
                        </div>
                        <button class="btn btn-danger-outline" (click)="archiveProject()">
                          Archive
                        </button>
                      </div>
                    }
                    <div class="danger-row">
                      <div class="danger-info">
                        <span class="danger-title">Delete Project</span>
                        <span class="danger-desc">
                          Permanently delete this project and its managed repositories.
                        </span>
                      </div>
                      <button class="btn btn-danger" (click)="deleteProject()">
                        Delete
                      </button>
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
    :host { display: block; height: 100%; overflow: auto; }

    .page-container {
      padding: 24px;
      max-width: 1200px;
      margin: 0 auto;
    }

    /* Header */
    .page-header {
      display: flex;
      align-items: center;
      gap: 12px;
      margin-bottom: 20px;
    }

    .btn-back {
      padding: 6px;
      border-radius: 6px;
      display: flex;
      align-items: center;
    }

    .header-info { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }

    .page-title {
      font-size: 22px;
      font-weight: 700;
      color: var(--text-primary, #cdd6f4);
      margin: 0;
    }

    .header-badges { display: flex; gap: 6px; }

    /* Tabs */
    .tab-bar {
      display: flex;
      gap: 2px;
      border-bottom: 1px solid var(--border-color, #313244);
      margin-bottom: 20px;
    }

    .tab-btn {
      padding: 10px 20px;
      background: none;
      border: none;
      border-bottom: 2px solid transparent;
      color: var(--text-secondary, #a6adc8);
      font-size: 13px;
      font-family: inherit;
      cursor: pointer;
      transition: all 0.15s ease;
    }

    .tab-btn:hover { color: var(--text-primary, #cdd6f4); }

    .tab-btn.active {
      color: var(--accent-color, #cba6f7);
      border-bottom-color: var(--accent-color, #cba6f7);
    }

    /* Shared */
    .btn {
      padding: 8px 16px;
      border-radius: 6px;
      font-size: 13px;
      font-family: inherit;
      cursor: pointer;
      border: 1px solid var(--border-color, #45475a);
      transition: all 0.15s ease;
    }

    .btn-sm { padding: 6px 12px; font-size: 12px; }

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

    .btn-ghost:hover { background: var(--surface-0, #313244); }

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

    .loading-state, .empty-state {
      display: flex;
      flex-direction: column;
      align-items: center;
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

    /* Overview */
    .overview-section { display: flex; flex-direction: column; gap: 16px; }

    .detail-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 16px;
    }

    .detail-card {
      background: var(--panel-bg, #181825);
      border: 1px solid var(--border-color, #313244);
      border-radius: 8px;
      padding: 14px;
    }

    .detail-card label {
      display: block;
      font-size: 11px;
      color: var(--text-muted, #6c7086);
      text-transform: uppercase;
      letter-spacing: 0.5px;
      margin-bottom: 6px;
    }

    .detail-value {
      margin: 0;
      font-size: 13px;
      color: var(--text-primary, #cdd6f4);
      line-height: 1.4;
    }

    .form-input {
      width: 100%;
      padding: 8px 10px;
      background: var(--surface-0, #313244);
      border: 1px solid var(--border-color, #45475a);
      border-radius: 6px;
      color: var(--text-primary, #cdd6f4);
      font-size: 13px;
      font-family: inherit;
      outline: none;
      resize: vertical;
      box-sizing: border-box;
    }

    .form-input:focus { border-color: var(--accent-color, #cba6f7); }

    .stats-row { display: flex; gap: 16px; }

    .stat-card {
      flex: 1;
      background: var(--panel-bg, #181825);
      border: 1px solid var(--border-color, #313244);
      border-radius: 8px;
      padding: 16px;
      text-align: center;
    }

    .stat-value {
      display: block;
      font-size: 28px;
      font-weight: 700;
      color: var(--accent-color, #cba6f7);
    }

    .stat-label {
      display: block;
      font-size: 11px;
      color: var(--text-muted, #6c7086);
      margin-top: 4px;
    }

    .overview-actions { display: flex; gap: 8px; }

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
      background: var(--surface-0, #313244);
      color: var(--text-muted, #6c7086);
      font-weight: 500;
      text-transform: uppercase;
      font-size: 10px;
      letter-spacing: 0.5px;
      border-bottom: 1px solid var(--border-color, #45475a);
    }

    .data-table td {
      padding: 10px 12px;
      border-bottom: 1px solid var(--border-color, #313244);
      color: var(--text-primary, #cdd6f4);
      vertical-align: middle;
    }

    .desc-cell { max-width: 250px; }
    .url-cell { max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

    .empty-inline {
      padding: 20px;
      text-align: center;
      color: var(--text-muted, #6c7086);
      font-size: 13px;
    }

    /* Status badges */
    .status-badge {
      display: inline-block;
      padding: 3px 8px;
      border-radius: 4px;
      font-size: 11px;
      font-weight: 500;
      text-transform: capitalize;
    }

    .status-created { background: rgba(137, 180, 250, 0.2); color: #89b4fa; }
    .status-pending { background: rgba(137, 180, 250, 0.2); color: #89b4fa; }
    .status-processing { background: rgba(249, 226, 175, 0.2); color: #f9e2af; }
    .status-completed { background: rgba(166, 227, 161, 0.2); color: #a6e3a1; }
    .status-failed { background: rgba(243, 139, 168, 0.2); color: #f38ba8; }
    .status-cancelled { background: rgba(108, 112, 134, 0.2); color: #6c7086; }
    .status-pending_review { background: rgba(250, 179, 135, 0.2); color: #fab387; }

    /* Merge badges */
    .merge-badge {
      display: inline-block;
      padding: 3px 8px;
      border-radius: 4px;
      font-size: 11px;
      font-weight: 500;
    }

    .merge-merged { background: rgba(166, 227, 161, 0.2); color: #a6e3a1; }
    .merge-conflict { background: rgba(243, 139, 168, 0.2); color: #f38ba8; }
    .merge-skipped { background: rgba(108, 112, 134, 0.2); color: #6c7086; }
    .merge-pending { background: rgba(249, 226, 175, 0.15); color: #f9e2af; }

    /* Role badges */
    .role-badge {
      display: inline-block;
      padding: 3px 8px;
      border-radius: 4px;
      font-size: 11px;
      font-weight: 500;
    }

    .role-jobs { background: rgba(137, 180, 250, 0.2); color: #89b4fa; }
    .role-source { background: rgba(166, 227, 161, 0.2); color: #a6e3a1; }
    .role-reference { background: rgba(249, 226, 175, 0.2); color: #f9e2af; }

    /* Member role badges */
    .member-role-badge {
      display: inline-block;
      padding: 3px 8px;
      border-radius: 4px;
      font-size: 11px;
    }

    /* Merge actions */
    .merge-actions {
      display: flex;
      align-items: center;
      gap: 6px;
      flex-wrap: wrap;
    }

    /* Action buttons */
    .action-btn {
      padding: 4px 8px;
      border: 1px solid var(--border-color, #45475a);
      border-radius: 4px;
      background: transparent;
      font-size: 10px;
      cursor: pointer;
      transition: all 0.15s ease;
    }

    .action-btn:disabled { opacity: 0.4; cursor: not-allowed; }
    .action-btn:hover:not(:disabled) { background: rgba(255, 255, 255, 0.1); }

    .action-btn.merge { color: #a6e3a1; border-color: #a6e3a1; }
    .action-btn.skip { color: #6c7086; border-color: #6c7086; }
    .action-btn.delete { color: #f38ba8; border-color: #f38ba8; }

    /* Inline form */
    .inline-form {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 12px;
      background: var(--panel-bg, #181825);
      border: 1px solid var(--border-color, #313244);
      border-radius: 8px;
      flex-wrap: wrap;
    }

    .form-input-sm {
      padding: 6px 10px;
      background: var(--surface-0, #313244);
      border: 1px solid var(--border-color, #45475a);
      border-radius: 4px;
      color: var(--text-primary, #cdd6f4);
      font-size: 12px;
      font-family: inherit;
      outline: none;
    }

    .form-input-sm:focus { border-color: var(--accent-color, #cba6f7); }

    .inline-select {
      padding: 4px 6px;
      background: var(--surface-0, #313244);
      border: 1px solid var(--border-color, #45475a);
      border-radius: 4px;
      color: var(--text-primary, #cdd6f4);
      font-size: 11px;
      outline: none;
    }

    .inline-checkbox {
      display: flex;
      align-items: center;
      gap: 4px;
      font-size: 11px;
      color: var(--text-secondary, #a6adc8);
      cursor: pointer;
    }

    .inline-checkbox input { cursor: pointer; }

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
      color: var(--timeline-bg, #11111b);
      flex-shrink: 0;
    }

    .text-muted { color: var(--text-muted, #6c7086); }

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
      border: 1px solid var(--border-color, #45475a);
      border-radius: 8px;
      background: var(--panel-bg, #181825);
    }

    .expert-icon {
      font-family: 'Material Symbols Outlined';
      font-size: 28px;
    }

    .expert-name {
      font-size: 13px;
      font-weight: 600;
      color: var(--text-primary, #cdd6f4);
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

    .expert-tags { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 2px; }

    .expert-tag {
      font-size: 10px;
      padding: 1px 6px;
      border-radius: 3px;
      background: rgba(205, 214, 244, 0.08);
      color: var(--text-muted, #6c7086);
    }

    .spinner-sm {
      display: inline-block;
      width: 14px; height: 14px;
      border: 2px solid var(--surface-0, #313244);
      border-top-color: var(--accent-color, #cba6f7);
      border-radius: 50%;
      animation: spin 0.8s linear infinite;
      vertical-align: middle;
      margin-right: 6px;
    }

    /* Settings */
    .settings-section { display: flex; flex-direction: column; gap: 20px; }

    .settings-group {
      background: var(--panel-bg, #181825);
      border: 1px solid var(--border-color, #313244);
      border-radius: 8px;
      padding: 16px;
    }

    .settings-heading {
      font-size: 14px;
      font-weight: 600;
      color: var(--text-primary, #cdd6f4);
      margin: 0 0 14px 0;
    }

    .form-row { margin-bottom: 12px; }

    .form-label-sm {
      display: block;
      font-size: 11px;
      color: var(--text-muted, #6c7086);
      margin-bottom: 4px;
    }

    .settings-actions { display: flex; gap: 8px; margin-top: 8px; }
    .toggle-row { display: flex; align-items: center; gap: 8px; cursor: pointer; }
    .toggle-label { font-size: 13px; color: var(--text); }

    /* Danger Zone */
    .danger-zone { border-color: rgba(243, 139, 168, 0.3); }
    .danger-heading { color: #f38ba8; }

    .danger-actions { display: flex; flex-direction: column; gap: 12px; }

    .danger-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 12px;
      border: 1px solid var(--border-color, #313244);
      border-radius: 6px;
    }

    .danger-info { flex: 1; min-width: 0; }

    .danger-title {
      display: block;
      font-size: 13px;
      font-weight: 500;
      color: var(--text-primary, #cdd6f4);
    }

    .danger-desc {
      display: block;
      font-size: 11px;
      color: var(--text-muted, #6c7086);
      margin-top: 2px;
    }

    .btn-danger-outline {
      padding: 6px 14px;
      border: 1px solid #f38ba8;
      border-radius: 6px;
      background: transparent;
      color: #f38ba8;
      font-size: 12px;
      font-family: inherit;
      cursor: pointer;
      transition: all 0.15s ease;
      white-space: nowrap;
    }

    .btn-danger-outline:hover { background: rgba(243, 139, 168, 0.1); }

    .btn-danger {
      padding: 6px 14px;
      border: 1px solid #f38ba8;
      border-radius: 6px;
      background: #f38ba8;
      color: var(--timeline-bg, #11111b);
      font-size: 12px;
      font-weight: 600;
      font-family: inherit;
      cursor: pointer;
      transition: all 0.15s ease;
      white-space: nowrap;
    }

    .btn-danger:hover { opacity: 0.9; }

    /* Knowledge Base */
    .kb-section { display: flex; flex-direction: column; gap: 16px; }

    .kb-stats-row {
      display: flex; gap: 12px; flex-wrap: wrap;
    }

    .kb-stat {
      flex: 1; min-width: 70px;
      background: var(--panel-bg, #181825);
      border: 1px solid var(--border-color, #313244);
      border-radius: 8px;
      padding: 12px;
      text-align: center;
    }

    .kb-stat-value {
      display: block; font-size: 22px; font-weight: 700;
      color: var(--accent-color, #cba6f7);
    }

    .kb-stat-label {
      display: block; font-size: 10px;
      color: var(--text-muted, #6c7086);
      text-transform: capitalize; margin-top: 2px;
    }

    .kb-toolbar {
      display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
    }

    .kb-search { flex: 1; min-width: 180px; }

    .kb-note-list { display: flex; flex-direction: column; gap: 8px; }

    .kb-note-card {
      background: var(--panel-bg, #181825);
      border: 1px solid var(--border-color, #313244);
      border-radius: 8px;
      padding: 14px;
      cursor: pointer;
      transition: border-color 0.15s ease;
    }

    .kb-note-card:hover { border-color: var(--accent-color, #cba6f7); }

    .kb-note-header {
      display: flex; align-items: center; gap: 8px; margin-bottom: 6px;
    }

    .kb-note-title {
      font-size: 14px; font-weight: 600;
      color: var(--text-primary, #cdd6f4);
      flex: 1; min-width: 0;
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }

    .kb-note-status {
      font-size: 10px; padding: 2px 6px; border-radius: 3px;
      font-weight: 500; text-transform: capitalize;
    }

    .kb-note-status[data-status="active"] { background: rgba(166, 227, 161, 0.15); color: #a6e3a1; }
    .kb-note-status[data-status="resolved"] { background: rgba(137, 180, 250, 0.15); color: #89b4fa; }
    .kb-note-status[data-status="superseded"] { background: rgba(249, 226, 175, 0.15); color: #f9e2af; }
    .kb-note-status[data-status="archived"] { background: rgba(108, 112, 134, 0.15); color: #6c7086; }

    .kb-type-badge {
      font-size: 10px; padding: 2px 8px; border-radius: 4px;
      font-weight: 600; text-transform: capitalize; white-space: nowrap;
    }

    .kb-type-badge[data-type="decision"] { background: rgba(203, 166, 247, 0.2); color: #cba6f7; }
    .kb-type-badge[data-type="learning"] { background: rgba(166, 227, 161, 0.2); color: #a6e3a1; }
    .kb-type-badge[data-type="goal"] { background: rgba(249, 226, 175, 0.2); color: #f9e2af; }
    .kb-type-badge[data-type="plan"] { background: rgba(137, 180, 250, 0.2); color: #89b4fa; }
    .kb-type-badge[data-type="code"] { background: rgba(148, 226, 213, 0.2); color: #94e2d5; }
    .kb-type-badge[data-type="question"] { background: rgba(250, 179, 135, 0.2); color: #fab387; }
    .kb-type-badge[data-type="state"] { background: rgba(180, 190, 254, 0.2); color: #b4befe; }
    .kb-type-badge[data-type="source"] { background: rgba(245, 194, 231, 0.2); color: #f5c2e7; }
    .kb-type-badge[data-type="retrospective"] { background: rgba(116, 199, 236, 0.2); color: #74c7ec; }

    .kb-note-preview {
      font-size: 12px; color: var(--text-secondary, #a6adc8);
      line-height: 1.5; margin-bottom: 8px;
      display: -webkit-box; -webkit-line-clamp: 2;
      -webkit-box-orient: vertical; overflow: hidden;
    }

    .kb-note-footer {
      display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
    }

    .kb-tag-sm {
      font-size: 10px; padding: 1px 5px; border-radius: 3px;
      background: rgba(205, 214, 244, 0.08); color: var(--text-muted, #6c7086);
    }

    .kb-confidence-sm {
      font-size: 10px; padding: 1px 5px; border-radius: 3px;
      background: rgba(203, 166, 247, 0.1); color: var(--accent-color, #cba6f7);
      text-transform: capitalize;
    }

    .kb-pagination {
      display: flex; align-items: center; justify-content: center; gap: 12px;
      padding: 12px;
    }

    /* Detail view */
    .kb-detail {
      background: var(--panel-bg, #181825);
      border: 1px solid var(--border-color, #313244);
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
      color: var(--text-primary, #cdd6f4);
    }

    .kb-tags { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 14px; }

    .kb-tag {
      font-size: 11px; padding: 3px 8px; border-radius: 4px;
      background: rgba(203, 166, 247, 0.1); color: var(--accent-color, #cba6f7);
    }

    .kb-confidence {
      font-size: 12px; padding: 2px 6px; border-radius: 3px;
      background: rgba(203, 166, 247, 0.1); color: var(--accent-color, #cba6f7);
      text-transform: capitalize;
    }

    .kb-detail-content {
      font-size: 13px; line-height: 1.7;
      color: var(--text-primary, #cdd6f4);
      white-space: pre-wrap; word-wrap: break-word;
    }

    .kb-relationships {
      margin-top: 16px; padding-top: 16px;
      border-top: 1px solid var(--border-color, #313244);
    }

    .kb-relationships h4 {
      font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px;
      color: var(--text-muted, #6c7086); margin: 0 0 8px 0;
    }

    .kb-rel-item {
      display: flex; align-items: center; gap: 8px;
      font-size: 12px; margin-bottom: 4px;
    }

    .kb-rel-type {
      font-size: 10px; padding: 2px 6px; border-radius: 3px;
      background: var(--surface-0, #313244); color: var(--text-secondary, #a6adc8);
      font-weight: 500;
    }

    .kb-rel-link {
      background: none; border: none; padding: 0;
      color: var(--accent-color, #cba6f7);
      cursor: pointer; font-size: 12px; font-family: inherit;
      text-decoration: underline;
    }

    .kb-rel-link:hover { opacity: 0.8; }

    @media (max-width: 768px) {
      .page-container { padding: 12px; }
      .detail-grid { grid-template-columns: 1fr; }
      .stats-row { flex-direction: column; }
      .inline-form { flex-direction: column; align-items: stretch; }
      .kb-stats-row { flex-direction: column; }
      .kb-toolbar { flex-direction: column; }
    }
  `],
})
export class ProjectDetailPageComponent implements OnInit, OnDestroy {
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly api = inject(ApiService);
  private readonly userService = inject(UserService);

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

  readonly tabList: { id: Tab; label: string }[] = [
    { id: 'overview', label: 'Overview' },
    { id: 'jobs', label: 'Jobs' },
    { id: 'knowledge', label: 'Knowledge' },
    { id: 'datasources', label: 'Data Sources' },
    { id: 'repos', label: 'Repos' },
    { id: 'experts', label: 'Experts' },
    { id: 'members', label: 'Members' },
    { id: 'settings', label: 'Settings' },
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

  setRepoRole(event: Event): void {
    this.repoRole.set((event.target as HTMLSelectElement).value as ProjectRepoRole);
  }

  setMemberRole(event: Event): void {
    this.memberRole.set((event.target as HTMLSelectElement).value as ProjectMemberRole);
  }

  onMemberRoleChange(userId: string, event: Event): void {
    const role = (event.target as HTMLSelectElement).value as ProjectMemberRole;
    this.changeMemberRole(userId, role);
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

  toggleProjectMemory(event: Event): void {
    const checked = (event.target as HTMLInputElement).checked;
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

  archiveProject(): void {
    if (!confirm('Are you sure you want to archive this project?')) return;
    this.api.updateProject(this.projectId, { status: 'archived' }).subscribe((res) => {
      if (res) this.api.getProject(this.projectId).subscribe((p) => this.project.set(p));
    });
  }

  deleteProject(): void {
    if (!confirm('Are you sure you want to permanently delete this project? This cannot be undone.')) return;
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
    if (!confirm(`Delete note "${noteId}" permanently? This cannot be undone.`)) return;
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
      if (res) alert(`Exported ${res.note_count} notes to:\n${res.path}`);
    });
  }

  kbTypeEntries(summary: KnowledgeSummary): [string, number][] {
    return Object.entries(summary.by_type).slice(0, 6);
  }

  // Helpers
  getInitials(name: string): string {
    return name.split(' ').map((w) => w[0]).join('').toUpperCase().slice(0, 2);
  }

  truncate(text: string | undefined, max: number): string {
    if (!text) return '';
    return text.length <= max ? text : text.slice(0, max) + '...';
  }

  formatDate(dateString: string): string {
    const date = new Date(dateString);
    return date.toLocaleDateString(undefined, {
      day: '2-digit',
      month: 'short',
      year: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    });
  }

  asInputValue(event: Event): string {
    return (event.target as HTMLInputElement).value;
  }

  asTextareaValue(event: Event): string {
    return (event.target as HTMLTextAreaElement).value;
  }

  asSelectValue(event: Event): string {
    return (event.target as HTMLSelectElement).value;
  }

  asChecked(event: Event): boolean {
    return (event.target as HTMLInputElement).checked;
  }
}
