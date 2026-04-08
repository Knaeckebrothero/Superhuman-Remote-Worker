import {Component, computed, inject, OnInit, signal} from '@angular/core';
import {FormsModule} from '@angular/forms';
import {ApiService} from '../../../core/services/api.service';
import {
    Datasource,
    DatasourceCreateRequest,
    DatasourceTestResult,
    DatasourceType,
    DatasourceUpdateRequest,
} from '../../../core/models/api.model';

/**
 * Datasource management panel with full CRUD, type filtering, and connection testing.
 */
@Component({
  selector: 'app-datasource-list',
  standalone: true,
  imports: [FormsModule],
  template: `
    <div class="ds-container">
      <!-- Header -->
      <div class="header-bar">
        <span class="title">Datasources</span>
        <div class="filter-chips">
          @for (filter of typeFilters; track filter.value) {
            <button
              class="filter-chip"
              [class.active]="typeFilter() === filter.value"
              (click)="typeFilter.set(filter.value)"
            >
              {{ filter.label }}
            </button>
          }
        </div>
        <div class="header-actions">
          <button class="action-btn new-btn" (click)="openCreateForm()" [disabled]="showForm()">
            <span class="mat-icon">add</span> New
          </button>
          <button class="action-btn" (click)="refresh()" [disabled]="isLoading()">
            <span class="mat-icon">refresh</span>
          </button>
        </div>
      </div>

      <!-- Messages -->
      @if (successMessage()) {
        <div class="msg success-msg">
          <span>{{ successMessage() }}</span>
          <button class="dismiss-btn" (click)="successMessage.set(null)">Dismiss</button>
        </div>
      }
      @if (errorMessage()) {
        <div class="msg error-msg">
          <span>{{ errorMessage() }}</span>
          <button class="dismiss-btn" (click)="errorMessage.set(null)">Dismiss</button>
        </div>
      }

      <!-- Create/Edit Form -->
      @if (showForm()) {
        <div class="form-panel">
          <div class="form-header">
            <span>{{ editingId() ? 'Edit Datasource' : 'New Datasource' }}</span>
            <button class="close-btn" (click)="closeForm()">
              <span class="mat-icon">close</span>
            </button>
          </div>

          <div class="form-body">
            <div class="form-row">
              <div class="form-group flex-1">
                <label class="form-label">Name <span class="required">*</span></label>
                <input
                  class="form-input"
                  [(ngModel)]="formData.name"
                  placeholder="e.g. Production Analytics DB"
                  [disabled]="isSaving()"
                >
              </div>
              <div class="form-group">
                <label class="form-label">Type <span class="required">*</span></label>
                <select
                  class="form-input form-select"
                  [(ngModel)]="formData.type"
                  [disabled]="isSaving() || !!editingId()"
                  (ngModelChange)="onTypeChange()"
                >
                  <optgroup label="CLI-based">
                    <option value="generic">Generic</option>
                    <option value="repository">Repository</option>
                  </optgroup>
                  <optgroup label="Managed">
                    <option value="postgresql">PostgreSQL</option>
                    <option value="neo4j">Neo4j</option>
                    <option value="mongodb">MongoDB</option>
                    <option value="webdav">WebDAV</option>
                  </optgroup>
                </select>
              </div>
            </div>

            <!-- Connection URL (required for non-generic types) -->
            @if (formData.type !== 'generic') {
              <div class="form-group">
                <label class="form-label">
                  {{ formData.type === 'repository' ? 'Repository URL' : 'Connection URL' }}
                  <span class="required">*</span>
                </label>
                <input
                  class="form-input mono"
                  [(ngModel)]="formData.connection_url"
                  [placeholder]="urlPlaceholder()"
                  [disabled]="isSaving()"
                >
              </div>
            }

            <!-- Generic: optional connection URL -->
            @if (formData.type === 'generic') {
              <div class="form-group">
                <label class="form-label">Connection URL <span class="hint-inline">(optional)</span></label>
                <input
                  class="form-input mono"
                  [(ngModel)]="formData.connection_url"
                  placeholder="e.g. postgresql://host:5432/db or https://api.example.com"
                  [disabled]="isSaving()"
                >
              </div>
            }

            <div class="form-group">
              <label class="form-label">
                Description
                @if (formData.type === 'generic') { <span class="required">*</span> }
              </label>
              <textarea
                class="form-textarea"
                [(ngModel)]="formData.description"
                [placeholder]="formData.type === 'generic' ? 'Describe this resource for the AI — what it is, how to use it, what data it contains' : 'What this datasource contains (included in agent context)'"
                [rows]="formData.type === 'generic' ? 3 : 2"
                [disabled]="isSaving()"
              ></textarea>
            </div>

            <!-- Generic: CLI hint -->
            @if (formData.type === 'generic') {
              <div class="form-group">
                <label class="form-label">CLI Hint <span class="hint-inline">(optional)</span></label>
                <input
                  class="form-input mono"
                  [(ngModel)]="formData.cli_hint"
                  placeholder="e.g. psql $DATABASE_URL"
                  [disabled]="isSaving()"
                >
              </div>
            }

            <!-- Generic: Environment Variables -->
            @if (formData.type === 'generic') {
              <div class="form-group">
                <label class="form-label">Environment Variables</label>
                <div class="env-vars-editor">
                  @for (envVar of envVars; track $index) {
                    <div class="env-var-row">
                      <input
                        class="form-input mono env-key"
                        [(ngModel)]="envVar.key"
                        placeholder="ENV_VAR_NAME"
                        [disabled]="isSaving()"
                      >
                      <span class="env-eq">=</span>
                      <input
                        class="form-input mono env-val"
                        type="password"
                        [(ngModel)]="envVar.value"
                        placeholder="value"
                        [disabled]="isSaving()"
                      >
                      <button class="icon-btn delete" (click)="removeEnvVar($index)" [disabled]="isSaving()" title="Remove">
                        <span class="mat-icon">close</span>
                      </button>
                    </div>
                  }
                  <button class="btn btn-add-env" (click)="addEnvVar()" [disabled]="isSaving()">
                    <span class="mat-icon">add</span> Add variable
                  </button>
                </div>
                <div class="form-hint">
                  Credentials are injected as environment variables into the agent workspace.
                  Read-only access cannot be enforced — use a database account with restricted permissions if needed.
                </div>
              </div>
            }

            <!-- Repository: default branch -->
            @if (formData.type === 'repository') {
              <div class="form-group">
                <label class="form-label">Default Branch <span class="hint-inline">(optional)</span></label>
                <input
                  class="form-input"
                  [(ngModel)]="formData.default_branch"
                  placeholder="main"
                  [disabled]="isSaving()"
                >
              </div>
            }

            <!-- Repository: auth method -->
            @if (formData.type === 'repository') {
              <div class="form-group">
                <label class="form-label">Auth Method <span class="required">*</span></label>
                <select
                  class="form-input form-select"
                  [(ngModel)]="gitAuthMethod"
                  [disabled]="isSaving()"
                >
                  <option value="token">HTTPS Token</option>
                  <option value="ssh">SSH Key</option>
                </select>
              </div>
              @if (gitAuthMethod === 'token') {
                <div class="form-group">
                  <label class="form-label">Access Token</label>
                  <input
                    class="form-input mono"
                    type="password"
                    [(ngModel)]="formCredentials.password"
                    placeholder="ghp_xxxx or personal access token"
                    [disabled]="isSaving()"
                  >
                </div>
              }
              @if (gitAuthMethod === 'ssh') {
                <div class="form-group">
                  <label class="form-label">SSH Private Key</label>
                  <textarea
                    class="form-textarea mono"
                    [(ngModel)]="gitSshKey"
                    placeholder="-----BEGIN OPENSSH PRIVATE KEY-----&#10;...&#10;-----END OPENSSH PRIVATE KEY-----"
                    rows="5"
                    [disabled]="isSaving()"
                  ></textarea>
                </div>
              }
              <div class="form-hint">
                The repository will be cloned into the workspace and git credentials configured automatically.
                The agent uses standard git commands — no credential setup required.
              </div>
            }

            <!-- Managed connectors: credentials -->
            @if (formData.type === 'neo4j' || formData.type === 'webdav') {
              <div class="form-row">
                <div class="form-group flex-1">
                  <label class="form-label">Username</label>
                  <input
                    class="form-input"
                    [(ngModel)]="formCredentials.username"
                    placeholder="neo4j"
                    [disabled]="isSaving()"
                  >
                </div>
                <div class="form-group flex-1">
                  <label class="form-label">Password</label>
                  <input
                    class="form-input"
                    type="password"
                    [(ngModel)]="formCredentials.password"
                    placeholder="password"
                    [disabled]="isSaving()"
                  >
                </div>
              </div>
            }

            <div class="form-row form-footer">
              <div class="form-actions">
                @if (formData.type !== 'generic' && formData.type !== 'repository') {
                  <button
                    class="btn btn-test"
                    (click)="testFromForm()"
                    [disabled]="!formData.connection_url || isTesting()"
                  >
                    @if (isTesting()) {
                      <span class="spinner-small"></span> Testing...
                    } @else {
                      <span class="mat-icon">cable</span> Test
                    }
                  </button>
                }
                <button class="btn btn-secondary" (click)="closeForm()" [disabled]="isSaving()">Cancel</button>
                <button
                  class="btn btn-primary"
                  (click)="saveForm()"
                  [disabled]="!canSave() || isSaving()"
                >
                  @if (isSaving()) {
                    <span class="spinner-small"></span> Saving...
                  } @else {
                    {{ editingId() ? 'Update' : 'Create' }}
                  }
                </button>
              </div>
            </div>

            @if (formTestResult()) {
              <div
                class="test-result"
                [class.test-ok]="formTestResult()!.status === 'ok'"
                [class.test-error]="formTestResult()!.status === 'error'"
              >
                <span class="mat-icon">{{ formTestResult()!.status === 'ok' ? 'check_circle' : 'error' }}</span>
                {{ formTestResult()!.message }}
              </div>
            }
          </div>
        </div>
      }

      <!-- Loading State -->
      @if (isLoading() && filteredDatasources().length === 0 && !showForm()) {
        <div class="center-state">
          <div class="spinner"></div>
          <span>Loading datasources...</span>
        </div>
      }

      <!-- Empty State -->
      @if (!isLoading() && filteredDatasources().length === 0 && !showForm()) {
        <div class="center-state">
          <span class="mat-icon empty-icon">database</span>
          <span>No datasources found</span>
          <span class="hint">Create a datasource to connect agents to external databases</span>
        </div>
      }

      <!-- Table -->
      @if (filteredDatasources().length > 0) {
        <div class="table-container">
          <table class="ds-table">
            <thead>
              <tr>
                <th>Type</th>
                <th>Name</th>
                <th>URL</th>
                <th>Scope</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              @for (ds of filteredDatasources(); track ds.id) {
                <tr>
                  <td>
                    <span class="type-badge" [class]="'type-' + ds.type">
                      <span class="mat-icon">{{ getTypeIcon(ds.type) }}</span>
                      {{ ds.type }}
                    </span>
                  </td>
                  <td class="name-cell">
                    <span class="ds-name">{{ ds.name }}</span>
                    @if (ds.description) {
                      <span class="ds-desc">{{ ds.description }}</span>
                    }
                  </td>
                  <td class="url-cell mono">{{ ds.connection_url ? maskUrl(ds.connection_url) : '—' }}</td>
                  <td>
                    <span class="scope-badge" [class.scope-global]="!ds.job_id" [class.scope-job]="!!ds.job_id">
                      {{ ds.job_id ? 'Job' : 'Global' }}
                    </span>
                  </td>
                  <td class="actions-cell">
                    <button
                      class="icon-btn test"
                      (click)="testDatasource(ds.id)"
                      [disabled]="testingIds().has(ds.id)"
                      title="Test connection"
                    >
                      @if (testingIds().has(ds.id)) {
                        <span class="spinner-tiny"></span>
                      } @else {
                        <span class="mat-icon">cable</span>
                      }
                    </button>
                    <button class="icon-btn edit" (click)="openEditForm(ds)" title="Edit">
                      <span class="mat-icon">edit</span>
                    </button>
                    <button class="icon-btn delete" (click)="deleteDatasource(ds)" title="Delete">
                      <span class="mat-icon">delete</span>
                    </button>

                    @if (testResults()[ds.id]; as result) {
                      <span
                        class="inline-test"
                        [class.test-ok]="result.status === 'ok'"
                        [class.test-error]="result.status === 'error'"
                        title="{{ result.message }}"
                      >
                        <span class="mat-icon">{{ result.status === 'ok' ? 'check_circle' : 'error' }}</span>
                      </span>
                    }
                  </td>
                </tr>
              }
            </tbody>
          </table>
        </div>
      }
    </div>
  `,
  styles: [
    `
      :host {
        display: block;
        height: 100%;
        overflow: hidden;
      }

      .ds-container {
        display: flex;
        flex-direction: column;
        height: 100%;
        background: var(--panel-bg, #181825);
      }

      .mat-icon {
        font-family: 'Material Symbols Outlined';
        font-size: 18px;
        line-height: 1;
        vertical-align: middle;
      }

      /* Header */
      .header-bar {
        display: flex;
        align-items: center;
        gap: 12px;
        padding: 10px 12px;
        background: var(--panel-header-bg, #1e1e2e);
        border-bottom: 1px solid var(--border-color, #313244);
        flex-shrink: 0;
        flex-wrap: wrap;
      }

      .title {
        font-weight: 600;
        color: var(--text-primary, #cdd6f4);
      }

      .filter-chips {
        display: flex;
        gap: 4px;
      }

      .filter-chip {
        padding: 4px 10px;
        border: 1px solid var(--border-color, #45475a);
        border-radius: 12px;
        background: transparent;
        color: var(--text-muted, #6c7086);
        font-size: 11px;
        cursor: pointer;
        transition: all 0.15s ease;
      }

      .filter-chip:hover {
        background: var(--surface-0, #313244);
        color: var(--text-primary, #cdd6f4);
      }

      .filter-chip.active {
        background: var(--accent-color, #cba6f7);
        color: var(--timeline-bg, #11111b);
        border-color: var(--accent-color, #cba6f7);
      }

      .header-actions {
        display: flex;
        gap: 6px;
        margin-left: auto;
      }

      .action-btn {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        padding: 5px 10px;
        border: 1px solid var(--border-color, #45475a);
        border-radius: 4px;
        background: transparent;
        color: var(--text-secondary, #a6adc8);
        font-size: 11px;
        cursor: pointer;
        transition: all 0.15s ease;
      }

      .action-btn:hover:not(:disabled) {
        background: var(--surface-0, #313244);
        color: var(--text-primary, #cdd6f4);
      }

      .action-btn:disabled {
        opacity: 0.5;
        cursor: not-allowed;
      }

      .new-btn {
        color: #a6e3a1;
        border-color: #a6e3a1;
      }

      .new-btn:hover:not(:disabled) {
        background: rgba(166, 227, 161, 0.1);
        color: #a6e3a1;
      }

      /* Messages */
      .msg {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 8px 12px;
        margin: 8px 12px 0;
        border-radius: 6px;
        font-size: 12px;
        flex-shrink: 0;
      }

      .success-msg {
        background: rgba(166, 227, 161, 0.15);
        border: 1px solid rgba(166, 227, 161, 0.3);
        color: #a6e3a1;
      }

      .error-msg {
        background: rgba(243, 139, 168, 0.15);
        border: 1px solid rgba(243, 139, 168, 0.3);
        color: #f38ba8;
      }

      .dismiss-btn {
        padding: 3px 8px;
        border: none;
        border-radius: 4px;
        background: rgba(255, 255, 255, 0.1);
        color: inherit;
        font-size: 10px;
        cursor: pointer;
      }

      .dismiss-btn:hover {
        background: rgba(255, 255, 255, 0.2);
      }

      /* Form Panel */
      .form-panel {
        margin: 8px 12px 0;
        border: 1px solid var(--border-color, #45475a);
        border-radius: 8px;
        background: rgba(0, 0, 0, 0.2);
        flex-shrink: 0;
      }

      .form-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 10px 14px;
        border-bottom: 1px solid var(--border-color, #313244);
        font-weight: 600;
        font-size: 13px;
        color: var(--text-primary, #cdd6f4);
      }

      .close-btn {
        background: none;
        border: none;
        color: var(--text-muted, #6c7086);
        cursor: pointer;
        padding: 2px;
        line-height: 1;
      }

      .close-btn:hover {
        color: var(--text-primary, #cdd6f4);
      }

      .form-body {
        padding: 14px;
      }

      .form-row {
        display: flex;
        gap: 12px;
        overflow: hidden;
      }

      .flex-1 {
        flex: 1;
      }

      .form-group {
        margin-bottom: 12px;
      }

      .form-label {
        display: block;
        margin-bottom: 4px;
        font-size: 11px;
        font-weight: 500;
        color: var(--text-primary, #cdd6f4);
      }

      .required {
        color: #f38ba8;
      }

      .form-input,
      .form-textarea,
      .form-select {
        width: 100%;
        padding: 8px 10px;
        border: 1px solid var(--border-color, #45475a);
        border-radius: 5px;
        background: var(--surface-0, #313244);
        color: var(--text-primary, #cdd6f4);
        font-family: inherit;
        font-size: 12px;
        transition: border-color 0.15s ease;
      }

      .form-input:focus,
      .form-textarea:focus,
      .form-select:focus {
        outline: none;
        border-color: var(--accent-color, #cba6f7);
      }

      .form-input:disabled,
      .form-textarea:disabled,
      .form-select:disabled {
        opacity: 0.6;
        cursor: not-allowed;
      }

      .form-input::placeholder,
      .form-textarea::placeholder {
        color: var(--text-muted, #6c7086);
      }

      .form-input.mono {
        font-family: 'JetBrains Mono', monospace;
        font-size: 11px;
      }

      .form-textarea {
        resize: vertical;
        min-height: 40px;
      }

      .form-select {
        appearance: auto;
        max-width: 100%;
      }

      .form-row > .form-group:not(.flex-1) {
        min-width: 0;
        max-width: 160px;
      }

      .form-footer {
        align-items: center;
        justify-content: flex-end;
        margin-bottom: 0;
      }

      .hint-inline {
        font-weight: 400;
        color: var(--text-muted, #6c7086);
        font-size: 11px;
      }

      .form-hint {
        margin-top: 6px;
        padding: 8px 10px;
        background: rgba(137, 180, 250, 0.08);
        border: 1px solid rgba(137, 180, 250, 0.15);
        border-radius: 4px;
        font-size: 11px;
        color: var(--text-secondary, #a6adc8);
        line-height: 1.5;
      }

      /* Env var editor */
      .env-vars-editor {
        display: flex;
        flex-direction: column;
        gap: 6px;
      }

      .env-var-row {
        display: flex;
        align-items: center;
        gap: 6px;
      }

      .env-key {
        flex: 0 0 200px;
        text-transform: uppercase;
      }

      .env-eq {
        color: var(--text-muted, #6c7086);
        font-family: 'JetBrains Mono', monospace;
        font-size: 12px;
      }

      .env-val {
        flex: 1;
      }

      .btn-add-env {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        padding: 5px 10px;
        border: 1px dashed var(--border-color, #45475a);
        border-radius: 4px;
        background: transparent;
        color: var(--text-secondary, #a6adc8);
        font-size: 11px;
        cursor: pointer;
        align-self: flex-start;
      }

      .btn-add-env:hover:not(:disabled) {
        background: var(--surface-0, #313244);
        color: var(--text-primary, #cdd6f4);
      }

      .toggle-label {
        display: flex;
        align-items: center;
        gap: 6px;
        font-size: 12px;
        color: var(--text-primary, #cdd6f4);
        cursor: pointer;
      }

      .form-actions {
        display: flex;
        gap: 8px;
      }

      .btn {
        display: inline-flex;
        align-items: center;
        gap: 5px;
        padding: 7px 14px;
        border: none;
        border-radius: 5px;
        font-size: 12px;
        font-weight: 500;
        cursor: pointer;
        transition: all 0.15s ease;
      }

      .btn:disabled {
        opacity: 0.5;
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

      .btn-test {
        background: transparent;
        border: 1px solid var(--border-color, #45475a);
        color: var(--text-secondary, #a6adc8);
      }

      .btn-test:hover:not(:disabled) {
        background: var(--surface-0, #313244);
        color: var(--text-primary, #cdd6f4);
      }

      .test-result {
        display: flex;
        align-items: center;
        gap: 6px;
        padding: 8px 12px;
        margin-top: 8px;
        border-radius: 5px;
        font-size: 12px;
      }

      .test-ok {
        background: rgba(166, 227, 161, 0.1);
        color: #a6e3a1;
      }

      .test-error {
        background: rgba(243, 139, 168, 0.1);
        color: #f38ba8;
      }

      .spinner-small {
        width: 12px;
        height: 12px;
        border: 2px solid rgba(0, 0, 0, 0.2);
        border-top-color: currentColor;
        border-radius: 50%;
        animation: spin 0.6s linear infinite;
        display: inline-block;
      }

      .spinner-tiny {
        width: 14px;
        height: 14px;
        border: 2px solid var(--border-color, #313244);
        border-top-color: var(--accent-color, #cba6f7);
        border-radius: 50%;
        animation: spin 0.6s linear infinite;
        display: inline-block;
      }

      /* Center States */
      .center-state {
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        gap: 10px;
        padding: 40px;
        flex: 1;
        color: var(--text-muted, #6c7086);
        font-size: 13px;
      }

      .empty-icon {
        font-size: 48px !important;
        opacity: 0.4;
      }

      .hint {
        font-size: 11px;
        opacity: 0.7;
      }

      .spinner {
        width: 28px;
        height: 28px;
        border: 3px solid var(--surface-0, #313244);
        border-top-color: var(--accent-color, #cba6f7);
        border-radius: 50%;
        animation: spin 0.8s linear infinite;
      }

      @keyframes spin {
        to { transform: rotate(360deg); }
      }

      /* Table */
      .table-container {
        flex: 1;
        overflow: auto;
        padding: 8px;
      }

      .ds-table {
        width: 100%;
        border-collapse: collapse;
        font-size: 12px;
      }

      .ds-table th {
        text-align: left;
        padding: 8px 10px;
        background: var(--surface-0, #313244);
        color: var(--text-muted, #6c7086);
        font-weight: 500;
        text-transform: uppercase;
        font-size: 10px;
        letter-spacing: 0.5px;
        border-bottom: 1px solid var(--border-color, #45475a);
      }

      .ds-table td {
        padding: 10px;
        border-bottom: 1px solid var(--border-color, #313244);
        color: var(--text-primary, #cdd6f4);
        vertical-align: middle;
      }

      .ds-table tbody tr:hover {
        background: var(--surface-0, #313244);
      }

      /* Type Badge */
      .type-badge {
        display: inline-flex;
        align-items: center;
        gap: 5px;
        padding: 3px 8px;
        border-radius: 4px;
        font-size: 11px;
        font-weight: 500;
        text-transform: capitalize;
        white-space: nowrap;
      }

      .type-badge .mat-icon {
        font-size: 16px;
      }

      .type-postgresql {
        background: rgba(137, 180, 250, 0.2);
        color: #89b4fa;
      }

      .type-neo4j {
        background: rgba(166, 227, 161, 0.2);
        color: #a6e3a1;
      }

      .type-mongodb {
        background: rgba(148, 226, 213, 0.2);
        color: #94e2d5;
      }

      .type-webdav {
        background: rgba(250, 179, 135, 0.2);
        color: #fab387;
      }

      .type-generic {
        background: rgba(203, 166, 247, 0.2);
        color: #cba6f7;
      }

      .type-repository {
        background: rgba(137, 180, 250, 0.2);
        color: #89b4fa;
      }

      /* Name cell */
      .name-cell {
        max-width: 200px;
      }

      .ds-name {
        display: block;
        font-weight: 500;
      }

      .ds-desc {
        display: block;
        font-size: 10px;
        color: var(--text-muted, #6c7086);
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
        max-width: 200px;
      }

      /* URL cell */
      .url-cell {
        font-family: 'JetBrains Mono', monospace;
        font-size: 11px;
        color: var(--text-secondary, #a6adc8);
        max-width: 220px;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }

      .mono {
        font-family: 'JetBrains Mono', monospace;
      }

      /* Read-only badge */
      .ro-badge {
        padding: 2px 6px;
        border-radius: 3px;
        font-size: 10px;
        font-weight: 500;
      }

      .ro-true {
        background: rgba(137, 180, 250, 0.15);
        color: #89b4fa;
      }

      .ro-false {
        background: rgba(249, 226, 175, 0.15);
        color: #f9e2af;
      }

      /* Scope badge */
      .scope-badge {
        padding: 2px 6px;
        border-radius: 3px;
        font-size: 10px;
        font-weight: 500;
      }

      .scope-global {
        background: rgba(203, 166, 247, 0.15);
        color: #cba6f7;
      }

      .scope-job {
        background: rgba(108, 112, 134, 0.15);
        color: #6c7086;
      }

      /* Action Buttons */
      .actions-cell {
        white-space: nowrap;
      }

      .icon-btn {
        padding: 4px;
        border: none;
        border-radius: 4px;
        background: transparent;
        cursor: pointer;
        transition: all 0.15s ease;
        line-height: 1;
        color: var(--text-muted, #6c7086);
      }

      .icon-btn:hover:not(:disabled) {
        background: rgba(255, 255, 255, 0.1);
      }

      .icon-btn:disabled {
        opacity: 0.4;
        cursor: not-allowed;
      }

      .icon-btn.test:hover:not(:disabled) { color: #89b4fa; }
      .icon-btn.edit:hover:not(:disabled) { color: #f9e2af; }
      .icon-btn.delete:hover:not(:disabled) { color: #f38ba8; }

      .inline-test {
        margin-left: 4px;
      }

      .inline-test .mat-icon {
        font-size: 16px;
      }

      .inline-test.test-ok { color: #a6e3a1; }
      .inline-test.test-error { color: #f38ba8; }
    `,
  ],
})
export class DatasourceListComponent implements OnInit {
  private readonly api = inject(ApiService);

