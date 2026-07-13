# Debug Page → Imperial Palette Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Debug page's hardcoded Catppuccin colors with a theme-aware Imperial categorical ramp (`--cat-1..8`) and sweep the app's dead Catppuccin fallbacks, so the theme config is the single source of color truth.

**Architecture:** Add eight per-theme ramp tokens to the SCSS theme config. DOM/SVG surfaces consume them as `var(--cat-N)` directly (theme-aware for free). The one JS-driven surface — the Cytoscape graph — reads the tokens' computed values through an injectable resolver and re-applies its stylesheet inside an Angular `effect()` on `ThemeService.resolved()`. Semantic categories (error/success/warning) pull existing semantic tokens; nominal categories cycle the ramp by a stable per-set key→slot map.

**Tech Stack:** Angular (standalone components, signals, `effect()`), SCSS theme maps, Cytoscape.js, Vitest + Angular TestBed.

## Global Constants

- **Spec:** `docs/superpowers/specs/2026-07-13-debug-page-imperial-palette-design.md`.
- **Ramp token hex** (added to both theme maps in Task 1):

  | Token | Senate (dark) | Travertine (light) |
  |-------|---------------|--------------------|
  | `--cat-1` Terracotta | `#c8674e` | `#a8492f` |
  | `--cat-2` Copper     | `#d48a4d` | `#c2722a` |
  | `--cat-3` Gold       | `#cdab68` | `#9a7822` |
  | `--cat-4` Olive      | `#a7b06a` | `#6e7534` |
  | `--cat-5` Slate-teal | `#5fb0a8` | `#2f7d74` |
  | `--cat-6` Lapis      | `#7a9bc6` | `#3f5e8c` |
  | `--cat-7` Violet     | `#a98fc4` | `#6f5591` |
  | `--cat-8` Mauve      | `#c98aa3` | `#8f4d63` |

- **Old-hue → ramp-slot rule** (assign each old Catppuccin category to its nearest ramp hue): blue→`--cat-6`, green→`--cat-4`, yellow→`--cat-3`, purple/mauve→`--cat-7`, peach→`--cat-2`, teal/cyan→`--cat-5`, red→`--cat-1` (or `--danger` if semantic), pink→`--cat-8`.
- **Semantic pull-outs** (never use the ramp): error/error_solution/tool_error/heatmap-low → `var(--danger)`; heatmap-high → `var(--success)`; heatmap-mid → `var(--warning)`; muted/unchanged/Default → `var(--text-muted)`.
- **Catppuccin signature hex** (the grep gate scans for these — zero allowed in the target scope after each sweep task):
  ```
  #cba6f7|#f38ba8|#a6e3a1|#89b4fa|#f9e2af|#fab387|#94e2d5|#f5c2e7|#89dceb|#74c7ec|#b4befe|#cdd6f4|#a6adc8|#6c7086|#7f849c|#313244|#1e1e2e|#181825|#11111b|#45475a
  ```
- **Run all tests:** `cd cockpit && npm run test`. **Run one spec:** `cd cockpit && npx vitest run <path>`.
- **Commit style:** `feat(debug): …` / `chore(cockpit): …`. Commit after each task. Do NOT push (project rule: push only when explicitly authorized).

### ⚠️ Deviation from spec (approved correction)
The spec's Rule 1 says graph `created → --success`, `deleted → --danger`. **This plan does NOT do that.** The graph's created/modified/deleted borders use an intentional Okabe-Ito colorblind-safe palette (`#0072B2` / `#E69F00` / `#D55E00`); recoloring them to red/green would regress colorblind accessibility. This plan **keeps them Okabe-Ito** and only themes the `unchanged` state → `--text-muted`. Flag this to the spec owner so the spec can be amended.

---

### Task 1: Add the categorical ramp tokens

**Files:**
- Modify: `cockpit/src/styles/themes/_theme-config.scss` (add 8 entries to `$travertine-theme` and `$senate-theme`)
- Test: `cockpit/src/styles/themes/theme-config.spec.ts` (create)

**Interfaces:**
- Produces: CSS custom properties `--cat-1 … --cat-8`, emitted per active theme via `apply-app-theme`. Consumed by every later task as `var(--cat-N)` (DOM/SVG) or read by name (graph resolver).

- [ ] **Step 1: Write the failing test**

