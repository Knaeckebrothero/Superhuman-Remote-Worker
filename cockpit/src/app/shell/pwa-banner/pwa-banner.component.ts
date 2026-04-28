import {Component, computed, DestroyRef, inject, OnInit, signal} from '@angular/core';
import {takeUntilDestroyed} from '@angular/core/rxjs-interop';
import {SwUpdate, VersionReadyEvent} from '@angular/service-worker';
import {filter} from 'rxjs/operators';
import {TranslocoPipe} from '@jsverse/transloco';

/**
 * Top-of-viewport status banner for PWA lifecycle states. Renders only when
 * something is actually wrong or new:
 *
 *  - Offline (warning) — `navigator.onLine` is false
 *  - Update available (info + Reload CTA) — service worker has fetched a new
 *    bundle. Clicking Reload activates it and refreshes the page.
 *
 * Offline takes priority over update-available, since reloading to apply an
 * update requires a network. Both signals coexist; we just hide the update
 * banner while offline.
 */
@Component({
  selector: 'app-pwa-banner',
  standalone: true,
  imports: [TranslocoPipe],
  template: `
    @if (offline()) {
      <div class="pwa-banner pwa-banner--warning" role="status" aria-live="polite">
        <span class="pwa-banner__text">{{ 'pwa.offline.banner' | transloco }}</span>
      </div>
    } @else if (updateReady()) {
      <div class="pwa-banner pwa-banner--info" role="status" aria-live="polite">
        <span class="pwa-banner__text">{{ 'pwa.update.title' | transloco }}</span>
        <button
          type="button"
          class="pwa-banner__cta"
          (click)="applyUpdate()"
        >{{ 'pwa.update.cta' | transloco }}</button>
      </div>
    }
  `,
  styles: [`
    .pwa-banner {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 10px 16px;
      font-size: 13px;
      line-height: 1.4;
    }
    .pwa-banner--warning {
      background: var(--warning-tint);
      border-bottom: 1px solid var(--warning);
      color: var(--warning);
    }
    .pwa-banner--info {
      background: var(--info-tint);
      border-bottom: 1px solid var(--info);
      color: var(--info);
    }
    .pwa-banner__text { flex: 1; }
    .pwa-banner__cta {
      background: transparent;
      border: 1px solid currentColor;
      color: inherit;
      padding: 4px 12px;
      font: inherit;
      font-weight: 600;
      cursor: pointer;
      border-radius: var(--radius-sm, 0);
    }
    .pwa-banner__cta:hover { background: rgba(0, 0, 0, 0.08); }
  `],
})
export class AppPwaBannerComponent implements OnInit {
  private readonly swUpdate = inject(SwUpdate);
  private readonly destroyRef = inject(DestroyRef);

  protected readonly offline = signal(typeof navigator !== 'undefined' ? !navigator.onLine : false);
  protected readonly updateReady = signal(false);

  ngOnInit(): void {
    if (typeof window !== 'undefined') {
      const goOnline = () => this.offline.set(false);
      const goOffline = () => this.offline.set(true);
      window.addEventListener('online', goOnline);
      window.addEventListener('offline', goOffline);
      this.destroyRef.onDestroy(() => {
        window.removeEventListener('online', goOnline);
        window.removeEventListener('offline', goOffline);
      });
    }

    if (this.swUpdate.isEnabled) {
      this.swUpdate.versionUpdates
        .pipe(
          filter((evt): evt is VersionReadyEvent => evt.type === 'VERSION_READY'),
          takeUntilDestroyed(this.destroyRef),
        )
        .subscribe(() => this.updateReady.set(true));
    }
  }

  applyUpdate(): void {
    if (!this.swUpdate.isEnabled) {
      window.location.reload();
      return;
    }
    this.swUpdate.activateUpdate().then(() => window.location.reload());
  }
}