  // State signals
  readonly datasources = signal<Datasource[]>([]);
  readonly isLoading = signal(false);
  readonly typeFilter = signal<string>('all');
  readonly showForm = signal(false);
  readonly editingId = signal<string | null>(null);
  readonly testResults = signal<Record<string, DatasourceTestResult>>({});
  readonly testingIds = signal<Set<string>>(new Set());
  readonly isTesting = signal(false);
  readonly isSaving = signal(false);
  readonly formTestResult = signal<DatasourceTestResult | null>(null);
  readonly successMessage = signal<string | null>(null);
  readonly errorMessage = signal<string | null>(null);

  // Filter options
  readonly typeFilters = [
    { label: 'All', value: 'all' },
    { label: 'Generic', value: 'generic' },
    { label: 'Repository', value: 'repository' },
    { label: 'PostgreSQL', value: 'postgresql' },
    { label: 'Neo4j', value: 'neo4j' },
    { label: 'MongoDB', value: 'mongodb' },
    { label: 'WebDAV', value: 'webdav' },
  ];

  // Computed filtered list
  readonly filteredDatasources = computed(() => {
    const filter = this.typeFilter();
    const all = this.datasources();
    if (filter === 'all') return all;
    return all.filter((ds) => ds.type === filter);
  });

  // URL placeholder based on type
  readonly urlPlaceholder = computed(() => {
    const placeholders: Record<string, string> = {
      generic: 'e.g. postgresql://host:5432/db or https://api.example.com',
      repository: 'https://github.com/org/repo.git',
      postgresql: 'postgres://user:pass@host:5432/dbname',
      neo4j: 'bolt://host:7687',
      mongodb: 'mongodb://user:pass@host:27017/dbname',
      webdav: 'http://host:8800/remote.php/dav/files/user/',
    };
    return placeholders[this.formData.type] || '';
  });

