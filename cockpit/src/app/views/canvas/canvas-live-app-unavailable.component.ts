import {
  ChangeDetectionStrategy,
  Component,
  EventEmitter,
  Input,
  Output,
} from '@angular/core';
import {TranslocoPipe} from '@jsverse/transloco';

export type CanvasLiveAppFailureKind = 'disabled' | 'unsupported' | 'temporary';

/** Map internal lifecycle failures to bounded, user-actionable trusted copy. */
export function canvasLiveAppFailureKind(code: string | null): CanvasLiveAppFailureKind {
  if (code === 'canvas_viewer_not_configured') return 'disabled';
  if (
    code === 'canvas_browser_unsupported' ||
    code === 'canvas_browser_storage_unavailable'
  ) {
    return 'unsupported';
  }
  return 'temporary';
}

@Component({
  selector: 'app-canvas-live-app-unavailable',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [TranslocoPipe],
  template: `
    <section class="live-app-unavailable" role="alert"
             [attr.data-failure-kind]="failureKind()">
      <span class="live-app-unavailable__icon material-symbols-outlined"
            aria-hidden="true">shield_lock</span>
      <div class="live-app-unavailable__copy">
        <h3>{{ ('canvas.app.failure.' + failureKind() + '.title') | transloco }}</h3>
        <p>{{ ('canvas.app.failure.' + failureKind() + '.body') | transloco }}</p>
        <p class="live-app-unavailable__guidance">
          {{ 'canvas.app.failure.safeFallback' | transloco }}
        </p>
      </div>
      @if (failureKind() !== 'disabled') {
        <button type="button" class="live-app-unavailable__retry" (click)="retry.emit()">
          {{ 'canvas.app.retry' | transloco }}
        </button>
      }
    </section>
  `,
  styles: `
    :host {
      display: grid;
      min-height: 100%;
      place-content: center;
      padding: 24px;
      box-sizing: border-box;
    }

    .live-app-unavailable {
      display: grid;
      width: min(520px, 100%);
      justify-items: center;
      gap: 14px;
      padding: clamp(20px, 5vw, 36px);
      box-sizing: border-box;
      color: var(--text-secondary);
      text-align: center;
      background: var(--panel-bg);
      border: 1px solid var(--border-color);
      border-radius: var(--radius-surface);
    }

    .live-app-unavailable__icon {
      color: var(--warning);
      font-size: 34px;
    }

    .live-app-unavailable__copy { display: grid; gap: 8px; }

    h3,
    p { margin: 0; }

    h3 {
      color: var(--text-primary);
      font-size: 16px;
    }

    p {
      max-width: 58ch;
      font-size: 13px;
      line-height: 1.5;
    }

    .live-app-unavailable__guidance {
      color: var(--text-muted);
      font-size: 12px;
    }

    .live-app-unavailable__retry {
      min-height: 30px;
      padding: 4px 10px;
      color: var(--text-primary);
      font: 600 12px/1 var(--font-primary, inherit);
      background: var(--surface-0);
      border: 1px solid var(--border-color);
      border-radius: var(--radius-control);
      cursor: pointer;
    }

    .live-app-unavailable__retry:hover { background: var(--surface-1); }
    .live-app-unavailable__retry:focus-visible { outline: 2px solid var(--accent-color); }
  `,
})
export class CanvasLiveAppUnavailableComponent {
  @Input() errorCode: string | null = null;
  @Output() readonly retry = new EventEmitter<void>();

  failureKind(): CanvasLiveAppFailureKind {
    return canvasLiveAppFailureKind(this.errorCode);
  }
}
