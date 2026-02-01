import { Component, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ApiService } from '../../core/services/api.service';
import { JobCreateRequest } from '../../core/models/api.model';

/**
 * Job Create component for submitting new jobs.
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

          <!-- Document Path Field (Optional) -->
          <div class="form-group">
            <label for="documentPath" class="form-label">
              Document Path
            </label>
            <input
              id="documentPath"
              name="documentPath"
              type="text"
              class="form-input"
              [(ngModel)]="formData.document_path"
              placeholder="/path/to/document.pdf"
              [disabled]="isSubmitting()"
            />
            <span class="field-hint">Optional: Path to a document for the agent to process</span>
          </div>

          <!-- Document Directory Field (Optional) -->
          <div class="form-group">
            <label for="documentDir" class="form-label">
              Document Directory
            </label>
            <input
              id="documentDir"
              name="documentDir"
              type="text"
              class="form-input"
              [(ngModel)]="formData.document_dir"
              placeholder="/path/to/documents/"
              [disabled]="isSubmitting()"
            />
            <span class="field-hint">Optional: Directory containing multiple documents</span>
          </div>

          <!-- Config Name Field (Optional) -->
          <div class="form-group">
            <label for="configName" class="form-label">
              Config Name
            </label>
            <input
              id="configName"
              name="configName"
              type="text"
              class="form-input"
              [(ngModel)]="formData.config_name"
              placeholder="defaults"
              [disabled]="isSubmitting()"
            />
            <span class="field-hint">Optional: Agent configuration to use (defaults to 'defaults')</span>
          </div>

          <!-- Context JSON Field (Optional) -->
          <div class="form-group">
            <label for="context" class="form-label">
              Context (JSON)
            </label>
            <textarea
              id="context"
              name="context"
              class="form-textarea mono"
              [(ngModel)]="contextJson"
              rows="4"
              placeholder='{"key": "value"}'
              [disabled]="isSubmitting()"
            ></textarea>
            @if (contextError()) {
              <span class="field-error">{{ contextError() }}</span>
            }
            <span class="field-hint">Optional: Additional context as JSON object</span>
          </div>

          <!-- Instructions Field (Optional) -->
          <div class="form-group">
            <label for="instructions" class="form-label">
              Additional Instructions
            </label>
            <textarea
              id="instructions"
              name="instructions"
              class="form-textarea"
              [(ngModel)]="formData.instructions"
              rows="3"
              placeholder="Any additional instructions for the agent..."
              [disabled]="isSubmitting()"
            ></textarea>
            <span class="field-hint">Optional: Extra instructions to guide the agent</span>
          </div>

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
              [disabled]="!formData.prompt || isSubmitting()"
            >
              @if (isSubmitting()) {
                <span class="spinner-small"></span>
                Creating...
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

  readonly isSubmitting = signal(false);
  readonly successMessage = signal<string | null>(null);
  readonly errorMessage = signal<string | null>(null);
  readonly contextError = signal<string | null>(null);

  formData: JobCreateRequest = {
    prompt: '',
    document_path: undefined,
    document_dir: undefined,
    config_name: undefined,
    context: undefined,
    instructions: undefined,
  };

  contextJson = '';

  onSubmit(): void {
    if (!this.formData.prompt || this.isSubmitting()) {
      return;
    }

    // Parse context JSON if provided
    if (this.contextJson.trim()) {
      try {
        this.formData.context = JSON.parse(this.contextJson);
        this.contextError.set(null);
      } catch {
        this.contextError.set('Invalid JSON format');
        return;
      }
    } else {
      this.formData.context = undefined;
    }

    // Clean up empty optional fields
    const request: JobCreateRequest = {
      prompt: this.formData.prompt,
    };

    if (this.formData.document_path?.trim()) {
      request.document_path = this.formData.document_path.trim();
    }
    if (this.formData.document_dir?.trim()) {
      request.document_dir = this.formData.document_dir.trim();
    }
    if (this.formData.config_name?.trim()) {
      request.config_name = this.formData.config_name.trim();
    }
    if (this.formData.context) {
      request.context = this.formData.context;
    }
    if (this.formData.instructions?.trim()) {
      request.instructions = this.formData.instructions.trim();
    }

    this.isSubmitting.set(true);
    this.clearMessages();

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

  resetForm(): void {
    this.formData = {
      prompt: '',
      document_path: undefined,
      document_dir: undefined,
      config_name: undefined,
      context: undefined,
      instructions: undefined,
    };
    this.contextJson = '';
    this.contextError.set(null);
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
