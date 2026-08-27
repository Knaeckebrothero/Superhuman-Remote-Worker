import {
  ChangeDetectionStrategy,
  Component,
  EventEmitter,
  Input,
  Output,
} from '@angular/core';
import {TranslocoPipe} from '@jsverse/transloco';
import {
  CanvasPresentationSummary,
  ToolCardAction,
} from '../../core/models/tool-card.model';

/** Compact trusted Canvas summary/action embedded in a generic tool card header. */
@Component({
  selector: 'app-canvas-tool-card-presentation',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [TranslocoPipe],
  template: `
    @if (presentation; as summary) {
      <span class="tc-canvas__kind">
        {{ ('toolCard.canvas.sourceKind.' + summary.sourceKind) | transloco }}
      </span>
      @if (summary.title) {
        <span class="tc-canvas__title" [title]="summary.title">{{ summary.title }}</span>
      }
    }
    <span class="tc-canvas__context">{{ contextLabel }}</span>
    <button type="button" class="tc-canvas__action" [disabled]="!available"
            (click)="request($event)">
      {{ 'toolCard.canvas.open' | transloco }}
    </button>
  `,
  styles: `
    :host { display: contents; }

    .tc-canvas__kind {
      flex: 0 0 auto;
      padding: 2px 6px;
      color: var(--accent-color);
      font-size: 9.5px;
      font-weight: 600;
      letter-spacing: 0.04em;
      background: color-mix(in srgb, var(--accent-color) 10%, transparent);
      border-radius: var(--radius-tag);
    }

    .tc-canvas__title {
      flex: 0 1 auto;
      min-width: 0;
      overflow: hidden;
      color: var(--text-secondary);
      font-size: 11px;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .tc-canvas__context {
      flex: 0 0 auto;
      color: var(--text-muted);
      font-size: 10px;
    }

    .tc-canvas__action {
      min-height: 26px;
      padding: 2px 8px;
      flex: 0 0 auto;
      color: var(--accent-color);
      font: 600 11px/1 var(--font-primary, inherit);
      background: transparent;
      border: 1px solid color-mix(in srgb, var(--accent-color) 45%, var(--border-color));
      border-radius: var(--radius-control);
      cursor: pointer;
    }

    .tc-canvas__action:hover:not(:disabled) {
      background: color-mix(in srgb, var(--accent-color) 10%, transparent);
    }

    .tc-canvas__action:disabled {
      color: var(--text-muted);
      border-color: var(--border-color);
      cursor: not-allowed;
    }
  `,
})
export class CanvasToolCardPresentationComponent {
  @Input() presentation?: CanvasPresentationSummary;
  @Input({required: true}) action!: ToolCardAction;
  @Input({required: true}) contextLabel = '';
  @Input() available = false;
  @Output() readonly requested = new EventEmitter<ToolCardAction>();

  request(event: MouseEvent): void {
    event.preventDefault();
    event.stopPropagation();
    if (this.available) this.requested.emit(this.action);
  }
}