  // Whether the form can be saved (method, not computed, because formData is a plain object)
  canSave(): boolean {
    if (!this.formData.name) return false;
    if (this.formData.type === 'generic') {
      return !!(this.formData.description);
    }
    return !!this.formData.connection_url;
  }

  // Form data (mutable object, not a signal, matching job-create pattern)
  formData: {
    name: string;
    type: DatasourceType;
    connection_url: string;
    description: string;
    cli_hint: string;
    default_branch: string;
  } = {
    name: '',
    type: 'generic',
    connection_url: '',
    description: '',
    cli_hint: '',
    default_branch: '',
  };

  formCredentials: { username: string; password: string } = {
    username: '',
    password: '',
  };

  // Repository auth form state
  gitAuthMethod: 'token' | 'ssh' = 'token';
  gitSshKey = '';

  // Generic env var editor
  envVars: { key: string; value: string }[] = [];

  ngOnInit(): void {
    this.refresh();
  }

  refresh(): void {
    this.isLoading.set(true);
    this.api.getDatasources().subscribe((datasources) => {
      this.datasources.set(datasources);
      this.isLoading.set(false);
    });
  }

  // ===== Form Methods =====

  openCreateForm(): void {
    this.resetFormData();
    this.editingId.set(null);
    this.showForm.set(true);
    this.formTestResult.set(null);
  }

