import { Injectable, signal, computed, PLATFORM_ID, inject } from '@angular/core';
import { isPlatformBrowser } from '@angular/common';
import { LayoutConfig } from '../models/layout.model';

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
      { type: 'component', component: 'placeholder-a' },
      { type: 'component', component: 'placeholder-b' },
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

  constructor() {
    this.loadFromStorage();
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
   * Load a preset layout by name.
   */
  loadPreset(presetName: 'default' | 'two-column' | 'grid'): void {
    switch (presetName) {
      case 'default':
        this.setLayout(getDefaultLayout());
        break;
      case 'two-column':
        this.setLayout({
          type: 'split',
          direction: 'vertical',
          sizes: [30, 70],
          children: [
            { type: 'component', component: 'placeholder-a' },
            { type: 'component', component: 'placeholder-b' },
          ],
        });
        break;
      case 'grid':
        this.setLayout({
          type: 'split',
          direction: 'vertical',
          sizes: [50, 50],
          children: [
            {
              type: 'split',
              direction: 'horizontal',
              sizes: [50, 50],
              children: [
                { type: 'component', component: 'placeholder-a' },
                { type: 'component', component: 'placeholder-b' },
              ],
            },
            {
              type: 'split',
              direction: 'horizontal',
              sizes: [50, 50],
              children: [
                { type: 'component', component: 'placeholder-c' },
                { type: 'component', component: 'placeholder-a' },
              ],
            },
          ],
        });
        break;
    }
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
}
