import { Component, inject, signal, ElementRef, ViewChild } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ApiService } from '../../core/services/api.service';
import { FileHandlingService } from '../../core/services/file-handling.service';
import { JobCreateRequest } from '../../core/models/api.model';
import { FilePreview, FileType, UploadStatus } from '../../core/models/file.model';

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
          <!-- Prompt Field (Required) -->
          <div class="form-group">
            <label for="prompt" class="form-label">
              Prompt <span class="required">*</span>
            </label>
            <textarea
              id="prompt"
              name="prompt"
              class="form-textarea"
              [(ngModel)]="formData.prompt"
              required
              rows="6"
              placeholder="Enter the task prompt for the agent..."
              [disabled]="isSubmitting()"
            ></textarea>
            <span class="field-hint">Describe what you want the agent to do</span>
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
              accept=".pdf,.doc,.docx,.txt,.md,.png,.jpg,.jpeg,.gif,.webp"
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
              <!-- Config Upload -->
              <div class="form-group">
                <label class="form-label">Agent Config (YAML)</label>
                <div
                  class="single-file-upload"
                  [class.has-file]="configFile()"
                  [class.disabled]="isSubmitting()"
                  (click)="triggerConfigInput()"
                >
                  @if (configFile()) {
                    <div class="file-chip">
                      <span class="chip-icon">settings</span>
                      <span class="chip-name">{{ configFile()!.name }}</span>
                      <button type="button" class="chip-remove" (click)="removeConfigFile($event)">close</button>
                    </div>
                  } @else {
                    <span class="upload-hint">Drop YAML file or click to browse</span>
                  }
                </div>
                <input
                  #configInput
                  type="file"
                  accept=".yaml,.yml"
                  (change)="onConfigFileSelected($event)"
                  style="display: none"
                >
                <span class="field-hint">Override default agent configuration settings</span>
              </div>

              <!-- Instructions Upload -->
              <div class="form-group">
                <label class="form-label">Instructions File (Markdown)</label>
                <div
                  class="single-file-upload"
                  [class.has-file]="instructionsFile()"
                  [class.disabled]="isSubmitting()"
                  (click)="triggerInstructionsInput()"
                >
                  @if (instructionsFile()) {
                    <div class="file-chip">
                      <span class="chip-icon">description</span>
                      <span class="chip-name">{{ instructionsFile()!.name }}</span>
                      <button type="button" class="chip-remove" (click)="removeInstructionsFile($event)">close</button>
                    </div>
                  } @else {
                    <span class="upload-hint">Drop .md/.txt file or click to browse</span>
                  }
                </div>
                <input
                  #instructionsInput
                  type="file"
                  accept=".md,.txt"
                  (change)="onInstructionsFileSelected($event)"
                  style="display: none"
                >
                <span class="field-hint">Custom task instructions for the agent (replaces default)</span>
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
              [disabled]="!formData.prompt || isSubmitting() || isUploading()"
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

      .single-file-upload {
        display: flex;
        align-items: center;
        justify-content: center;
        padding: 16px;
        border: 2px dashed var(--border-color, #45475a);
        border-radius: 6px;
        background: var(--surface-0, #313244);
        cursor: pointer;
        transition: all 0.15s ease;
        min-height: 50px;
      }

      .single-file-upload:hover:not(.disabled) {
        border-color: var(--accent-color, #cba6f7);
        background: rgba(203, 166, 247, 0.05);
      }

      .single-file-upload.has-file {
        border-style: solid;
        justify-content: flex-start;
      }

      .single-file-upload.disabled {
        opacity: 0.6;
        cursor: not-allowed;
      }

      .upload-hint {
        font-size: 13px;
        color: var(--text-muted, #6c7086);
      }

      .file-chip {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 6px 8px;
        background: var(--panel-bg, #181825);
        border-radius: 4px;
        border: 1px solid var(--border-color, #313244);
      }

      .chip-icon {
        font-family: 'Material Symbols Outlined';
        font-size: 18px;
        color: var(--accent-color, #cba6f7);
      }

      .chip-name {
        font-size: 12px;
        color: var(--text-primary, #cdd6f4);
        max-width: 200px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      .chip-remove {
        font-family: 'Material Symbols Outlined';
        font-size: 16px;
        padding: 2px;
        border: none;
        border-radius: 3px;
        background: transparent;
        color: var(--text-muted, #6c7086);
        cursor: pointer;
        line-height: 1;
      }

      .chip-remove:hover {
        background: rgba(255, 255, 255, 0.1);
        color: #f38ba8;
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
    `,
  ],
})
export class JobCreateComponent {
  private readonly api = inject(ApiService);
  readonly fileService = inject(FileHandlingService);

  @ViewChild('fileInput') fileInput!: ElementRef<HTMLInputElement>;
  @ViewChild('configInput') configInput!: ElementRef<HTMLInputElement>;
  @ViewChild('instructionsInput') instructionsInput!: ElementRef<HTMLInputElement>;

  readonly isSubmitting = signal(false);
  readonly isUploading = signal(false);
  readonly isDragOver = signal(false);
  readonly successMessage = signal<string | null>(null);
  readonly errorMessage = signal<string | null>(null);
  readonly filePreviews = signal<FilePreview[]>([]);

  // Advanced section state
  readonly showAdvanced = signal(false);
  readonly configFile = signal<FilePreview | null>(null);
  readonly instructionsFile = signal<FilePreview | null>(null);

  // Current upload_id after successful upload
  private uploadId: string | null = null;
  private configUploadId: string | null = null;
  private instructionsUploadId: string | null = null;

  formData: JobCreateRequest = {
    prompt: '',
  };

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

  // ===== Config/Instructions Upload Methods =====

  triggerConfigInput(): void {
    if (!this.isSubmitting()) {
      this.configInput.nativeElement.click();
    }
  }

  triggerInstructionsInput(): void {
    if (!this.isSubmitting()) {
      this.instructionsInput.nativeElement.click();
    }
  }

  async onConfigFileSelected(event: Event): Promise<void> {
    const input = event.target as HTMLInputElement;
    if (input.files?.[0]) {
      const file = input.files[0];
      if (!file.name.toLowerCase().endsWith('.yaml') && !file.name.toLowerCase().endsWith('.yml')) {
        this.errorMessage.set('Config must be a YAML file (.yaml or .yml)');
        input.value = '';
        return;
      }
      const previews = await this.fileService.createFilePreviews([file]);
      this.configFile.set(previews[0] || null);
      this.configUploadId = null; // Clear previous upload
      input.value = '';
    }
  }

  async onInstructionsFileSelected(event: Event): Promise<void> {
    const input = event.target as HTMLInputElement;
    if (input.files?.[0]) {
      const file = input.files[0];
      if (!file.name.toLowerCase().endsWith('.md') && !file.name.toLowerCase().endsWith('.txt')) {
        this.errorMessage.set('Instructions must be a markdown or text file (.md or .txt)');
        input.value = '';
        return;
      }
      const previews = await this.fileService.createFilePreviews([file]);
      this.instructionsFile.set(previews[0] || null);
      this.instructionsUploadId = null; // Clear previous upload
      input.value = '';
    }
  }

  removeConfigFile(event: Event): void {
    event.stopPropagation();
    this.configFile.set(null);
    this.configUploadId = null;
  }

  removeInstructionsFile(event: Event): void {
    event.stopPropagation();
    this.instructionsFile.set(null);
    this.instructionsUploadId = null;
  }

  // ===== Form Submission =====

  async onSubmit(): Promise<void> {
    if (!this.formData.prompt || this.isSubmitting() || this.isUploading()) {
      return;
    }

    this.clearMessages();

    // Upload config file if provided
    if (this.configFile() && !this.configUploadId) {
      const configSuccess = await this.uploadConfigFile();
      if (!configSuccess) {
        return;
      }
    }

    // Upload instructions file if provided
    if (this.instructionsFile() && !this.instructionsUploadId) {
      const instrSuccess = await this.uploadInstructionsFile();
      if (!instrSuccess) {
        return;
      }
    }

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
      prompt: this.formData.prompt,
    };

    if (this.uploadId) {
      request.upload_id = this.uploadId;
    }
    if (this.configUploadId) {
      request.config_upload_id = this.configUploadId;
    }
    if (this.instructionsUploadId) {
      request.instructions_upload_id = this.instructionsUploadId;
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

  private async uploadConfigFile(): Promise<boolean> {
    const config = this.configFile();
    if (!config) return true;

    this.isUploading.set(true);

    try {
      const response = await new Promise<{ upload_id: string } | null>((resolve, reject) => {
        this.api.uploadConfig(config.file).subscribe({
          next: (res) => resolve(res),
          error: (err) => reject(err),
        });
      });

      if (response) {
        this.configUploadId = response.upload_id;
        this.isUploading.set(false);
        return true;
      } else {
        throw new Error('Config upload failed');
      }
    } catch (err) {
      this.isUploading.set(false);
      this.errorMessage.set('Failed to upload config file. Please try again.');
      return false;
    }
  }

  private async uploadInstructionsFile(): Promise<boolean> {
    const instructions = this.instructionsFile();
    if (!instructions) return true;

    this.isUploading.set(true);

    try {
      const response = await new Promise<{ upload_id: string } | null>((resolve, reject) => {
        this.api.uploadInstructions(instructions.file).subscribe({
          next: (res) => resolve(res),
          error: (err) => reject(err),
        });
      });

      if (response) {
        this.instructionsUploadId = response.upload_id;
        this.isUploading.set(false);
        return true;
      } else {
        throw new Error('Instructions upload failed');
      }
    } catch (err) {
      this.isUploading.set(false);
      this.errorMessage.set('Failed to upload instructions file. Please try again.');
      return false;
    }
  }

  resetForm(): void {
    this.formData = {
      prompt: '',
    };
    this.filePreviews.set([]);
    this.uploadId = null;
    // Reset advanced options
    this.showAdvanced.set(false);
    this.configFile.set(null);
    this.instructionsFile.set(null);
    this.configUploadId = null;
    this.instructionsUploadId = null;
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
