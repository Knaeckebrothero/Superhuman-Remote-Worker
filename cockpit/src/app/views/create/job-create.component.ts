import {Component, computed, effect, ElementRef, inject, OnInit, signal, ViewChild} from '@angular/core';
import {ActivatedRoute, Router} from '@angular/router';
import {ApiService} from '../../core/services/api.service';
import {FileHandlingService} from '../../core/services/file-handling.service';
import {JobArtifactService} from '../../core/services/job-artifact.service';
import {UserService} from '../../core/services/user.service';
import {Datasource, Expert, ExpertDetail, JobCreateRequest, Project} from '../../core/models/api.model';
import {FilePreview, UploadStatus} from '../../core/models/file.model';
import {AgentSettingsComponent} from '../agent-settings/agent-settings.component';
import {PRIORITY_LEVELS} from '../agent-settings/agent-settings.types';
import {ModelService} from '../../core/services/model.service';
import {TranslocoPipe} from '@jsverse/transloco';
import {AppButtonComponent} from '../../ui/button';
import {AppIconButtonComponent} from '../../ui/icon-button';
import {AppSelectComponent} from '../../ui/select';
import {AppTextareaComponent} from '../../ui/textarea';
import {AppIconComponent} from '../../ui/icon';
import {AppSpinnerComponent} from '../../ui/spinner';
import {AppFormFieldComponent} from '../../ui/form-field';
import {AppTooltipDirective} from '../../ui/tooltip';

/**
 * Job Create component for submitting new jobs with file upload support.
 */
