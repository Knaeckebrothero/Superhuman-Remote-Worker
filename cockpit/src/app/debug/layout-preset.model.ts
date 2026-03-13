import { LayoutConfig } from './layout.model';

/**
 * A saved layout preset that can be loaded by the user.
 */
export interface LayoutPreset {
  /** Unique identifier for the preset */
  id: string;
  /** Display name shown in the UI */
  name: string;
  /** Optional description of the layout */
  description?: string;
  /** Whether this preset should appear in the featured section */
  featured?: boolean;
  /** The layout configuration */
  config: LayoutConfig;
}
