# Design System Reference — Cockpit

This is the design system for the cockpit application. Every mockup you create MUST use these tokens.
Do not invent colors, spacing, or typography outside this system.

---

## Color Tokens (Catppuccin Mocha)

```css
:root {
  /* Backgrounds */
  --app-bg: #1e1e2e;         /* Main application background */
  --panel-bg: #181825;        /* Panel/sidebar backgrounds */
  --panel-header-bg: #1e1e2e; /* Panel header bars */
  --timeline-bg: #11111b;     /* Deepest background (inset areas) */
  --surface-0: #313244;       /* Elevated surface (cards, dropdowns) */
  --surface-1: #45475a;       /* Higher elevation (hover states, active items) */
  --surface-2: #585b70;       /* Highest elevation (pressed states, borders) */

  /* Borders */
  --border-color: #313244;    /* Default border */

  /* Text */
  --text-primary: #cdd6f4;    /* Primary content text */
  --text-secondary: #a6adc8;  /* Secondary/supporting text */
  --text-muted: #8b92a8;      /* Muted text (timestamps, labels, placeholders) */

  /* Accent */
  --accent-color: #cba6f7;    /* Primary accent (Mauve — links, active states, focus rings) */
  --accent-hover: #b4befe;    /* Accent hover state (Lavender) */
}
```

### Extended Catppuccin Palette (for status colors, charts, badges)

Use these sparingly and consistently:
- **Red** (error/danger): `#f38ba8`
- **Green** (success/online): `#a6e3a1`
- **Yellow** (warning/pending): `#f9e2af`
- **Blue** (info/link): `#89b4fa`
- **Peach** (highlight): `#fab387`
- **Teal** (secondary accent): `#94e2d5`
- **Pink** (designer accent): `#f5c2e7`

---

## Typography

```css
font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
font-size: 14px;      /* Base */
line-height: 1.5;     /* Base */
```

### Scale (observed in codebase)

| Use | Size | Weight | Color |
|-----|------|--------|-------|
| Page title | 18-20px | 600 | --text-primary |
| Section heading | 16px | 600 | --text-primary |
| Body text | 14px | 400 | --text-primary |
| Secondary text | 13px | 400 | --text-secondary |
| Label / caption | 12px | 400-500 | --text-muted |
| Monospace / code | 13px | 400 | --text-primary, font-family: monospace |

---

## Spacing

The codebase uses a loose 4px/8px base:

| Token | Value | Use |
|-------|-------|-----|
| xs | 4px | Inline spacing, icon-to-text gap |
| sm | 8px | Compact padding (badges, tags, dense lists) |
| md | 12px | Standard padding (cards, list items) |
| lg | 16px | Section padding, gaps between components |
| xl | 24px | Page margins, major section separation |
| 2xl | 32px | Hero spacing, empty state padding |

---

## Common Component Patterns

### Cards / Panels

```css
.card {
  background: var(--surface-0);
  border: 1px solid var(--border-color);
  border-radius: 8px;
  padding: 12px 16px;
}

.card:hover {
  background: var(--surface-1);
  border-color: var(--surface-1);
}

.card.selected {
  border-color: var(--accent-color);
  box-shadow: 0 0 0 1px var(--accent-color);
}
```

### Buttons

```css
/* Primary */
.btn-primary {
  background: var(--accent-color);
  color: var(--timeline-bg);
  border: none;
  border-radius: 6px;
  padding: 8px 16px;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: background 0.15s ease;
}

.btn-primary:hover {
  background: var(--accent-hover);
}

/* Secondary / Ghost */
.btn-secondary {
  background: transparent;
  color: var(--text-secondary);
  border: 1px solid var(--border-color);
  border-radius: 6px;
  padding: 8px 16px;
  font-size: 13px;
  cursor: pointer;
}

.btn-secondary:hover {
  background: var(--surface-0);
  color: var(--text-primary);
}

/* Danger */
.btn-danger {
  background: transparent;
  color: #f38ba8;
  border: 1px solid #f38ba8;
  border-radius: 6px;
  padding: 8px 16px;
}
```

### Input Fields