@Component({
  selector: 'app-job-create',
  standalone: true,
  imports: [
    AgentSettingsComponent,
    TranslocoPipe,
    AppButtonComponent,
    AppIconButtonComponent,
    AppSelectComponent,
    AppTextareaComponent,
    AppIconComponent,
    AppSpinnerComponent,
    AppFormFieldComponent,
    AppTooltipDirective,
  ],
  template: `
    <div class="job-create-container">
      <div class="header-bar">
        <span class="title">{{ 'jobs.create.title' | transloco }}</span>
        <app-button variant="secondary" size="sm" class="back-btn" (clicked)="cancel()">
          {{ 'jobs.create.backToJobs' | transloco }}
        </app-button>
      </div>

      <div class="form-container">
        <!-- Success Message -->
        @if (successMessage()) {
          <div class="success-message">
            <span>{{ successMessage() }}</span>
            <app-button variant="ghost" size="sm" (clicked)="clearSuccess()">
              {{ 'jobs.create.dismiss' | transloco }}
            </app-button>
          </div>
        }

        <!-- Error Message -->
        @if (errorMessage()) {
          <div class="error-message">
            <span>{{ errorMessage() }}</span>
            <app-button variant="ghost" size="sm" (clicked)="clearError()">
              {{ 'jobs.create.dismiss' | transloco }}
            </app-button>
          </div>
        }

        <form (submit)="$event.preventDefault(); onSubmit()">
          <!-- Project Selector -->
          @if (projects().length > 0) {
            <app-form-field [label]="'jobs.create.projectLabel' | transloco" [hint]="'jobs.create.projectHint' | transloco">
              <app-select
                [value]="selectedProjectId() ?? ''"
                (changed)="onProjectIdChange($event)"
                [disabled]="isSubmitting()"
              >
                <option value="">{{ 'jobs.create.projectNone' | transloco }}</option>
                @for (proj of projects(); track proj.id) {
                  <option [value]="proj.id">
                    {{ proj.name }}@if (proj.is_default) { {{ 'jobs.create.projectPersonal' | transloco }}}
                  </option>
                }
              </app-select>
            </app-form-field>
          }

          <!-- Description Field (Required) -->
          <app-form-field [label]="'jobs.create.descriptionLabel' | transloco" [required]="true" [hint]="'jobs.create.descriptionHint' | transloco">
            <app-textarea
              [value]="formData.description"
              (valueChange)="onDescriptionEdit($event)"
              [required]="true"
              [rows]="6"
              [placeholder]="'jobs.create.descriptionPlaceholder' | transloco"
              [disabled]="isSubmitting()"
            />
          </app-form-field>

          <!-- Kickoff Message (optional opening prompt) -->
          <app-form-field [label]="'jobs.create.kickoffLabel' | transloco" [hint]="'jobs.create.kickoffHint' | transloco">
            <app-textarea
              [value]="kickoffMessage"
              (valueChange)="kickoffMessage = $event"
              [rows]="3"
              [placeholder]="'jobs.create.kickoffPlaceholder' | transloco"
              [disabled]="isSubmitting()"
            />
          </app-form-field>

          <!-- Expert Selector -->
          <div class="form-group">
            <label class="form-label">{{ 'jobs.create.expertLabel' | transloco }}</label>
            @if (isLoadingExperts()) {
              <div class="expert-loading">
                <app-spinner size="sm" />
                {{ 'jobs.create.expertLoading' | transloco }}
              </div>
            } @else if (experts().length > 0) {
              <div class="expert-grid">
                @for (expert of experts(); track expert.id) {
                  <button
                    type="button"
                    class="expert-card"
                    [class.selected]="selectedExpert()?.id === expert.id"
                    [appTooltip]="expert.description"
                    tooltipPlacement="top"
                    (click)="toggleExpert(expert)"
                    [disabled]="isSubmitting()"
                  >
                    @if (selectedExpert()?.id === expert.id) {
                      <app-icon size="lg" class="expert-check">check_circle</app-icon>
                    }
                    <app-icon size="inherit" class="expert-icon">{{ expert.icon }}</app-icon>
                    <span class="expert-name">{{ expert.display_name }}</span>
                    <span class="expert-desc">{{ expert.description }}</span>
                    @if (expert.tags.length > 0) {
                      <div class="expert-tags">
                        @for (tag of expert.tags; track tag) {
                          <span class="expert-tag">{{ tag }}</span>
                        }
                      </div>
                    }
                  </button>
                }
              </div>
            }
            <span class="field-hint">
              @if (selectedExpert()) {
                {{ 'jobs.create.expertSelectedPrefix' | transloco }} {{ selectedExpert()!.display_name }}
              } @else {
                {{ 'jobs.create.expertHintUnselected' | transloco }}
              }
            </span>
          </div>

          <!-- File Upload Dropzone -->
          <app-form-field [label]="'jobs.create.documentsLabel' | transloco" [hint]="'jobs.create.documentsOptional' | transloco">
            <div
              class="dropzone"
              [class.dragover]="isDragOver()"
              [class.has-files]="filePreviews().length > 0"
              [class.disabled]="isSubmitting()"
              (dragover)="onDragOver($event)"
              (dragleave)="onDragLeave($event)"
              (drop)="onDrop($event)"
              (click)="triggerFileInput()"
            >
              @if (filePreviews().length === 0) {
                <div class="dropzone-content">
                  <app-icon size="inherit" class="dropzone-icon">upload_file</app-icon>
                  <span class="dropzone-text">{{ 'jobs.create.dropHint' | transloco }}</span>
                  <span class="dropzone-hint">{{ 'jobs.create.maxHint' | transloco:{ maxFiles: fileService.getMaxFiles(), maxSizeMb: fileService.getMaxFileSizeMB() } }}</span>
                </div>
              } @else {
                <div class="file-list">
                  @for (file of filePreviews(); track file.id) {
                    <div class="file-item" [class.uploading]="file.uploadStatus === 'uploading'" [class.failed]="file.uploadStatus === 'failed'">
                      @if (file.type === 'image' && file.preview) {
                        <img [src]="file.preview" class="file-thumb" alt="">
                      } @else {
                        <app-icon size="lg" class="file-icon">{{ fileService.getFileIcon(file.type) }}</app-icon>
                      }
                      <div class="file-info">
                        <span class="file-name">{{ file.name }}</span>
                        <span class="file-size">{{ file.sizeFormatted }}</span>
                        @if (file.error) {
                          <span class="file-error">{{ file.error }}</span>
                        }
                      </div>
                      @if (file.uploadStatus === 'uploading') {
                        <div class="upload-progress">
                          <div class="progress-bar" [style.width.%]="file.uploadProgress || 0"></div>
                        </div>
                      }
                      @if (file.uploadStatus === 'completed') {
                        <app-icon size="md" class="status-icon success">check_circle</app-icon>
                      }
                      @if (file.uploadStatus === 'failed') {
                        <app-icon size="md" class="status-icon error">error</app-icon>
                      }
                      @if (file.uploadStatus === 'pending') {
                        <app-icon size="md" class="status-icon pending">schedule</app-icon>
                      }
                      <app-icon-button
                        variant="danger"
                        size="sm"
                        type="button"
                        [ariaLabel]="'jobs.create.removeFile' | transloco"
                        [disabled]="isSubmitting()"
                        (clicked)="removeFile(file.id, $event)"
                      >
                        <app-icon size="sm">close</app-icon>
                      </app-icon-button>
                    </div>
                  }
                </div>
                @if (!isSubmitting()) {
                  <app-button
                    type="button"
                    variant="ghost"
                    size="sm"
                    [fullWidth]="true"
                    class="add-more-btn"
                    (clicked)="triggerFileInput()"
                  >
                    {{ 'jobs.create.addMore' | transloco }}
                  </app-button>
                }
              }
            </div>
            <input
              #fileInput
              type="file"
              multiple
              accept=".pdf,.doc,.docx,.txt,.md,.png,.jpg,.jpeg,.gif,.webp,.zip"
              (change)="onFilesSelected($event)"
              style="display: none"
            >
          </app-form-field>

          <!-- Priority -->
          <app-form-field [label]="'jobs.create.priorityLabel' | transloco" [hint]="'jobs.create.priorityHint' | transloco">
            <app-select
              [value]="selectedPriority()"
              (changed)="onPriorityChange($event)"
              [disabled]="isSubmitting()"
            >
              @for (level of priorityLevels; track level.value) {
                <option [value]="level.value">{{ getPriorityKey(level.value) | transloco }}</option>
              }
            </app-select>
          </app-form-field>

          <!-- Cloud Storage Access Override -->
          @if (selectedProjectHasCloudStorage()) {
            <app-form-field [label]="'jobs.create.cloudStorageLabel' | transloco" [hint]="'jobs.create.cloudStorageHint' | transloco">
              <app-select
                [value]="cloudStorageOverride()"
                (changed)="onCloudStorageChange($event)"
                [disabled]="isSubmitting()"
              >
                <option value="inherit">{{ 'jobs.create.cloudStorageInherit' | transloco }}</option>
                <option value="readonly">{{ 'jobs.create.cloudStorageReadonly' | transloco }}</option>
                <option value="readwrite">{{ 'jobs.create.cloudStorageReadwrite' | transloco }}</option>
              </app-select>
            </app-form-field>
          }

          <!-- Agent Settings (tabbed: Settings / Instructions / Advanced) -->
          <app-agent-settings
            mode="job"
            [config]="expertDetail()?.config ?? frameworkDefaults() ?? {}"
            [disabled]="isSubmitting()"
            [showProjectMemory]="projectHasSharedMemory()"
            [defaultsTools]="expertDetail()?.defaults_tools ?? {}"
            [settingsMatrix]="expertDetail()?.settings_matrix ?? frameworkSettingsMatrix()"
            [datasources]="availableDatasources()"
            [loadingDatasources]="isLoadingDatasources()"
            [loadingExpert]="isLoadingExpertDetail()"
            (instructionsChange)="onInstructionsChange($event)"
          />

          <!-- Submit Button -->
          <div class="form-actions">
            <app-button
              type="button"
              variant="secondary"
              [disabled]="isSubmitting()"
              (clicked)="resetForm()"
            >
              {{ 'jobs.create.reset' | transloco }}
            </app-button>
            <app-button
              type="submit"
              variant="primary"
              [loading]="isSubmitting() || isUploading()"
              [disabled]="!formData.description"
            >
              @if (isSubmitting()) {
                {{ 'jobs.create.creating' | transloco }}
              } @else if (isUploading()) {
                {{ 'jobs.create.uploading' | transloco }}
              } @else {
                {{ 'jobs.create.submit' | transloco }}
              }
            </app-button>
          </div>
        </form>
      </div>
    </div>
  `,
  styles: [
    `
      :host {
        display: block;
        height: 100%;
        overflow: hidden;
      }

      .job-create-container {
        display: flex;
        flex-direction: column;
        height: 100%;
        background: var(--panel-bg, var(--panel-bg));
      }

      /* Header */
      .header-bar {
        display: flex;
        align-items: center;
        padding: 10px 12px;
        background: var(--panel-header-bg, #1e1e2e);
        border-bottom: 1px solid var(--border-color, var(--surface-0));
        flex-shrink: 0;
      }

      .back-btn {
        margin-left: auto;
      }

      .title {
        font-weight: 600;
        color: var(--text-primary, var(--text-primary));
      }

      /* Form Container */
      .form-container {
        flex: 1;
        overflow: auto;
        padding: 16px;
        container-type: inline-size;
      }

      /* Messages */
      .success-message,
      .error-message {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 10px 14px;
        border-radius: var(--radius-control);
        margin-bottom: 16px;
        font-size: 13px;
      }

      .success-message {
        background: var(--success-tint);
        border: 1px solid var(--success-tint);
        color: var(--success);
      }

      .error-message {
        background: var(--danger-tint);
        border: 1px solid var(--danger-tint);
        color: var(--danger);
      }

      /* Form Groups */
      .form-group {
        margin-bottom: 16px;
      }

      .form-label {
        display: block;
        margin-bottom: 6px;
        font-size: 12px;
        font-weight: 500;
        color: var(--text-primary, var(--text-primary));
      }

      .required {
        color: var(--danger);
      }

      .instructions-actions {
        display: flex;
        gap: 8px;
        margin-top: 4px;
      }

      .btn-text {
        padding: 2px 8px;
        border: none;
        border-radius: var(--radius-control);
        background: transparent;
        color: var(--text-muted, #6c7086);
        font-size: 11px;
        cursor: pointer;
        transition: all 0.15s ease;
      }

      .btn-text:hover:not(:disabled) {
        color: var(--accent-color, var(--accent-color));
        background: color-mix(in srgb, var(--accent-color) 20%, transparent);
      }

      .btn-text:disabled {
        opacity: 0.5;
        cursor: not-allowed;
      }

      .inline-spinner {
        display: inline-block;
        vertical-align: middle;
        margin-left: 6px;
      }

      /* Model preset chips */
      .preset-chips {
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
      }

      .preset-chip {
        padding: 6px 14px;
        border: 1px solid var(--border-color, var(--surface-1));
        border-radius: var(--radius-pill);
        background: var(--surface-0, var(--surface-0));
        color: var(--text-secondary, var(--text-secondary));
        font-size: 12px;
        font-family: inherit;
        cursor: pointer;
        transition: all 0.15s ease;
      }

      .preset-chip:hover:not(:disabled) {
        border-color: var(--accent-color, var(--accent-color));
        color: var(--text-primary, var(--text-primary));
      }

      .preset-chip.active {
        border-color: var(--accent-color, var(--accent-color));
        background: color-mix(in srgb, var(--accent-color) 20%, transparent);
        color: var(--accent-color, var(--accent-color));
      }

      .preset-chip:disabled {
        opacity: 0.5;
        cursor: not-allowed;
      }

      /* Slider */
      .slider-row {
        display: flex;
        align-items: center;
        gap: 10px;
      }

      .slider-label {
        font-size: 11px;
        color: var(--text-muted, #6c7086);
        min-width: 14px;
        text-align: center;
      }

      .form-range {
        flex: 1;
        height: 4px;
        -webkit-appearance: none;
        appearance: none;
        background: var(--border-color, var(--surface-1));
        border-radius: var(--radius-pill);
        outline: none;
      }

      .form-range::-webkit-slider-thumb {
        -webkit-appearance: none;
        width: 16px;
        height: 16px;
        border-radius: 50%;
        background: var(--accent-color, var(--accent-color));
        cursor: pointer;
      }

      .form-range::-moz-range-thumb {
        width: 16px;
        height: 16px;
        border-radius: 50%;
        background: var(--accent-color, var(--accent-color));
        cursor: pointer;
        border: none;
      }

      .form-range:disabled {
        opacity: 0.5;
      }

      /* Phase cards */
      .phase-cards {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 12px;
        margin-bottom: 16px;
      }

      @container (max-width: 600px) {
        .phase-cards {
          grid-template-columns: 1fr;
        }
      }

      .phase-card {
        border: 1px solid var(--border-color, var(--surface-1));
        border-radius: var(--radius-surface);
        background: var(--surface-0, var(--surface-0));
        padding: 14px;
        min-width: 0;
      }

      .phase-card-header {
        display: flex;
        align-items: center;
        gap: 6px;
        font-size: 12px;
        font-weight: 600;
        color: var(--accent-color, var(--accent-color));
        margin-bottom: 12px;
      }

      .form-group.compact {
        margin-bottom: 12px;
      }

      .multimodal-toggle {
        display: flex;
        align-items: center;
        gap: 8px;
        font-size: 12px;
        color: var(--text-secondary, var(--text-secondary));
        cursor: pointer;
        padding: 4px 0;
      }

      .multimodal-toggle input[type="checkbox"] {
        margin: 0;
        accent-color: var(--accent-color, var(--accent-color));
      }

      .memory-toggle {
        display: flex;
        align-items: center;
        gap: 8px;
        font-size: 13px;
        color: var(--text, var(--text-primary));
        cursor: pointer;
      }

      .memory-toggle input[type="checkbox"] {
        margin: 0;
        accent-color: var(--accent-color, var(--accent-color));
      }

      .subjob-detail {
        display: flex;
        align-items: center;
        gap: 8px;
        margin-top: 6px;
        margin-left: 24px;
      }

      .compact-label {
        margin-bottom: 0 !important;
        white-space: nowrap;
      }

      .compact-select {
        width: auto;
        min-width: 140px;
        padding: 6px 28px 6px 10px !important;
        font-size: 12px;
      }

      /* Select dropdown */
      select.form-input {
        cursor: pointer;
        -webkit-appearance: none;
        appearance: none;
        background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%236c7086' d='M6 8L1 3h10z'/%3E%3C/svg%3E");
        background-repeat: no-repeat;
        background-position: right 12px center;
        padding-right: 32px;
      }

      /* Tool toggles */
      .tool-toggles {
        display: flex;
        flex-direction: column;
        gap: 6px;
      }

      .tool-toggle {
        display: flex;
        align-items: center;
        gap: 10px;
        padding: 8px 10px;
        border: 1px solid var(--border-color, var(--surface-1));
        border-radius: var(--radius-control);
        background: var(--surface-0, var(--surface-0));
        cursor: pointer;
        transition: all 0.15s ease;
      }

      .tool-toggle:hover:not(.disabled) {
        border-color: var(--accent-color, var(--accent-color));
        background: color-mix(in srgb, var(--accent-color) 20%, transparent);
      }

      .tool-toggle.disabled {
        opacity: 0.6;
        cursor: not-allowed;
      }

      .tool-toggle input[type="checkbox"] {
        margin: 0;
        accent-color: var(--accent-color, var(--accent-color));
      }

      .tool-toggle-info {
        flex: 1;
        min-width: 0;
      }

      .tool-toggle-name {
        display: block;
        font-size: 12px;
        font-weight: 500;
        color: var(--text-primary, var(--text-primary));
      }

      .tool-toggle-desc {
        display: block;
        font-size: 11px;
        color: var(--text-muted, #6c7086);
      }

      .field-hint {
        display: block;
        margin-top: 4px;
        font-size: 11px;
        color: var(--text-muted, #6c7086);
      }

      .field-error {
        display: block;
        margin-top: 4px;
        font-size: 11px;
        color: var(--danger);
      }

      .field-warning {
        display: block;
        margin-top: 4px;
        font-size: 11px;
        color: var(--ctp-yellow, var(--warning));
      }

      /* Expert Selector */
      .expert-loading {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 16px;
        color: var(--text-muted, #6c7086);
        font-size: 13px;
      }

      .expert-grid {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
        gap: 10px;
      }

      @container (max-width: 400px) {
        .expert-grid {
          grid-template-columns: 1fr;
        }
      }

      .expert-card {
        position: relative;
        display: flex;
        flex-direction: column;
        align-items: flex-start;
        gap: 6px;
        padding: 14px;
        min-width: 0;
        overflow: hidden;
        border: 1px solid var(--border-color, var(--surface-1));
        border-radius: var(--radius-surface);
        background: var(--surface-0, var(--surface-0));
        cursor: pointer;
        text-align: left;
        transition: all 0.15s ease;
        font-family: inherit;
        color: var(--text-primary, var(--text-primary));
      }

      .expert-card:hover:not(:disabled) {
        border-color: var(--accent-color);
        background: color-mix(in srgb, var(--accent-color) 20%, transparent);
      }

      .expert-card.selected {
        border-color: var(--accent-color);
        background: color-mix(in srgb, var(--accent-color) 20%, transparent);
        box-shadow: 0 0 0 1px var(--accent-color);
      }

      .expert-card:disabled {
        opacity: 0.6;
        cursor: not-allowed;
      }

      .expert-check {
        position: absolute;
        top: 8px;
        right: 8px;
        color: var(--accent-color);
      }

      .expert-icon {
        font-size: 28px;
        color: color-mix(in srgb, var(--accent-color) 25%, var(--text-secondary) 75%);
      }

      .expert-name {
        font-size: 13px;
        font-weight: 600;
        color: var(--text-primary, var(--text-primary));
        max-width: 100%;
        overflow-wrap: break-word;
      }

      .expert-desc {
        font-size: 11px;
        color: var(--text-muted, #6c7086);
        line-height: 1.4;
        display: -webkit-box;
        -webkit-line-clamp: 2;
        -webkit-box-orient: vertical;
        overflow: hidden;
        overflow-wrap: break-word;
        max-width: 100%;
      }

      .expert-tags {
        display: flex;
        flex-wrap: wrap;
        gap: 4px;
        margin-top: 2px;
      }

      .expert-tag {
        font-size: 10px;
        padding: 1px 6px;
        border-radius: var(--radius-tag);
        background: color-mix(in srgb, var(--accent-color) 8%, transparent);
        color: color-mix(in srgb, var(--accent-color) 35%, var(--text-secondary) 65%);
      }

      /* Dropzone */
      .dropzone {
        border: 2px dashed var(--border-color, var(--surface-1));
        border-radius: var(--radius-surface);
        padding: 20px;
        text-align: center;
        cursor: pointer;
        transition: all 0.2s ease;
        background: var(--surface-0, var(--surface-0));
      }

      .dropzone:hover:not(.disabled) {
        border-color: var(--accent-color, var(--accent-color));
        background: color-mix(in srgb, var(--accent-color) 20%, transparent);
      }

      .dropzone.dragover {
        border-color: var(--accent-color, var(--accent-color));
        background: color-mix(in srgb, var(--accent-color) 20%, transparent);
      }

      .dropzone.has-files {
        text-align: left;
        padding: 12px;
      }

      .dropzone.disabled {
        opacity: 0.6;
        cursor: not-allowed;
      }

      .dropzone-content {
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 8px;
      }

      .dropzone-icon {
        font-size: 48px;
        color: var(--text-muted, #6c7086);
      }

      .dropzone-text {
        font-size: 14px;
        color: var(--text-primary, var(--text-primary));
      }

      .dropzone-hint {
        font-size: 12px;
        color: var(--text-muted, #6c7086);
      }

      /* File List */
      .file-list {
        display: flex;
        flex-direction: column;
        gap: 8px;
      }

      .file-item {
        display: flex;
        align-items: center;
        gap: 10px;
        padding: 8px 10px;
        background: var(--panel-bg, var(--panel-bg));
        border-radius: var(--radius-control);
        border: 1px solid var(--border-color, var(--surface-0));
      }

      .file-item.uploading {
        border-color: var(--ctp-yellow, var(--warning));
      }

      .file-item.failed {
        border-color: var(--danger);
      }

      .file-thumb {
        width: 36px;
        height: 36px;
        object-fit: cover;
        border-radius: var(--radius-tag);
      }

      .file-icon {
        font-size: 28px;
        color: var(--text-muted, #6c7086);
        width: 36px;
        text-align: center;
      }

      .file-info {
        flex: 1;
        min-width: 0;
        display: flex;
        flex-direction: column;
        gap: 2px;
      }

      .file-name {
        font-size: 13px;
        color: var(--text-primary, var(--text-primary));
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }

      .file-size {
        font-size: 11px;
        color: var(--text-muted, #6c7086);
      }

      .file-error {
        font-size: 11px;
        color: var(--danger);
      }

      .upload-progress {
        width: 60px;
        height: 4px;
        background: var(--border-color, var(--surface-0));
        border-radius: var(--radius-pill);
        overflow: hidden;
      }

      .progress-bar {
        height: 100%;
        background: var(--ctp-yellow, var(--warning));
        transition: width 0.2s ease;
      }

      .status-icon {
        font-size: 20px;
      }

      .status-icon.success {
        color: var(--success);
      }

      .status-icon.error {
        color: var(--danger);
      }

      .status-icon.pending {
        color: var(--text-muted, #6c7086);
      }

      app-button.add-more-btn {
        display: block;
        margin-top: 8px;
      }

      /* Advanced Section */
      .toggle-advanced {
        display: flex;
        align-items: center;
        justify-content: center;
        gap: 6px;
        width: 100%;
        padding: 10px;
        margin-bottom: 16px;
        border: 1px dashed var(--border-color, var(--surface-1));
        border-radius: var(--radius-control);
        background: transparent;
        color: var(--text-muted, #6c7086);
        font-size: 12px;
        cursor: pointer;
        transition: all 0.15s ease;
      }

      .toggle-advanced:hover:not(:disabled) {
        border-color: var(--accent-color, var(--accent-color));
        color: var(--accent-color, var(--accent-color));
      }

      .toggle-advanced:disabled {
        opacity: 0.6;
        cursor: not-allowed;
      }

      .advanced-section {
        padding: 16px;
        margin-bottom: 16px;
        background: rgba(0, 0, 0, 0.2);
        border-radius: var(--radius-surface);
        border: 1px solid var(--border-color, var(--surface-0));
      }

      /* Datasource Picker */
      .ds-loading,
      .ds-empty {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 12px;
        color: var(--text-muted, #6c7086);
        font-size: 12px;
      }

      .ds-picker {
        display: flex;
        flex-direction: column;
        gap: 6px;
      }

      .ds-option {
        display: flex;
        align-items: center;
        gap: 10px;
        padding: 10px 12px;
        border: 1px solid var(--border-color, var(--surface-1));
        border-radius: var(--radius-control);
        background: var(--surface-0, var(--surface-0));
        cursor: pointer;
        transition: all 0.15s ease;
      }

      .ds-option:hover {
        border-color: var(--accent-color, var(--accent-color));
        background: color-mix(in srgb, var(--accent-color) 20%, transparent);
      }

      .ds-option.selected {
        border-color: var(--accent-color, var(--accent-color));
        background: color-mix(in srgb, var(--accent-color) 20%, transparent);
      }

      .ds-option input[type="checkbox"] {
        margin: 0;
        accent-color: var(--accent-color, var(--accent-color));
      }


      .ds-info {
        flex: 1;
        min-width: 0;
      }

      .ds-name {
        display: block;
        font-size: 13px;
        font-weight: 500;
        color: var(--text-primary, var(--text-primary));
      }

      .ds-desc {
        display: block;
        font-size: 11px;
        color: var(--text-muted, #6c7086);
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }

      .ds-ro-badge {
        padding: 2px 6px;
        border-radius: var(--radius-tag);
        font-size: 10px;
        font-weight: 600;
        background: var(--info-tint);
        color: var(--info);
      }

      /* Form Actions */
      .form-actions {
        display: flex;
        justify-content: flex-end;
        gap: 10px;
        margin-top: 20px;
        padding-top: 16px;
        border-top: 1px solid var(--border-color, var(--surface-0));
      }

      /* Narrow panel responsive overrides */
      @container (max-width: 360px) {
        .form-container {
          padding: 10px;
        }

        .advanced-section {
          padding: 10px;
        }

        .expert-card {
          padding: 10px;
        }

        .tool-toggle {
          padding: 6px 8px;
        }

        .slider-row {
          gap: 6px;
        }

        .form-actions {
          flex-direction: column;
        }

        .form-actions app-button {
          width: 100%;
        }
      }
    `,
  ],
})
export class JobCreateComponent implements OnInit {
  private readonly api = inject(ApiService);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly userService = inject(UserService);
  private readonly modelService = inject(ModelService);
  readonly fileService = inject(FileHandlingService);
  readonly artifacts = inject(JobArtifactService);