Create `cockpit/src/styles/themes/theme-config.spec.ts`:
```typescript
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import {dirname, join} from 'node:path';
import {describe, expect, it} from 'vitest';

const here = dirname(fileURLToPath(import.meta.url));
const scss = readFileSync(join(here, '_theme-config.scss'), 'utf8');

describe('_theme-config.scss ramp tokens', () => {
  it('defines cat-1..8 in BOTH theme maps (once each = twice total)', () => {
    for (let i = 1; i <= 8; i++) {
      const occurrences = scss.split(`'cat-${i}':`).length - 1;
      expect(occurrences, `--cat-${i} must appear in both theme maps`).toBe(2);
    }
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd cockpit && npx vitest run src/styles/themes/theme-config.spec.ts`
Expected: FAIL — each `cat-N` appears 0 times, `expected 0 to be 2`.

- [ ] **Step 3: Add the tokens**

In `_theme-config.scss`, inside the `$travertine-theme` map, after the `// Track / gutter` block (before `// Interactive overlays`), add:
```scss
  // Categorical ramp — Imperial-harmonized (see spec 2026-07-13). Slots 2/3/6
  // reuse alert/warning/info; 1 & 4 are offset cousins of danger/success; 5/7/8 new.
  'cat-1': #a8492f, // terracotta
  'cat-2': #c2722a, // copper
  'cat-3': #9a7822, // gold
  'cat-4': #6e7534, // olive
  'cat-5': #2f7d74, // slate-teal
  'cat-6': #3f5e8c, // lapis
  'cat-7': #6f5591, // violet
  'cat-8': #8f4d63, // mauve
```
And inside `$senate-theme`, in the matching spot:
```scss
  // Categorical ramp — Imperial-harmonized (see spec 2026-07-13).
  'cat-1': #c8674e, // terracotta
  'cat-2': #d48a4d, // copper
  'cat-3': #cdab68, // gold
  'cat-4': #a7b06a, // olive
  'cat-5': #5fb0a8, // slate-teal
  'cat-6': #7a9bc6, // lapis
  'cat-7': #a98fc4, // violet
  'cat-8': #c98aa3, // mauve
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd cockpit && npx vitest run src/styles/themes/theme-config.spec.ts`
Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add cockpit/src/styles/themes/_theme-config.scss cockpit/src/styles/themes/theme-config.spec.ts
git commit -m "feat(debug): add Imperial categorical ramp tokens (--cat-1..8)"
```

---

### Task 2: agent-activity — badges to ramp + file sweep

**Files:**
- Modify: `cockpit/src/app/debug/components/agent-activity/agent-activity.component.ts`
- Test: `cockpit/src/app/debug/components/agent-activity/agent-activity.component.spec.ts` (create)

**Interfaces:**
- Consumes: `--cat-N` tokens (Task 1).
- Produces: public `getStepColor(stepType, entry?)` and `getToolColor(name)` return `var(--…)` strings; `stepColors` / `toolCategoryColors` maps hold token strings.

- [ ] **Step 1: Write the failing test**

Create `agent-activity.component.spec.ts`:
```typescript
import {describe, expect, it, beforeEach} from 'vitest';
import {TestBed} from '@angular/core/testing';
import {AgentActivityComponent} from './agent-activity.component';

