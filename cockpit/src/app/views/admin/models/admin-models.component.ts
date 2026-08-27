import {ChangeDetectionStrategy, Component, OnInit, inject, signal} from '@angular/core';
import {ActivatedRoute} from '@angular/router';
import {SidebarToggleComponent} from '../../../shell/sidebar-toggle/sidebar-toggle.component';
import {AppTabNavComponent, AppTabNavItemComponent} from '../../../ui/tab-nav';
import {AdminProvidersComponent} from '../providers/admin-providers.component';
import {AdminCatalogComponent} from '../catalog/admin-catalog.component';
import {AdminDefaultsComponent} from '../defaults/admin-defaults.component';

type AdminModelsTab = 'providers' | 'catalog' | 'defaults';

/**
 * `?tab=models` predates the rename and is linked from the readiness-gate
 * banner as well as whatever anyone bookmarked. Resolved rather than dropped,
 * because the failure mode is silent: an unrecognised value falls through to
 * the Providers tab, so a stale link would quietly land on the wrong page
 * instead of erroring.
 */
const LEGACY_TAB_ALIASES: Record<string, AdminModelsTab> = {models: 'catalog'};

function resolveTab(raw: string | null): AdminModelsTab | null {
  if (!raw) return null;
  const aliased = LEGACY_TAB_ALIASES[raw] ?? raw;
  return aliased === 'providers' || aliased === 'catalog' || aliased === 'defaults'
    ? aliased
    : null;
}

@Component({
  selector: 'app-admin-models',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    SidebarToggleComponent,
    AppTabNavComponent,
    AppTabNavItemComponent,
    AdminProvidersComponent,
    AdminCatalogComponent,
    AdminDefaultsComponent,
  ],
  template: `
    <div class="admin-page">
      <div class="admin-container">
        <div class="page-header">
          <app-sidebar-toggle />
          <h1 class="page-title">Models</h1>
        </div>
        <p class="page-desc">
          Provider keys, system endpoints, the model catalog, and default-model
          assignments — managed in one place. Covers every model the system
          calls, not just chat: vision, speech and embeddings live here too.
        </p>

        <app-tab-nav
          class="admin-models-tabs"
          [value]="tab()"
          (valueChange)="onTabChange($event)"
        >
          <app-tab-nav-item value="providers">Providers</app-tab-nav-item>
          <app-tab-nav-item value="catalog">Catalog</app-tab-nav-item>
          <app-tab-nav-item value="defaults">Defaults</app-tab-nav-item>
        </app-tab-nav>

        <div class="tab-content">
          <div class="tab-panel" [class.tab-hidden]="tab() !== 'providers'">
            <app-admin-providers (switchTab)="onTabChange($event)" />
          </div>
          <div class="tab-panel" [class.tab-hidden]="tab() !== 'catalog'">
            <app-admin-catalog />
          </div>
          <div class="tab-panel" [class.tab-hidden]="tab() !== 'defaults'">
            <app-admin-defaults />
          </div>
        </div>
      </div>
    </div>
  `,
  styles: [`
    :host {
      display: block;
      height: 100%;
      overflow: auto;
    }
    .admin-page {
      padding: 32px;
      max-width: var(--content-max-width);
      margin: 0 auto;
      color: var(--text-primary);
    }
    .page-header {
      display: flex;
      align-items: center;
      gap: 12px;
      margin-bottom: 8px;
    }
    .page-title {
      font-size: 24px;
      font-weight: 700;
      margin: 0;
      color: var(--text-primary);
    }
    .page-desc {
      font-size: 13px;
      color: var(--text-muted);
      margin: 0 0 16px 0;
    }
    .admin-models-tabs {
      display: flex;
      gap: 4px;
      margin-bottom: 16px;
      border-bottom: 1px solid var(--border-color);
    }
    .tab-panel {
      display: block;
    }
    .tab-hidden {
      display: none !important;
    }
  `],
})
export class AdminModelsComponent implements OnInit {
  private readonly route = inject(ActivatedRoute);

  readonly tab = signal<AdminModelsTab>('providers');

  ngOnInit(): void {
    const initial = resolveTab(this.route.snapshot.queryParamMap.get('tab'));
    if (initial) this.tab.set(initial);
  }

  protected onTabChange(value: AdminModelsTab | string | null): void {
    const next = resolveTab(typeof value === 'string' ? value : null);
    if (next) this.tab.set(next);
  }
}
