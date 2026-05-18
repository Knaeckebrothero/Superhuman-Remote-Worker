import {ChangeDetectionStrategy, Component, computed, inject} from '@angular/core';
import {TranslocoPipe} from '@jsverse/transloco';
import {UserService} from '../../core/services/user.service';
import {ViewModeService} from '../../core/services/view-mode.service';

/**
 * Per-page pill rendered on list views (jobs, projects, datasources) when
 * an admin is in `'me'` mode. Tells the admin why the page looks small
 * and offers a one-click switch back to fleet-wide visibility — saves
 * them scanning the sidebar footer for the toggle.
 *
 * Renders nothing when the admin is in `'all'` mode (no need to draw
 * attention) or when the user is not admin. Design:
 * `docs/features/admin_view_as_user.md` § "Per-page badge".
 */
@Component({
  selector: 'app-view-mode-pill',
  standalone: true,
  imports: [TranslocoPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (isVisible()) {
      <span class="view-mode-pill" role="status">
        <span class="dot" aria-hidden="true"></span>
        <span class="label">{{ 'admin.viewMode.pill.label' | transloco }}</span>
        <button
          type="button"
          class="show-all"
          [title]="'admin.viewMode.pill.tooltip' | transloco"
          (click)="showAll()"
        >
          {{ 'admin.viewMode.pill.showAll' | transloco }}
        </button>
      </span>
    }
  `,
  styles: [
    `
      :host {
        display: contents;
      }

      .view-mode-pill {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        padding: 2px 8px 2px 6px;
        border-radius: var(--radius-tag, 999px);
        border: 1px solid color-mix(in oklab, var(--warn-color, #f9e2af) 40%, transparent);
        background-color: color-mix(in oklab, var(--warn-color, #f9e2af) 10%, transparent);
        color: var(--warn-color, #f9e2af);
        font-size: 11px;
        font-weight: 500;
        line-height: 1.4;
        flex-shrink: 0;
      }

      .dot {
        width: 6px;
        height: 6px;
        border-radius: 999px;
        background-color: currentColor;
        opacity: 0.85;
      }

      .show-all {
        appearance: none;
        background: transparent;
        border: 0;
        padding: 0;
        margin-left: 2px;
        font: inherit;
        font-weight: 600;
        color: inherit;
        cursor: pointer;
        text-decoration: underline;
        text-underline-offset: 2px;
      }

      .show-all:hover,
      .show-all:focus-visible {
        opacity: 0.85;
        outline: none;
      }
    `,
  ],
})
export class ViewModePillComponent {
  private readonly userService = inject(UserService);
  private readonly viewMode = inject(ViewModeService);

  readonly isVisible = computed(
    () =>
      this.userService.currentUser()?.is_admin === true &&
      this.viewMode.viewMode() === 'me',
  );

  showAll(): void {
    this.viewMode.setMode('all');
  }
}
