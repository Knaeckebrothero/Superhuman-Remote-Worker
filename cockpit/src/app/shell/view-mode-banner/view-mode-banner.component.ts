import {ChangeDetectionStrategy, Component, computed, inject} from '@angular/core';
import {TranslocoPipe} from '@jsverse/transloco';
import {UserService} from '../../core/services/user.service';
import {ViewModeService} from '../../core/services/view-mode.service';

/**
 * Global status banner shown on every route when an admin is viewing
 * fleet-wide data (`'all'`) — the elevated, see-everyone's-data mode. It's the
 * always-visible reminder that admin-wide visibility is on, plus a one-click
 * shortcut to narrow to their own data (`'me'`). When narrowed the banner is
 * absent — that's the normal, regular-user-like experience and needs no notice.
 *
 * Mounted once in `app.ts` above the router outlet (alongside the readiness
 * and empty-catalog banners). Replaces the old per-page `view-mode-pill`s,
 * which only covered the three list pages. The control that *sets* the mode
 * lives in Settings → Data visibility; this banner only reflects the 'all'
 * state and offers the narrow shortcut.
 *
 * Gating: `is_admin && viewMode === 'all'` — deliberately NOT
 * `ViewModeService.effectiveMode`, which reports `'me'` for non-admins and
 * would make the banner misfire for them.
 *
 * Design: `docs/features/admin_view_as_user.md`.
 */
@Component({
  selector: 'app-view-mode-banner',
  standalone: true,
  imports: [TranslocoPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (isVisible()) {
      <div class="view-mode-banner" role="status" aria-live="polite">
        <span class="dot" aria-hidden="true"></span>
        <span class="banner-text">{{ 'admin.viewMode.banner.text' | transloco }}</span>
        <button
          type="button"
          class="banner-action"
          [title]="'admin.viewMode.banner.tooltip' | transloco"
          (click)="showMine()"
        >
          {{ 'admin.viewMode.banner.showMine' | transloco }}
        </button>
      </div>
    }
  `,
  styles: [
    `
      .view-mode-banner {
        display: flex;
        align-items: center;
        gap: 10px;
        padding: 8px 16px;
        /* Use the theme warning tokens. The previous hardcoded #f9e2af is a
           dark-theme amber and rendered near-invisible (~1.1:1) on light
           themes, where --warn-color is undefined. */
        background: var(--warning-tint);
        border-bottom: 1px solid color-mix(in srgb, var(--warning) 30%, transparent);
        /* Mix toward the theme text color so the amber clears WCAG AA (4.5:1)
           on the light tint; mixing toward --text-primary raises contrast in
           both light and dark themes. */
        color: color-mix(in srgb, var(--warning) 55%, var(--text-primary));
        font-size: 13px;
        line-height: 1.4;
        flex-shrink: 0;
      }

      .dot {
        width: 6px;
        height: 6px;
        border-radius: 999px;
        background-color: currentColor;
        opacity: 0.85;
        flex-shrink: 0;
      }

      .banner-text {
        flex: 1;
      }

      .banner-action {
        appearance: none;
        background: transparent;
        border: 0;
        padding: 0;
        font: inherit;
        font-weight: 600;
        color: inherit;
        cursor: pointer;
        text-decoration: underline;
        text-underline-offset: 2px;
        flex-shrink: 0;
      }

      .banner-action:hover,
      .banner-action:focus-visible {
        opacity: 0.85;
        outline: none;
      }
    `,
  ],
})
export class ViewModeBannerComponent {
  private readonly userService = inject(UserService);
  private readonly viewMode = inject(ViewModeService);

  readonly isVisible = computed(
    () =>
      this.userService.currentUser()?.is_admin === true &&
      this.viewMode.viewMode() === 'all',
  );

  showMine(): void {
    this.viewMode.setMode('me');
  }
}
