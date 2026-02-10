import { Component, inject, signal, effect, ElementRef, ViewChild, OnInit } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ApiService } from '../../core/services/api.service';
import { FileHandlingService } from '../../core/services/file-handling.service';
import { JobArtifactService } from '../../core/services/job-artifact.service';
import { JobCreateRequest, Expert, ExpertDetail, Datasource, DatasourceType } from '../../core/models/api.model';
import { FilePreview, FileType, UploadStatus } from '../../core/models/file.model';
import { environment } from '../../core/environment';

/**
 * Job Create component for submitting new jobs with file upload support.
 */
@Component({
  selector: 'app-job-create',
  standalone: true,
  imports: [FormsModule],
  template: `
    <div class="job-create-container">
      <div class="header-bar">
        <span class="title">Create New Job</span>
      </div>

      <div class="form-container">
        <!-- Success Message -->
        @if (successMessage()) {
          <div class="success-message">
            <span>{{ successMessage() }}</span>
            <button class="dismiss-btn" (click)="clearSuccess()">Dismiss</button>
          </div>
        }

        <!-- Error Message -->
        @if (errorMessage()) {
          <div class="error-message">
            <span>{{ errorMessage() }}</span>
            <button class="dismiss-btn" (click)="clearError()">Dismiss</button>
          </div>
        }

        <form (ngSubmit)="onSubmit()" #jobForm="ngForm">
          <!-- Description Field (Required) -->
          <div class="form-group">
            <label for="description" class="form-label">
              Description <span class="required">*</span>
            </label>
            <textarea
              id="description"
              name="description"
              class="form-textarea"
              [(ngModel)]="formData.description"
              (ngModelChange)="onDescriptionEdit($event)"
              required
              rows="6"
              placeholder="Describe what the agent should accomplish..."
              [disabled]="isSubmitting() || artifacts.streaming()"
            ></textarea>
            <span class="field-hint">Describe what you want the agent to do</span>
          </div>

          <!-- Expert Selector -->
          <div class="form-group">
            <label class="form-label">Agent Expert</label>
            @if (isLoadingExperts()) {
              <div class="expert-loading">
                <span class="spinner-small"></span>
                Loading experts...
              </div>
            } @else if (experts().length > 0) {
              <div class="expert-grid">
                @for (expert of experts(); track expert.id) {
                  <button
                    type="button"
                    class="expert-card"
                    [class.selected]="selectedExpert()?.id === expert.id"
                    [style.--expert-color]="expert.color"
                    (click)="toggleExpert(expert)"
                    [disabled]="isSubmitting()"
                  >
                    @if (selectedExpert()?.id === expert.id) {
                      <span class="expert-check">check_circle</span>
                    }
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
                  </button>
                }
              </div>
            }
            <span class="field-hint">
              @if (selectedExpert()) {
                Selected: {{ selectedExpert()!.display_name }}
              } @else {
                Select an expert profile or leave unselected for the default agent
              }
            </span>
          </div>

          <!-- File Upload Dropzone -->
          <div class="form-group">
            <label class="form-label">Documents</label>
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
                  <span class="dropzone-icon">upload_file</span>
                  <span class="dropzone-text">Drop files here or click to browse</span>
                  <span class="dropzone-hint">Max {{ fileService.getMaxFiles() }} files, {{ fileService.getMaxFileSizeMB() }}MB each</span>
                </div>
              } @else {
                <div class="file-list">
                  @for (file of filePreviews(); track file.id) {
                    <div class="file-item" [class.uploading]="file.uploadStatus === 'uploading'" [class.failed]="file.uploadStatus === 'failed'">
                      @if (file.type === 'image' && file.preview) {
                        <img [src]="file.preview" class="file-thumb" alt="">
                      } @else {
                        <span class="file-icon">{{ fileService.getFileIcon(file.type) }}</span>
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
                        <span class="status-icon success">check_circle</span>
                      }
                      @if (file.uploadStatus === 'failed') {
                        <span class="status-icon error">error</span>
                      }
                      @if (file.uploadStatus === 'pending') {
                        <span class="status-icon pending">schedule</span>
                      }
                      <button type="button" class="remove-btn" (click)="removeFile(file.id, $event)" [disabled]="isSubmitting()">
                        close
                      </button>
                    </div>
                  }
                </div>
                @if (!isSubmitting()) {
                  <button type="button" class="add-more-btn" (click)="triggerFileInput()">
                    + Add more files
                  </button>
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
            <span class="field-hint">Optional: Upload documents for the agent to process</span>
          </div>

          <!-- Advanced Section Toggle -->
          <button
            type="button"
            class="toggle-advanced"
            (click)="showAdvanced.set(!showAdvanced())"
            [disabled]="isSubmitting()"
          >
            <span class="toggle-text">{{ showAdvanced() ? 'Hide' : 'Show' }} Advanced Options</span>
            <span class="toggle-icon">{{ showAdvanced() ? 'expand_less' : 'expand_more' }}</span>
          </button>

          @if (showAdvanced()) {
            <div class="advanced-section">
              <!-- Model Settings -->
              <div class="form-group">
                <label class="form-label">Model</label>
                <div class="model-combo">
                  <input
                    type="text"
                    class="form-input"
                    [ngModel]="configModel()"
                    (ngModelChange)="configModel.set($event)"
                    name="configModel"
                    placeholder="Select or type a model name..."
                    list="modelList"
                    [disabled]="isSubmitting()"
                    autocomplete="off"
                  >
                  <datalist id="modelList">
                    @for (group of availableModels; track group.group) {
                      @for (model of group.models; track model) {
                        <option [value]="model">{{ group.group }}</option>
                      }
                    }
                  </datalist>
                </div>
                <span class="field-hint">
                  @if (getExpertDefault('llm.model'); as defaultModel) {
                    Expert default: {{ defaultModel }}
                  } @else {
                    Leave empty to use the expert's default model
                  }
                </span>
              </div>

              <!-- Temperature -->
              <div class="form-group">
                <label class="form-label">
                  Temperature: {{ configTemperature() !== null ? configTemperature() : '(default)' }}
                </label>
                <div class="slider-row">
                  <span class="slider-label">0</span>
                  <input
                    type="range"
                    class="form-range"
                    min="0"
                    max="2"
                    step="0.1"
                    [ngModel]="configTemperature() ?? getExpertDefault('llm.temperature') ?? 0"
                    (ngModelChange)="onTemperatureChange($event)"
                    name="configTemperature"
                    [disabled]="isSubmitting()"
                  >
                  <span class="slider-label">2</span>
                </div>
                <span class="field-hint">Controls randomness. 0 = deterministic, higher = more creative</span>
              </div>

              <!-- Reasoning Level -->
              <div class="form-group">
                <label class="form-label">Reasoning Level</label>
                <select
                  class="form-input"
                  [ngModel]="configReasoning()"
                  (ngModelChange)="configReasoning.set($event)"
                  name="configReasoning"
                  [disabled]="isSubmitting()"
                >
                  <option [ngValue]="null">Default</option>
                  <option value="none">None</option>
                  <option value="low">Low</option>
                  <option value="medium">Medium</option>
                  <option value="high">High</option>
                </select>
                <span class="field-hint">
                  @if (getExpertDefault('llm.reasoning_level'); as defaultLevel) {
                    Expert default: {{ defaultLevel }}
                  }
                </span>
              </div>

              <!-- Tool Category Toggles -->
              <div class="form-group">
                <label class="form-label">Tool Categories</label>
                <div class="tool-toggles">
                  @for (cat of toolCategories; track cat.key) {
                    <label class="tool-toggle" [class.disabled]="isSubmitting()">
                      <input
                        type="checkbox"
                        [checked]="isToolCategoryEnabled(cat.key)"
                        (change)="toggleToolCategory(cat.key)"
                        [disabled]="isSubmitting()"
                      >
                      <span class="tool-toggle-icon">{{ cat.icon }}</span>
                      <span class="tool-toggle-info">
                        <span class="tool-toggle-name">{{ cat.label }}</span>
                        <span class="tool-toggle-desc">{{ cat.description }}</span>
                      </span>
                    </label>
                  }
                </div>
                <span class="field-hint">Enable or disable tool categories for this job</span>
              </div>

              <!-- Instructions Editor -->
              <div class="form-group">
                <label class="form-label">
                  Instructions (Markdown)
                  @if (isLoadingExpertDetail()) {
                    <span class="spinner-small inline-spinner"></span>
                  }
                </label>
                <textarea
                  id="instructions"
                  name="instructions"
                  class="form-textarea mono"
                  [ngModel]="instructionsContent()"
                  (ngModelChange)="onInstructionsEdit($event)"
                  rows="12"
                  placeholder="Select an expert to pre-fill instructions, or type custom instructions..."
                  [disabled]="isSubmitting() || isLoadingExpertDetail() || artifacts.streaming()"
                ></textarea>
                @if (artifacts.streaming()) {
                  <span class="field-hint" style="color: var(--accent-color, #cba6f7)">
                    <span class="spinner-small inline-spinner"></span>
                    AI is editing instructions...
                  </span>
                }
                <div class="instructions-actions">
                  @if (instructionsContent()) {
                    <button type="button" class="btn-text" (click)="clearInstructions()" [disabled]="isSubmitting()">
                      Clear
                    </button>
                  }
                  @if (selectedExpert() && expertDetail()) {
                    <button type="button" class="btn-text" (click)="resetInstructionsToExpert()" [disabled]="isSubmitting() || isLoadingExpertDetail()">
                      Reset to expert default
                    </button>
                  }
                </div>
                <span class="field-hint">Custom task instructions for the agent (replaces default)</span>
                @if (selectedExpert() && !instructionsContent() && !isLoadingExpertDetail()) {
                  <span class="field-warning">
                    No instructions set — the agent will use the expert's default prompt
                  </span>
                }
              </div>

              <!-- Datasource Picker -->
              <div class="form-group">
                <label class="form-label">Datasources</label>
                @if (isLoadingDatasources()) {
                  <div class="ds-loading">
                    <span class="spinner-small"></span>
                    Loading datasources...
                  </div>
                } @else if (availableDatasources().length === 0) {
                  <div class="ds-empty">No global datasources configured</div>
                } @else {
                  <div class="ds-picker">
                    @for (ds of availableDatasources(); track ds.id) {
                      <label
                        class="ds-option"
                        [class.selected]="selectedDatasourceIds().has(ds.id)"
                      >
                        <input
                          type="checkbox"
                          [checked]="selectedDatasourceIds().has(ds.id)"
                          (change)="toggleDatasource(ds.id)"
                          [disabled]="isSubmitting()"
                        >
                        <span class="ds-type-icon" [class]="'ds-type-' + ds.type">
                          {{ getDsTypeIcon(ds.type) }}
                        </span>
                        <span class="ds-info">
                          <span class="ds-name">{{ ds.name }}</span>
                          @if (ds.description) {
                            <span class="ds-desc">{{ ds.description }}</span>
                          }
                        </span>
                        @if (ds.read_only) {
                          <span class="ds-ro-badge">RO</span>
                        }
                      </label>
                    }
                  </div>
                }
                <span class="field-hint">Select external databases the agent can access during this job</span>
              </div>
            </div>
          }

          <!-- Submit Button -->
          <div class="form-actions">
            <button
              type="button"
              class="btn btn-secondary"
              (click)="resetForm()"
              [disabled]="isSubmitting()"
            >
              Reset
            </button>
            <button
              type="submit"
              class="btn btn-primary"
              [disabled]="!formData.description || isSubmitting() || isUploading() || artifacts.streaming()"
            >
              @if (isSubmitting()) {
                <span class="spinner-small"></span>
                Creating...
              } @else if (isUploading()) {
                <span class="spinner-small"></span>
                Uploading...
              } @else {
                Create Job
              }
            </button>
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
        background: var(--panel-bg, #181825);
      }

      /* Header */
      .header-bar {
        display: flex;
        align-items: center;
        padding: 10px 12px;
        background: var(--panel-header-bg, #1e1e2e);
        border-bottom: 1px solid var(--border-color, #313244);
        flex-shrink: 0;
      }

      .title {
        font-weight: 600;
        color: var(--text-primary, #cdd6f4);
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
        border-radius: 6px;
        margin-bottom: 16px;
        font-size: 13px;
      }

      .success-message {
        background: rgba(166, 227, 161, 0.15);
        border: 1px solid rgba(166, 227, 161, 0.3);
        color: #a6e3a1;
      }

      .error-message {
        background: rgba(243, 139, 168, 0.15);
        border: 1px solid rgba(243, 139, 168, 0.3);
        color: #f38ba8;
      }

      .dismiss-btn {
        padding: 4px 8px;
        border: none;
        border-radius: 4px;
        background: rgba(255, 255, 255, 0.1);
        color: inherit;
        font-size: 11px;
        cursor: pointer;
      }

      .dismiss-btn:hover {
        background: rgba(255, 255, 255, 0.2);
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
        color: var(--text-primary, #cdd6f4);
      }

      .required {
        color: #f38ba8;
      }

      .form-input,
      .form-textarea {
        width: 100%;
        padding: 10px 12px;
        border: 1px solid var(--border-color, #45475a);
        border-radius: 6px;
        background: var(--surface-0, #313244);
        color: var(--text-primary, #cdd6f4);
        font-family: inherit;
        font-size: 13px;
        transition: border-color 0.15s ease;
      }

      .form-input:focus,
      .form-textarea:focus {
        outline: none;
        border-color: var(--accent-color, #cba6f7);
      }

      .form-input:disabled,
      .form-textarea:disabled {
        opacity: 0.6;
        cursor: not-allowed;
      }

      .form-input::placeholder,
      .form-textarea::placeholder {
        color: var(--text-muted, #6c7086);
      }

      .form-textarea {
        resize: vertical;
        min-height: 80px;
      }

      .form-textarea.mono {
        font-family: 'JetBrains Mono', monospace;
        font-size: 12px;
        line-height: 1.5;
      }

      .instructions-actions {
        display: flex;
        gap: 8px;
        margin-top: 4px;
      }

      .btn-text {
        padding: 2px 8px;
        border: none;
        border-radius: 4px;
        background: transparent;
        color: var(--text-muted, #6c7086);
        font-size: 11px;
        cursor: pointer;
        transition: all 0.15s ease;
      }

      .btn-text:hover:not(:disabled) {
        color: var(--accent-color, #cba6f7);
        background: rgba(203, 166, 247, 0.1);
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

      /* Model combo-box */
      .model-combo {
        position: relative;
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
        background: var(--border-color, #45475a);
        border-radius: 2px;
        outline: none;
      }

      .form-range::-webkit-slider-thumb {
        -webkit-appearance: none;
        width: 16px;
        height: 16px;
        border-radius: 50%;
        background: var(--accent-color, #cba6f7);
        cursor: pointer;
      }

      .form-range::-moz-range-thumb {
        width: 16px;
        height: 16px;
        border-radius: 50%;
        background: var(--accent-color, #cba6f7);
        cursor: pointer;
        border: none;
      }

      .form-range:disabled {
        opacity: 0.5;
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
        border: 1px solid var(--border-color, #45475a);
        border-radius: 6px;
        background: var(--surface-0, #313244);
        cursor: pointer;
        transition: all 0.15s ease;
      }

      .tool-toggle:hover:not(.disabled) {
        border-color: var(--accent-color, #cba6f7);
        background: rgba(203, 166, 247, 0.05);
      }

      .tool-toggle.disabled {
        opacity: 0.6;
        cursor: not-allowed;
      }

      .tool-toggle input[type="checkbox"] {
        margin: 0;
        accent-color: var(--accent-color, #cba6f7);
      }

      .tool-toggle-icon {
        font-family: 'Material Symbols Outlined';
        font-size: 20px;
        color: var(--text-muted, #6c7086);
        width: 24px;
        text-align: center;
      }

      .tool-toggle-info {
        flex: 1;
        min-width: 0;
      }

      .tool-toggle-name {
        display: block;
        font-size: 12px;
        font-weight: 500;
        color: var(--text-primary, #cdd6f4);
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
        color: #f38ba8;
      }

      .field-warning {
        display: block;
        margin-top: 4px;
        font-size: 11px;
        color: var(--ctp-yellow, #f9e2af);
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
        border: 1px solid var(--border-color, #45475a);
        border-radius: 8px;
        background: var(--surface-0, #313244);
        cursor: pointer;
        text-align: left;
        transition: all 0.15s ease;
        font-family: inherit;
        color: var(--text-primary, #cdd6f4);
      }

      .expert-card:hover:not(:disabled) {
        border-color: var(--expert-color, #cba6f7);
        background: rgba(203, 166, 247, 0.05);
      }

      .expert-card.selected {
        border-color: var(--expert-color, #cba6f7);
        background: rgba(203, 166, 247, 0.08);
        box-shadow: 0 0 0 1px var(--expert-color, #cba6f7);
      }

      .expert-card:disabled {
        opacity: 0.6;
        cursor: not-allowed;
      }

      .expert-check {
        position: absolute;
        top: 8px;
        right: 8px;
        font-family: 'Material Symbols Outlined';
        font-size: 18px;
        color: var(--expert-color, #cba6f7);
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

      .expert-tags {
        display: flex;
        flex-wrap: wrap;
        gap: 4px;
        margin-top: 2px;
      }

      .expert-tag {
        font-size: 10px;
        padding: 1px 6px;
        border-radius: 3px;
        background: rgba(205, 214, 244, 0.08);
        color: var(--text-muted, #6c7086);
      }

      /* Dropzone */
      .dropzone {
        border: 2px dashed var(--border-color, #45475a);
        border-radius: 8px;
        padding: 20px;
        text-align: center;
        cursor: pointer;
        transition: all 0.2s ease;
        background: var(--surface-0, #313244);
      }

      .dropzone:hover:not(.disabled) {
        border-color: var(--accent-color, #cba6f7);
        background: rgba(203, 166, 247, 0.05);
      }

      .dropzone.dragover {
        border-color: var(--accent-color, #cba6f7);
        background: rgba(203, 166, 247, 0.1);
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
        font-family: 'Material Symbols Outlined';
        font-size: 48px;
        color: var(--text-muted, #6c7086);
      }

      .dropzone-text {
        font-size: 14px;
        color: var(--text-primary, #cdd6f4);
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
        background: var(--panel-bg, #181825);
        border-radius: 6px;
        border: 1px solid var(--border-color, #313244);
      }

      .file-item.uploading {
        border-color: var(--ctp-yellow, #f9e2af);
      }

      .file-item.failed {
        border-color: #f38ba8;
      }

      .file-thumb {
        width: 36px;
        height: 36px;
        object-fit: cover;
        border-radius: 4px;
      }

      .file-icon {
        font-family: 'Material Symbols Outlined';
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
        color: var(--text-primary, #cdd6f4);
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
        color: #f38ba8;
      }

      .upload-progress {
        width: 60px;
        height: 4px;
        background: var(--border-color, #313244);
        border-radius: 2px;
        overflow: hidden;
      }

      .progress-bar {
        height: 100%;
        background: var(--ctp-yellow, #f9e2af);
        transition: width 0.2s ease;
      }

      .status-icon {
        font-family: 'Material Symbols Outlined';
        font-size: 20px;
      }

      .status-icon.success {
        color: #a6e3a1;
      }

      .status-icon.error {
        color: #f38ba8;
      }

      .status-icon.pending {
        color: var(--text-muted, #6c7086);
      }

      .remove-btn {
        font-family: 'Material Symbols Outlined';
        font-size: 18px;
        padding: 4px;
        border: none;
        border-radius: 4px;
        background: transparent;
        color: var(--text-muted, #6c7086);
        cursor: pointer;
        line-height: 1;
      }

      .remove-btn:hover:not(:disabled) {
        background: rgba(255, 255, 255, 0.1);
        color: #f38ba8;
      }

      .remove-btn:disabled {
        opacity: 0.5;
        cursor: not-allowed;
      }

      .add-more-btn {
        display: block;
        width: 100%;
        margin-top: 8px;
        padding: 8px;
        border: 1px dashed var(--border-color, #45475a);
        border-radius: 6px;
        background: transparent;
        color: var(--text-muted, #6c7086);
        font-size: 12px;
        cursor: pointer;
        transition: all 0.15s ease;
      }

      .add-more-btn:hover {
        border-color: var(--accent-color, #cba6f7);
        color: var(--accent-color, #cba6f7);
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
        border: 1px dashed var(--border-color, #45475a);
        border-radius: 6px;
        background: transparent;
        color: var(--text-muted, #6c7086);
        font-size: 12px;
        cursor: pointer;
        transition: all 0.15s ease;
      }

      .toggle-advanced:hover:not(:disabled) {
        border-color: var(--accent-color, #cba6f7);
        color: var(--accent-color, #cba6f7);
      }

      .toggle-advanced:disabled {
        opacity: 0.6;
        cursor: not-allowed;
      }

      .toggle-icon {
        font-family: 'Material Symbols Outlined';
        font-size: 18px;
      }

      .advanced-section {
        padding: 16px;
        margin-bottom: 16px;
        background: rgba(0, 0, 0, 0.2);
        border-radius: 8px;
        border: 1px solid var(--border-color, #313244);
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
        border: 1px solid var(--border-color, #45475a);
        border-radius: 6px;
        background: var(--surface-0, #313244);
        cursor: pointer;
        transition: all 0.15s ease;
      }

      .ds-option:hover {
        border-color: var(--accent-color, #cba6f7);
        background: rgba(203, 166, 247, 0.05);
      }

      .ds-option.selected {
        border-color: var(--accent-color, #cba6f7);
        background: rgba(203, 166, 247, 0.08);
      }

      .ds-option input[type="checkbox"] {
        margin: 0;
        accent-color: var(--accent-color, #cba6f7);
      }

      .ds-type-icon {
        font-family: 'Material Symbols Outlined';
        font-size: 20px;
        width: 24px;
        text-align: center;
      }

      .ds-type-postgresql { color: #89b4fa; }
      .ds-type-neo4j { color: #a6e3a1; }
      .ds-type-mongodb { color: #94e2d5; }

      .ds-info {
        flex: 1;
        min-width: 0;
      }

      .ds-name {
        display: block;
        font-size: 13px;
        font-weight: 500;
        color: var(--text-primary, #cdd6f4);
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
        border-radius: 3px;
        font-size: 10px;
        font-weight: 600;
        background: rgba(137, 180, 250, 0.15);
        color: #89b4fa;
      }

      /* Form Actions */
      .form-actions {
        display: flex;
        justify-content: flex-end;
        gap: 10px;
        margin-top: 20px;
        padding-top: 16px;
        border-top: 1px solid var(--border-color, #313244);
      }

      .btn {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        padding: 10px 20px;
        border: none;
        border-radius: 6px;
        font-size: 13px;
        font-weight: 500;
        cursor: pointer;
        transition: all 0.15s ease;
      }

      .btn:disabled {
        opacity: 0.6;
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

      .spinner-small {
        width: 14px;
        height: 14px;
        border: 2px solid rgba(0, 0, 0, 0.2);
        border-top-color: currentColor;
        border-radius: 50%;
        animation: spin 0.6s linear infinite;
      }

      @keyframes spin {
        to { transform: rotate(360deg); }
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

        .tool-toggle-icon {
          display: none;
        }

        .slider-row {
          gap: 6px;
        }

        .form-actions {
          flex-direction: column;
        }

        .form-actions .btn {
          width: 100%;
          justify-content: center;
        }
      }
    `,
  ],
})
export class JobCreateComponent implements OnInit {
  private readonly api = inject(ApiService);
  readonly fileService = inject(FileHandlingService);
  readonly artifacts = inject(JobArtifactService);

