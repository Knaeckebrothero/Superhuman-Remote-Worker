import { Injectable, Type } from '@angular/core';
import { ComponentMetadata, ComponentType } from '../models/layout.model';

/**
 * Registry for components that can be loaded into layout panels.
 * Components must be registered before they can be used in layouts.
 */
@Injectable({
  providedIn: 'root',
})
export class ComponentRegistryService {
  private registry = new Map<ComponentType, ComponentMetadata>();

  /**
   * Register a component for use in the layout system.
   */
  register(metadata: ComponentMetadata): void {
    this.registry.set(metadata.type, metadata);
  }

  /**
   * Get metadata for a component type.
   */
  get(type: ComponentType): ComponentMetadata | undefined {
    return this.registry.get(type);
  }

  /**
   * Get the Angular component class for a type.
   */
  getComponent(type: ComponentType): Type<unknown> | undefined {
    return this.registry.get(type)?.component;
  }

  /**
   * Get display name for a component type.
   */
  getDisplayName(type: ComponentType): string {
    return this.registry.get(type)?.displayName ?? type;
  }

  /**
   * Check if a component type is registered.
   */
  has(type: ComponentType): boolean {
    return this.registry.has(type);
  }

  /**
   * Get all registered component types.
   */
  getRegisteredTypes(): ComponentType[] {
    return Array.from(this.registry.keys());
  }
}
