import {ChangeDetectionStrategy, Component, computed, inject} from '@angular/core';
import {TranslocoPipe} from '@jsverse/transloco';
import {UserService} from '../../core/services/user.service';
import {ViewModeService} from '../../core/services/view-mode.service';

/**
 * Sidebar admin badge — toggles between `'me'` (view only your data) and
 * `'all'` (fleet-wide view). Visible only to admins. Click cycles the
 * mode; tooltip explains what the click does. Replaces the static
 * `[ADMIN]` span that used to live in sidebar.component.ts:178.
 *
 * Design: `docs/features/admin_view_as_user.md` § "Toggle UI".
 */
@Component({
  selector: 'app-view-mode-toggle',
  standalone: true,
  imports: [TranslocoPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (isAdmin()) {
      <button
        type="button"
        class="view-mode-toggle"
        [class.is-narrowed]="isNarrowed()"
        [title]="(isNarrowed() ? 'admin.viewMode.tooltip.switchToAll' : 'admin.viewMode.tooltip.switchToMe') | transloco"
        (click)="cycle()"
      >
        <span class="role">{{ 'admin.viewMode.role' | transloco }}</span>
        <span class="sep">·</span>
        <span class="scope">{{ scopeLabelKey() | transloco }}</span>
      </button>
    }
  `,
  styles: [
    `
      :host {
        display: contents;
      }

      .view-mode-toggle {
        appearance: none;
        background: transparent;
        border: 1px solid transparent;
        padding: 2px 6px;
        font-family: inherit;
        font-size: 9px;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        color: var(--accent-color, #cba6f7);
        opacity: 0.7;
        flex-shrink: 0;
        cursor: pointer;
        border-radius: var(--radius-tag, 4px);
        transition: background-color 0.15s ease, opacity 0.15s ease, border-color 0.15s ease;
        white-space: nowrap;
        display: inline-flex;
        align-items: center;
        gap: 4px;
        line-height: 1.2;
      }

      .view-mode-toggle:hover,
      .view-mode-toggle:focus-visible {
        opacity: 1;
        background-color: color-mix(in oklab, var(--accent-color, #cba6f7) 12%, transparent);
        outline: none;
        border-color: color-mix(in oklab, var(--accent-color, #cba6f7) 35%, transparent);
      }

      .view-mode-toggle.is-narrowed {
        /* Visual hint that the admin is currently filtered to their own data */
        color: var(--warn-color, #f9e2af);
        border-color: color-mix(in oklab, var(--warn-color, #f9e2af) 35%, transparent);
        opacity: 0.85;
      }

      .view-mode-toggle.is-narrowed:hover,
      .view-mode-toggle.is-narrowed:focus-visible {
        background-color: color-mix(in oklab, var(--warn-color, #f9e2af) 12%, transparent);
        border-color: color-mix(in oklab, var(--warn-color, #f9e2af) 50%, transparent);
      }

      .sep {
        opacity: 0.6;
      }
    `,
  ],
})
export class ViewModeToggleComponent {
  private readonly userService = inject(UserService);
  private readonly viewMode = inject(ViewModeService);

  readonly isAdmin = computed(() => this.userService.currentUser()?.is_admin === true);
  readonly isNarrowed = computed(() => this.viewMode.viewMode() === 'me');
  readonly scopeLabelKey = computed(() =>
    this.isNarrowed() ? 'admin.viewMode.scopeMe' : 'admin.viewMode.scopeAll',
  );

  cycle(): void {
    this.viewMode.setMode(this.isNarrowed() ? 'all' : 'me');
  }
}
