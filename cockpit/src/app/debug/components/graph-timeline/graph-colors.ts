import type {CyStyle} from './graph-styles';

/** Okabe-Ito colorblind-safe change-state palette — intentionally NOT themed. */
const CHANGE_CREATED = '#0072B2';
const CHANGE_MODIFIED = '#E69F00';
const CHANGE_DELETED = '#D55E00';

/** Node label → ramp slot (nearest Imperial hue to the old Catppuccin hue). */
const NODE_TYPE_TOKEN: Record<string, string> = {
  Requirement: '--cat-6',      // lapis  (was blue)
  BusinessObject: '--cat-4',   // olive  (was green)
  Message: '--cat-3',          // gold   (was yellow)
  BusinessService: '--cat-7',  // violet (was purple)
  Process: '--cat-2',          // copper (was peach)
  Field: '--cat-5',            // slate-teal (was cyan)
  Rule: '--cat-1',             // terracotta (was red)
  Document: '--cat-8',         // mauve  (was teal)
};

export interface GraphColors {
  nodeType: Record<string, string>;
  nodeDefault: string;
  nodeLabelText: string;
  nodeOutline: string;
  nodeBorder: string;
  selected: string;
  edgeLine: string;
  edgeLabelText: string;
  edgeLabelBg: string;
  changeCreated: string;
  changeModified: string;
  changeDeleted: string;
}

const defaultReader = (name: string): string =>
  getComputedStyle(document.body).getPropertyValue(name).trim();

/**
 * Resolve the theme tokens the graph needs into concrete color strings.
 * `read` is injectable so tests can bypass jsdom (which does not compute custom props).
 */
export function resolveGraphColors(read: (name: string) => string = defaultReader): GraphColors {
  const nodeType: Record<string, string> = {};
  for (const [label, token] of Object.entries(NODE_TYPE_TOKEN)) {
    nodeType[label] = read(token);
  }
  return {
    nodeType,
    nodeDefault: read('--text-muted'),
    // Node label sits on a saturated ramp node: use the theme background (dark in dark
    // theme where nodes are light; light in light theme where nodes are dark), and the
    // primary text color as the contrast halo. Both flip correctly with the theme.
    nodeLabelText: read('--timeline-bg'),
    nodeOutline: read('--text-primary'),
    nodeBorder: read('--border-color'),
    selected: read('--accent-color'),
    edgeLine: read('--text-muted'),
    edgeLabelText: read('--text-primary'),
    edgeLabelBg: read('--panel-bg'),
    changeCreated: CHANGE_CREATED,
    changeModified: CHANGE_MODIFIED,
    changeDeleted: CHANGE_DELETED,
  };
}

/** Build the Cytoscape stylesheet from resolved colors. */
export function buildCytoscapeStyles(c: GraphColors): CyStyle[] {
  const nodeTypeRules: CyStyle[] = Object.entries(c.nodeType).map(([label, color]) => ({
    selector: `node[label="${label}"]`,
    style: {'background-color': color},
  }));

  return [
    {
      selector: 'node',
      style: {
        'label': 'data(displayLabel)',
        'background-color': c.nodeDefault,
        'color': c.nodeLabelText,
        'text-valign': 'center',
        'text-halign': 'center',
        'font-size': '11px',
        'font-weight': 600,
        'font-family': '"JetBrains Mono", monospace',
        'width': '60px',
        'height': '60px',
        'border-width': 2,
        'border-color': c.nodeBorder,
        'text-wrap': 'ellipsis',
        'text-max-width': '90px',
        'text-outline-color': c.nodeOutline,
        'text-outline-width': 1,
        'text-outline-opacity': 0.8,
      },
    },
    ...nodeTypeRules,
    {
      selector: 'node.created',
      style: {'border-width': 4, 'border-color': c.changeCreated, 'border-style': 'solid'},
    },
    {
      selector: 'node.modified',
      style: {'border-width': 4, 'border-color': c.changeModified, 'border-style': 'dashed'},
    },
    {
      selector: 'node.deleted',
      style: {
        'border-width': 4, 'border-color': c.changeDeleted, 'border-style': 'solid',
        'opacity': 0.4, 'background-blacken': 0.3,
      },
    },
    {
      selector: 'node:selected',
      style: {'border-width': 4, 'border-color': c.selected, 'background-blacken': -0.1},
    },
    {
      selector: 'edge',
      style: {
        'width': 2,
        'line-color': c.edgeLine,
        'target-arrow-color': c.edgeLine,
        'target-arrow-shape': 'triangle',
        'curve-style': 'bezier',
        'label': 'data(type)',
        'font-size': '9px',
        'font-family': '"JetBrains Mono", monospace',
        'color': c.edgeLabelText,
        'text-rotation': 'autorotate',
        'text-margin-y': -10,
        'text-background-color': c.edgeLabelBg,
        'text-background-opacity': 0.85,
        'text-background-padding': '2px',
        'text-background-shape': 'roundrectangle',
      },
    },
    {
      selector: 'edge.created',
      style: {'line-color': c.changeCreated, 'target-arrow-color': c.changeCreated, 'width': 3},
    },
    {
      selector: 'edge.deleted',
      style: {
        'line-color': c.changeDeleted, 'target-arrow-color': c.changeDeleted,
        'line-style': 'dashed', 'opacity': 0.4,
      },
    },
    {
      selector: 'edge:selected',
      style: {'line-color': c.selected, 'target-arrow-color': c.selected, 'width': 3},
    },
    {selector: '.hidden', style: {'display': 'none'}},
  ];
}
