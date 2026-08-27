/**
 * Cytoscape.js styles for graph timeline visualization.
 */

// Cytoscape stylesheet type
export interface CyStyle {
  selector: string;
  style: Record<string, string | number>;
}

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
