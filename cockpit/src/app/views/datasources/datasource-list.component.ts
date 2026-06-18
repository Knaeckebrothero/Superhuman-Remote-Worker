import {Component, computed, inject, OnInit, signal} from '@angular/core';
import {TranslocoPipe, TranslocoService} from '@jsverse/transloco';
import {ApiService} from '../../core/services/api.service';
import {
    CredentialFileEntry,
    Datasource,
    DatasourceCreateRequest,
    DatasourceTestResult,
    DatasourceType,
    DatasourceUpdateRequest,
} from '../../core/models/api.model';
import {AppButtonComponent} from '../../ui/button';
import {AppIconButtonComponent} from '../../ui/icon-button';
import {AppBadgeComponent, type BadgeTone} from '../../ui/badge';
import {AppChipComponent} from '../../ui/chip';
import {AppInputComponent} from '../../ui/input';
import {AppSelectComponent} from '../../ui/select';
import {AppTextareaComponent} from '../../ui/textarea';
import {AppIconComponent} from '../../ui/icon';
import {AppSpinnerComponent} from '../../ui/spinner';
import {AppFormFieldComponent} from '../../ui/form-field';
import {AppDialogComponent} from '../../ui/dialog';
/**
 * Datasource management panel with full CRUD, type filtering, and connection testing.
 */