  constructor() {
    // Sync artifact signals → local form state (builder → form direction)
    effect(() => {
      const instructions = this.artifacts.instructions();
      if (instructions !== null && instructions !== this.instructionsContent()) {
        this.instructionsContent.set(instructions);
      }
    });
    effect(() => {
      const description = this.artifacts.description();
      if (description !== null && description !== this.formData.description) {
        this.formData.description = description;
      }
    });
  }

  @ViewChild('fileInput') fileInput!: ElementRef<HTMLInputElement>;

  readonly isSubmitting = signal(false);
  readonly isUploading = signal(false);
  readonly isDragOver = signal(false);
  readonly successMessage = signal<string | null>(null);
  readonly errorMessage = signal<string | null>(null);
  readonly filePreviews = signal<FilePreview[]>([]);

  // Expert selector state
  readonly experts = signal<Expert[]>([]);
  readonly selectedExpert = signal<Expert | null>(null);
  readonly isLoadingExperts = signal(false);
  readonly expertDetail = signal<ExpertDetail | null>(null);
  readonly isLoadingExpertDetail = signal(false);

  // Instructions editor state
  readonly instructionsContent = signal<string | null>(null);

  // Config settings form state
  readonly configModel = signal<string | null>(null);
  readonly configTemperature = signal<number | null>(null);
  readonly configReasoning = signal<string | null>(null);
  readonly disabledToolCategories = signal<Set<string>>(new Set());