  @ViewChild('fileInput') fileInput!: ElementRef<HTMLInputElement>;
  @ViewChild(AgentSettingsComponent) agentSettings!: AgentSettingsComponent;

  constructor() {
    // Mirror artifact form-state (expert prefill / user edits) into the
    // settings component. The description effect below also writes back
    // formData.description, so it is load-bearing for the manual form.
    effect(() => {
      const instructions = this.artifacts.instructions();
      if (instructions !== null) {
        this.agentSettings?.instructionsTab?.setContent(instructions);
      }
    });
    effect(() => {
      const description = this.artifacts.description();
      if (description !== null && description !== this.formData.description) {
        this.formData.description = description;
      }
    });
    effect(() => {
      const config = this.artifacts.config();
      if (config && this.agentSettings) {
        this.agentSettings.prefillFromConfig(config);
      }
    });
    effect(() => {
      const userId = this.userService.currentUserId();
      if (userId) {
        this.loadProjects(userId);
      }
    });
  }

  readonly isSubmitting = signal(false);
  readonly isUploading = signal(false);
  readonly isDragOver = signal(false);
  readonly successMessage = signal<string | null>(null);
  readonly errorMessage = signal<string | null>(null);
  readonly filePreviews = signal<FilePreview[]>([]);

  readonly experts = signal<Expert[]>([]);
  readonly selectedExpert = signal<Expert | null>(null);
  readonly isLoadingExperts = signal(false);
  readonly expertDetail = signal<ExpertDetail | null>(null);
  readonly isLoadingExpertDetail = signal(false);

