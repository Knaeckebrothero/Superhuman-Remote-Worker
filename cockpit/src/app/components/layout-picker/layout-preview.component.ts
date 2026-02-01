import { Component, Input, computed } from '@angular/core';
import { LayoutConfig } from '../../core/models/layout.model';

/**
 * Component that renders a mini SVG preview of a layout configuration.
 * Used in the layout picker to show visual representations of each preset.
 */
@Component({
  selector: 'app-layout-preview',
  template: `
    <svg
      [attr.viewBox]="'0 0 ' + width + ' ' + height"
      [attr.width]="width"
      [attr.height]="height"
      class="layout-preview"
    >
      @for (rect of rectangles(); track $index) {
        <rect
          [attr.x]="rect.x"
          [attr.y]="rect.y"
          [attr.width]="rect.width"
          [attr.height]="rect.height"
          [attr.fill]="rect.fill"
          [attr.stroke]="strokeColor"
          stroke-width="1"
          rx="2"
        />
      }
    </svg>
  `,
  styles: [
    `
      .layout-preview {
        display: block;
      }
    `,
  ],
})
export class LayoutPreviewComponent {
  @Input({ required: true }) config!: LayoutConfig;
  @Input() width = 80;
  @Input() height = 50;

  // Catppuccin Mocha colors for visual distinction
  private readonly colors = [
    '#89b4fa', // Blue
    '#a6e3a1', // Green
    '#f9e2af', // Yellow
    '#fab387', // Peach
    '#cba6f7', // Mauve
    '#f38ba8', // Red
    '#94e2d5', // Teal
    '#f5c2e7', // Pink
  ];

  readonly strokeColor = '#313244'; // Surface 0
  readonly bgColor = '#1e1e2e'; // Base

  /**
   * Computed array of rectangles to render.
   */
  readonly rectangles = computed(() => {
    const rects: { x: number; y: number; width: number; height: number; fill: string }[] = [];
    let colorIndex = 0;

    const generateRects = (
      config: LayoutConfig,
      x: number,
      y: number,
      width: number,
      height: number
    ): void => {
      const padding = 2;

      if (config.type === 'component') {
        // Leaf node - draw a rectangle
        rects.push({
          x: x + padding,
          y: y + padding,
          width: Math.max(0, width - padding * 2),
          height: Math.max(0, height - padding * 2),
          fill: this.colors[colorIndex % this.colors.length],
        });
        colorIndex++;
        return;
      }

      // Split node - divide space and recurse
      if (!config.children || !config.sizes) {
        return;
      }

      const isVertical = config.direction === 'vertical';
      let offset = 0;

      config.children.forEach((child, i) => {
        const size = config.sizes![i] / 100;
        const childWidth = isVertical ? width * size : width;
        const childHeight = isVertical ? height : height * size;
        const childX = isVertical ? x + offset : x;
        const childY = isVertical ? y : y + offset;

        generateRects(child, childX, childY, childWidth, childHeight);

        offset += isVertical ? childWidth : childHeight;
      });
    };

    if (this.config) {
      generateRects(this.config, 0, 0, this.width, this.height);
    }

    return rects;
  });
}
