/**
 * Models for graph timeline visualization.
 * Used to represent Neo4j graph state at different points in time.
 */

/**
 * Response from /api/graph/changes/{job_id}
 */
export interface GraphChangeResponse {
  jobId: string;
  timeRange: TimeRange | null;
  summary: GraphChangeSummary;
  snapshots: GraphSnapshot[];
  deltas: GraphDelta[];
}

export interface TimeRange {
  start: string;
  end: string;
}

export interface GraphChangeSummary {
  totalToolCalls: number;
  graphToolCalls: number;
  nodesCreated: number;
  nodesDeleted: number;
  nodesModified: number;
  relationshipsCreated: number;
  relationshipsDeleted: number;
}

/**
 * Complete graph state at a point in time.
 * Created at regular intervals for fast seeking.
 */
export interface GraphSnapshot {
  /** Timestamp of this snapshot */
  timestamp: string;

  /** Index of the tool call this snapshot represents */
  toolCallIndex: number;

  /** All nodes that exist at this point (keyed by ID) */
  nodes: Record<string, NodeState>;

  /** All relationships that exist at this point (keyed by ID) */
  relationships: Record<string, RelationshipState>;

  /** Pre-computed node positions for layout stability */
  positions?: Record<string, Position>;
}

export interface Position {
  x: number;
  y: number;
}

/**
 * Incremental change between tool calls.
 * Applied sequentially during slow scrubbing.
 */
export interface GraphDelta {
  /** Timestamp of this change */
  timestamp: string;

  /** Tool call index */
  toolCallIndex: number;

  /** Original Cypher query (for display) */
  cypherQuery: string;

  /** Tool call ID from MongoDB (for linking to audit panel) */
  toolCallId: string;

  /** Step number in audit trail */
  stepNumber?: number;

  /** The actual changes */
  changes: GraphChanges;
}

export interface GraphChanges {
  nodesCreated: NodeCreation[];
  nodesDeleted: NodeDeletion[];
  nodesModified: NodeModification[];
  relationshipsCreated: RelationshipCreation[];
  relationshipsDeleted: RelationshipDeletion[];
  matchedVariables?: MatchedVariable[];
}

export interface MatchedVariable {
  variable: string;
  label: string;
  properties: Record<string, unknown>;
}

export interface NodeCreation {
  variable: string;
  label: string;
  properties: Record<string, unknown>;
  merge?: boolean;
}

export interface NodeDeletion {
  variable: string;
  detach: boolean;
}

export interface NodeModification {
  variable: string;
  property?: string;
  value?: unknown;
  removed?: boolean;
  labelRemoved?: string;
}

export interface RelationshipCreation {
  sourceVar: string;
  type: string;
  properties: Record<string, unknown>;
  targetVar: string;
  merge?: boolean;
}

export interface RelationshipDeletion {
  variable: string;
}

/**
 * State of a node at a specific point in time.
 */
export interface NodeState {
  id: string;
  labels: string[];
  properties: Record<string, unknown>;

  /** When this node was first seen (tool call index) */
  createdAt: number;

  /** Last modification (tool call index) */
  modifiedAt: number;

  /** When this node was deleted (tool call index) */
  deletedAt?: number;

  /** Visibility flag for deleted nodes */
  visible: boolean;
}

/**
 * State of a relationship at a specific point in time.
 */
export interface RelationshipState {
  id: string;
  type: string;
  sourceId: string;
  targetId: string;
  properties: Record<string, unknown>;

  /** When this relationship was created (tool call index) */
  createdAt: number;

  /** When this relationship was deleted (tool call index) */
  deletedAt?: number;

  /** Visibility flag */
  visible: boolean;
}

/**
 * Change state for visual highlighting.
 */
export type ChangeState = 'unchanged' | 'created' | 'modified' | 'deleted';

/**
 * Node with computed change state for rendering.
 */
export interface RenderNode extends NodeState {
  changeState: ChangeState;
}

/**
 * Relationship with computed change state for rendering.
 */
export interface RenderRelationship extends RelationshipState {
  changeState: ChangeState;
}