describe('AgentActivityComponent colors', () => {
  let c: AgentActivityComponent;
  beforeEach(() => {
    TestBed.configureTestingModule({imports: [AgentActivityComponent]});
    c = TestBed.createComponent(AgentActivityComponent).componentInstance;
  });

  it('maps nominal step types to ramp tokens', () => {
    expect(c.getStepColor('llm')).toBe('var(--cat-4)');
    expect(c.getStepColor('tool')).toBe('var(--cat-7)');
    expect(c.getStepColor('initialize')).toBe('var(--cat-6)');
  });

  it('maps the error step type to the semantic danger token', () => {
    expect(c.getStepColor('error')).toBe('var(--danger)');
  });

  it('maps tool categories to ramp tokens', () => {
    expect(c.getToolColor('read_file')).toBe('var(--cat-6)'); // workspace
  });

  it('falls back to the muted token for unknown step types', () => {
    expect(c.getStepColor('nonsense' as never)).toBe('var(--text-muted)');
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd cockpit && npx vitest run src/app/debug/components/agent-activity/agent-activity.component.spec.ts`
Expected: FAIL — returns `#a6e3a1` etc., not `var(--cat-4)`.

- [ ] **Step 3: Rewrite the color maps + fallback**

Replace `stepColors` (line ~716) with:
```typescript
  private readonly stepColors: Record<AuditStepType, string> = {
    initialize: 'var(--cat-6)',      // lapis  (was blue)
    llm: 'var(--cat-4)',             // olive  (was green)
    tool: 'var(--cat-7)',            // violet (was purple)
    check: 'var(--cat-2)',           // copper (was peach)
    routing: 'var(--cat-5)',         // slate-teal (was teal)
    phase_complete: 'var(--cat-3)',  // gold   (was sapphire)
    error: 'var(--danger)',          // semantic
  };
```
Replace `toolCategoryColors` (line ~727) with:
```typescript
  private readonly toolCategoryColors: Record<string, string> = {
    workspace: 'var(--cat-6)',      // lapis  (was blue)
    core: 'var(--cat-7)',           // violet (was purple)
    research: 'var(--cat-5)',       // slate-teal (was teal)
    citation: 'var(--cat-3)',       // gold   (was yellow)
    graph: 'var(--cat-8)',          // mauve  (was pink)
    communication: 'var(--cat-4)',  // olive  (was green)
    delegation: 'var(--cat-2)',     // copper (was peach)
  };
```
In `getStepColor` (line ~832) change the default `|| '#6c7086'` to `|| 'var(--text-muted)'`.

- [ ] **Step 4: Sweep the component's static styles**

In the same file's `styles` template string, apply the mechanical rules:
- Drop every Catppuccin fallback: `var(--token, #hexOrRgba)` → `var(--token)` (delete `, #…` / `, rgba(…)` inside the `var()`).
- Standalone semantic hardcodes → tokens: `#f38ba8` → `var(--danger)`; `#a6e3a1` → `var(--success)`; `#f9e2af` → `var(--warning)`.
- Accent/success rgba tints → `color-mix`: `rgba(203, 166, 247, 0.06)` → `color-mix(in srgb, var(--accent-color) 6%, transparent)` (repeat for `0.10`→`10%`); `rgba(166, 227, 161, 0.06)` → `color-mix(in srgb, var(--success) 6%, transparent)` (and `0.10`→`10%`); `rgba(243, 139, 168, 0.1)` → `color-mix(in srgb, var(--danger) 10%, transparent)`.
- Leave neutral `rgba(0,0,0,…)` / `rgba(17,17,27,…)` overlays as-is (not Catppuccin signatures).

- [ ] **Step 5: Run tests + grep gate**

Run: `cd cockpit && npx vitest run src/app/debug/components/agent-activity/agent-activity.component.spec.ts`
Expected: PASS.
Run: `rg -n '#cba6f7|#f38ba8|#a6e3a1|#89b4fa|#f9e2af|#fab387|#94e2d5|#f5c2e7|#89dceb|#74c7ec|#b4befe|#cdd6f4|#a6adc8|#6c7086|#7f849c|#313244|#1e1e2e|#181825|#11111b|#45475a' cockpit/src/app/debug/components/agent-activity/agent-activity.component.ts`
Expected: no matches (exit 1).

- [ ] **Step 6: Commit**
```bash
git add cockpit/src/app/debug/components/agent-activity/
git commit -m "feat(debug): Imperial ramp for agent-activity badges"
```

---

### Task 3: memory-panel — types/sources + heatmap + alpha-concat fix

**Files:**
- Modify: `cockpit/src/app/debug/components/memory-panel/memory-panel.component.ts`
- Test: `cockpit/src/app/debug/components/memory-panel/memory-panel.component.spec.ts` (create)

**Interfaces:**
- Consumes: `--cat-N`, `--danger`, `--success`, `--warning` (Tasks 1).
- Produces: public `typeColors`, `sourceColorMap` (token strings), `importanceColor(value)` (semantic token), and a new public helper `tint(color: string): string` returning a `color-mix` string.

- [ ] **Step 1: Write the failing test**

Create `memory-panel.component.spec.ts`:
```typescript
import {describe, expect, it, beforeEach} from 'vitest';
import {TestBed} from '@angular/core/testing';
import {MemoryPanelComponent} from './memory-panel.component';

describe('MemoryPanelComponent colors', () => {
  let c: MemoryPanelComponent;
  beforeEach(() => {
    TestBed.configureTestingModule({imports: [MemoryPanelComponent]});
    c = TestBed.createComponent(MemoryPanelComponent).componentInstance;
  });

  it('maps memory types to ramp / semantic tokens', () => {
    expect(c.typeColors.factual).toBe('var(--cat-6)');
    expect(c.typeColors.error_solution).toBe('var(--danger)');
  });

  it('maps memory sources, with tool_error semantic', () => {
    expect(c.sourceColorMap.observer).toBe('var(--cat-5)');
    expect(c.sourceColorMap.tool_error).toBe('var(--danger)');
  });

  it('maps importance to semantic thresholds', () => {
    expect(c.importanceColor(0.9)).toBe('var(--success)');
    expect(c.importanceColor(0.6)).toBe('var(--warning)');
    expect(c.importanceColor(0.2)).toBe('var(--danger)');
  });

  it('tints a token via color-mix (not hex-alpha concat)', () => {
    expect(c.tint('var(--cat-6)')).toBe('color-mix(in srgb, var(--cat-6) 20%, transparent)');
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd cockpit && npx vitest run src/app/debug/components/memory-panel/memory-panel.component.spec.ts`
Expected: FAIL — hex values returned; `tint` undefined.

- [ ] **Step 3: Rewrite maps + importanceColor + add tint helper**

Replace `typeColors` (line ~733):
```typescript
  readonly typeColors: Record<MemoryType, string> = {
    factual: 'var(--cat-6)',          // lapis
    procedural: 'var(--cat-4)',       // olive
    error_solution: 'var(--danger)',  // semantic
    vocabulary: 'var(--cat-3)',       // gold
    relational: 'var(--cat-7)',       // violet
  };
```
Replace `sourceColorMap` (line ~741):
```typescript
  readonly sourceColorMap: Record<MemorySource, string> = {
    observer: 'var(--cat-5)',       // slate-teal
    todo: 'var(--cat-6)',           // lapis
    compaction: 'var(--cat-2)',     // copper
    phase_archive: 'var(--cat-8)',  // mauve
    tool_error: 'var(--danger)',    // semantic
  };
```
Replace the body of `importanceColor` (line ~875):
```typescript
  importanceColor(value: number): string {
    if (value >= 0.8) return 'var(--success)';
    if (value >= 0.5) return 'var(--warning)';
    return 'var(--danger)';
  }
```
Add a `tint` helper next to `importanceColor`:
```typescript
  /** Translucent background from a token — replaces the old `color + '33'` hex-alpha concat. */
  tint(color: string): string {
    return `color-mix(in srgb, ${color} 20%, transparent)`;
  }
```

- [ ] **Step 4: Fix the alpha-concat template binding + sweep static styles**

In the template, change line ~198 from:
```html
[style.background]="typeColors[mem.memory_type] + '33'"
```
to:
```html
[style.background]="tint(typeColors[mem.memory_type])"
```
Then sweep the `styles` block with the same rules as Task 2 Step 4 (drop `var(--x, #cat)` fallbacks; `#f38ba8`→`var(--danger)`; `rgba(243,139,168,0.1)`→`color-mix(in srgb, var(--danger) 10%, transparent)`; leave `rgba(17,17,27,…)` / `rgba(0,0,0,…)` overlays).

- [ ] **Step 5: Run tests + grep gate**

Run: `cd cockpit && npx vitest run src/app/debug/components/memory-panel/memory-panel.component.spec.ts`
Expected: PASS.
Run: `rg -n '#cba6f7|#f38ba8|#a6e3a1|#89b4fa|#f9e2af|#fab387|#94e2d5|#f5c2e7|#89dceb|#74c7ec|#b4befe|#cdd6f4|#a6adc8|#6c7086|#7f849c|#313244|#1e1e2e|#181825|#11111b|#45475a' cockpit/src/app/debug/components/memory-panel/memory-panel.component.ts`
Expected: no matches.

- [ ] **Step 6: Commit**
```bash
git add cockpit/src/app/debug/components/memory-panel/
git commit -m "feat(debug): Imperial ramp for memory-panel + color-mix tints"
```

---

### Task 4: layout-preview — SVG fill via style, ramp array

**Files:**
- Modify: `cockpit/src/app/debug/components/layout-picker/layout-preview.component.ts`
- Test: `cockpit/src/app/debug/components/layout-picker/layout-preview.component.spec.ts` (create)

**Interfaces:**
- Consumes: `--cat-N`, `--surface-1`, `--panel-bg`.
- Produces: none (leaf component).

Note: SVG presentation attribute `fill=` does NOT resolve `var()`; the CSS `fill` property (via `[style.fill]`) does — hence the binding switch.

- [ ] **Step 1: Write the failing test**
```typescript
import {describe, expect, it, beforeEach} from 'vitest';
import {TestBed} from '@angular/core/testing';
import {LayoutPreviewComponent} from './layout-preview.component';

describe('LayoutPreviewComponent palette', () => {
  let c: LayoutPreviewComponent;
  beforeEach(() => {
    TestBed.configureTestingModule({imports: [LayoutPreviewComponent]});
    c = TestBed.createComponent(LayoutPreviewComponent).componentInstance;
  });
  it('uses the ramp tokens for its palette', () => {
    expect(c['colors']).toEqual([
      'var(--cat-1)','var(--cat-2)','var(--cat-3)','var(--cat-4)',
      'var(--cat-5)','var(--cat-6)','var(--cat-7)','var(--cat-8)',
    ]);
  });
  it('uses theme tokens for stroke and background', () => {
    expect(c.strokeColor).toBe('var(--surface-1)');
    expect(c.bgColor).toBe('var(--panel-bg)');
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd cockpit && npx vitest run src/app/debug/components/layout-picker/layout-preview.component.spec.ts`
Expected: FAIL — hex arrays returned.

- [ ] **Step 3: Swap palette + template bindings**

Replace the `colors` array (line ~45):
```typescript
  private readonly colors = [
    'var(--cat-1)', 'var(--cat-2)', 'var(--cat-3)', 'var(--cat-4)',
    'var(--cat-5)', 'var(--cat-6)', 'var(--cat-7)', 'var(--cat-8)',
  ];
```
Replace lines ~56-57:
```typescript
  readonly strokeColor = 'var(--surface-1)';
  readonly bgColor = 'var(--panel-bg)';
```
In the template, change the `<rect>` bindings (lines ~23-24) from attributes to style:
```html
          [style.fill]="rect.fill"
          [style.stroke]="strokeColor"
```
(Delete the old `[attr.fill]` / `[attr.stroke]` lines.)

- [ ] **Step 4: Run test + grep gate**

Run: `cd cockpit && npx vitest run src/app/debug/components/layout-picker/layout-preview.component.spec.ts`
Expected: PASS.
Run: `rg -n '#cba6f7|#f38ba8|#a6e3a1|#89b4fa|#f9e2af|#fab387|#94e2d5|#f5c2e7|#89dceb|#74c7ec|#b4befe|#cdd6f4|#a6adc8|#6c7086|#7f849c|#313244|#1e1e2e|#181825|#11111b|#45475a' cockpit/src/app/debug/components/layout-picker/layout-preview.component.ts`
Expected: no matches.

- [ ] **Step 5: Commit**
```bash
git add cockpit/src/app/debug/components/layout-picker/layout-preview.component.ts cockpit/src/app/debug/components/layout-picker/layout-preview.component.spec.ts
git commit -m "feat(debug): Imperial ramp for layout preview (SVG style.fill)"
```

---

### Task 5: graph color resolver + graph-styles refactor

**Files:**
- Create: `cockpit/src/app/debug/components/graph-timeline/graph-colors.ts`
- Modify: `cockpit/src/app/debug/components/graph-timeline/graph-styles.ts` (remove `cytoscapeStyles`/`CHANGE_COLORS`/`LABEL_COLORS`/`getLabelColor`; keep `CyStyle` + layout options)
- Test: `cockpit/src/app/debug/components/graph-timeline/graph-colors.spec.ts` (create)

**Interfaces:**
- Consumes: `--cat-N`, `--text-muted`, `--timeline-bg`, `--text-primary`, `--border-color`, `--accent-color`, `--panel-bg` (Task 1 + existing theme).
- Produces:
  - `resolveGraphColors(read?: (name: string) => string): GraphColors`
  - `buildCytoscapeStyles(c: GraphColors): CyStyle[]`
  - `interface GraphColors { nodeType: Record<string,string>; nodeDefault; nodeLabelText; nodeOutline; nodeBorder; selected; edgeLine; edgeLabelText; edgeLabelBg; changeCreated; changeModified; changeDeleted: string }`
  - The `read` param is injectable so tests avoid jsdom's missing custom-property computation.

- [ ] **Step 1: Write the failing test**

Create `graph-colors.spec.ts`:
```typescript
import {describe, expect, it} from 'vitest';
import {resolveGraphColors, buildCytoscapeStyles} from './graph-colors';

// Fake reader: returns the token name back so we can assert mapping without a real DOM.
const echo = (name: string) => `RESOLVED${name}`;

describe('resolveGraphColors', () => {
  it('maps node types to ramp tokens', () => {
    const c = resolveGraphColors(echo);
    expect(c.nodeType['Rule']).toBe('RESOLVED--cat-1');       // terracotta (was red)
    expect(c.nodeType['Requirement']).toBe('RESOLVED--cat-6'); // lapis (was blue)
    expect(c.nodeDefault).toBe('RESOLVED--text-muted');
  });
  it('keeps change states on the Okabe-Ito palette (NOT themed)', () => {
    const c = resolveGraphColors(echo);
    expect(c.changeCreated).toBe('#0072B2');
    expect(c.changeModified).toBe('#E69F00');
    expect(c.changeDeleted).toBe('#D55E00');
  });
});

describe('buildCytoscapeStyles', () => {
  it('produces concrete colors — no var() and no Catppuccin hex', () => {
    const styles = buildCytoscapeStyles(resolveGraphColors(echo));
    const json = JSON.stringify(styles);
    expect(json).not.toContain('var(');
    expect(json).not.toMatch(/#cba6f7|#f38ba8|#a6e3a1|#89b4fa|#6c7086|#1e1e2e|#45475a|#f5c2e7|#7f849c|#cdd6f4/);
  });
  it('applies the resolved node-type color to its selector', () => {
    const c = resolveGraphColors(echo);
    const styles = buildCytoscapeStyles(c);
    const rule = styles.find((s) => s.selector === 'node[label="Rule"]');
    expect(rule?.style['background-color']).toBe(c.nodeType['Rule']);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd cockpit && npx vitest run src/app/debug/components/graph-timeline/graph-colors.spec.ts`
Expected: FAIL — module `./graph-colors` not found.

- [ ] **Step 3: Create `graph-colors.ts`**
```typescript
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
  getComputedStyle(document.documentElement).getPropertyValue(name).trim();

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
```

- [ ] **Step 4: Strip the old color exports from `graph-styles.ts`**

Delete `CHANGE_COLORS` (lines ~15-20), `LABEL_COLORS` (~26-36), `getLabelColor` (~41-43), and the entire `cytoscapeStyles` const (~48-209). **Keep** the `CyStyle` interface (~7-10) and all `*LayoutOptions` exports (~215-281). Verify nothing else imports the removed symbols:
Run: `rg -n 'cytoscapeStyles|CHANGE_COLORS|LABEL_COLORS|getLabelColor' cockpit/src/app`
Expected: only `graph-timeline.component.ts:15` still imports the now-removed `cytoscapeStyles`.

> **Cross-task compile note:** removing `cytoscapeStyles` leaves `graph-timeline.component.ts:15` importing a symbol that no longer exists, so the app does **not** typecheck between Task 5 and Task 6. This is expected — Task 6 fixes that import. Do NOT run a full `tsc`/build at the end of Task 5 (the per-file grep + the `graph-colors.spec.ts` unit test are its gates); commit Tasks 5 and 6 back-to-back (or squash them) so no broken build is left standing for review.

- [ ] **Step 5: Run test + grep gate**

Run: `cd cockpit && npx vitest run src/app/debug/components/graph-timeline/graph-colors.spec.ts`
Expected: PASS.
Run: `rg -n '#cba6f7|#f38ba8|#a6e3a1|#89b4fa|#f9e2af|#fab387|#94e2d5|#f5c2e7|#89dceb|#74c7ec|#b4befe|#cdd6f4|#a6adc8|#6c7086|#7f849c|#313244|#1e1e2e|#181825|#11111b|#45475a' cockpit/src/app/debug/components/graph-timeline/graph-styles.ts cockpit/src/app/debug/components/graph-timeline/graph-colors.ts`
Expected: no matches. (Okabe-Ito hex `#0072B2/#E69F00/#D55E00` are not in the signature list — intended.)

- [ ] **Step 6: Commit**
```bash
git add cockpit/src/app/debug/components/graph-timeline/graph-colors.ts cockpit/src/app/debug/components/graph-timeline/graph-colors.spec.ts cockpit/src/app/debug/components/graph-timeline/graph-styles.ts
git commit -m "feat(debug): theme-aware graph color resolver (keeps Okabe-Ito change states)"
```

---

### Task 6: graph-timeline — apply resolver + recolor on theme flip

**Files:**
- Modify: `cockpit/src/app/debug/components/graph-timeline/graph-timeline.component.ts`
- Test: `cockpit/src/app/debug/components/graph-timeline/graph-timeline.retheme.spec.ts` (create)

**Interfaces:**
- Consumes: `resolveGraphColors`, `buildCytoscapeStyles` (Task 5); `ThemeService.resolved()` signal (`cockpit/src/app/core/services/theme.service.ts`).
- Produces: none.

Rationale: Cytoscape can't read `var()`, so it holds concrete colors that must be rebuilt when the theme changes. `ThemeService.resolved` is a signal, so an `effect()` reacts to flips. Full component instantiation needs a real canvas (not available in jsdom), so the automated test targets the extracted `rethemeGraph()` method with a fake `cy`; the effect wiring is verified visually.

- [ ] **Step 1: Write the failing test**
```typescript
import {describe, expect, it, vi} from 'vitest';
import {GraphTimelineComponent} from './graph-timeline.component';

describe('GraphTimelineComponent.rethemeGraph', () => {
  it('re-applies concrete (var-free) styles to the cytoscape instance', () => {
    const update = vi.fn();
    const style = vi.fn(() => ({update}));
    const comp = Object.create(GraphTimelineComponent.prototype);
    comp.cy = {style};
    comp.rethemeGraph();
    expect(style).toHaveBeenCalledOnce();
    const applied = JSON.stringify(style.mock.calls[0][0]);
    expect(applied).not.toContain('var(');
    expect(update).toHaveBeenCalledOnce();
  });

  it('no-ops safely when cy is not yet created', () => {
    const comp = Object.create(GraphTimelineComponent.prototype);
    comp.cy = undefined;
    expect(() => comp.rethemeGraph()).not.toThrow();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd cockpit && npx vitest run src/app/debug/components/graph-timeline/graph-timeline.retheme.spec.ts`
Expected: FAIL — `rethemeGraph` is not a function.

- [ ] **Step 3: Wire the resolver + effect into the component**

- Update the import (line ~15):
```typescript
import {buildCytoscapeStyles, resolveGraphColors} from './graph-colors';
```
- Add the ThemeService injection near the other `inject()` calls (~line 489):
```typescript
  private readonly theme = inject(ThemeService);
```
  and import it: `import {ThemeService} from '../../../core/services/theme.service';` (verify the relative depth against the file's existing core imports).
- At the `cytoscape({...})` init (~line 596), replace `style: cytoscapeStyles,` with:
```typescript
        style: buildCytoscapeStyles(resolveGraphColors()),
```
- Add the recolor method (anywhere in the class body):
```typescript
  /** Rebuild the Cytoscape stylesheet from the current theme's tokens. */
  rethemeGraph(): void {
    if (!this.cy) return;
    this.cy.style(buildCytoscapeStyles(resolveGraphColors())).update();
  }
```
- Register an effect in the constructor (beside the existing `effect(() => …)` blocks at ~509/534/543):
```typescript
    // Recolor the graph when the user flips light/dark (Cytoscape holds concrete colors).
    effect(() => {
      this.theme.resolved();      // establish the dependency
      this.rethemeGraph();
    });
```

- [ ] **Step 4: Run test + grep gate + typecheck**

Run: `cd cockpit && npx vitest run src/app/debug/components/graph-timeline/graph-timeline.retheme.spec.ts`
Expected: PASS.
Run: `rg -n '#cba6f7|#f38ba8|#a6e3a1|#89b4fa|#f9e2af|#fab387|#94e2d5|#f5c2e7|#89dceb|#74c7ec|#b4befe|#cdd6f4|#a6adc8|#6c7086|#7f849c|#313244|#1e1e2e|#181825|#11111b|#45475a' cockpit/src/app/debug/components/graph-timeline/graph-timeline.component.ts`
Expected: no matches.

- [ ] **Step 5: Commit**
```bash
git add cockpit/src/app/debug/components/graph-timeline/graph-timeline.component.ts cockpit/src/app/debug/components/graph-timeline/graph-timeline.retheme.spec.ts
git commit -m "feat(debug): recolor graph on theme flip via effect()"
```

---

### Task 7: Sweep remaining debug-page files

**Files (modify; drop Catppuccin fallbacks + map any semantic hardcodes):**
- `components/request-viewer/request-viewer.component.ts`
- `components/timeline/timeline.component.ts`
- `components/db-table/db-table.component.ts`
- `layout/panel-header/panel-header.component.ts`
- `components/menu/menu.component.ts`
- `components/placeholders/placeholder-{a,b,c}.component.ts`
- `layout/component-host/component-host.component.ts`

No new tests — the deliverable is verified by the whole-directory grep gate (behavior is unchanged; these are fallback/semantic swaps only).

- [ ] **Step 1: Apply the mechanical sweep to each file**

Per file, apply the Global-Constants rules:
- `var(--token, #hexOrRgba)` → `var(--token)` (drop the fallback).
- Standalone semantic hex → token: `#f38ba8`→`var(--danger)`, `#a6e3a1`→`var(--success)`, `#f9e2af`→`var(--warning)`.
- Catppuccin rgba tints → `color-mix` against the matching token (accent `#cba6f7`/`203,166,247`→`--accent-color`; success `166,227,161`→`--success`; danger `243,139,168`→`--danger`), preserving the percentage.
- Leave neutral black/near-black rgba overlays as-is.

- [ ] **Step 2: Whole-debug-directory grep gate**

Run: `rg -n '#cba6f7|#f38ba8|#a6e3a1|#89b4fa|#f9e2af|#fab387|#94e2d5|#f5c2e7|#89dceb|#74c7ec|#b4befe|#cdd6f4|#a6adc8|#6c7086|#7f849c|#313244|#1e1e2e|#181825|#11111b|#45475a' cockpit/src/app/debug`
Expected: **no matches** (the entire debug page is now Catppuccin-free).

- [ ] **Step 3: Full debug suite + build sanity**

Run: `cd cockpit && npm run test`
Expected: all specs pass (including the four new ones).
Run: `cd cockpit && npx tsc -p tsconfig.app.json --noEmit`
Expected: no type errors.

- [ ] **Step 4: Commit**
```bash
git add cockpit/src/app/debug
git commit -m "chore(debug): drop dead Catppuccin fallbacks across remaining panels"
```

---

### Task 8: App-wide fallback sweep + stray semantic hardcodes

**Files:** the ~25 non-debug files carrying Catppuccin (enumerate with the discovery command below), plus the named strays:
- `app/views/agent-steps/agent-steps.component.scss` — `.complete` `#a6e3a1`→`var(--success)`, `.error` `#f38ba8`→`var(--danger)`, and the `linear-gradient(135deg, #f97316, #fab387)` step-icon → `linear-gradient(135deg, var(--alert), var(--warning))`.
- `app/core/services/user.service.ts:80` — default avatar `'#89b4fa'` → `'var(--cat-6)'` (lapis; keeps avatars off the accent color).

No new tests — verified by the app-wide grep gate + existing suite.

- [ ] **Step 1: Enumerate the target files**

Run:
```bash
rg -l '#cba6f7|#f38ba8|#a6e3a1|#89b4fa|#f9e2af|#fab387|#94e2d5|#f5c2e7|#89dceb|#74c7ec|#b4befe|#cdd6f4|#a6adc8|#6c7086|#7f849c|#313244|#1e1e2e|#181825|#11111b|#45475a' cockpit/src/app cockpit/src/styles | grep -v '/debug/'
```
Expected: ~25 files (sidebar, inbox, job-review, job-create, agents, workspace-browser, sudo, sessions, jobs, datasources, config-editor, agent-settings/*, notification-bell, toast, form-field, statistics, todos, projects, chat-history, view-mode-banner, readiness-gate-banner, empty-catalog-banner, user.service, app.ts, …).

- [ ] **Step 2: Sweep each file**

Apply the same rules as Task 7 Step 1 across every enumerated file, plus the two named strays above. The overwhelming majority are Bucket-A fallbacks: `var(--token, #cat)` → `var(--token)`.

- [ ] **Step 3: App-wide grep gate**

Run:
```bash
rg -n '#cba6f7|#f38ba8|#a6e3a1|#89b4fa|#f9e2af|#fab387|#94e2d5|#f5c2e7|#89dceb|#74c7ec|#b4befe|#cdd6f4|#a6adc8|#6c7086|#7f849c|#313244|#1e1e2e|#181825|#11111b|#45475a' cockpit/src/app cockpit/src/styles
```
Expected: **no matches** anywhere under `app/` or `styles/`.

- [ ] **Step 4: Full suite + build + typecheck**

Run: `cd cockpit && npm run test`
Expected: green.
Run: `cd cockpit && npx tsc -p tsconfig.app.json --noEmit`
Expected: no errors.

- [ ] **Step 5: Commit**
```bash
git add cockpit/src/app cockpit/src/styles
git commit -m "chore(cockpit): remove dead Catppuccin fallbacks app-wide + retint strays"
```

---

## Manual Verification (after Task 8)

Automated tests cover the color logic; the visual result and the graph's reactive recolor need a human eyeball:

1. `cd cockpit && npm run start` (or the project's Tilt/k3d dev flow), open the **Debug** page.
2. Toggle **Travertine ⇄ Senate** (theme toggle in the shell). Confirm on each theme:
   - Agent-activity badges, memory-panel chips, layout thumbnails all render in Imperial hues (no purple/blue/green Catppuccin).
   - The **graph recolors** on the flip (exercises the Task 6 `effect()`), node labels stay legible, change-state borders stay blue/orange/vermillion.
   - Terracotta category chips read as distinct from the blood-red **error** badge; olive distinct from laurel **success**.
3. Spot-check a few swept non-debug pages (sidebar, inbox, job-review) look unchanged (fallback removal should be invisible).

## Self-Review Notes (coverage vs. spec)

- Spec §1 ramp tokens → Task 1. §2 assignment rules → Tasks 2/3/5 (maps) + Global Constants. §3 consumption mechanics: DOM `var()` → Tasks 2/3; SVG `style.fill` → Task 4; Cytoscape resolver+effect → Tasks 5/6. §4 full sweep → Tasks 7/8. §5 verification → grep gates per task + Manual Verification.
- **Documented deviation:** graph change-states stay Okabe-Ito (see the ⚠️ box) rather than success/danger — accessibility correction; surface to spec owner.