@Component({
  selector: 'app-datasource-list',
  standalone: true,
  imports: [
    TranslocoPipe,
    AppButtonComponent,
    AppIconButtonComponent,
    AppBadgeComponent,
    AppChipComponent,
    AppInputComponent,
    AppSelectComponent,
    AppTextareaComponent,
    AppIconComponent,
    AppSpinnerComponent,
    AppFormFieldComponent,
    AppDialogComponent,  ],
  template: `
    <div class="ds-container">
      <!-- Header -->
      <div class="header-bar">
        <span class="title">{{ 'datasources.title' | transloco }}</span>
        <div class="filter-chips">
          @for (filter of typeFilters; track filter.value) {
            <app-chip
              size="sm"
              [selected]="typeFilter() === filter.value"
              (clicked)="typeFilter.set(filter.value)"
            >
              {{ filter.labelKey | transloco }}
            </app-chip>
          }
        </div>
        <div class="header-actions">
          <app-button
            variant="success"
            size="sm"
            [disabled]="showForm()"
            (clicked)="openCreateForm()"
          >
            <app-icon size="sm">add</app-icon> {{ 'datasources.new' | transloco }}
          </app-button>
          <app-icon-button
            variant="ghost"
            size="sm"
            [ariaLabel]="'datasources.refresh' | transloco"
            [disabled]="isLoading()"
            (clicked)="refresh()"
          >
            <app-icon size="sm">refresh</app-icon>
          </app-icon-button>
        </div>
      </div>

      <!-- Messages -->
      @if (successMessage()) {
        <div class="msg success-msg">
          <span>{{ successMessage() }}</span>
          <app-button variant="ghost" size="sm" (clicked)="successMessage.set(null)">
            {{ 'datasources.dismiss' | transloco }}
          </app-button>
        </div>
      }
      @if (errorMessage()) {
        <div class="msg error-msg">
          <span>{{ errorMessage() }}</span>
          <app-button variant="ghost" size="sm" (clicked)="errorMessage.set(null)">
            {{ 'datasources.dismiss' | transloco }}
          </app-button>
        </div>
      }

      <!-- Create/Edit Form -->
      @if (showForm()) {
        <div class="form-panel">
          <div class="form-header">
            <span>{{ (editingId() ? 'datasources.form.editTitle' : 'datasources.form.newTitle') | transloco }}</span>
            <app-icon-button
              variant="ghost"
              size="sm"
              [ariaLabel]="'datasources.form.close' | transloco"
              (clicked)="closeForm()"
            >
              <app-icon size="sm">close</app-icon>
            </app-icon-button>
          </div>

          <div class="form-body">
            <div class="form-row">
              <app-form-field class="flex-1" [label]="'datasources.form.nameLabel' | transloco" [required]="true">
                <app-input
                  size="sm"
                  [value]="formData.name"
                  (valueChange)="formData.name = $event"
                  [placeholder]="'datasources.form.namePlaceholder' | transloco"
                  [disabled]="isSaving()"
                />
              </app-form-field>
              <app-form-field [label]="'datasources.form.typeLabel' | transloco" [required]="true">
                <app-select
                  size="sm"
                  [value]="formData.type"
                  (changed)="onTypeSelect($event)"
                  [disabled]="isSaving() || !!editingId()"
                >
                  <optgroup [label]="'datasources.form.typeGroupCli' | transloco">
                    <option value="generic">{{ 'datasources.form.optGeneric' | transloco }}</option>
                    <option value="repository">{{ 'datasources.form.optRepository' | transloco }}</option>
                  </optgroup>
                  <optgroup [label]="'datasources.form.typeGroupManaged' | transloco">
                    <option value="postgresql">{{ 'datasources.form.optPostgresql' | transloco }}</option>
                    <option value="neo4j">{{ 'datasources.form.optNeo4j' | transloco }}</option>
                    <option value="mongodb">{{ 'datasources.form.optMongodb' | transloco }}</option>
                    <option value="webdav">{{ 'datasources.form.optWebdav' | transloco }}</option>
                  </optgroup>
                  <optgroup [label]="'datasources.form.typeGroupCredentialFiles' | transloco">
                    <option value="kubeconfig">{{ 'datasources.form.optKubeconfig' | transloco }}</option>
                    <option value="ssh_key">{{ 'datasources.form.optSshKey' | transloco }}</option>
                    <option value="generic_file">{{ 'datasources.form.optGenericFile' | transloco }}</option>
                  </optgroup>
                </app-select>
              </app-form-field>
            </div>

            <!-- Connection URL (required for non-generic, non-credential-file types) -->
            @if (hasConnectionUrl() && formData.type !== 'generic') {
              <app-form-field
                [label]="(formData.type === 'repository' ? 'datasources.form.repoUrlLabel' : 'datasources.form.connectionUrlLabel') | transloco"
                [required]="true"
              >
                <app-input
                  size="sm"
                  class="mono"
                  [value]="formData.connection_url"
                  (valueChange)="formData.connection_url = $event"
                  [placeholder]="urlPlaceholder()"
                  [disabled]="isSaving()"
                />
              </app-form-field>
            }

            <!-- Generic: optional connection URL -->
            @if (formData.type === 'generic') {
              <app-form-field [label]="'datasources.form.connectionUrlLabel' | transloco" [optional]="'datasources.form.optional' | transloco">
                <app-input
                  size="sm"
                  class="mono"
                  [value]="formData.connection_url"
                  (valueChange)="formData.connection_url = $event"
                  [placeholder]="'datasources.form.genericUrlPlaceholder' | transloco"
                  [disabled]="isSaving()"
                />
              </app-form-field>
            }

            <app-form-field
              [label]="'datasources.form.descriptionLabel' | transloco"
              [required]="formData.type === 'generic'"
            >
              <app-textarea
                size="sm"
                [value]="formData.description"
                (valueChange)="formData.description = $event"
                [placeholder]="(formData.type === 'generic' ? 'datasources.form.descriptionPlaceholderGeneric' : 'datasources.form.descriptionPlaceholderManaged') | transloco"
                [rows]="formData.type === 'generic' ? 3 : 2"
                [disabled]="isSaving()"
              />
            </app-form-field>

            <!-- Generic: CLI hint -->
            @if (formData.type === 'generic') {
              <app-form-field [label]="'datasources.form.cliHintLabel' | transloco" [optional]="'datasources.form.optional' | transloco">
                <app-input
                  size="sm"
                  class="mono"
                  [value]="formData.cli_hint"
                  (valueChange)="formData.cli_hint = $event"
                  [placeholder]="'datasources.form.cliHintPlaceholder' | transloco"
                  [disabled]="isSaving()"
                />
              </app-form-field>
            }

            <!-- Generic: Environment Variables -->
            @if (formData.type === 'generic') {
              <app-form-field [label]="'datasources.form.envVarsLabel' | transloco" [hint]="'datasources.form.envHint' | transloco">
                <div class="env-vars-editor">
                  @for (envVar of envVars; track $index) {
                    <div class="env-var-row">
                      <app-input
                        size="sm"
                        class="mono env-key"
                        [value]="envVar.key"
                        (valueChange)="envVar.key = $event"
                        [placeholder]="'datasources.form.envKeyPlaceholder' | transloco"
                        [disabled]="isSaving()"
                      />
                      <span class="env-eq">=</span>
                      <app-input
                        size="sm"
                        type="password"
                        class="mono env-val"
                        [value]="envVar.value"
                        (valueChange)="envVar.value = $event"
                        [placeholder]="'datasources.form.envValuePlaceholder' | transloco"
                        [disabled]="isSaving()"
                      />
                      <app-icon-button
                        variant="danger"
                        size="sm"
                        [ariaLabel]="'datasources.form.envRemoveTooltip' | transloco"
                        [tooltip]="'datasources.form.envRemoveTooltip' | transloco"
                        [disabled]="isSaving()"
                        (clicked)="removeEnvVar($index)"
                      >
                        <app-icon size="sm">close</app-icon>
                      </app-icon-button>
                    </div>
                  }
                  <app-button
                    variant="ghost"
                    size="sm"
                    class="btn-add-env"
                    [disabled]="isSaving()"
                    (clicked)="addEnvVar()"
                  >
                    <app-icon size="sm">add</app-icon> {{ 'datasources.form.envAdd' | transloco }}
                  </app-button>
                </div>
              </app-form-field>
            }

            <!-- Repository: default branch -->
            @if (formData.type === 'repository') {
              <app-form-field [label]="'datasources.form.defaultBranchLabel' | transloco" [optional]="'datasources.form.optional' | transloco">
                <app-input
                  size="sm"
                  [value]="formData.default_branch"
                  (valueChange)="formData.default_branch = $event"
                  [placeholder]="'datasources.form.defaultBranchPlaceholder' | transloco"
                  [disabled]="isSaving()"
                />
              </app-form-field>
            }

            <!-- Repository: auth method -->
            @if (formData.type === 'repository') {
              <app-form-field [label]="'datasources.form.authMethodLabel' | transloco" [required]="true">
                <app-select
                  size="sm"
                  [value]="gitAuthMethod"
                  (changed)="onGitAuthMethodChange($event)"
                  [disabled]="isSaving()"
                >
                  <option value="token">{{ 'datasources.form.authToken' | transloco }}</option>
                  <option value="ssh">{{ 'datasources.form.authSsh' | transloco }}</option>
                </app-select>
              </app-form-field>
              @if (gitAuthMethod === 'token') {
                <app-form-field [label]="'datasources.form.tokenLabel' | transloco">
                  <app-input
                    size="sm"
                    type="password"
                    class="mono"
                    [value]="formCredentials.password"
                    (valueChange)="formCredentials.password = $event"
                    [placeholder]="'datasources.form.tokenPlaceholder' | transloco"
                    [disabled]="isSaving()"
                  />
                </app-form-field>
              }
              @if (gitAuthMethod === 'ssh') {
                <app-form-field [label]="'datasources.form.sshKeyLabel' | transloco">
                  <app-textarea
                    size="sm"
                    class="mono"
                    [value]="gitSshKey"
                    (valueChange)="gitSshKey = $event"
                    placeholder="-----BEGIN OPENSSH PRIVATE KEY-----&#10;...&#10;-----END OPENSSH PRIVATE KEY-----"
                    [rows]="5"
                    [disabled]="isSaving() || isGeneratingKey()"
                  />
                </app-form-field>
                <div class="ssh-key-actions">
                  @if (!showGenerateConfirm()) {
                    <app-button
                      size="sm"
                      variant="secondary"
                      type="button"
                      [disabled]="isSaving() || isGeneratingKey()"
                      (clicked)="onGenerateSshKeyClick()"
                    >
                      @if (isGeneratingKey()) {
                        <app-spinner size="sm" />
                        {{ 'datasources.form.sshGenerateBusy' | transloco }}
                      } @else {
                        {{ 'datasources.form.sshGenerate' | transloco }}
                      }
                    </app-button>
                  } @else {
                    <span class="confirm-text">
                      {{ 'datasources.form.sshGenerateConfirm' | transloco }}
                    </span>
                    <app-button
                      size="sm"
                      variant="primary"
                      type="button"
                      (clicked)="confirmGenerateSshKey()"
                    >
                      {{ 'datasources.form.sshGenerateReplace' | transloco }}
                    </app-button>
                    <app-button
                      size="sm"
                      variant="secondary"
                      type="button"
                      (clicked)="cancelGenerateSshKey()"
                    >
                      {{ 'datasources.form.cancel' | transloco }}
                    </app-button>
                  }
                </div>
              }
              <div class="form-hint">
                {{ 'datasources.form.repoHint' | transloco }}
              </div>
            }

            <!-- Managed connectors: credentials -->
            @if (formData.type === 'neo4j' || formData.type === 'webdav') {
              <div class="form-row">
                <app-form-field class="flex-1" [label]="'datasources.form.usernameLabel' | transloco">
                  <app-input
                    size="sm"
                    [value]="formCredentials.username"
                    (valueChange)="formCredentials.username = $event"
                    [placeholder]="'datasources.form.usernamePlaceholder' | transloco"
                    [disabled]="isSaving()"
                  />
                </app-form-field>
                <app-form-field class="flex-1" [label]="'datasources.form.passwordLabel' | transloco">
                  <app-input
                    size="sm"
                    type="password"
                    [value]="formCredentials.password"
                    (valueChange)="formCredentials.password = $event"
                    [placeholder]="'datasources.form.passwordPlaceholder' | transloco"
                    [disabled]="isSaving()"
                  />
                </app-form-field>
              </div>
            }

            <!-- Kubeconfig: paste/upload YAML -->
            @if (formData.type === 'kubeconfig') {
              <app-form-field [label]="'datasources.form.kubeconfigLabel' | transloco" [required]="!editingId()">
                <app-textarea
                  size="sm"
                  class="mono"
                  [value]="kubeconfigContent"
                  (valueChange)="kubeconfigContent = $event"
                  [placeholder]="'datasources.form.kubeconfigPlaceholder' | transloco"
                  [rows]="8"
                  [disabled]="isSaving()"
                />
              </app-form-field>
              <div class="cred-file-actions">
                <input #kubeFileInput type="file" hidden accept=".yaml,.yml,.conf,application/x-yaml,text/yaml" (change)="onUploadFile($event, 'kubeconfig')" />
                <app-button size="sm" variant="secondary" type="button" [disabled]="isSaving()" (clicked)="kubeFileInput.click()">
                  <app-icon size="sm">attach_file</app-icon> {{ 'datasources.form.uploadFile' | transloco }}
                </app-button>
              </div>
              <div class="form-hint">{{ 'datasources.form.kubeconfigHint' | transloco }}</div>
              <div class="trust-notice">
                <app-icon size="sm">shield</app-icon>
                <div>
                  <strong>{{ 'datasources.form.trustNoticeTitle' | transloco }}.</strong>
                  {{ 'datasources.form.trustNotice' | transloco }}
                </div>
              </div>
            }

            <!-- SSH key: paste or generate -->
            @if (formData.type === 'ssh_key') {
              <app-form-field [label]="'datasources.form.sshKeyContentLabel' | transloco" [required]="!editingId()">
                <app-textarea
                  size="sm"
                  class="mono"
                  [value]="gitSshKey"
                  (valueChange)="gitSshKey = $event"
                  [placeholder]="'datasources.form.sshKeyContentPlaceholder' | transloco"
                  [rows]="6"
                  [disabled]="isSaving() || isGeneratingKey()"
                />
              </app-form-field>
              <div class="cred-file-actions ssh-key-actions">
                <input #sshFileInput type="file" hidden (change)="onUploadFile($event, 'ssh_key')" />
                <app-button size="sm" variant="secondary" type="button" [disabled]="isSaving() || isGeneratingKey()" (clicked)="sshFileInput.click()">
                  <app-icon size="sm">attach_file</app-icon> {{ 'datasources.form.uploadFile' | transloco }}
                </app-button>
                @if (!showGenerateConfirm()) {
                  <app-button
                    size="sm"
                    variant="secondary"
                    type="button"
                    [disabled]="isSaving() || isGeneratingKey()"
                    (clicked)="onGenerateSshKeyClick()"
                  >
                    @if (isGeneratingKey()) {
                      <app-spinner size="sm" />
                      {{ 'datasources.form.sshGenerateBusy' | transloco }}
                    } @else {
                      {{ 'datasources.form.sshGenerate' | transloco }}
                    }
                  </app-button>
                } @else {
                  <span class="confirm-text">{{ 'datasources.form.sshGenerateConfirm' | transloco }}</span>
                  <app-button size="sm" variant="primary" type="button" (clicked)="confirmGenerateSshKey()">
                    {{ 'datasources.form.sshGenerateReplace' | transloco }}
                  </app-button>
                  <app-button size="sm" variant="secondary" type="button" (clicked)="cancelGenerateSshKey()">
                    {{ 'datasources.form.cancel' | transloco }}
                  </app-button>
                }
              </div>
              <div class="form-hint">{{ 'datasources.form.sshKeyHint' | transloco }}</div>
              <div class="trust-notice">
                <app-icon size="sm">shield</app-icon>
                <div>
                  <strong>{{ 'datasources.form.trustNoticeTitle' | transloco }}.</strong>
                  {{ 'datasources.form.trustNotice' | transloco }}
                </div>
              </div>
            }

            <!-- Generic file: repeatable editor -->
            @if (formData.type === 'generic_file') {
              <app-form-field [label]="'datasources.form.credFilesLabel' | transloco">
                <div class="generic-files-editor">
                  @for (gf of genericFiles; track $index) {
                    <div class="generic-file-card">
                      <div class="form-row">
                        <app-form-field class="flex-1" [label]="'datasources.form.credFileNameLabel' | transloco">
                          <app-input
                            size="sm"
                            [value]="gf.name"
                            (valueChange)="gf.name = $event"
                            [placeholder]="'datasources.form.credFileNamePlaceholder' | transloco"
                            [disabled]="isSaving()"
                          />
                        </app-form-field>
                        <app-form-field class="flex-1" [label]="'datasources.form.credFileTargetPathLabel' | transloco" [required]="true">
                          <app-input
                            size="sm"
                            class="mono"
                            [value]="gf.target_path"
                            (valueChange)="gf.target_path = $event"
                            [placeholder]="'datasources.form.credFileTargetPathPlaceholder' | transloco"
                            [disabled]="isSaving()"
                          />
                        </app-form-field>
                      </div>
                      <app-form-field [label]="'datasources.form.credFileContentsLabel' | transloco" [required]="true">
                        <app-textarea
                          size="sm"
                          class="mono"
                          [value]="gf.contents"
                          (valueChange)="gf.contents = $event"
                          [placeholder]="'datasources.form.credFileContentsPlaceholder' | transloco"
                          [rows]="5"
                          [disabled]="isSaving()"
                        />
                      </app-form-field>
                      <div class="form-row">
                        <app-form-field [label]="'datasources.form.credFileModeLabel' | transloco">
                          <app-input
                            size="sm"
                            class="mono"
                            [value]="gf.mode"
                            (valueChange)="gf.mode = $event"
                            [placeholder]="'datasources.form.credFileModePlaceholder' | transloco"
                            [disabled]="isSaving()"
                          />
                        </app-form-field>
                        <app-form-field class="flex-1" [label]="'datasources.form.credFileEnvVarLabel' | transloco" [optional]="'datasources.form.optional' | transloco">
                          <app-input
                            size="sm"
                            class="mono"
                            [value]="gf.env_var"
                            (valueChange)="gf.env_var = $event"
                            [placeholder]="'datasources.form.credFileEnvVarPlaceholder' | transloco"
                            [disabled]="isSaving()"
                          />
                        </app-form-field>
                        <div class="generic-file-card-actions">
                          <input #gfFileInput type="file" hidden (change)="onUploadFile($event, { file: $index })" />
                          <app-button size="sm" variant="ghost" type="button" [disabled]="isSaving()" (clicked)="gfFileInput.click()">
                            <app-icon size="sm">attach_file</app-icon> {{ 'datasources.form.uploadFile' | transloco }}
                          </app-button>
                          <app-icon-button
                            variant="danger"
                            size="sm"
                            [ariaLabel]="'datasources.form.credFileRemoveTooltip' | transloco"
                            [tooltip]="'datasources.form.credFileRemoveTooltip' | transloco"
                            [disabled]="isSaving()"
                            (clicked)="removeGenericFile($index)"
                          >
                            <app-icon size="sm">close</app-icon>
                          </app-icon-button>
                        </div>
                      </div>
                    </div>
                  }
                  <app-button
                    size="sm"
                    variant="ghost"
                    class="btn-add-env"
                    [disabled]="isSaving() || genericFiles.length >= 5"
                    (clicked)="addGenericFile()"
                  >
                    <app-icon size="sm">add</app-icon> {{ 'datasources.form.credFilesAdd' | transloco }}
                  </app-button>
                </div>
              </app-form-field>
              <div class="form-hint">{{ 'datasources.form.credFileGenericHint' | transloco }}</div>
              <div class="trust-notice">
                <app-icon size="sm">shield</app-icon>
                <div>
                  <strong>{{ 'datasources.form.trustNoticeTitle' | transloco }}.</strong>
                  {{ 'datasources.form.trustNotice' | transloco }}
                </div>
              </div>
            }

            <div class="form-row form-footer">
              <div class="form-actions">
                @if (formData.type !== 'generic' && formData.type !== 'repository' && !isCredentialFileType()) {
                  <app-button
                    variant="secondary"
                    size="sm"
                    [loading]="isTesting()"
                    [disabled]="!formData.connection_url"
                    (clicked)="testFromForm()"
                  >
                    @if (isTesting()) {
                      {{ 'datasources.form.testing' | transloco }}
                    } @else {
                      <app-icon size="sm">cable</app-icon> {{ 'datasources.form.test' | transloco }}
                    }
                  </app-button>
                }
                <app-button
                  variant="secondary"
                  size="sm"
                  [disabled]="isSaving()"
                  (clicked)="closeForm()"
                >
                  {{ 'datasources.form.cancel' | transloco }}
                </app-button>
                <app-button
                  variant="primary"
                  size="sm"
                  [loading]="isSaving()"
                  [disabled]="!canSave()"
                  (clicked)="saveForm()"
                >
                  @if (isSaving()) {
                    {{ 'datasources.form.saving' | transloco }}
                  } @else {
                    {{ (editingId() ? 'datasources.form.update' : 'datasources.form.create') | transloco }}
                  }
                </app-button>
              </div>
            </div>

            @if (formTestResult()) {
              <div
                class="test-result"
                [class.test-ok]="formTestResult()!.status === 'ok'"
                [class.test-error]="formTestResult()!.status === 'error'"
              >
                <app-icon size="sm">{{ formTestResult()!.status === 'ok' ? 'check_circle' : 'error' }}</app-icon>
                {{ formTestResult()!.message }}
              </div>
            }
          </div>
        </div>
      }

      <!-- Loading State -->
      @if (isLoading() && filteredDatasources().length === 0 && !showForm()) {
        <div class="center-state">
          <app-spinner size="lg" tone="accent" />
          <span>{{ 'datasources.table.loading' | transloco }}</span>
        </div>
      }

      <!-- Empty State -->
      @if (!isLoading() && filteredDatasources().length === 0 && !showForm()) {
        <div class="center-state">
          <app-icon size="inherit" class="empty-icon">database</app-icon>
          <span>{{ 'datasources.table.empty' | transloco }}</span>
          <span class="hint">{{ 'datasources.table.emptyHint' | transloco }}</span>
        </div>
      }

      <!-- Table -->
      @if (filteredDatasources().length > 0) {
        <div class="table-container">
          <table class="ds-table">
            <thead>
              <tr>
                <th>{{ 'datasources.table.colType' | transloco }}</th>
                <th>{{ 'datasources.table.colName' | transloco }}</th>
                <th>{{ 'datasources.table.colUrl' | transloco }}</th>
                <th>{{ 'datasources.table.colScope' | transloco }}</th>
                <th>{{ 'datasources.table.colActions' | transloco }}</th>
              </tr>
            </thead>
            <tbody>
              @for (ds of filteredDatasources(); track ds.id) {
                <tr>
                  <td>
                    <app-badge [tone]="dsTypeTone(ds.type)" size="sm">
                      <app-icon size="sm">{{ getTypeIcon(ds.type) }}</app-icon>
                      {{ 'datasources.filter.' + ds.type | transloco }}
                    </app-badge>
                  </td>
                  <td class="name-cell">
                    <span class="ds-name">{{ ds.name }}</span>
                    @if (ds.description) {
                      <span class="ds-desc">{{ ds.description }}</span>
                    }
                  </td>
                  <td class="url-cell mono">{{ ds.connection_url ? maskUrl(ds.connection_url) : '—' }}</td>
                  <td>
                    <app-badge [tone]="ds.job_id ? 'neutral' : 'accent'" size="xs">
                      {{ (ds.job_id ? 'datasources.table.scopeJob' : 'datasources.table.scopeGlobal') | transloco }}
                    </app-badge>
                  </td>
                  <td class="actions-cell">
                    <app-icon-button
                      variant="ghost"
                      size="sm"
                      [ariaLabel]="'datasources.table.testTooltip' | transloco"
                      [tooltip]="'datasources.table.testTooltip' | transloco"
                      [loading]="testingIds().has(ds.id)"
                      (clicked)="testDatasource(ds.id)"
                    >
                      <app-icon size="sm">cable</app-icon>
                    </app-icon-button>
                    <app-icon-button
                      variant="ghost"
                      size="sm"
                      [ariaLabel]="'datasources.table.editTooltip' | transloco"
                      [tooltip]="'datasources.table.editTooltip' | transloco"
                      (clicked)="openEditForm(ds)"
                    >
                      <app-icon size="sm">edit</app-icon>
                    </app-icon-button>
                    <app-icon-button
                      variant="danger"
                      size="sm"
                      [ariaLabel]="'datasources.table.deleteTooltip' | transloco"
                      [tooltip]="'datasources.table.deleteTooltip' | transloco"
                      (clicked)="deleteDatasource(ds)"
                    >
                      <app-icon size="sm">delete</app-icon>
                    </app-icon-button>

                    @if (testResults()[ds.id]; as result) {
                      <span
                        class="inline-test"
                        [class.test-ok]="result.status === 'ok'"
                        [class.test-error]="result.status === 'error'"
                        title="{{ result.message }}"
                      >
                        <app-icon size="sm">{{ result.status === 'ok' ? 'check_circle' : 'error' }}</app-icon>
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

    <!-- Generated SSH public-key dialog -->
    <app-dialog
      [open]="showPublicKeyDialog()"
      size="md"
      [title]="'datasources.sshKeyDialog.title' | transloco"
      (closed)="closePublicKeyDialog()"
    >
      <p class="public-key-intro">
        {{ 'datasources.sshKeyDialog.intro' | transloco }}
      </p>
      <div class="public-key-wrap">
        <pre class="public-key-block">{{ generatedPublicKey() }}</pre>
        <app-icon-button
          class="public-key-copy"
          size="sm"
          variant="ghost"
          [ariaLabel]="
            (publicKeyCopied()
              ? 'datasources.sshKeyDialog.copied'
              : 'datasources.sshKeyDialog.copy'
            ) | transloco
          "
          [tooltip]="
            (publicKeyCopied()
              ? 'datasources.sshKeyDialog.copied'
              : 'datasources.sshKeyDialog.copy'
            ) | transloco
          "
          (clicked)="copyPublicKey()"
        >
          <app-icon size="sm">{{
            publicKeyCopied() ? 'check' : 'content_copy'
          }}</app-icon>
        </app-icon-button>
      </div>
      <p class="public-key-hint">
        {{ 'datasources.sshKeyDialog.privateNote' | transloco }}
      </p>
      <ng-container appDialogActions>
        <app-button
          size="sm"
          [variant]="publicKeyCopied() ? 'success' : 'primary'"
          type="button"
          (clicked)="copyPublicKey()"
        >
          {{
            (publicKeyCopied()
              ? 'datasources.sshKeyDialog.copied'
              : 'datasources.sshKeyDialog.copy'
            ) | transloco
          }}
        </app-button>
        <app-button
          size="sm"
          variant="secondary"
          type="button"
          (clicked)="closePublicKeyDialog()"
        >
          {{ 'datasources.sshKeyDialog.done' | transloco }}
        </app-button>
      </ng-container>
    </app-dialog>
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
        background: var(--panel-bg, var(--panel-bg));
      }

      /* Header */
      .header-bar {
        display: flex;
        align-items: center;
        gap: 12px;
        padding: 10px 12px;
        background: var(--panel-header-bg, #1e1e2e);
        border-bottom: 1px solid var(--border-color, var(--surface-0));
        flex-shrink: 0;
        flex-wrap: wrap;
      }

      .title {
        font-weight: 600;
        color: var(--text-primary, var(--text-primary));
      }

      .filter-chips {
        display: flex;
        gap: 4px;
      }

      .header-actions {
        display: flex;
        gap: 6px;
        margin-left: auto;
        align-items: center;
      }

      /* Messages */
      .msg {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 8px 12px;
        margin: 8px 12px 0;
        border-radius: var(--radius-control);
        font-size: 12px;
        flex-shrink: 0;
      }

      .success-msg {
        background: var(--success-tint);
        border: 1px solid var(--success-tint);
        color: var(--success);
      }

      .error-msg {
        background: var(--danger-tint);
        border: 1px solid var(--danger-tint);
        color: var(--danger);
      }

      /* Form Panel */
      .form-panel {
        margin: 8px 12px 0;
        border: 1px solid var(--border-color, var(--surface-1));
        border-radius: var(--radius-surface);
        background: rgba(0, 0, 0, 0.2);
        flex-shrink: 0;
      }

      .form-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 10px 14px;
        border-bottom: 1px solid var(--border-color, var(--surface-0));
        font-weight: 600;
        font-size: 13px;
        color: var(--text-primary, var(--text-primary));
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

      app-input.mono,
      app-textarea.mono {
        font-family: 'JetBrains Mono', monospace;
      }

      .form-row > app-form-field:not(.flex-1) {
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
        background: var(--info-tint);
        border: 1px solid var(--info-tint);
        border-radius: var(--radius-tag);
        font-size: 11px;
        color: var(--text-secondary, var(--text-secondary));
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

      app-input.env-key {
        flex: 0 0 200px;
      }

      app-input.env-key ::ng-deep input {
        text-transform: uppercase;
      }

      .env-eq {
        color: var(--text-muted, #6c7086);
        font-family: 'JetBrains Mono', monospace;
        font-size: 12px;
      }

      app-input.env-val {
        flex: 1;
      }

      .btn-add-env {
        align-self: flex-start;
      }

      .toggle-label {
        display: flex;
        align-items: center;
        gap: 6px;
        font-size: 12px;
        color: var(--text-primary, var(--text-primary));
        cursor: pointer;
      }

      .form-actions {
        display: flex;
        gap: 8px;
        align-items: center;
      }

      /* SSH key generation */
      .ssh-key-actions {
        display: flex;
        align-items: center;
        gap: 8px;
        margin-top: 6px;
        flex-wrap: wrap;
      }

      .ssh-key-actions .confirm-text {
        font-size: 12px;
        color: var(--warning, var(--text-primary));
      }

      /* Credential-file actions row (upload, generate) */
      .cred-file-actions {
        display: flex;
        align-items: center;
        gap: 8px;
        margin-top: 6px;
        flex-wrap: wrap;
      }

      /* Posture-1 trust notice — warning-tinted, slightly more prominent
         than .form-hint because it carries security-relevant copy. */
      .trust-notice {
        display: flex;
        align-items: flex-start;
        gap: 8px;
        margin-top: 8px;
        padding: 10px 12px;
        background: var(--warning-tint, var(--info-tint));
        border: 1px solid var(--warning-border, var(--info-tint));
        border-radius: var(--radius-tag);
        font-size: 12px;
        line-height: 1.5;
        color: var(--text-primary, var(--text-primary));
      }

      .trust-notice app-icon {
        flex-shrink: 0;
        margin-top: 1px;
        color: var(--warning, var(--text-primary));
      }

      /* Repeatable file editor for generic_file type */
      .generic-files-editor {
        display: flex;
        flex-direction: column;
        gap: 10px;
      }

      .generic-file-card {
        padding: 10px;
        border: 1px solid var(--border-color, var(--surface-0));
        border-radius: var(--radius-tag);
        background: var(--surface-1, transparent);
        display: flex;
        flex-direction: column;
        gap: 8px;
      }

      .generic-file-card-actions {
        display: flex;
        align-items: center;
        gap: 6px;
        align-self: flex-end;
      }

      .public-key-intro,
      .public-key-hint {
        margin: 0 0 10px;
        font-size: 13px;
        color: var(--text-primary, var(--text-primary));
      }

      .public-key-hint {
        margin-top: 10px;
        margin-bottom: 0;
        color: var(--text-muted, #6c7086);
      }

      .public-key-wrap {
        position: relative;
      }

      .public-key-block {
        font-family: 'JetBrains Mono', monospace;
        font-size: 12px;
        line-height: 1.4;
        padding: 10px 36px 10px 12px;
        margin: 0;
        background: rgba(0, 0, 0, 0.25);
        border: 1px solid var(--border-color, var(--surface-1));
        border-radius: var(--radius-tag);
        white-space: pre-wrap;
        word-break: break-all;
      }

      .public-key-copy {
        position: absolute;
        top: 4px;
        right: 4px;
      }

      .test-result {
        display: flex;
        align-items: center;
        gap: 6px;
        padding: 8px 12px;
        margin-top: 8px;
        border-radius: var(--radius-control);
        font-size: 12px;
      }

      .test-ok {
        background: var(--success-tint);
        color: var(--success);
      }

      .test-error {
        background: var(--danger-tint);
        color: var(--danger);
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
        background: var(--surface-0, var(--surface-0));
        color: var(--text-muted, #6c7086);
        font-weight: 500;
        text-transform: uppercase;
        font-size: 10px;
        letter-spacing: 0.5px;
        border-bottom: 1px solid var(--border-color, var(--surface-1));
      }

      .ds-table td {
        padding: 10px;
        border-bottom: 1px solid var(--border-color, var(--surface-0));
        color: var(--text-primary, var(--text-primary));
        vertical-align: middle;
      }

      .ds-table tbody tr:hover {
        background: var(--surface-0, var(--surface-0));
      }

      app-badge app-icon {
        margin-right: 4px;
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
        color: var(--text-secondary, var(--text-secondary));
        max-width: 220px;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }

      .mono {
        font-family: 'JetBrains Mono', monospace;
      }

      /* Action Buttons */
      .actions-cell {
        white-space: nowrap;
        text-align: right;
      }

      .actions-cell app-icon-button,
      .actions-cell .inline-test {
        vertical-align: middle;
      }

      .actions-cell app-icon-button + app-icon-button {
        margin-left: 2px;
      }

      .inline-test {
        margin-left: 4px;
      }

      .inline-test.test-ok { color: var(--success); }
      .inline-test.test-error { color: var(--danger); }
    `,
  ],
})
export class DatasourceListComponent implements OnInit {
  private readonly api = inject(ApiService);
  private readonly transloco = inject(TranslocoService);

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