  openEditForm(ds: Datasource): void {
    this.formData = {
      name: ds.name,
      type: ds.type,
      connection_url: ds.connection_url || '',
      description: ds.description || '',
      cli_hint: ds.cli_hint || '',
      default_branch: ds.default_branch || '',
    };
    const creds = ds.credentials || {};
    this.formCredentials = {
      username: (creds['username'] as string) || '',
      password: (creds['password'] as string) || (creds['token'] as string) || '',
    };
    // Populate repository auth fields
    if (ds.type === 'repository') {
      this.gitAuthMethod = (creds['auth_method'] as 'token' | 'ssh') || 'token';
      this.gitSshKey = (creds['ssh_key'] as string) || '';
    } else {
      this.gitAuthMethod = 'token';
      this.gitSshKey = '';
    }
    // Populate generic env vars
    if (ds.type === 'generic') {
      const envVarsObj = (creds['env_vars'] as Record<string, string>) || {};
      this.envVars = Object.entries(envVarsObj).map(([key, value]) => ({ key, value }));
    } else {
      this.envVars = [];
    }
    this.editingId.set(ds.id);
    this.showForm.set(true);
    this.formTestResult.set(null);
  }

  closeForm(): void {
    this.showForm.set(false);
    this.editingId.set(null);
    this.formTestResult.set(null);
    this.resetFormData();
  }

