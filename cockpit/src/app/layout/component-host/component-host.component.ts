import {
  Component,
  input,
  inject,
  ViewContainerRef,
  effect,
  viewChild,
  computed,
} from '@angular/core';
import { ComponentMetadata, ComponentType } from '../../core/models/layout.model';
import { ComponentRegistryService } from '../../core/services/component-registry.service';
import { LayoutService } from '../../core/services/layout.service';
import { PanelHeaderComponent } from '../panel-header/panel-header.component';

/**
 * Dynamically loads and displays a registered component.
 * Wraps the component with a panel header showing the display name
 * and controls for switching components, splitting, and closing.
 */
@Component({
  selector: 'app-component-host',
  imports: [PanelHeaderComponent],
  template: `
    <div class="component-host">
      <app-panel-header
        [title]="displayName()"
        [componentType]="componentType()"
        [availableComponents]="availableComponents()"
        [canClose]="canClose()"
        (componentChange)="onComponentChange($event)"
        (splitHorizontal)="onSplitHorizontal()"
        (splitVertical)="onSplitVertical()"
        (close)="onClose()"
      />
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
  private readonly layoutService = inject(LayoutService);

  readonly componentType = input.required<ComponentType>();
  readonly path = input<number[]>([]);
  private readonly outlet = viewChild('outlet', { read: ViewContainerRef });

  /** Available components for the dropdown */
  readonly availableComponents = computed<ComponentMetadata[]>(() => {
    return this.registry
      .getRegisteredTypes()
      .map((type) => this.registry.get(type))
      .filter((meta): meta is ComponentMetadata => meta !== undefined);
  });

  /** Whether this panel can be closed (more than one panel exists) */
  readonly canClose = computed(() => this.layoutService.getPanelCount() > 1);

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

  onComponentChange(newType: ComponentType): void {
    this.layoutService.updateComponent(this.path(), newType);
  }

  onSplitHorizontal(): void {
    this.layoutService.splitPanel(this.path(), 'horizontal');
  }

  onSplitVertical(): void {
    this.layoutService.splitPanel(this.path(), 'vertical');
  }

  onClose(): void {
    this.layoutService.closePanel(this.path());
  }
}