  // Model list for combo-box (loaded from env.js at runtime)
  readonly availableModels = environment.models;

  // Tool category metadata for toggles
  readonly toolCategories = [
    { key: 'research', label: 'Research', icon: 'travel_explore', description: 'Web search, paper search, browsing' },
    { key: 'citation', label: 'Citation', icon: 'format_quote', description: 'Citation and literature management' },
    { key: 'document', label: 'Document', icon: 'article', description: 'Document processing and chunking' },
    { key: 'coding', label: 'Coding', icon: 'code', description: 'Shell command execution' },
  ];

  // Advanced section state
  readonly showAdvanced = signal(false);

  // Datasource picker state
  readonly availableDatasources = signal<Datasource[]>([]);
  readonly selectedDatasourceIds = signal<Set<string>>(new Set());
  readonly isLoadingDatasources = signal(false);

  // Current upload_id after successful upload
  private uploadId: string | null = null;

  formData: JobCreateRequest = {
    description: '',
  };

  ngOnInit(): void {
    this.loadExperts();
    this.loadDatasources();
  }

  private loadExperts(): void {
    this.isLoadingExperts.set(true);
    this.api.getExperts().subscribe({
      next: (experts) => {
        this.experts.set(experts);
        this.isLoadingExperts.set(false);
      },
      error: () => {
        this.isLoadingExperts.set(false);
      },
    });
  }