  onTypeChange(): void {
    // Reset URL placeholder trigger
    this.formTestResult.set(null);
  }

  saveForm(): void {
    if (!this.formData.name) return;

    // Warn when creating a repository without credentials
    if (this.formData.type === 'repository' && this.buildCredentials()?.['read_only']) {
      const confirmed = confirm(
        'No authentication credentials provided.\n\n' +
        'This repository will be cloned as read-only — the agent can browse ' +
        'and read the code but will not be able to push changes.\n\n' +
        'Continue?'
      );
      if (!confirmed) return;
    }

    this.isSaving.set(true);
    this.clearMessages();

    const editId = this.editingId();
    const creds = this.buildCredentials();

    if (editId) {
      const update: DatasourceUpdateRequest = {
        name: this.formData.name,
        description: this.formData.description || undefined,
        connection_url: this.formData.connection_url || undefined,
        credentials: creds,
        cli_hint: this.formData.cli_hint || undefined,
        default_branch: this.formData.default_branch || undefined,
      };

      this.api.updateDatasource(editId, update).subscribe({
        next: (result) => {
          this.isSaving.set(false);
          if (result) {
            this.successMessage.set('Datasource updated');
            this.closeForm();
            this.refresh();
          } else {
            this.errorMessage.set('Failed to update datasource');
          }
        },
        error: () => {
          this.isSaving.set(false);
          this.errorMessage.set('Error updating datasource');
        },
      });
    } else {
      const create: DatasourceCreateRequest = {
        name: this.formData.name,
        type: this.formData.type,
        connection_url: this.formData.connection_url || undefined,
        description: this.formData.description || undefined,
        credentials: creds,
        cli_hint: this.formData.cli_hint || undefined,
        default_branch: this.formData.default_branch || undefined,
      };

      this.api.createDatasource(create).subscribe({
        next: (result) => {
          this.isSaving.set(false);
          this.successMessage.set(`Datasource "${result.name}" created`);
          this.closeForm();
          this.refresh();
        },
        error: (err) => {
          this.isSaving.set(false);
          const detail = err?.error?.detail;
          this.errorMessage.set(detail || 'Failed to create datasource');
        },
      });
    }
  }

