import { Component, inject } from '@angular/core';
import { ToastService } from '../../services/toast.service';

@Component({
  selector: 'app-toast-container',
  standalone: true,
  template: `
    <div class="toast-container">
      @for (toast of toastService.toasts(); track toast.id) {
        <div
          class="toast"
          [class]="'toast toast-' + toast.type"
          (click)="toastService.dismiss(toast.id)"
        >
          <span class="toast-icon">
            @switch (toast.type) {
              @case ('success') { check_circle }
              @case ('error') { error }
              @default { info }
            }
          </span>
          <span class="toast-message">{{ toast.message }}</span>
        </div>
      }
    </div>
  `,
  styles: [
    `
      .toast-container {
        position: fixed;
        bottom: 16px;
        right: 16px;
        z-index: 10000;
        display: flex;
        flex-direction: column-reverse;
        gap: 8px;
        pointer-events: none;
        max-width: 400px;
      }

      .toast {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 12px 16px;
        border-radius: 8px;
        background: var(--surface-0, #313244);
        color: var(--text-primary, #cdd6f4);
        font-size: 13px;
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4);
        cursor: pointer;
        pointer-events: auto;
        animation: slideIn 0.2s ease-out;
        border-left: 3px solid var(--text-muted, #6c7086);
      }

      .toast-success {
        border-left-color: var(--green, #a6e3a1);
      }

      .toast-error {
        border-left-color: var(--red, #f38ba8);
      }

      .toast-info {
        border-left-color: var(--blue, #89b4fa);
      }

      .toast-icon {
        font-family: 'Material Symbols Outlined';
        font-size: 18px;
        flex-shrink: 0;
      }

      .toast-success .toast-icon {
        color: var(--green, #a6e3a1);
      }

      .toast-error .toast-icon {
        color: var(--red, #f38ba8);
      }

      .toast-info .toast-icon {
        color: var(--blue, #89b4fa);
      }

      .toast-message {
        flex: 1;
        line-height: 1.4;
      }

      @keyframes slideIn {
        from {
          opacity: 0;
          transform: translateX(20px);
        }
        to {
          opacity: 1;
          transform: translateX(0);
        }
      }
    `,
  ],
})
export class ToastComponent {
  readonly toastService = inject(ToastService);
}