```css
input, select, textarea {
  background: var(--panel-bg);
  border: 1px solid var(--border-color);
  border-radius: 6px;
  color: var(--text-primary);
  padding: 8px 12px;
  font-size: 14px;
  outline: none;
  transition: border-color 0.15s ease;
}

input:focus {
  border-color: var(--accent-color);
  box-shadow: 0 0 0 1px var(--accent-color);
}

input::placeholder {
  color: var(--text-muted);
}
```

### Lists

```css
.list-item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 16px;
  border-bottom: 1px solid var(--border-color);
  cursor: pointer;
  transition: background 0.1s ease;
}

.list-item:hover {
  background: var(--surface-0);
}

.list-item.active {
  background: var(--surface-0);
  border-left: 3px solid var(--accent-color);
}
```

### Status Badges

```css
.badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 11px;
  font-weight: 500;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.badge-success { background: rgba(166, 227, 161, 0.15); color: #a6e3a1; }
.badge-error   { background: rgba(243, 139, 168, 0.15); color: #f38ba8; }
.badge-warning { background: rgba(249, 226, 175, 0.15); color: #f9e2af; }
.badge-info    { background: rgba(137, 180, 250, 0.15); color: #89b4fa; }
.badge-muted   { background: rgba(69, 71, 90, 0.5);     color: #a6adc8; }
```

### Empty States

```css
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 48px 24px;
  text-align: center;
  color: var(--text-muted);
}

.empty-state-icon {
  font-size: 48px;
  margin-bottom: 16px;
  opacity: 0.4;
}

.empty-state-title {
  font-size: 16px;
  font-weight: 500;
  color: var(--text-secondary);
  margin-bottom: 8px;
}

.empty-state-message {
  font-size: 13px;
  max-width: 360px;
  line-height: 1.6;
}
```

### Modals / Dialogs

```css
.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.5);
  backdrop-filter: blur(4px);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}

.modal {
  background: var(--panel-bg);
  border: 1px solid var(--border-color);
  border-radius: 12px;
  width: min(480px, 90vw);
  max-height: 80vh;
  overflow-y: auto;
  box-shadow: 0 16px 48px rgba(0, 0, 0, 0.3);
}

.modal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 20px;
  border-bottom: 1px solid var(--border-color);
}

.modal-body {
  padding: 20px;
}

.modal-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding: 16px 20px;
  border-top: 1px solid var(--border-color);
}
```

### Toast / Notifications

```css
.toast {
  background: var(--surface-0);
  border: 1px solid var(--border-color);
  border-radius: 8px;
  padding: 12px 16px;
  display: flex;
  align-items: flex-start;
  gap: 12px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.2);
  max-width: 400px;
}

.toast-error   { border-left: 3px solid #f38ba8; }
.toast-success { border-left: 3px solid #a6e3a1; }
.toast-warning { border-left: 3px solid #f9e2af; }
.toast-info    { border-left: 3px solid #89b4fa; }
```

---

## Layout Patterns

### App Shell
The cockpit uses a split-panel layout (angular-split):
- **Desktop**: Sidebar + main content area, resizable
- **Mobile** (simple/ layout): Tab-based navigation at bottom, full-screen pages

### Panel Layout
```css
.panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: var(--panel-bg);
}

.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  background: var(--panel-header-bg);
  border-bottom: 1px solid var(--border-color);
  flex-shrink: 0;
}

.panel-body {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
}
```

### Responsive Breakpoints

| Breakpoint | Target |
|------------|--------|
| < 768px | Mobile (simple/ layout) |
| 768px - 1024px | Tablet (collapsed sidebar) |
| > 1024px | Desktop (full layout) |

Mobile touch targets: minimum 44x44px for primary/navigation buttons.

---

## Scrollbar Styling

```css
::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-track { background: var(--panel-bg); }
::-webkit-scrollbar-thumb { background: var(--surface-1); border-radius: 4px; }
::-webkit-scrollbar-thumb:hover { background: var(--surface-2); }
```

---

## Transitions

Standard transition: `0.15s ease` for background, border, color changes.
Use `0.2s ease` for transforms and opacity (appear/disappear animations).

---

## Accessibility