  testFromForm(): void {
    // For forms we need to save first to test, or test an existing one
    const editId = this.editingId();
    if (editId) {
      this.isTesting.set(true);
      this.formTestResult.set(null);
      this.api.testDatasource(editId).subscribe({
        next: (result) => {
          this.isTesting.set(false);
          this.formTestResult.set(result);
        },
        error: () => {
          this.isTesting.set(false);
          this.formTestResult.set({ status: 'error', message: 'Request failed' });
        },
      });
    } else {
      // For new datasources, save first then test
      if (!this.formData.name || !this.formData.connection_url) return;
      this.isSaving.set(true);
      this.formTestResult.set(null);

      const create: DatasourceCreateRequest = {
        name: this.formData.name,
        type: this.formData.type,
        connection_url: this.formData.connection_url || undefined,
        description: this.formData.description || undefined,
        credentials: this.buildCredentials(),
      };

      this.api.createDatasource(create).subscribe({
        next: (created) => {
          this.isSaving.set(false);
          if (created) {
            this.editingId.set(created.id);
            this.refresh();
            // Now test
            this.isTesting.set(true);
            this.api.testDatasource(created.id).subscribe({
              next: (result) => {
                this.isTesting.set(false);
                this.formTestResult.set(result);
              },
              error: () => {
                this.isTesting.set(false);
                this.formTestResult.set({ status: 'error', message: 'Test request failed' });
              },
            });
          } else {
            this.errorMessage.set('Failed to create datasource for testing');
          }
        },
        error: () => {
          this.isSaving.set(false);
          this.errorMessage.set('Error creating datasource');
        },
      });
    }
  }