  // SSH key generation state
  readonly isGeneratingKey = signal(false);
  readonly showGenerateConfirm = signal(false);
  readonly showPublicKeyDialog = signal(false);
  readonly generatedPublicKey = signal<string>('');
  readonly publicKeyCopied = signal(false);
  private copiedResetTimer: ReturnType<typeof setTimeout> | null = null;

  // Filter options
  readonly typeFilters = [
    { labelKey: 'datasources.filter.all', value: 'all' },
    { labelKey: 'datasources.filter.generic', value: 'generic' },
    { labelKey: 'datasources.filter.repository', value: 'repository' },
    { labelKey: 'datasources.filter.postgresql', value: 'postgresql' },
    { labelKey: 'datasources.filter.neo4j', value: 'neo4j' },
    { labelKey: 'datasources.filter.mongodb', value: 'mongodb' },
    { labelKey: 'datasources.filter.webdav', value: 'webdav' },
    { labelKey: 'datasources.filter.kubeconfig', value: 'kubeconfig' },
    { labelKey: 'datasources.filter.ssh_key', value: 'ssh_key' },
    { labelKey: 'datasources.filter.generic_file', value: 'generic_file' },
  ];

  // Types whose credentials are materialized as files on the agent (rather
  // than env vars or live connections). No connection URL, no test button.
  readonly credentialFileTypes: DatasourceType[] = ['kubeconfig', 'ssh_key', 'generic_file'];

