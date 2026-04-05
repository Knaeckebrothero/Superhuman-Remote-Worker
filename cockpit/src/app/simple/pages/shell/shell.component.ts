import {Component, inject, OnInit} from '@angular/core';
import {ViewportService} from '../../../core/services/viewport.service';
import {JobArtifactService} from '../../../core/services/job-artifact.service';
import {ModelService} from '../../../core/services/model.service';
import {MobileShellComponent} from '../../layout/mobile-shell/mobile-shell.component';
import {SidebarToggleComponent} from '../../layout/sidebar-toggle/sidebar-toggle.component';
import {
  InstructionBuilderComponent
} from '../../../shared/components/instruction-builder/instruction-builder.component';

@Component({
  selector: 'app-shell-page',
  standalone: true,
  imports: [MobileShellComponent, SidebarToggleComponent, InstructionBuilderComponent],
  template: `
    @if (viewport.isMobile()) {
      <app-mobile-shell />
    } @else {
      <div class="page">
        <header class="page-header">
          <app-sidebar-toggle />
          <div class="header-spacer"></div>
          <select
            class="model-select"
            [value]="artifacts.builderModel()"
            (change)="onModelChange($event)"
          >
            @for (m of builderModels(); track m.id) {
              <option [value]="m.id">{{ m.label }}</option>
            }
          </select>
          @if (artifacts.streaming()) {
            <span class="streaming-badge">
              <span class="pulse-dot"></span>
            </span>
          }
        </header>
        <main class="page-content">
          <app-instruction-builder />
        </main>
      </div>
    }
  `,
  styles: [
    `
      :host {
        display: block;
        height: 100%;
      }

      .page {
        display: flex;
        flex-direction: column;
        height: 100%;
      }

      .page-header {
        display: flex;
        align-items: center;
        gap: 8px;
        height: 48px;
        padding: 0 12px;
        background: var(--timeline-bg, #11111b);
        border-bottom: 1px solid var(--border-color, #313244);
        flex-shrink: 0;
      }

      .header-spacer {
        flex: 1;
      }

      .model-select {
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

      .model-select:focus-visible {
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
        0%,
        100% {
          opacity: 1;
        }
        50% {
          opacity: 0.3;
        }
      }

      .page-content {
        flex: 1;
        overflow: hidden;
      }
    `,
  ],
})
export class ShellPageComponent implements OnInit {
  readonly viewport = inject(ViewportService);
  readonly artifacts = inject(JobArtifactService);
  private readonly modelService = inject(ModelService);
  readonly builderModels = this.modelService.builderModels;

  ngOnInit(): void {
    this.modelService.load();
  }

  onModelChange(event: Event): void {
    const value = (event.target as HTMLSelectElement).value;
    this.artifacts.builderModel.set(value);
  }
}