  // ===== Table Actions =====

  testDatasource(id: string): void {
    this.testingIds.update((s) => new Set(s).add(id));
    this.api.testDatasource(id).subscribe({
      next: (result) => {
        this.testingIds.update((s) => {
          const next = new Set(s);
          next.delete(id);
          return next;
        });
        if (result) {
          this.testResults.update((r) => ({ ...r, [id]: result }));
        }
      },
      error: () => {
        this.testingIds.update((s) => {
          const next = new Set(s);
          next.delete(id);
          return next;
        });
        this.testResults.update((r) => ({
          ...r,
          [id]: { status: 'error' as const, message: 'Request failed' },
        }));
      },
    });
  }

  deleteDatasource(ds: Datasource): void {
    this.clearMessages();
    this.api.deleteDatasource(ds.id).subscribe({
      next: (result) => {
        if (result) {
          this.successMessage.set(`Datasource "${ds.name}" deleted`);
          this.refresh();
        } else {
          this.errorMessage.set('Failed to delete datasource');
        }
      },
      error: () => {
        this.errorMessage.set('Error deleting datasource');
      },
    });
  }

  // ===== Helpers =====

  getTypeIcon(type: DatasourceType | string): string {
    const icons: Record<string, string> = {
      generic: 'settings_input_component',
      repository: 'code',
      postgresql: 'database',
      neo4j: 'hub',
      mongodb: 'eco',
      webdav: 'cloud',
    };
    return icons[type] || 'storage';
  }