  isCredentialFileType(type: DatasourceType | string = this.formData.type): boolean {
    return this.credentialFileTypes.includes(type as DatasourceType);
  }

  /** True for types that connect to something with a URL (everything except credential-files). */
  hasConnectionUrl(): boolean {
    return !this.isCredentialFileType();
  }

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

  // Credential-file form state. Reused for editing too: when the user opens
  // an edit form, the contents stay blank (credentials never come back from
  // the API), so the user re-pastes if they want to replace the stored value.
  kubeconfigContent = '';
  genericFiles: Array<{
    name: string;
    target_path: string;
    contents: string;
    mode: string;
    env_var: string;
  }> = [];

  // Whether the form can be saved (method, not computed, because formData is a plain object)
  canSave(): boolean {
    if (!this.formData.name) return false;
    if (this.formData.type === 'generic') {
      return !!(this.formData.description);
    }
    if (this.formData.type === 'kubeconfig') {
      // On edit, leaving contents blank means "keep existing".
      return this.editingId() !== null || !!this.kubeconfigContent.trim();
    }
    if (this.formData.type === 'ssh_key') {
      return this.editingId() !== null || !!this.gitSshKey.trim();
    }
    if (this.formData.type === 'generic_file') {
      if (this.editingId() !== null) return true;
      // At least one file with both target_path and contents.
      return this.genericFiles.some(
        (f) => f.target_path.trim() && f.contents.length > 0,
      );
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
    this.cancelGenerateSshKey();
    this.closePublicKeyDialog();
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
    // F3 (docs/multi_tenancy.md): credentials never come back from the
    // API. The user re-enters them only if they want to change the stored
    // value — submitting blank fields leaves the secret untouched.
    this.formCredentials = { username: '', password: '' };
    // Repository auth: pick the right tab based on cli_hint so the user
    // sees the same auth method they'd previously configured, but the
    // SSH key field stays blank for the same "leave blank to keep" reason.
    if (ds.type === 'repository') {
      this.gitAuthMethod = ds.cli_hint?.includes('ssh') ? 'ssh' : 'token';
      this.gitSshKey = '';
    } else {
      this.gitAuthMethod = 'token';
      this.gitSshKey = '';
    }
    // Generic env vars also live in credentials; keep editing UX consistent.
    this.envVars = [];
    // Credential-file types: contents never come back from the API (F3
    // redaction), so the textareas stay blank. The user re-pastes only
    // if they want to replace the stored value.
    this.kubeconfigContent = '';
    this.genericFiles = [];
    this.editingId.set(ds.id);
    this.showForm.set(true);
    this.formTestResult.set(null);
    this.cancelGenerateSshKey();
    this.closePublicKeyDialog();
  }

  closeForm(): void {
    this.showForm.set(false);
    this.editingId.set(null);
    this.formTestResult.set(null);
    this.cancelGenerateSshKey();
    this.closePublicKeyDialog();
    this.resetFormData();
  }

  // ===== SSH Key Generation =====

  /**
   * Entry point for the Generate button. Asks for confirmation if the
   * textarea already has content (so a hand-pasted key in progress isn't
   * silently wiped); otherwise generates immediately.
   */
  onGenerateSshKeyClick(): void {
    if (this.isGeneratingKey()) return;
    if (this.gitSshKey.trim()) {
      this.showGenerateConfirm.set(true);
      return;
    }
    void this.runGenerateSshKey();
  }

  confirmGenerateSshKey(): void {
    this.showGenerateConfirm.set(false);
    void this.runGenerateSshKey();
  }

  cancelGenerateSshKey(): void {
    this.showGenerateConfirm.set(false);
  }

  closePublicKeyDialog(): void {
    this.showPublicKeyDialog.set(false);
    this.publicKeyCopied.set(false);
    if (this.copiedResetTimer) {
      clearTimeout(this.copiedResetTimer);
      this.copiedResetTimer = null;
    }
  }

  copyPublicKey(): void {
    const text = this.generatedPublicKey();
    if (!text) return;
    const finish = () => {
      this.publicKeyCopied.set(true);
      if (this.copiedResetTimer) clearTimeout(this.copiedResetTimer);
      this.copiedResetTimer = setTimeout(() => this.publicKeyCopied.set(false), 2500);
    };
    if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(text).then(finish).catch((err) => {
        console.error('Clipboard write failed:', err);
      });
    } else {
      // Fallback for environments without the async clipboard API
      try {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
        finish();
      } catch (err) {
        console.error('Clipboard fallback failed:', err);
      }
    }
  }

