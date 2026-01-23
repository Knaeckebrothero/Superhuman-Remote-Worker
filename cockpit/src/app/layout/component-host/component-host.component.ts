import {
  Component,
  input,
  inject,
  ViewContainerRef,
  effect,
  viewChild,
} from '@angular/core';
import { ComponentType } from '../../core/models/layout.model';
import { ComponentRegistryService } from '../../core/services/component-registry.service';
import { PanelHeaderComponent } from '../panel-header/panel-header.component';

/**
 * Dynamically loads and displays a registered component.
 * Wraps the component with a panel header showing the display name.
 */
@Component({
  selector: 'app-component-host',
  imports: [PanelHeaderComponent],
  template: `
    <div class="component-host">
      <app-panel-header [title]="displayName()" />
      <div class="component-content">
        <ng-container #outlet />
      </div>
    </div>
  `,
  styles: [
    `
      .component-host {
        display: flex;
        flex-direction: column;
        height: 100%;
        background: var(--panel-bg, #181825);
      }

      .component-content {
        flex: 1;
        overflow: auto;
        position: relative;
      }
    `,
  ],
})
export class ComponentHostComponent {
  private readonly registry = inject(ComponentRegistryService);

  readonly componentType = input.required<ComponentType>();
  private readonly outlet = viewChild('outlet', { read: ViewContainerRef });

  constructor() {
    effect(() => {
      const type = this.componentType();
      const container = this.outlet();
      if (!container) return;

      container.clear();

      const componentClass = this.registry.getComponent(type);
      if (componentClass) {
        container.createComponent(componentClass);
      }
    });
  }

  displayName(): string {
    return this.registry.getDisplayName(this.componentType());
  }
}
