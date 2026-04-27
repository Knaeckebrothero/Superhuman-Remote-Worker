import {ChangeDetectionStrategy, Component, inject} from '@angular/core';
import {AppToastService, ToastEntry} from './toast.service';

@Component({
  selector: 'app-toast-container',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div
      class="app-toast-container"
      role="region"
      aria-label="Notifications"
    >
      @for (toast of toasts(); track toast.id) {
        <div
          class="app-toast"
          [attr.data-tone]="toast.tone"
          [attr.role]="toast.tone === 'danger' ? 'alert' : 'status'"
          [attr.aria-live]="toast.tone === 'danger' ? 'assertive' : 'polite'"
        >
          <span class="app-toast__message">{{ toast.message }}</span>
          @if (toast.dismissible) {
            <button
              type="button"
              class="app-toast__close"
              aria-label="Dismiss"
              (click)="dismiss(toast)"
            >
              ×
            </button>
          }
        </div>
      }
    </div>
  `,
  styleUrl: './toast-container.component.scss',
})
export class AppToastContainerComponent {
  private service = inject(AppToastService);
  protected toasts = this.service.toasts;

  protected dismiss(toast: ToastEntry): void {
    this.service.dismiss(toast.id);
  }
}