- Focus indicator: `outline: 2px solid var(--accent-color); outline-offset: 2px;`
- Input focus: `border-color: var(--accent-color); box-shadow: 0 0 0 1px var(--accent-color);`
- Selection: `background: var(--accent-color); color: var(--timeline-bg);`
- Minimum contrast: text-primary on panel-bg passes WCAG AA for normal text
- Semantic HTML: use `<button>`, `<a>`, `<input>`, `<nav>`, `<main>`, `<section>` — not divs with click handlers

---

## Mockup Starter Template

**ALWAYS start every mockup from this template.** Do not rebuild the boilerplate from memory — copy this exactly and fill in the content sections. This prevents token drift (where you start hardcoding colors midway through generation).

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{{MOCKUP_TITLE}} — Design Mockup</title>
  <style>
    /* ===== Catppuccin Mocha Design Tokens ===== */
    :root {
      --app-bg: #1e1e2e;
      --panel-bg: #181825;
      --panel-header-bg: #1e1e2e;
      --timeline-bg: #11111b;
      --surface-0: #313244;
      --surface-1: #45475a;
      --surface-2: #585b70;
      --border-color: #313244;
      --text-primary: #cdd6f4;
      --text-secondary: #a6adc8;
      --text-muted: #8b92a8;
      --accent-color: #cba6f7;
      --accent-hover: #b4befe;
      /* Status colors */
      --color-red: #f38ba8;
      --color-green: #a6e3a1;
      --color-yellow: #f9e2af;
      --color-blue: #89b4fa;
      --color-peach: #fab387;
      --color-teal: #94e2d5;
      --color-pink: #f5c2e7;
    }

    /* ===== Reset ===== */
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    html, body {
      height: 100%;
      background: var(--app-bg);
      color: var(--text-primary);
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
      font-size: 14px;
      line-height: 1.5;
      -webkit-font-smoothing: antialiased;
    }

    /* ===== Scrollbars ===== */
    ::-webkit-scrollbar { width: 8px; height: 8px; }
    ::-webkit-scrollbar-track { background: var(--panel-bg); }
    ::-webkit-scrollbar-thumb { background: var(--surface-1); border-radius: 4px; }
    ::-webkit-scrollbar-thumb:hover { background: var(--surface-2); }

    /* ===== Focus ===== */
    :focus-visible { outline: 2px solid var(--accent-color); outline-offset: 2px; }
    input:focus-visible, select:focus-visible, textarea:focus-visible {
      outline: none;
      border-color: var(--accent-color) !important;
      box-shadow: 0 0 0 1px var(--accent-color);
    }

    /* ===== Component Styles ===== */
    /* ADD YOUR STYLES HERE — use only var(--token) references for colors */

  </style>
</head>
<body>

  <!-- ===== Component Mapping =====
    Map HTML structure to intended Angular components:
    .example-container -> ExampleComponent (new)
    .example-header   -> reuse existing panel-header pattern
  -->

  <!-- ===== State: Default (populated) ===== -->
  <section id="state-default">
    <!-- PRIMARY MOCKUP CONTENT HERE -->
  </section>

  <!-- ===== State: Empty ===== -->
  <section id="state-empty" style="display: none;">
    <!-- EMPTY STATE CONTENT HERE -->
  </section>

  <!-- ===== State: Error ===== -->
  <section id="state-error" style="display: none;">
    <!-- ERROR STATE CONTENT HERE -->
  </section>

  <!-- State switcher (for preview convenience) -->
  <div style="position: fixed; bottom: 16px; right: 16px; display: flex; gap: 4px; z-index: 9999;">
    <button onclick="document.querySelectorAll('section[id^=state]').forEach(s=>s.style.display='none');document.getElementById('state-default').style.display=''" style="padding: 4px 8px; background: var(--surface-0); color: var(--text-secondary); border: 1px solid var(--border-color); border-radius: 4px; font-size: 11px; cursor: pointer;">Default</button>
    <button onclick="document.querySelectorAll('section[id^=state]').forEach(s=>s.style.display='none');document.getElementById('state-empty').style.display=''" style="padding: 4px 8px; background: var(--surface-0); color: var(--text-secondary); border: 1px solid var(--border-color); border-radius: 4px; font-size: 11px; cursor: pointer;">Empty</button>
    <button onclick="document.querySelectorAll('section[id^=state]').forEach(s=>s.style.display='none');document.getElementById('state-error').style.display=''" style="padding: 4px 8px; background: var(--surface-0); color: var(--text-secondary); border: 1px solid var(--border-color); border-radius: 4px; font-size: 11px; cursor: pointer;">Error</button>
  </div>

