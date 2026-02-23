import {
  Component,
  inject,
  signal,
  ViewContainerRef,
  viewChild,
  effect,
} from '@angular/core';
import { ComponentRegistryService } from '../../core/services/component-registry.service';
import { DataService } from '../../core/services/data.service';
import { JobContextService } from '../../core/services/job-context.service';
import { JobArtifactService } from '../../core/services/job-artifact.service';
import { SidebarToggleComponent } from '../sidebar-toggle/sidebar-toggle.component';
import { ComponentType } from '../../core/models/layout.model';
import { environment } from '../../core/environment';

interface MobileTab {
  id: string;
  label: string;
  icon: string;
  componentType: ComponentType;
}

const TABS: MobileTab[] = [
  { id: 'builder', label: 'Builder', icon: 'construction', componentType: 'instruction-builder' },
  { id: 'jobs', label: 'Jobs', icon: 'work', componentType: 'job-list' },
  { id: 'projects', label: 'Projects', icon: 'folder_shared', componentType: 'project-list' },
  { id: 'create', label: 'Create', icon: 'add_circle', componentType: 'job-create' },
  { id: 'review', label: 'Review', icon: 'rate_review', componentType: 'job-review' },
];

/**
 * Mobile-first shell that replaces the split-panel layout on small screens.
 *
 * Shows a compact header, full-height content area hosting one component
 * at a time, and a bottom tab bar for navigation.
 */
@Component({
  selector: 'app-mobile-shell',
  standalone: true,
  imports: [SidebarToggleComponent],
  template: `
    <div class="mobile-shell">
      <header class="mobile-header">
        <app-sidebar-toggle />
        <span class="mobile-title">SRW</span>

        @if (activeTab() === 'builder') {
          <select
            class="mobile-model-select"
            [value]="artifacts.builderModel()"
            (change)="onModelChange($event)"
          >
            @for (m of builderModels; track m.id) {
              <option [value]="m.id">{{ m.label }}</option>
            }
          </select>
          @if (artifacts.streaming()) {
            <span class="streaming-badge">
              <span class="pulse-dot"></span>
            </span>
          }
        }

        <div class="header-spacer"></div>

        <select
          class="mobile-job-select"
          [value]="jobContext.activeJobId() ?? ''"
          (change)="onJobChange($event)"
        >
          <option value="">No job context</option>
          @for (job of jobContext.jobs(); track job.id) {
            <option [value]="job.id">{{ job.id.slice(0, 8) }}… · {{ job.status }}</option>
          }
        </select>
      </header>

      <main class="mobile-content">
        <ng-container #outlet />
      </main>

      <nav class="mobile-tab-bar">
        @for (tab of tabs; track tab.id) {
          <button
            class="tab-button"
            [class.active]="activeTab() === tab.id"
            (click)="switchTab(tab)"
          >
            <span class="tab-icon">{{ tab.icon }}</span>
            <span class="tab-label">{{ tab.label }}</span>
          </button>
        }
      </nav>
    </div>
  `,
  styles: [
    `
      :host {
        display: block;
        height: 100%;
        width: 100%;
      }

      .mobile-shell {
        display: flex;
        flex-direction: column;
        height: 100vh;
        height: 100dvh;
        background: var(--app-bg, #1e1e2e);
      }

      .mobile-header {
        display: flex;
        align-items: center;
        gap: 8px;
        height: 48px;
        padding: 0 12px;
        background: var(--timeline-bg, #11111b);
        border-bottom: 1px solid var(--border-color, #313244);
        flex-shrink: 0;
      }

      .mobile-title {
        font-weight: 700;
        font-size: 16px;
        color: var(--accent-color, #cba6f7);
        letter-spacing: 1px;
      }

      .mobile-job-select {
        background: var(--surface-0, #313244);
        color: var(--text-secondary, #a6adc8);
        border: 1px solid var(--border-color, #313244);
        border-radius: 6px;
        padding: 6px 8px;
        font-size: 12px;
        max-width: 200px;
        min-height: 36px;
        cursor: pointer;
        outline: none;
      }

      .mobile-job-select:focus {
        border-color: var(--accent-color, #cba6f7);
      }

      .mobile-model-select {
        background: var(--surface-0, #313244);
        color: var(--text-secondary, #a6adc8);
        border: 1px solid var(--border-color, #313244);
        border-radius: 6px;
        padding: 6px 8px;
        font-size: 12px;
        max-width: 160px;
        min-height: 36px;
        cursor: pointer;
        outline: none;
      }

      .mobile-model-select:focus {
        border-color: var(--accent-color, #cba6f7);
      }

      .streaming-badge {
        display: flex;
        align-items: center;
        flex-shrink: 0;
      }

      .pulse-dot {
        width: 8px;
        height: 8px;
        border-radius: 50%;
        background: var(--accent-color, #cba6f7);
        animation: pulse 1.5s ease-in-out infinite;
      }

      @keyframes pulse {
        0%, 100% { opacity: 1; }
        50% { opacity: 0.3; }
      }

      .header-spacer {
        flex: 1;
      }

      .mobile-content {
        flex: 1;
        overflow: hidden;
        position: relative;
      }

      .mobile-tab-bar {
        display: flex;
        height: 56px;
        background: var(--timeline-bg, #11111b);
        border-top: 1px solid var(--border-color, #313244);
        flex-shrink: 0;
        padding-bottom: env(safe-area-inset-bottom, 0px);
      }

      .tab-button {
        flex: 1;
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        gap: 2px;
        background: none;
        border: none;
        color: var(--text-muted, #6c7086);
        cursor: pointer;
        min-height: 44px;
        min-width: 44px;
        padding: 4px;
        -webkit-tap-highlight-color: transparent;
        transition: color 0.15s ease;
      }

      .tab-button.active {
        color: var(--accent-color, #cba6f7);
      }

      .tab-button:active {
        opacity: 0.7;
      }

      .tab-icon {
        font-family: 'Material Symbols Outlined';
        font-size: 22px;
      }

      .tab-label {
        font-size: 10px;
        font-weight: 500;
        letter-spacing: 0.3px;
      }
    `,
  ],
})
export class MobileShellComponent {
  private readonly registry = inject(ComponentRegistryService);
  readonly data = inject(DataService);
  readonly jobContext = inject(JobContextService);
  readonly artifacts = inject(JobArtifactService);
  readonly builderModels = environment.builderModels;

  private readonly outlet = viewChild('outlet', { read: ViewContainerRef });

  readonly tabs = TABS;
  readonly activeTab = signal('builder');

  constructor() {
    this.jobContext.loadJobs();

    effect(() => {
      const tab = this.tabs.find((t) => t.id === this.activeTab());
      const container = this.outlet();
      if (!container || !tab) return;

      container.clear();
      const componentClass = this.registry.getComponent(tab.componentType);
      if (componentClass) {
        container.createComponent(componentClass);
      }
    });
  }

  switchTab(tab: MobileTab): void {
    this.activeTab.set(tab.id);
  }

  onModelChange(event: Event): void {
    const value = (event.target as HTMLSelectElement).value;
    this.artifacts.builderModel.set(value);
  }

  onJobChange(event: Event): void {
    const value = (event.target as HTMLSelectElement).value;
    this.data.setCurrentJob(value || null);
  }
}
