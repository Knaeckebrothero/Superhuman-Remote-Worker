import {ChangeDetectionStrategy, Component, OnInit, inject, signal} from '@angular/core';
import {ActivatedRoute} from '@angular/router';
import {SidebarToggleComponent} from '../../../shell/sidebar-toggle/sidebar-toggle.component';
import {AppTabNavComponent, AppTabNavItemComponent} from '../../../ui/tab-nav';
import {AdminProvidersComponent} from '../providers/admin-providers.component';
import {AdminModelsComponent} from '../models/admin-models.component';
import {AdminDefaultsComponent} from '../defaults/admin-defaults.component';

type AdminLlmTab = 'providers' | 'models' | 'defaults';

@Component({
  selector: 'app-admin-llm',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    SidebarToggleComponent,
    AppTabNavComponent,
    AppTabNavItemComponent,
    AdminProvidersComponent,
    AdminModelsComponent,
    AdminDefaultsComponent,
  ],
  template: `
    <div class="admin-page">
      <div class="admin-container">
        <div class="page-header">
          <app-sidebar-toggle />
          <h1 class="page-title">LLM Configuration</h1>
        </div>
        <p class="page-desc">
          Provider keys, system endpoints, model catalog, and default-model
          assignments — managed in one place.
        </p>

        <app-tab-nav
          class="admin-llm-tabs"
          [value]="tab()"
          (valueChange)="onTabChange($event)"
        >
          <app-tab-nav-item value="providers">Providers</app-tab-nav-item>
          <app-tab-nav-item value="models">Models</app-tab-nav-item>
          <app-tab-nav-item value="defaults">Defaults</app-tab-nav-item>
        </app-tab-nav>

        <div class="tab-content">
          <div class="tab-panel" [class.tab-hidden]="tab() !== 'providers'">
            <app-admin-providers (switchTab)="onTabChange($event)" />
          </div>
          <div class="tab-panel" [class.tab-hidden]="tab() !== 'models'">
            <app-admin-models />
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
    .admin-llm-tabs {
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
export class AdminLlmComponent implements OnInit {
  private readonly route = inject(ActivatedRoute);

  readonly tab = signal<AdminLlmTab>('providers');

  ngOnInit(): void {
    const initial = this.route.snapshot.queryParamMap.get('tab');
    if (initial === 'providers' || initial === 'models' || initial === 'defaults') {
      this.tab.set(initial);
    }
  }

  protected onTabChange(value: AdminLlmTab | string | null): void {
    if (value === 'providers' || value === 'models' || value === 'defaults') {
      this.tab.set(value);
    }
  }
}