</body>
</html>
```

---

## Few-Shot Examples (from the actual cockpit)

These are simplified extracts of real components. **Mimic these patterns** — they are ground truth for how the app looks.

### Example 1: Header Bar with Filter Chips

```html
<div class="header-bar" style="display: flex; align-items: center; gap: 12px; padding: 10px 12px; background: var(--panel-header-bg); border-bottom: 1px solid var(--border-color); flex-shrink: 0;">
  <span style="font-weight: 600; color: var(--text-primary);">Jobs</span>
  <div style="display: flex; gap: 4px; flex-wrap: wrap;">
    <button class="filter-chip active" style="padding: 4px 10px; border-radius: 4px; font-size: 11px; background: var(--accent-color); color: var(--timeline-bg); border: 1px solid var(--accent-color); cursor: pointer;">All</button>
    <button class="filter-chip" style="padding: 4px 10px; border-radius: 4px; font-size: 11px; background: transparent; color: var(--text-secondary); border: 1px solid var(--border-color); cursor: pointer;">Processing <span style="opacity: 0.7; font-size: 10px;">(3)</span></button>
    <button class="filter-chip" style="padding: 4px 10px; border-radius: 4px; font-size: 11px; background: transparent; color: var(--text-secondary); border: 1px solid var(--border-color); cursor: pointer;">Completed <span style="opacity: 0.7; font-size: 10px;">(12)</span></button>
  </div>
  <button style="margin-left: auto; padding: 5px 12px; border: 1px solid var(--border-color); border-radius: 4px; background: transparent; color: var(--text-secondary); font-size: 11px; cursor: pointer;">Refresh</button>
</div>
```

### Example 2: Table with Status Badges

```html
<div style="flex: 1; overflow-y: auto;">
  <table style="width: 100%; border-collapse: collapse; font-size: 12px;">
    <thead>
      <tr>
        <th style="text-align: left; padding: 10px 12px; background: var(--surface-0); color: var(--text-muted); font-weight: 500; text-transform: uppercase; font-size: 10px; letter-spacing: 0.5px; border-bottom: 1px solid var(--border-color); position: sticky; top: 0;">Job</th>
        <th style="text-align: left; padding: 10px 12px; background: var(--surface-0); color: var(--text-muted); font-weight: 500; text-transform: uppercase; font-size: 10px; letter-spacing: 0.5px; border-bottom: 1px solid var(--border-color); position: sticky; top: 0; white-space: nowrap;">Status</th>
        <th style="text-align: left; padding: 10px 12px; background: var(--surface-0); color: var(--text-muted); font-weight: 500; text-transform: uppercase; font-size: 10px; letter-spacing: 0.5px; border-bottom: 1px solid var(--border-color); position: sticky; top: 0; white-space: nowrap;">Created</th>
      </tr>
    </thead>
    <tbody>
      <tr style="cursor: pointer; transition: background 0.15s ease;" onmouseover="this.style.background='var(--surface-0)'" onmouseout="this.style.background=''">
        <td style="padding: 10px 12px; border-bottom: 1px solid var(--border-color); color: var(--text-primary);">Implement batch export feature for cockpit</td>
        <td style="padding: 10px 12px; border-bottom: 1px solid var(--border-color);"><span style="display: inline-block; padding: 3px 8px; border-radius: 4px; font-size: 11px; font-weight: 500; background: rgba(249, 226, 175, 0.2); color: #f9e2af;">processing</span></td>
        <td style="padding: 10px 12px; border-bottom: 1px solid var(--border-color); color: var(--text-secondary); white-space: nowrap;">2 hours ago</td>
      </tr>
      <tr style="cursor: pointer; background: rgba(203, 166, 247, 0.15);">
        <td style="padding: 10px 12px; border-bottom: 1px solid var(--border-color); color: var(--text-primary);">Fix authentication token refresh on cockpit</td>
        <td style="padding: 10px 12px; border-bottom: 1px solid var(--border-color);"><span style="display: inline-block; padding: 3px 8px; border-radius: 4px; font-size: 11px; font-weight: 500; background: rgba(166, 227, 161, 0.2); color: #a6e3a1;">completed</span></td>
        <td style="padding: 10px 12px; border-bottom: 1px solid var(--border-color); color: var(--text-secondary); white-space: nowrap;">Yesterday</td>
      </tr>
    </tbody>
  </table>
