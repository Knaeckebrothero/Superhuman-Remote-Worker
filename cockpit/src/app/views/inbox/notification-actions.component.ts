import {Component, computed, input, output, signal} from '@angular/core';
import {TranslocoPipe} from '@jsverse/transloco';
import {AppButtonComponent} from '../../ui/button';
import {AppTextareaComponent} from '../../ui/textarea';
import {AppInputComponent} from '../../ui/input';
import {NotificationAction} from '../../core/models/notification.model';

export interface NotificationActEvent {
  type: string;
  params: Record<string, unknown>;
}

/**
 * The generic action bar (unified notification system, D7): renders the
 * server-declared action set of a feed row and emits `{type, params}`. It
 * knows nothing about jobs, sudo requests or officers — only that an action
 * has a label key, a style, and optionally one input to collect first.
 *
 * On a resolved row only navigation-style actions (`open*`) remain: the
 * decision has been made, by whoever made it.
 */
@Component({
  selector: 'app-notification-actions',
  standalone: true,
  imports: [TranslocoPipe, AppButtonComponent, AppTextareaComponent, AppInputComponent],
  template: `
    @if (visible().length === 0) {
      <div class="no-actions">{{ 'notifications.detail.noActions' | transloco }}</div>
    } @else {
      <div class="actions">
        @for (action of visible(); track action.type) {
          @if (action.input) {
            <div class="action-group">
              @if (action.input === 'textarea') {
                <app-textarea
                  [value]="valueFor(action)"
                  (valueChange)="setValue(action, $event)"
                  [placeholder]="inputPlaceholder(action) | transloco"
                  [rows]="2"
                />
              } @else {
                <app-input
                  size="sm"
                  [value]="valueFor(action)"
                  (valueChange)="setValue(action, $event)"
                  [placeholder]="inputPlaceholder(action) | transloco"
                />
              }
              <app-button
                [variant]="variant(action)"
                size="sm"
                [disabled]="!canRun(action)"
                [loading]="busy()"
                (clicked)="run(action)"
              >
                {{ action.label_key | transloco }}
              </app-button>
            </div>
          } @else {
            <app-button
              [variant]="variant(action)"
              size="sm"
              [disabled]="busy()"
              [loading]="busy()"
              (clicked)="run(action)"
            >
              {{ action.label_key | transloco }}
            </app-button>
          }
        }
      </div>
    }
  `,
  styles: [`
    :host { display: block; }
    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: flex-start;
    }
    .action-group {
      display: flex;
      flex-direction: column;
      gap: 6px;
      flex: 1 1 260px;
      min-width: 220px;
    }
    .no-actions {
      color: var(--text-secondary);
      font-size: 12px;
    }
  `],
})
export class NotificationActionsComponent {
  readonly actions = input<NotificationAction[]>([]);
  readonly resolved = input(false);
  readonly busy = input(false);
  readonly act = output<NotificationActEvent>();

  private readonly values = signal<Record<string, string>>({});

  readonly visible = computed(() => {
    const all = this.actions();
    return this.resolved() ? all.filter((a) => a.type.startsWith('open')) : all;
  });

  private key(action: NotificationAction): string {
    return action.input_name || action.type;
  }

  valueFor(action: NotificationAction): string {
    return this.values()[this.key(action)] ?? '';
  }

  setValue(action: NotificationAction, value: string): void {
    this.values.update((m) => ({...m, [this.key(action)]: value}));
  }

  canRun(action: NotificationAction): boolean {
    if (this.busy()) return false;
    if (!action.input) return true;
    return this.valueFor(action).trim().length > 0;
  }

  inputPlaceholder(action: NotificationAction): string {
    return `notifications.inputs.${action.input_name || 'value'}`;
  }

  variant(action: NotificationAction): 'primary' | 'danger' | 'ghost' {
    if (action.style === 'primary') return 'primary';
    if (action.style === 'danger') return 'danger';
    return 'ghost';
  }

  run(action: NotificationAction): void {
    if (!this.canRun(action)) return;
    const params: Record<string, unknown> = {};
    if (action.input && action.input_name) {
      params[action.input_name] = this.valueFor(action).trim();
    }
    this.act.emit({type: action.type, params});
    if (action.input) this.setValue(action, '');
  }
}
