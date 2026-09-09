import {ChangeDetectionStrategy, Component, input, output} from '@angular/core';
import {RouterLink} from '@angular/router';
import {TranslocoPipe} from '@jsverse/transloco';
import {AppIconComponent} from '../../../ui/icon';

/** One suggestion chip, already resolved to the active language. */
export interface DisplayedSuggestion {
  icon: string;
  text: string;
}

@Component({
  selector: 'app-draft-empty-state',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, TranslocoPipe, AppIconComponent],
  styleUrl: './draft-empty-state.component.scss',
  template: `
    <div class="empty-inner">
      <img class="empty-mark" src="assets/icons/icon-mark.svg" alt="" />
      <h2 class="empty-title">{{ 'chat.draft.title' | transloco }}</h2>
      <p class="empty-subtitle">{{ 'chat.draft.subtitle' | transloco }}</p>

      <div class="draft-connectors" role="group"
           [attr.aria-label]="'chat.draft.connectorsLabel' | transloco">
        @if (connectorsLoading()) {
          <span class="draft-connectors-state">{{ 'chat.draft.connectorsLoading' | transloco }}</span>
        } @else if (connectorsError()) {
          <span class="draft-connectors-state draft-connectors-error">
            {{ 'chat.draft.connectorsFailed' | transloco }}
            <button type="button" (click)="retryRequested.emit()">
              {{ 'chat.draft.connectorsRetry' | transloco }}
            </button>
          </span>
        } @else {
          <label class="draft-connectors-toggle">
            <input type="checkbox" [checked]="connectorsEnabled()"
                   (change)="onConnectorsToggle($event)">
            <span>{{ 'chat.draft.connectorsCount' | transloco: {count: datasourceCount()} }}</span>
          </label>
        }
      </div>

      @if (suggestions().length > 0) {
        <div class="suggestion-grid">
          @for (s of suggestions(); track $index) {
            <button type="button" class="suggestion-chip" (click)="pick(s)">
              <app-icon size="lg" class="suggestion-icon">{{ s.icon }}</app-icon>
              <span class="suggestion-text">{{ s.text }}</span>
            </button>
          }
        </div>
      }

      <a class="draft-advanced" routerLink="/sessions/new">{{ 'chat.draft.advanced' | transloco }}</a>
    </div>
  `,
})
export class DraftEmptyStateComponent {
  suggestions = input.required<DisplayedSuggestion[]>();
  connectorsLoading = input.required<boolean>();
  connectorsError = input.required<boolean>();
  connectorsEnabled = input.required<boolean>();
  datasourceCount = input.required<number>();

  suggestionPicked = output<DisplayedSuggestion>();
  connectorsToggled = output<boolean>();
  retryRequested = output<void>();

  pick(s: DisplayedSuggestion): void {
    this.suggestionPicked.emit(s);
  }

  onConnectorsToggle(event: Event): void {
    this.connectorsToggled.emit((event.target as HTMLInputElement).checked);
  }
}