  private runGenerateSshKey(): Promise<void> {
    this.isGeneratingKey.set(true);
    const comment = this.buildSshKeyComment();
    return new Promise((resolve) => {
      this.api.generateSshKey(comment).subscribe((result) => {
        this.isGeneratingKey.set(false);
        if (!result) {
          this.errorMessage.set(
            this.transloco.translate('datasources.sshKeyDialog.generateFailed'),
          );
          resolve();
          return;
        }
        this.gitSshKey = result.private_key;
        this.generatedPublicKey.set(result.public_key);
        this.publicKeyCopied.set(false);
        this.showPublicKeyDialog.set(true);
        resolve();
      });
    });
  }

  private buildSshKeyComment(): string {
    const name = (this.formData.name || '').trim();
    return name ? `${name} (srw)` : 'srw';
  }

  onTypeChange(): void {
    // Reset URL placeholder trigger
    this.formTestResult.set(null);
  }

  onTypeSelect(value: DatasourceType | null): void {
    if (value) {
      this.formData.type = value;
      this.onTypeChange();
    }
  }

  onGitAuthMethodChange(value: string | null): void {
    this.gitAuthMethod = value === 'ssh' ? 'ssh' : 'token';
  }

  dsTypeTone(type: DatasourceType | string): BadgeTone {
    switch (type) {
      case 'generic':
        return 'accent';
      case 'repository':
      case 'postgresql':
        return 'info';
      case 'neo4j':
        return 'success';
      case 'webdav':
        return 'warning';
      case 'mongodb':
        return 'neutral';
      case 'kubeconfig':
      case 'ssh_key':
      case 'generic_file':
        // Credential-file types share a tone so the table groups them visually.
        return 'warning';
      default:
        return 'neutral';
    }
  }

