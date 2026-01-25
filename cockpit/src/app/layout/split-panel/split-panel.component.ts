import { Component, input, inject } from '@angular/core';
import { AngularSplitModule, SplitGutterInteractionEvent } from 'angular-split';
import { LayoutConfig } from '../../core/models/layout.model';
import { LayoutService } from '../../core/services/layout.service';
import { ComponentHostComponent } from '../component-host/component-host.component';

/**
 * Recursive component that renders the layout configuration.
 * For split nodes, renders an as-split with child panels.
 * For component nodes, renders a component-host.
 */
@Component({
  selector: 'app-split-panel',
  imports: [AngularSplitModule, ComponentHostComponent],
  template: `
    @if (config().type === 'component' && config().component) {
      <app-component-host [componentType]="config().component!" [path]="path()" />
    } @else if (config().type === 'split' && config().children) {
      <as-split
        [direction]="config().direction === 'horizontal' ? 'vertical' : 'horizontal'"
        [gutterSize]="6"
        (dragEnd)="onDragEnd($event)"
      >
        @for (child of config().children; track $index) {
          <as-split-area [size]="getSize($index)">
            <app-split-panel [config]="child" [path]="childPath($index)" />
          </as-split-area>
        }
      </as-split>
    }
  `,
  styles: [
    `
      :host {
        display: block;
        height: 100%;
        width: 100%;
      }

      as-split {
        height: 100%;
      }
    `,
  ],
})
export class SplitPanelComponent {
  private readonly layoutService = inject(LayoutService);

  readonly config = input.required<LayoutConfig>();
  readonly path = input<number[]>([]);

  getSize(index: number): number {
    const sizes = this.config().sizes;
    return sizes?.[index] ?? 100 / (this.config().children?.length ?? 1);
  }

  childPath(index: number): number[] {
    return [...this.path(), index];
  }

  onDragEnd(event: SplitGutterInteractionEvent): void {
    const sizes = event.sizes.map((s) => (typeof s === 'number' ? s : 0));
    this.layoutService.updateSizes(this.path(), sizes);
  }
}