  readonly selectedPriority = signal<number>(5);
  readonly priorityLevels = PRIORITY_LEVELS;

  getPriorityKey(value: number): string {
    if (value === 0) return 'jobs.create.priorityLow';
    if (value === 10) return 'jobs.create.priorityHigh';
    return 'jobs.create.priorityNormal';
  }

  onPriorityChange(value: unknown): void {
    if (value == null || value === '') return;
    const num = typeof value === 'number' ? value : parseInt(String(value), 10);
    if (!Number.isNaN(num)) {
      this.selectedPriority.set(num);
    }
  }

  onProjectIdChange(value: string | null): void {
    this.selectedProjectId.set(value && value !== '' ? value : null);
    // Refresh eligible datasources for the newly selected project.
    this.loadDatasources();
  }

  onCloudStorageChange(value: string | null): void {
    if (value === 'inherit' || value === 'readonly' || value === 'readwrite') {
      this.cloudStorageOverride.set(value);
    }
  }

  readonly projects = signal<Project[]>([]);
  readonly selectedProjectId = signal<string | null>(null);

  readonly cloudStorageOverride = signal<'inherit' | 'readonly' | 'readwrite'>('inherit');
  readonly selectedProjectHasCloudStorage = computed(() => {
    const pid = this.selectedProjectId();
    if (!pid) return false;
    const proj = this.projects().find((p) => p.id === pid);
    return !!proj?.cloud_storage_url;
  });