  saveForm(): void {
    if (!this.formData.name) return;

    // Warn when creating a repository without credentials
    if (this.formData.type === 'repository' && this.buildCredentials()?.['read_only']) {
      const confirmed = confirm(this.transloco.translate('datasources.messages.repoNoAuthConfirm'));
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
            this.successMessage.set(this.transloco.translate('datasources.messages.updated'));
            this.closeForm();
            this.refresh();
          } else {
            this.errorMessage.set(this.transloco.translate('datasources.messages.updateFailed'));
          }
        },
        error: () => {
          this.isSaving.set(false);
          this.errorMessage.set(this.transloco.translate('datasources.messages.updateError'));
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
          this.successMessage.set(this.transloco.translate('datasources.messages.created', {name: result.name}));
          this.closeForm();
          this.refresh();
        },
        error: (err) => {
          this.isSaving.set(false);
          const detail = err?.error?.detail;
          this.errorMessage.set(detail || this.transloco.translate('datasources.messages.createFailed'));
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
          this.formTestResult.set({ status: 'error', message: this.transloco.translate('datasources.messages.testFailed') });
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
                this.formTestResult.set({ status: 'error', message: this.transloco.translate('datasources.messages.testRequestFailed') });
              },
            });
          } else {
            this.errorMessage.set(this.transloco.translate('datasources.messages.createForTestFailed'));
          }
        },
        error: () => {
          this.isSaving.set(false);
          this.errorMessage.set(this.transloco.translate('datasources.messages.createError'));
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
          [id]: { status: 'error' as const, message: this.transloco.translate('datasources.messages.testFailed') },
        }));
      },
    });
  }

  deleteDatasource(ds: Datasource): void {
    this.clearMessages();
    this.api.deleteDatasource(ds.id).subscribe({
      next: (result) => {
        if (result) {
          this.successMessage.set(this.transloco.translate('datasources.messages.deleted', {name: ds.name}));
          this.refresh();
        } else {
          this.errorMessage.set(this.transloco.translate('datasources.messages.deleteFailed'));
        }
      },
      error: () => {
        this.errorMessage.set(this.transloco.translate('datasources.messages.deleteError'));
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
      kubeconfig: 'rocket_launch',
      ssh_key: 'key',
      generic_file: 'description',
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
    // F3: when editing, blank credentials mean "leave existing alone"
    // (the API never returns credentials, so the form can't show them
    // back). Returning undefined skips the credentials column in the
    // PUT body so the orchestrator preserves the stored secret.
    const isEditing = this.editingId() !== null;
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
        if (!this.gitSshKey) return isEditing ? undefined : { read_only: true };
        return { auth_method: 'ssh', ssh_key: this.gitSshKey };
      } else {
        if (!this.formCredentials.password) {
          return isEditing ? undefined : { read_only: true };
        }
        return { auth_method: 'token', token: this.formCredentials.password };
      }
    }
    if (this.formData.type === 'kubeconfig') {
      const contents = this.kubeconfigContent;
      if (!contents.trim()) return isEditing ? undefined : { files: [] };
      const file: CredentialFileEntry = { contents };
      return { files: [file] };
    }
    if (this.formData.type === 'ssh_key') {
      const contents = this.gitSshKey;
      if (!contents.trim()) return isEditing ? undefined : { files: [] };
      const file: CredentialFileEntry = { contents };
      return { files: [file] };
    }
    if (this.formData.type === 'generic_file') {
      const entries: CredentialFileEntry[] = [];
      for (const f of this.genericFiles) {
        if (!f.target_path.trim() || f.contents.length === 0) continue;
        const entry: CredentialFileEntry = {
          contents: f.contents,
          target_path: f.target_path.trim(),
        };
        if (f.name.trim()) entry.name = f.name.trim();
        if (f.mode.trim()) entry.mode = f.mode.trim();
        if (f.env_var.trim()) entry.env_var = f.env_var.trim();
        entries.push(entry);
      }
      if (entries.length === 0) return isEditing ? undefined : { files: [] };
      return { files: entries };
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
    this.kubeconfigContent = '';
    this.genericFiles = [];
  }

  // ===== Generic file editor =====

  addGenericFile(): void {
    if (this.genericFiles.length >= 5) return;
    this.genericFiles.push({
      name: '',
      target_path: '',
      contents: '',
      mode: '0600',
      env_var: '',
    });
  }

  removeGenericFile(index: number): void {
    this.genericFiles.splice(index, 1);
  }

  // ===== File upload (reads selected file into a textarea client-side) =====

  /** Read a user-selected file into the target text variable. No multipart upload. */
  onUploadFile(evt: Event, target: 'kubeconfig' | 'ssh_key' | { file: number }): void {
    const input = evt.target as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;
    // Hard-cap at 64KB to match the backend validator; surface a clear error otherwise.
    if (file.size > 64 * 1024) {
      this.errorMessage.set(
        this.transloco.translate('datasources.messages.fileTooLarge') ||
          'File exceeds 64 KB',
      );
      input.value = '';
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      const text = typeof reader.result === 'string' ? reader.result : '';
      if (target === 'kubeconfig') {
        this.kubeconfigContent = text;
      } else if (target === 'ssh_key') {
        this.gitSshKey = text;
      } else if (typeof target === 'object' && 'file' in target) {
        const idx = target.file;
        if (this.genericFiles[idx]) {
          this.genericFiles[idx].contents = text;
          if (!this.genericFiles[idx].name) {
            this.genericFiles[idx].name = file.name;
          }
        }
      }
      input.value = '';
    };
    reader.onerror = () => {
      this.errorMessage.set('Failed to read file');
      input.value = '';
    };
    reader.readAsText(file);
  }

  private clearMessages(): void {
    this.successMessage.set(null);
    this.errorMessage.set(null);
  }
}
