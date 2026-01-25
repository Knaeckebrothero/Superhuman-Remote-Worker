import { Injectable, signal, computed, PLATFORM_ID, inject } from '@angular/core';
import { isPlatformBrowser } from '@angular/common';
import { ComponentType, LayoutConfig } from '../models/layout.model';
import { LayoutPreset } from '../models/layout-preset.model';

const STORAGE_KEY = 'cockpit-layout';

/**
 * Returns the default 3-column layout.
 */
function getDefaultLayout(): LayoutConfig {
  return {
    type: 'split',
    direction: 'vertical', // left-to-right columns
    sizes: [25, 50, 25],
    children: [
      { type: 'component', component: 'request-viewer' },
      { type: 'component', component: 'agent-activity' },
      { type: 'component', component: 'db-table' },
    ],
  };
}

/**
 * Service managing the layout configuration using Angular signals.
 * Supports persistence to localStorage and preset layouts.
 */
@Injectable({
  providedIn: 'root',
})
export class LayoutService {
  private readonly platformId = inject(PLATFORM_ID);

  /** Current layout configuration */
  readonly layout = signal<LayoutConfig>(getDefaultLayout());

  /** Whether the layout has been modified from default */
  readonly isModified = computed(() => {
    const current = JSON.stringify(this.layout());
    const defaultLayout = JSON.stringify(getDefaultLayout());
    return current !== defaultLayout;
  });

  /** Available layout presets loaded from assets */
  private readonly presetsSignal = signal<LayoutPreset[]>([]);
  readonly availablePresets = this.presetsSignal.asReadonly();

  /** List of preset file names to load */
  private readonly presetFiles = [
    'single',
    'two-column',
    'two-row',
    'three-column',
    'left-right-stack',
    'grid-2x2',
  ];

  constructor() {
    this.loadFromStorage();
    this.loadPresetsFromAssets();
  }

  /**
   * Set a new layout configuration.
   */
  setLayout(config: LayoutConfig): void {
    this.layout.set(config);
    this.saveToStorage();
  }

  /**
   * Reset to default layout.
   */
  resetLayout(): void {
    this.layout.set(getDefaultLayout());
    this.saveToStorage();
  }

  /**
   * Update sizes at a specific path in the layout tree.
   * Path is an array of indices into the children arrays.
   */
  updateSizes(path: number[], sizes: number[]): void {
    const current = this.layout();
    const updated = this.updateSizesAtPath(current, path, sizes);
    this.layout.set(updated);
    this.saveToStorage();
  }

  /**
   * Update the component type at a specific path in the layout tree.
   * Path is an array of indices into the children arrays.
   */
  updateComponent(path: number[], componentType: ComponentType): void {
    const current = this.layout();
    const updated = this.updateComponentAtPath(current, path, componentType);
    this.layout.set(updated);
    this.saveToStorage();
  }

  /**
   * Split a panel into two at the given path.
   * Replaces the component with a split containing two copies.
   */
  splitPanel(path: number[], direction: 'horizontal' | 'vertical'): void {
    const current = this.layout();
    const updated = this.splitAtPath(current, path, direction);
    this.layout.set(updated);
    this.saveToStorage();
  }

  /**
   * Close/remove a panel at the given path.
   * Redistributes space to siblings. Unwraps if only one sibling remains.
   */
  closePanel(path: number[]): void {
    // Can't close if it's the root and only panel
    if (path.length === 0) {
      return;
    }
    const current = this.layout();
    const updated = this.closeAtPath(current, path);
    if (updated) {
      this.layout.set(updated);
      this.saveToStorage();
    }
  }

  /**
   * Count total panels in the layout.
   */
  getPanelCount(): number {
    return this.countPanels(this.layout());
  }

  /**
   * Apply a preset by its ID.
   */
  applyPreset(presetId: string): void {
    const preset = this.presetsSignal().find((p) => p.id === presetId);
    if (preset) {
      this.setLayout(preset.config);
    }
  }

  /**
   * Load preset files from assets folder.
   */
  private async loadPresetsFromAssets(): Promise<void> {
    if (!isPlatformBrowser(this.platformId)) {
      return;
    }

    const loaded: LayoutPreset[] = [];
    for (const name of this.presetFiles) {
      try {
        const resp = await fetch(`/assets/layout-presets/${name}.json`);
        if (resp.ok) {
          const preset = (await resp.json()) as LayoutPreset;
          if (this.isValidPreset(preset)) {
            loaded.push(preset);
          }
        }
      } catch {
        // Ignore failed loads
      }
    }
    this.presetsSignal.set(loaded);
  }