  readonly availableDatasources = signal<Datasource[]>([]);
  readonly isLoadingDatasources = signal(false);

  readonly frameworkDefaults = signal<Record<string, unknown> | null>(null);
  readonly frameworkSettingsMatrix = signal<Record<string, Record<string, unknown>>>({});
  readonly projectHasSharedMemory = computed(() => {
    const pid = this.selectedProjectId();
    if (!pid) return false;
    const proj = this.projects().find((p) => p.id === pid);
    if (!proj) return false;
    const override = proj.default_config_override as Record<string, any> | null;
    const val = override?.['memory']?.['project_scoped'];
    if (typeof val === 'boolean') return val;
    const defaults = this.frameworkDefaults();
    const defaultVal = (defaults?.['memory'] as any)?.['project_scoped'];
    return typeof defaultVal === 'boolean' ? defaultVal : true;
  });

  private uploadId: string | null = null;
  kickoffMessage = '';
  formData: JobCreateRequest = { description: '' };

  ngOnInit(): void {
    this.modelService.load();
    this.loadExperts();
    this.loadDatasources();
    this.api.getExpertDetail('defaults').subscribe((d) => {
      if (d?.config) this.frameworkDefaults.set(d.config);
      if (d?.settings_matrix) this.frameworkSettingsMatrix.set(d.settings_matrix);
    });
  }

