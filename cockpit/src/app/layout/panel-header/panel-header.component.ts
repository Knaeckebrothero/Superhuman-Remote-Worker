import { Component, input } from '@angular/core';

/**
 * Header bar for layout panels.
 * Displays the panel title with optional icon.
 */
@Component({
  selector: 'app-panel-header',
  template: `
    <div class="panel-header">
      @if (icon()) {
        <span class="panel-icon">{{ icon() }}</span>
      }
      <span class="panel-title">{{ title() }}</span>
    </div>
  `,
  styles: [
    `
      .panel-header {
        display: flex;
        align-items: center;
        gap: 8px;
        height: 32px;
        padding: 0 12px;
        background: var(--panel-header-bg, #1e1e2e);
        border-bottom: 1px solid var(--border-color, #313244);
        font-size: 12px;
        font-weight: 500;
        color: var(--text-secondary, #a6adc8);
        user-select: none;
      }

      .panel-icon {
        font-size: 14px;
      }

      .panel-title {
        text-transform: uppercase;
        letter-spacing: 0.5px;
      }
    `,
  ],
})
export class PanelHeaderComponent {
  readonly title = input.required<string>();
  readonly icon = input<string>();
}