  /**
   * Validate a preset object.
   */
  private isValidPreset(preset: unknown): preset is LayoutPreset {
    if (!preset || typeof preset !== 'object') {
      return false;
    }
    const p = preset as LayoutPreset;
    return (
      typeof p.id === 'string' &&
      typeof p.name === 'string' &&
      this.isValidLayout(p.config)
    );
  }

  /**
   * Save current layout to localStorage.
   */
  saveToStorage(): void {
    if (isPlatformBrowser(this.platformId)) {
      try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(this.layout()));
      } catch {
        // Storage may be unavailable or full
      }
    }
  }

  /**
   * Load layout from localStorage if available.
   */
  loadFromStorage(): void {
    if (isPlatformBrowser(this.platformId)) {
      try {
        const stored = localStorage.getItem(STORAGE_KEY);
        if (stored) {
          const parsed = JSON.parse(stored) as LayoutConfig;
          if (this.isValidLayout(parsed)) {
            this.layout.set(parsed);
          }
        }
      } catch {
        // Invalid stored data, use default
      }
    }
  }

  /**
   * Recursively update sizes at the given path.
   */
  private updateSizesAtPath(config: LayoutConfig, path: number[], sizes: number[]): LayoutConfig {
    if (path.length === 0) {
      return { ...config, sizes };
    }

    if (!config.children) {
      return config;
    }

    const [head, ...rest] = path;
    const newChildren = config.children.map((child, index) =>
      index === head ? this.updateSizesAtPath(child, rest, sizes) : child
    );

    return { ...config, children: newChildren };
  }

  /**
   * Recursively update component type at the given path.
   */
  private updateComponentAtPath(
    config: LayoutConfig,
    path: number[],
    componentType: ComponentType
  ): LayoutConfig {
    if (path.length === 0) {
      return { ...config, component: componentType };
    }

    if (!config.children) {
      return config;
    }

    const [head, ...rest] = path;
    const newChildren = config.children.map((child, index) =>
      index === head ? this.updateComponentAtPath(child, rest, componentType) : child
    );

    return { ...config, children: newChildren };
  }

  /**
   * Basic validation for layout configuration.
   */
  private isValidLayout(config: unknown): config is LayoutConfig {
    if (!config || typeof config !== 'object') {
      return false;
    }
    const c = config as LayoutConfig;
    if (c.type !== 'component' && c.type !== 'split') {
      return false;
    }
    return true;
  }

  /**
   * Count panels recursively.
   */
  private countPanels(config: LayoutConfig): number {
    if (config.type === 'component') {
      return 1;
    }
    return config.children?.reduce((sum, child) => sum + this.countPanels(child), 0) ?? 0;
  }

  /**
   * Recursively split at the given path.
   */
  private splitAtPath(
    config: LayoutConfig,
    path: number[],
    direction: 'horizontal' | 'vertical'
  ): LayoutConfig {
    if (path.length === 0) {
      // This is the target - replace with a split containing two copies
      const component = config.component ?? 'agent-activity';
      return {
        type: 'split',
        direction,
        sizes: [50, 50],
        children: [
          { type: 'component', component },
          { type: 'component', component },
        ],
      };
    }

    if (!config.children) {
      return config;
    }

    const [head, ...rest] = path;
    const newChildren = config.children.map((child, index) =>
      index === head ? this.splitAtPath(child, rest, direction) : child
    );

    return { ...config, children: newChildren };
  }

  /**
   * Recursively close at the given path.
   * Returns the updated config, or null if the entire tree should be removed.
   */
  private closeAtPath(config: LayoutConfig, path: number[]): LayoutConfig | null {
    if (path.length === 0) {
      // This shouldn't happen at root level (guarded in closePanel)
      return null;
    }

    if (path.length === 1) {
      // We're at the parent of the node to remove
      const indexToRemove = path[0];
      if (!config.children || config.type !== 'split') {
        return config;
      }

      // Remove the child
      const newChildren = config.children.filter((_, i) => i !== indexToRemove);

      // If only one child remains, unwrap (return the child directly)
      if (newChildren.length === 1) {
        return newChildren[0];
      }

      // Redistribute sizes evenly
      const newSize = 100 / newChildren.length;
      const newSizes = newChildren.map(() => newSize);

      return {
        ...config,
        children: newChildren,
        sizes: newSizes,
      };
    }

    // Recurse deeper
    if (!config.children) {
      return config;
    }

    const [head, ...rest] = path;
    const newChildren = config.children.map((child, index) => {
      if (index === head) {
        const result = this.closeAtPath(child, rest);
        return result ?? child; // If null, keep the child (shouldn't happen)
      }
      return child;
    });

    return { ...config, children: newChildren };
  }
}