  private loadExperts(): void {
    this.isLoadingExperts.set(true);
    this.api.getExperts().subscribe({
      next: (experts) => { this.experts.set(experts); this.isLoadingExperts.set(false); },
      error: () => { this.isLoadingExperts.set(false); },
    });
  }

  toggleExpert(expert: Expert): void {
    if (this.selectedExpert()?.id === expert.id) {
      this.selectedExpert.set(null);
      this.expertDetail.set(null);
      this.artifacts.instructions.set(null);
      this.agentSettings?.resetAll();
    } else {
      this.selectedExpert.set(expert);
      this.fetchExpertDetail(expert.id);
    }
  }

  private fetchExpertDetail(expertId: string): void {
    this.isLoadingExpertDetail.set(true);
    this.api.getExpertDetail(expertId).subscribe({
      next: (detail) => {
        this.expertDetail.set(detail);
        if (detail?.instructions) {
          this.agentSettings?.instructionsTab?.setFromExpert(detail.instructions);
          this.artifacts.instructions.set(detail.instructions);
        }
        if (detail?.config) {
          this.agentSettings?.prefillFromConfig(detail.config);
        }
        this.isLoadingExpertDetail.set(false);
      },
      error: () => { this.isLoadingExpertDetail.set(false); },
    });
  }

