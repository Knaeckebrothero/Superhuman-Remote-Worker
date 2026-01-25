/**
 * Cytoscape.js styles for graph timeline visualization.
 * Uses Okabe-Ito colorblind-safe palette for change state indicators.
 */

// Cytoscape stylesheet type
interface CyStyle {
  selector: string;
  style: Record<string, string | number>;
}

/**
 * Okabe-Ito colorblind-safe palette for change states.
 */
export const CHANGE_COLORS = {
  created: '#0072B2',    // Blue
  modified: '#E69F00',   // Orange
  deleted: '#D55E00',    // Vermillion
  unchanged: '#6c7086',  // Muted gray
} as const;

/**
 * Node label colors (domain-specific).
 * Catppuccin Mocha palette.
 */
export const LABEL_COLORS: Record<string, string> = {
  Requirement: '#89b4fa',      // Blue
  BusinessObject: '#a6e3a1',   // Green
  Message: '#f9e2af',          // Yellow
  BusinessService: '#cba6f7',  // Purple
  Process: '#fab387',          // Peach
  Field: '#89dceb',            // Cyan
  Rule: '#f38ba8',             // Red
  Document: '#94e2d5',         // Teal
  Default: '#6c7086',          // Muted
};

/**
 * Get background color for a node label.
 */
export function getLabelColor(label: string): string {
  return LABEL_COLORS[label] ?? LABEL_COLORS['Default'];
}

/**
 * Cytoscape stylesheet for graph rendering.
 */
export const cytoscapeStyles: CyStyle[] = [
  // Base node style
  {
    selector: 'node',
    style: {
      'label': 'data(displayLabel)',
      'background-color': '#6c7086',
      'color': '#1e1e2e',
      'text-valign': 'center',
      'text-halign': 'center',
      'font-size': '11px',
      'font-weight': 600,
      'font-family': '"JetBrains Mono", monospace',
      'width': '60px',
      'height': '60px',
      'border-width': 2,
      'border-color': '#45475a',
      'text-wrap': 'ellipsis',
      'text-max-width': '90px',
      'text-outline-color': '#ffffff',
      'text-outline-width': 1,
      'text-outline-opacity': 0.8,
    },
  },

  // Node label styling by type
  {
    selector: 'node[label="Requirement"]',
    style: { 'background-color': LABEL_COLORS['Requirement'] },
  },
  {
    selector: 'node[label="BusinessObject"]',
    style: { 'background-color': LABEL_COLORS['BusinessObject'] },
  },
  {
    selector: 'node[label="Message"]',
    style: { 'background-color': LABEL_COLORS['Message'] },
  },
  {
    selector: 'node[label="BusinessService"]',
    style: { 'background-color': LABEL_COLORS['BusinessService'] },
  },
  {
    selector: 'node[label="Process"]',
    style: { 'background-color': LABEL_COLORS['Process'] },
  },
  {
    selector: 'node[label="Field"]',
    style: { 'background-color': LABEL_COLORS['Field'] },
  },
  {
    selector: 'node[label="Rule"]',
    style: { 'background-color': LABEL_COLORS['Rule'] },
  },
  {
    selector: 'node[label="Document"]',
    style: { 'background-color': LABEL_COLORS['Document'] },
  },

  // Change state: Created (blue border, solid)
  {
    selector: 'node.created',
    style: {
      'border-width': 4,
      'border-color': CHANGE_COLORS.created,
      'border-style': 'solid',
    },
  },

  // Change state: Modified (orange border, dashed)
  {
    selector: 'node.modified',
    style: {
      'border-width': 4,
      'border-color': CHANGE_COLORS.modified,
      'border-style': 'dashed',
    },
  },

  // Change state: Deleted (vermillion border, reduced opacity)
  {
    selector: 'node.deleted',
    style: {
      'border-width': 4,
      'border-color': CHANGE_COLORS.deleted,
      'border-style': 'solid',
      'opacity': 0.4,
      'background-blacken': 0.3,
    },
  },

  // Selected node
  {
    selector: 'node:selected',
    style: {
      'border-width': 4,
      'border-color': '#f5c2e7',
      'background-blacken': -0.1,
    },
  },

  // Base edge style
  {
    selector: 'edge',
    style: {
      'width': 2,
      'line-color': '#7f849c',
      'target-arrow-color': '#7f849c',
      'target-arrow-shape': 'triangle',
      'curve-style': 'bezier',
      'label': 'data(type)',
      'font-size': '9px',
      'font-family': '"JetBrains Mono", monospace',
      'color': '#cdd6f4',
      'text-rotation': 'autorotate',
      'text-margin-y': -10,
      'text-background-color': '#1e1e2e',
      'text-background-opacity': 0.85,
      'text-background-padding': '2px',
      'text-background-shape': 'roundrectangle',
    },
  },

  // Edge: Created
  {
    selector: 'edge.created',
    style: {
      'line-color': CHANGE_COLORS.created,
      'target-arrow-color': CHANGE_COLORS.created,
      'width': 3,
    },
  },

  // Edge: Deleted
  {
    selector: 'edge.deleted',
    style: {
      'line-color': CHANGE_COLORS.deleted,
      'target-arrow-color': CHANGE_COLORS.deleted,
      'line-style': 'dashed',
      'opacity': 0.4,
    },
  },

  // Selected edge
  {
    selector: 'edge:selected',
    style: {
      'line-color': '#f5c2e7',
      'target-arrow-color': '#f5c2e7',
      'width': 3,
    },
  },

  // Hidden elements (for performance - don't render off-screen)
  {
    selector: '.hidden',
    style: {
      'display': 'none',
    },
  },
];

/**
 * Layout options for fCoSE algorithm.
 * Optimized for stability during timeline scrubbing.
 */
export const fcoseLayoutOptions = {
  name: 'fcose',
  quality: 'proof',
  randomize: false,           // CRITICAL: preserve existing positions
  animate: true,
  animationDuration: 300,
  animationEasing: 'ease-out',
  fit: true,
  padding: 50,

  // Force-directed parameters
  nodeRepulsion: 4500,
  idealEdgeLength: 100,
  edgeElasticity: 0.45,
  nestingFactor: 0.1,
  gravity: 0.25,

  // Node dimensions
  nodeDimensionsIncludeLabels: true,

  // Incremental layout
  uniformNodeDimensions: false,
  packComponents: true,

  // Sampling (for large graphs)
  samplingType: true,
  sampleSize: 25,
  nodeSeparation: 75,

  // Performance
  tile: true,
  tilingPaddingVertical: 10,
  tilingPaddingHorizontal: 10,
};

/**
 * Simple grid layout for initial placement.
 */
export const gridLayoutOptions = {
  name: 'grid',
  fit: true,
  padding: 30,
  avoidOverlap: true,
  avoidOverlapPadding: 10,
  nodeDimensionsIncludeLabels: true,
  condense: true,
  rows: undefined,
  cols: undefined,
};

/**
 * Concentric layout (useful for hierarchical data).
 */
export const concentricLayoutOptions = {
  name: 'concentric',
  fit: true,
  padding: 30,
  startAngle: (3 / 2) * Math.PI,
  sweep: undefined,
  clockwise: true,
  equidistant: false,
  minNodeSpacing: 50,
  avoidOverlap: true,
  nodeDimensionsIncludeLabels: true,
  concentric: (node: { degree: () => number }) => node.degree(),
  levelWidth: () => 2,
};
