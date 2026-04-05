import {Component, computed, input, output, signal} from '@angular/core';
import {FormsModule} from '@angular/forms';

/**
 * Instructions tab: full-height markdown textarea with clear and reset actions.
 * Used only in job creation mode. Exposes instructionsContent for parent wiring
 * to the JobArtifactService (builder AI streaming).
 */
@Component({
  selector: 'app-instructions-tab',
  standalone: true,
  imports: [FormsModule],
  template: `
    <div class="instructions-container">
      <div class="instructions-header">
        <label class="instructions-label">Custom Instructions</label>
        @if (loadingExpert()) {
          <span class="spinner-small"></span>
        }
      </div>

      <textarea
        class="instructions-editor"
        [ngModel]="content()"
        (ngModelChange)="onEdit($event)"
        rows="16"
        placeholder="Select an expert to pre-fill instructions, or type custom instructions..."
        [disabled]="disabled() || loadingExpert() || streaming()"
      ></textarea>

      @if (streaming()) {
        <span class="streaming-hint">
          <span class="spinner-small"></span>
          AI is editing instructions...
        </span>
      }

      <div class="instructions-actions">
        @if (content()) {
          <button
            type="button"
            class="btn-text"
            (click)="clearContent()"
            [disabled]="disabled()"
          >Clear</button>
        }
        @if (hasExpertDefault()) {
          <button
            type="button"
            class="btn-text"
            (click)="resetToExpert()"
            [disabled]="disabled() || loadingExpert()"
          >Reset to expert default</button>
        }
      </div>

      <div class="instructions-hint">
        <span class="hint-icon">info</span>
        Builder AI can edit instructions via the job builder chat.
      </div>
    </div>
  `,
  styles: [`
    .instructions-container {
      display: flex;
      flex-direction: column;
      height: 100%;
      gap: 8px;
    }
    .instructions-header {
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .instructions-label {
      font-size: 13px;
      font-weight: 600;
      color: var(--text-primary, #cdd6f4);
    }
    .instructions-editor {
      flex: 1;
      min-height: 200px;
      padding: 12px 14px;
      border: 1px solid var(--border-color, #45475a);
      border-radius: 6px;
      background: var(--surface-0, #313244);
      color: var(--text-primary, #cdd6f4);
      font-family: 'JetBrains Mono', monospace;
      font-size: 12px;
      line-height: 1.5;
      resize: vertical;
      transition: border-color 0.15s;
    }
    .instructions-editor:focus {
      outline: none;
      border-color: var(--accent-color, #cba6f7);
    }
    .instructions-editor:disabled {
      opacity: 0.6;
      cursor: not-allowed;
    }
    .instructions-editor::placeholder {
      color: var(--text-muted, #6c7086);
    }
    .streaming-hint {
      display: flex;
      align-items: center;
      gap: 6px;
      font-size: 12px;
      color: var(--accent-color, #cba6f7);
    }
    .instructions-actions {
      display: flex;
      gap: 12px;
    }
    .btn-text {
      padding: 4px 0;
      border: none;
      background: none;
      color: var(--accent-color, #cba6f7);
      font-size: 12px;
      cursor: pointer;
      text-decoration: underline;
      text-underline-offset: 2px;
    }
    .btn-text:hover {
      opacity: 0.8;
    }
    .btn-text:disabled {
      opacity: 0.5;
      cursor: not-allowed;
    }
    .instructions-hint {
      display: flex;
      align-items: center;
      gap: 6px;
      font-size: 11px;
      color: var(--text-muted, #6c7086);
      padding: 6px 0;
    }
    .hint-icon {
      font-family: 'Material Symbols Outlined', sans-serif;
      font-size: 14px;
    }
    .spinner-small {
      display: inline-block;
      width: 14px;
      height: 14px;
      border: 2px solid rgba(205, 214, 244, 0.2);
      border-top-color: var(--accent-color, #cba6f7);
      border-radius: 50%;
      animation: spin 0.6s linear infinite;
    }
    @keyframes spin {
      to { transform: rotate(360deg); }
    }
  `],
})
export class InstructionsTabComponent {
  disabled = input(false);
  loadingExpert = input(false);
  streaming = input(false);

  contentChange = output<string | null>();

  /** Current instructions content. */
  readonly content = signal<string | null>(null);
  /** Expert default instructions (set by parent when expert detail loads). */
  private expertDefault: string | null = null;

  readonly hasExpertDefault = computed(() => !!this.expertDefault);

  readonly isModified = computed(() => {
    const c = this.content();
    return c !== null && c !== this.expertDefault;
  });

  onEdit(value: string): void {
    this.content.set(value || null);
    this.contentChange.emit(value || null);
  }

  clearContent(): void {
    this.content.set(null);
    this.contentChange.emit(null);
  }

  resetToExpert(): void {
    this.content.set(this.expertDefault);
    this.contentChange.emit(this.expertDefault);
  }

  /** Called by parent to set both the content and the expert default. */
  setFromExpert(instructions: string | null): void {
    this.expertDefault = instructions;
    this.content.set(instructions);
    this.contentChange.emit(instructions);
  }

  /** Called by parent to update content from external source (builder AI). */
  setContent(value: string | null): void {
    this.content.set(value);
  }

  resetAll(): void {
    this.content.set(null);
    this.expertDefault = null;
  }
}