  onDescriptionEdit(value: string): void {
    this.artifacts.description.set(value || null);
  }

  onInstructionsChange(value: string | null): void {
    this.artifacts.instructions.set(value);
  }

  private loadDatasources(): void {
    this.isLoadingDatasources.set(true);
    // Eligible = owner + global + linked to the selected project. The picker
    // pre-selects these; explicit-only resolution attaches exactly what stays
    // checked. Re-fetched whenever the project changes.
    const pid = this.selectedProjectId();
    this.api.getEligibleDatasources(pid ? [pid] : []).subscribe({
      next: (datasources) => { this.availableDatasources.set(datasources); this.isLoadingDatasources.set(false); },
      error: () => { this.isLoadingDatasources.set(false); },
    });
  }

  private loadProjects(userId: string): void {
    this.api.getProjects(userId).subscribe((projects) => {
      this.projects.set(projects);
      const qp = this.route.snapshot.queryParamMap.get('project');
      if (qp && projects.some((p) => p.id === qp)) {
        this.selectedProjectId.set(qp);
      } else {
        const defaultProject = projects.find((p) => p.is_default);
        this.selectedProjectId.set(defaultProject?.id ?? projects[0]?.id ?? null);
      }
      // Now that a project is selected, refresh eligible datasources so
      // project-linked sources are included and pre-selected.
      this.loadDatasources();
    });
  }

  // ===== File Upload Methods =====

  triggerFileInput(): void {
    if (!this.isSubmitting()) this.fileInput.nativeElement.click();
  }

  onDragOver(event: DragEvent): void {
    event.preventDefault();
    event.stopPropagation();
    if (!this.isSubmitting()) this.isDragOver.set(true);
  }

  onDragLeave(event: DragEvent): void {
    event.preventDefault();
    event.stopPropagation();
    this.isDragOver.set(false);
  }

  async onDrop(event: DragEvent): Promise<void> {
    event.preventDefault();
    event.stopPropagation();
    this.isDragOver.set(false);
    if (this.isSubmitting()) return;
    const files = event.dataTransfer?.files;
    if (files && files.length > 0) await this.addFiles(Array.from(files));
  }

  async onFilesSelected(event: Event): Promise<void> {
    const input = event.target as HTMLInputElement;
    if (input.files && input.files.length > 0) {
      await this.addFiles(Array.from(input.files));
      input.value = '';
    }
  }