</div>
```

### Example 3: Empty State

```html
<div style="display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 12px; padding: 40px; color: var(--text-muted); flex: 1;">
  <span style="font-size: 48px; opacity: 0.5;">&#x1F4CB;</span>
  <span style="font-size: 16px; font-weight: 500; color: var(--text-secondary);">No jobs found</span>
  <span style="font-size: 11px; opacity: 0.6;">Create a new job to get started</span>
</div>
```

---

## Self-Review Protocol

After generating each mockup, you MUST self-review before marking the todo complete.

### Step 1: Visual Validation

If `browse_website` is available and a file server is running, open the mockup in the browser and visually inspect it. If not, re-read the generated HTML carefully and mentally render it.

Check for:
- Does the layout structure match the intent? (sidebar where expected, content flowing correctly)
- Are all states present and accessible? (state switcher buttons work)
- Is content realistic and varied? (different text lengths, different item counts)

### Step 2: Token Compliance Audit

Search your generated HTML for any hardcoded color values outside the `:root` block:
- Scan for `#` hex values in inline styles or CSS rules (outside `:root`)
- The ONLY acceptable hardcoded colors are rgba() values for badge/status backgrounds (e.g., `rgba(166, 227, 161, 0.2)`)
- Every other color must use `var(--token-name)`
- If you find violations, fix them before completing the todo

### Step 3: Spacing Consistency Check

Verify all padding, margin, and gap values use the spacing scale: 4, 8, 10, 12, 16, 20, 24, 32, 40, 48px.
Flag any odd values (e.g., 15px, 18px, 7px) and correct them to the nearest scale value.

### Step 4: Responsive Spot-Check

Verify the mockup includes:
- At least one `@media (max-width: 768px)` rule
- Mobile adjustments for layout (e.g., grid columns collapse, sidebar hides)
- Touch targets are at least 44x44px on mobile for primary actions

### Step 5: Semantic HTML Check

Verify you used:
- `<button>` for clickable actions (not `<div onclick>`)
- `<a>` for navigation links
- `<nav>`, `<main>`, `<section>` for structural landmarks
- `<table>` for tabular data (not div grids)
- `<label>` associated with form inputs

---

## Common Mistakes to Avoid

These are the most frequent LLM failure modes when generating UI. Read this list before every mockup.

1. **Token drift**: Starting with `var(--accent-color)` then switching to `#cba6f7` later in the file. Always use the variable, never the raw value outside `:root`.

2. **Fixed heights on content containers**: Never use `height: 500px` on a container that should grow with content. Use `min-height` or `flex: 1` with overflow.

3. **Forgetting hover/focus/active states**: Every interactive element (button, link, list item, card) needs at minimum a hover state. Inputs need a focus state.

4. **Inconsistent border-radius**: The system uses `4px` (chips, badges), `6px` (buttons, inputs), `8px` (cards, panels), `12px` (modals). Don't invent new values.

5. **Missing gap, using margins instead**: Use `gap` in flex/grid containers. Don't use margin-bottom on children for vertical spacing in a flex column — use `gap`.

6. **Generic placeholder content**: "Item 1", "Item 2", "Item 3" tells you nothing about layout edge cases. Use realistic content: a short title, a medium title, and a long title that might wrap.

7. **Icons via CDN**: Never link to Font Awesome, Material Icons, or any external CDN. Use Unicode characters (&#x1F4CB;, &#x2713;, &#x26A0;) or inline SVG.

8. **Forgetting the state switcher**: Every mockup needs the state switcher buttons (from the starter template) so the reviewer can toggle between states without editing HTML.

9. **Desktop-only thinking**: If you didn't write a `@media (max-width: 768px)` block, the mockup is incomplete.