  maskUrl(url: string): string {
    try {
      return url.replace(/:([^/:@]+)@/, ':***@');
    } catch {
      return url;
    }
  }

  // Env var editor methods
  addEnvVar(): void {
    this.envVars.push({ key: '', value: '' });
  }

  removeEnvVar(index: number): void {
    this.envVars.splice(index, 1);
  }

  private buildCredentials(): Record<string, unknown> | undefined {
    if (this.formData.type === 'generic') {
      const envVarsObj: Record<string, string> = {};
      for (const ev of this.envVars) {
        if (ev.key.trim()) {
          envVarsObj[ev.key.trim()] = ev.value;
        }
      }
      if (Object.keys(envVarsObj).length === 0) return undefined;
      return { env_vars: envVarsObj };
    }
    if (this.formData.type === 'repository') {
      if (this.gitAuthMethod === 'ssh') {
        if (!this.gitSshKey) return { read_only: true };
        return { auth_method: 'ssh', ssh_key: this.gitSshKey };
      } else {
        if (!this.formCredentials.password) return { read_only: true };
        return { auth_method: 'token', token: this.formCredentials.password };
      }
    }
    if (!this.formCredentials.username && !this.formCredentials.password) return undefined;
    return {
      username: this.formCredentials.username || undefined,
      password: this.formCredentials.password || undefined,
    };
  }

  private resetFormData(): void {
    this.formData = {
      name: '',
      type: 'generic',
      connection_url: '',
      description: '',
      cli_hint: '',
      default_branch: '',
    };
    this.formCredentials = { username: '', password: '' };
    this.gitAuthMethod = 'token';
    this.gitSshKey = '';
    this.envVars = [];
  }

  private clearMessages(): void {
    this.successMessage.set(null);
    this.errorMessage.set(null);
  }
}