  private async addFiles(files: File[]): Promise<void> {
    const currentCount = this.filePreviews().length;
    const maxFiles = this.fileService.getMaxFiles();
    if (currentCount + files.length > maxFiles) {
      this.errorMessage.set(`Maximum ${maxFiles} files allowed`);
      files = files.slice(0, maxFiles - currentCount);
    }
    if (files.length === 0) return;
    const newPreviews = await this.fileService.createFilePreviews(files);
    this.filePreviews.update((current) => [...current, ...newPreviews]);
    this.uploadId = null;
  }

  removeFile(fileId: string, event: Event): void {
    event.stopPropagation();
    this.filePreviews.update((current) => current.filter((f) => f.id !== fileId));
    this.uploadId = null;
  }

  // ===== Form Submission =====

  async onSubmit(): Promise<void> {
    if (!this.formData.description || this.isSubmitting() || this.isUploading()) return;
    this.clearMessages();

    const files = this.filePreviews();
    if (files.length > 0 && !this.uploadId) {
      if (!(await this.uploadFiles())) return;
    }

    this.isSubmitting.set(true);
    const request: JobCreateRequest = { description: this.formData.description };

    const expert = this.selectedExpert();
    if (expert && expert.id !== 'defaults') {
      // DB-backed experts (source user/global) go via expert_id — the
      // orchestrator resolves them into the job config. Bundled experts keep
      // the config_name path. Fixes the config_name=<uuid> conflation.
      if (expert.source === 'user' || expert.source === 'global') {
        request.expert_id = expert.id;
      } else {
        request.config_name = expert.id;
      }
    }
    if (this.uploadId) request.upload_id = this.uploadId;

    // Collect overrides from the settings component
    const configOverride = this.agentSettings?.getOverrides();
    if (configOverride && Object.keys(configOverride).length > 0) {
      request.config_override = configOverride;
    }

    const instructions = this.agentSettings?.getInstructions();
    if (instructions) request.instructions = instructions;
    if (this.kickoffMessage.trim()) request.kickoff_message = this.kickoffMessage.trim();

    const dsIds = this.agentSettings?.getSelectedDatasourceIds() ?? [];
    if (dsIds.length > 0) request.datasource_ids = dsIds;

    const projectId = this.selectedProjectId();
    if (projectId) request.project_id = projectId;

    const priority = this.selectedPriority();
    if (priority !== 5) request.priority = priority;

    // Cloud storage override
    const csOverride = this.cloudStorageOverride();
    if (csOverride !== 'inherit') {
      request.context = {
        ...(request.context ?? {}),
        cloud_storage_read_only: csOverride === 'readonly',
      };
    }

    const currentUserId = this.userService.currentUserId();
    if (currentUserId) request.user_id = currentUserId;

    this.api.createJob(request).subscribe({
      next: (job) => {
        this.isSubmitting.set(false);
        if (job) {
          this.successMessage.set(`Job created successfully! ID: ${job.id.slice(0, 8)}...`);
          this.resetForm();
        } else {
          this.errorMessage.set('Failed to create job. Please try again.');
        }
      },
      error: (err) => {
        this.isSubmitting.set(false);
        this.errorMessage.set(`Error: ${err.message || 'Unknown error'}`);
      },
    });
  }

  private async uploadFiles(): Promise<boolean> {
    const previews = this.filePreviews();
    const filesToUpload = previews.filter((p) => p.uploadStatus !== UploadStatus.COMPLETED);
    if (filesToUpload.length === 0) return true;

    this.isUploading.set(true);
    this.filePreviews.update((current) =>
      current.map((f) => ({ ...f, uploadStatus: UploadStatus.UPLOADING, uploadProgress: 0 })),
    );

    try {
      const files = filesToUpload.map((p) => p.file);
      const response = await new Promise<{ upload_id: string } | null>((resolve, reject) => {
        this.api.uploadFiles(files).subscribe({ next: resolve, error: reject });
      });
      if (response) {
        this.uploadId = response.upload_id;
        this.filePreviews.update((current) =>
          current.map((f) => ({ ...f, uploadStatus: UploadStatus.COMPLETED, uploadProgress: 100 })),
        );
        this.isUploading.set(false);
        return true;
      } else {
        throw new Error('Upload failed');
      }
    } catch {
      this.filePreviews.update((current) =>
        current.map((f) => ({ ...f, uploadStatus: UploadStatus.FAILED, error: 'Upload failed' })),
      );
      this.isUploading.set(false);
      this.errorMessage.set('Failed to upload files. Please try again.');
      return false;
    }
  }

  resetForm(): void {
    this.formData = { description: '' };
    this.kickoffMessage = '';
    this.filePreviews.set([]);
    this.uploadId = null;
    this.selectedExpert.set(null);
    this.expertDetail.set(null);
    this.selectedPriority.set(5);
    this.cloudStorageOverride.set('inherit');
    this.agentSettings?.resetAll();
    const defaultProject = this.projects().find((p) => p.is_default);
    this.selectedProjectId.set(defaultProject?.id ?? this.projects()[0]?.id ?? null);
    this.artifacts.reset();
  }

  clearSuccess(): void { this.successMessage.set(null); }
  clearError(): void { this.errorMessage.set(null); }
  private clearMessages(): void { this.successMessage.set(null); this.errorMessage.set(null); }

  /** Leave the create form and return to the job list. */
  cancel(): void {
    this.router.navigate(['/jobs']);
  }
}