  toggleExpert(expert: Expert): void {
    if (this.selectedExpert()?.id === expert.id) {
      this.selectedExpert.set(null);
      this.expertDetail.set(null);
      this.instructionsContent.set(null);
      this.artifacts.instructions.set(null);
      this.configModel.set(null);
      this.configTemperature.set(null);
      this.configReasoning.set(null);
      this.disabledToolCategories.set(new Set());
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
          this.instructionsContent.set(detail.instructions);
          this.artifacts.instructions.set(detail.instructions);
        }
        this.prefillConfigFromExpert();
        this.isLoadingExpertDetail.set(false);
      },
      error: () => {
        this.isLoadingExpertDetail.set(false);
      },
    });
  }

  /** Sync instructions edits to artifact service (form → builder direction). */
  onInstructionsEdit(value: string): void {
    this.instructionsContent.set(value);
    if (!this.artifacts.streaming()) {
      this.artifacts.instructions.set(value || null);
    }
  }

  /** Sync description edits to artifact service (form → builder direction). */
  onDescriptionEdit(value: string): void {
    if (!this.artifacts.streaming()) {
      this.artifacts.description.set(value || null);
    }
  }

  clearInstructions(): void {
    this.instructionsContent.set(null);
    this.artifacts.instructions.set(null);
  }

  resetInstructionsToExpert(): void {
    const detail = this.expertDetail();
    if (detail?.instructions) {
      this.instructionsContent.set(detail.instructions);
      this.artifacts.instructions.set(detail.instructions);
    }
  }

  // ===== Config Settings Methods =====

  /** Clamp temperature to valid 0–2 range. */
  onTemperatureChange(value: number): void {
    this.configTemperature.set(Math.round(Math.min(2, Math.max(0, value)) * 10) / 10);
  }

  getExpertDefault(path: string): unknown {
    const detail = this.expertDetail();
    if (!detail?.config) return null;
    return path.split('.').reduce((obj: any, key) => obj?.[key], detail.config) ?? null;
  }

  isToolCategoryEnabled(category: string): boolean {
    return !this.disabledToolCategories().has(category);
  }

  toggleToolCategory(category: string): void {
    this.disabledToolCategories.update((current) => {
      const next = new Set(current);
      if (next.has(category)) {
        next.delete(category);
      } else {
        next.add(category);
      }
      return next;
    });
  }

  /** Build config_override by diffing form values against expert defaults. */
  private buildConfigOverride(): Record<string, unknown> | undefined {
    const override: Record<string, unknown> = {};
    const llm: Record<string, unknown> = {};

    const model = this.configModel();
    if (model && model !== this.getExpertDefault('llm.model')) {
      llm['model'] = model;
    }

    const temp = this.configTemperature();
    if (temp !== null && temp !== this.getExpertDefault('llm.temperature')) {
      llm['temperature'] = temp;
    }

    const reasoning = this.configReasoning();
    if (reasoning !== null && reasoning !== this.getExpertDefault('llm.reasoning_level')) {
      llm['reasoning_level'] = reasoning;
    }

    if (Object.keys(llm).length > 0) {
      override['llm'] = llm;
    }

    // Tool overrides — only include disabled categories (set to empty array)
    const disabled = this.disabledToolCategories();
    if (disabled.size > 0) {
      const tools: Record<string, unknown> = {};
      disabled.forEach((cat) => {
        tools[cat] = [];
      });
      override['tools'] = tools;
    }

    return Object.keys(override).length > 0 ? override : undefined;
  }

  private prefillConfigFromExpert(): void {
    const detail = this.expertDetail();
    if (!detail?.config) {
      this.configModel.set(null);
      this.configTemperature.set(null);
      this.configReasoning.set(null);
      this.disabledToolCategories.set(new Set());
      return;
    }

    // Pre-fill from expert config (user can then change)
    const llm = detail.config['llm'] as Record<string, unknown> | undefined;
    this.configModel.set((llm?.['model'] as string) ?? null);
    this.configTemperature.set((llm?.['temperature'] as number) ?? null);
    this.configReasoning.set((llm?.['reasoning_level'] as string) ?? null);

    // Detect which tool categories are empty (disabled)
    const tools = detail.config['tools'] as Record<string, unknown[]> | undefined;
    const disabled = new Set<string>();
    if (tools) {
      for (const cat of this.toolCategories) {
        const val = tools[cat.key];
        if (Array.isArray(val) && val.length === 0) {
          disabled.add(cat.key);
        }
      }
    }
    this.disabledToolCategories.set(disabled);
  }

  // ===== Datasource Methods =====

  private loadDatasources(): void {
    this.isLoadingDatasources.set(true);
    this.api.getDatasources('global').subscribe({
      next: (datasources) => {
        this.availableDatasources.set(datasources);
        this.isLoadingDatasources.set(false);
      },
      error: () => {
        this.isLoadingDatasources.set(false);
      },
    });
  }

  toggleDatasource(id: string): void {
    this.selectedDatasourceIds.update((current) => {
      const next = new Set(current);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  }

  getDsTypeIcon(type: DatasourceType | string): string {
    const icons: Record<string, string> = {
      postgresql: 'database',
      neo4j: 'hub',
      mongodb: 'eco',
    };
    return icons[type] || 'storage';
  }

  // ===== File Upload Methods =====

  triggerFileInput(): void {
    if (!this.isSubmitting()) {
      this.fileInput.nativeElement.click();
    }
  }

  onDragOver(event: DragEvent): void {
    event.preventDefault();
    event.stopPropagation();
    if (!this.isSubmitting()) {
      this.isDragOver.set(true);
    }
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
    if (files && files.length > 0) {
      await this.addFiles(Array.from(files));
    }
  }

  async onFilesSelected(event: Event): Promise<void> {
    const input = event.target as HTMLInputElement;
    if (input.files && input.files.length > 0) {
      await this.addFiles(Array.from(input.files));
      // Clear input so same file can be selected again
      input.value = '';
    }
  }

  private async addFiles(files: File[]): Promise<void> {
    const currentCount = this.filePreviews().length;
    const maxFiles = this.fileService.getMaxFiles();

    // Check file count limit
    if (currentCount + files.length > maxFiles) {
      this.errorMessage.set(`Maximum ${maxFiles} files allowed`);
      files = files.slice(0, maxFiles - currentCount);
    }

    if (files.length === 0) return;

    // Create previews
    const newPreviews = await this.fileService.createFilePreviews(files);
    this.filePreviews.update((current) => [...current, ...newPreviews]);

    // Clear any previous upload
    this.uploadId = null;
  }

  removeFile(fileId: string, event: Event): void {
    event.stopPropagation();
    this.filePreviews.update((current) => current.filter((f) => f.id !== fileId));
    // Clear upload_id since file list changed
    this.uploadId = null;
  }

  // ===== Form Submission =====

  async onSubmit(): Promise<void> {
    if (!this.formData.description || this.isSubmitting() || this.isUploading()) {
      return;
    }

    this.clearMessages();

    // Upload document files if any
    const files = this.filePreviews();
    if (files.length > 0 && !this.uploadId) {
      const uploadSuccess = await this.uploadFiles();
      if (!uploadSuccess) {
        return;
      }
    }

    // Create job
    this.isSubmitting.set(true);

    const request: JobCreateRequest = {
      description: this.formData.description,
    };

    // Set expert config if selected (and not the defaults entry)
    const expert = this.selectedExpert();
    if (expert && expert.id !== 'defaults') {
      request.config_name = expert.id;
    }

    if (this.uploadId) {
      request.upload_id = this.uploadId;
    }
    const configOverride = this.buildConfigOverride();
    if (configOverride) {
      request.config_override = configOverride;
    }
    const instructions = this.instructionsContent();
    if (instructions) {
      request.instructions = instructions;
    }

    const dsIds = this.selectedDatasourceIds();
    if (dsIds.size > 0) {
      request.datasource_ids = Array.from(dsIds);
    }

    // Link builder session if one was started
    const builderSessionId = this.artifacts.sessionId();
    if (builderSessionId) {
      request.builder_session_id = builderSessionId;
    }

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

    if (filesToUpload.length === 0) {
      return true;
    }

    this.isUploading.set(true);

    // Mark all as uploading
    this.filePreviews.update((current) =>
      current.map((f) => ({
        ...f,
        uploadStatus: UploadStatus.UPLOADING,
        uploadProgress: 0,
      })),
    );

    try {
      const files = filesToUpload.map((p) => p.file);
      const response = await new Promise<{ upload_id: string } | null>((resolve, reject) => {
        this.api.uploadFiles(files).subscribe({
          next: (res) => resolve(res),
          error: (err) => reject(err),
        });
      });

      if (response) {
        this.uploadId = response.upload_id;

        // Mark all as completed
        this.filePreviews.update((current) =>
          current.map((f) => ({
            ...f,
            uploadStatus: UploadStatus.COMPLETED,
            uploadProgress: 100,
          })),
        );

        this.isUploading.set(false);
        return true;
      } else {
        throw new Error('Upload failed');
      }
    } catch (err) {
      // Mark all as failed
      this.filePreviews.update((current) =>
        current.map((f) => ({
          ...f,
          uploadStatus: UploadStatus.FAILED,
          error: 'Upload failed',
        })),
      );

      this.isUploading.set(false);
      this.errorMessage.set('Failed to upload files. Please try again.');
      return false;
    }
  }

  resetForm(): void {
    this.formData = {
      description: '',
    };
    this.filePreviews.set([]);
    this.uploadId = null;
    // Reset expert selection
    this.selectedExpert.set(null);
    this.expertDetail.set(null);
    // Reset instructions editor
    this.instructionsContent.set(null);
    // Reset config settings
    this.configModel.set(null);
    this.configTemperature.set(null);
    this.configReasoning.set(null);
    this.disabledToolCategories.set(new Set());
    // Reset advanced options
    this.showAdvanced.set(false);
    // Reset datasource selections
    this.selectedDatasourceIds.set(new Set());
    // Reset artifact service (clears builder session)
    this.artifacts.reset();
  }

  clearSuccess(): void {
    this.successMessage.set(null);
  }

  clearError(): void {
    this.errorMessage.set(null);
  }

  private clearMessages(): void {
    this.successMessage.set(null);
    this.errorMessage.set(null);
  }
}
