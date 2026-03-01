# Cockpit UI Review

Reviewed: 2026-02-24. Covers login, mobile shell (5 tabs), debug page, projects pages, and layout system.

---

## 1. Layout & Navigation

### 1.1 Desktop renders the mobile shell as its default view

**Severity: High**

The `/` route always renders `MobileShellComponent` with a bottom tab bar, regardless of viewport width. On a desktop browser, users see the sidebar AND the bottom tab bar simultaneously — dual navigation chrome that no major admin tool uses (Vercel, Linear, Supabase, and Grafana all swap between sidebar and bottom tabs at a breakpoint, never showing both).

The `ViewportService` already detects mobile via `window.matchMedia('(max-width: 768px)')`, but it's only consumed in one place — `app.ts` hides the sidebar on mobile. It does nothing to give desktop users a desktop-appropriate layout.

**Suggestion:** On desktop (>768px), route `/` to the tiling debug layout or a dedicated dashboard. Reserve the bottom-tab mobile shell for mobile viewports only. This matches the Vercel pattern: collapsible sidebar on desktop, floating bottom bar on mobile, never both.

### 1.2 Duplicate and ambiguous navigation paths

**Severity: Medium**

| Sidebar | Bottom Tabs |
|---------|-------------|
| "Simple" → `/` | Builder, Jobs, Projects, Create, Review (all render inside `/`) |
| "Projects" → `/projects` | "Projects" tab (same component, different chrome) |
| "Debug" → `/debug` | — |

The sidebar "Projects" link navigates to `/projects` (full-page `ProjectListPageComponent`). The bottom tab "Projects" renders the identical `ProjectListPageComponent` inside the mobile shell at `/`. Two paths to the same content with different surrounding chrome. The sidebar label "Simple" is unclear — it's not obvious what it means.

**Suggestion:** Unify the navigation model. The sidebar should be the primary nav on desktop; the bottom tabs should only appear on mobile. Each nav item should map to exactly one destination.

---

## 2. Accessibility

### 2.1 No ARIA attributes across the application

**Severity: High**

The app has essentially zero accessibility markup. The only `aria-*` usage found in the entire codebase is `aria-label` on the timeline play button and a menu hamburger button. Everything else is missing:

| Component | Issue |
|-----------|-------|
| **Login form** | No `<label>` elements. The email `<input>` relies on placeholder text as its only label. Error messages (`<p class="error-message">`) have no `role="alert"` or `aria-live`, so screen readers won't announce login failures. |
| **Job list table** | No `<caption>` or `aria-label` on `<table>`. Row `<tr>` elements are clickable (`cursor: pointer` + click handler) but have no `tabindex`, `role="button"`, or keyboard event handlers — unreachable by keyboard. Action buttons use `title` attributes instead of `aria-label` (title is unreliable for screen readers). |
| **Mobile shell tabs** | Tab buttons lack `role="tab"`, `role="tablist"`, and `aria-selected`. The bottom nav looks like tabs visually but is semantically just a row of `<button>` elements. |
| **Sidebar** | No `aria-current="page"` on the active nav link. Collapse button has no `aria-expanded`. |
| **Job dropdowns** | `<select>` elements have no associated `<label>`. |

### 2.2 Keyboard navigation is absent

**Severity: High**

Beyond Enter-to-submit on text inputs and Escape-to-close on overlays, the app has no keyboard operability:

- Interactive table rows cannot be reached or activated by keyboard
- No visible focus rings — multiple components set `outline: none` on inputs without providing an alternative `:focus-visible` style
- No focus trap on the layout picker overlay
- No skip-to-content link
- No global keyboard shortcuts

### 2.3 WCAG contrast failures on muted text

**Severity: Medium**

The `--text-muted` color (`#6c7086`, Catppuccin Overlay 0) is used extensively for secondary labels, timestamps, helper text, job IDs, and column headers. On the app background (`#1e1e2e`), it produces a **2.4:1 contrast ratio — failing WCAG AA for both normal and large text** (requires 4.5:1 and 3:1 respectively).

Affected elements:
- Job list: timestamps in "Created" column, job ID snippets, helper text below form fields, filter chip count text, footer "Showing X of Y"
- Create form: all helper descriptions below fields
- Table headers (`font-size: 10px` + muted color = very hard to read)
- Login page subtitle "Cockpit"

The `--text-secondary` color (`#a6adc8`, 8.2:1) passes comfortably. The fix is straightforward: replace `--text-muted` usage on informational text with `--text-secondary` or a new intermediate stop at minimum 4.5:1.

---

## 3. Error Handling

### 3.1 API errors are silently swallowed

**Severity: High**

Every method in `api.service.ts` uses `catchError` that returns a fallback value (`of([])`, `of(null)`) and logs to `console.error`. No error signal is set, no toast or notification is triggered.

In practice this means:
- **Cancel a job** → if the API call fails, nothing happens. No error, no feedback. The button click just does nothing.
- **Delete a job** → same. Silent failure.
- **Resume a job** → same. The user has no idea it failed.
- **Load jobs** → the list shows empty. Indistinguishable from "you have no jobs."

The job review component is a partial exception: it sets a `resultMessage()` / `resultIsError()` signal on approve failure, but the message says "Check console for details" — not user-friendly.

**Suggestion:** Add a global error notification system (toast/snackbar). At minimum, mutating operations (cancel, delete, resume, approve) must show success/failure feedback. Grafana and Buildkite both show inline status banners after actions.

---

## 4. Job List

### 4.1 Status badges display raw database values

**Severity: Low**

`pending_review` renders with the underscore. Should display as "Pending Review" or "In Review". Quick fix — a display name map.

### 4.2 "Progress" column conveys no information

**Severity: Medium**

Every row shows `C: pending / V: pending`. The creator/validator progress model isn't populated with real data. This column occupies ~140px per row without conveying anything useful.

**Suggestion:** Replace with actionable data — current phase number, todo completion count, or token usage. Or remove the column and reclaim the space for the description, which is currently truncated at 80 characters.

### 4.3 No confirmation for destructive actions

**Severity: High**

Delete and Cancel buttons execute immediately on click. One misclick permanently deletes a job. No undo capability exists.

Industry patterns (from Smashing Magazine's 2024 analysis of dangerous action UX):
- **Modal with specific language**: "Delete job *Determine next steps*? This cannot be undone." CTA: "Delete Job" (red). Cancel: "Keep Job."
- **Inline double-click**: First click arms ("Are you sure?"), second click confirms. Lower friction, appropriate for lower-stakes items.
- **Undo toast**: For soft-deletable resources, skip the modal and show a 5-second undo toast. Only works if the backend supports soft delete.

At minimum, Delete should require a confirmation modal. Cancel (on a running job) should use inline confirmation.

### 4.4 Job selector dropdowns show raw UUIDs

**Severity: Medium**

The header dropdown shows `6118debd… · processing`. Since every job has a description ("Determine next steps", "Transform Graph"), the description should be the primary display text, with the UUID as secondary or omitted.

GitHub Actions uses integer run numbers (never UUIDs in the UI). Buildkite uses human-defined job names. The pattern is: always prefer human-readable identifiers; show technical IDs only in detail views or copy-to-clipboard affordances.

### 4.5 No text search

**Severity: Low**

With 22 jobs, status-only filtering is manageable. But at scale, users need to search by description. A simple client-side filter input above the table would suffice.

---

## 5. Review Tab

### 5.1 Dead end without pre-selected job context

**Severity: Medium**

The Review tab shows "Select a job to review" but has no built-in mechanism to select one. It depends on `DataService.currentJobId` being set by an *external* action — either the header dropdown or clicking "Review" in the job list.

Verified: selecting a `pending_review` job in the header dropdown, then switching to the Review tab, does load the review UI correctly (summary, confidence bar, deliverables, approve/continue buttons). The component works — it just lacks its own job picker.

**Suggestion:** Add a dropdown or auto-filtered list of `pending_review` jobs at the top of the Review tab. Alternatively, show a list of all reviewable jobs when no job is pre-selected — similar to how Buildkite's build UI shows state-based views with quick filtering.

### 5.2 No confirmation on Approve

**Severity: Medium**

The "Approve" button triggers an immediate server-side state transition with no confirmation. Given that approval is the terminal action in the review flow (marks the job as completed), this deserves at minimum an inline confirmation.

---

## 6. Instruction Builder

### 6.1 Empty state has no quick-start prompts

**Severity: Low**

The empty state shows a generic message ("Start typing below to begin a conversation"). Adding 3-4 clickable example prompts would reduce the blank-screen intimidation factor and teach users what the builder can do. Pattern: ChatGPT, Claude, and Cursor all use suggestion chips in their empty chat states.

### 6.2 No way to start a new session

**Severity: Low**

Once a builder session is active, there's no "New conversation" or "Clear" button. The user is stuck in the current session until the page is reloaded.

---

## 7. Debug Page

### 7.1 PostgreSQL table shows all 24 columns

**Severity: Medium**

The DB table panel renders all columns at once — including `resolved_config` and `context`, which contain massive JSON blobs truncated to `{"instructions": "# Instructions: Fessi Knowled..."`. At `font-size: 10px`, the table is nearly illegible.

**Suggestion:** Default to a useful column subset (id, description, status, created_at, config_name). Add a column toggle dropdown — the pattern used by Datadog's log explorer and most data table libraries.

### 7.2 Timeline bar is cryptic before a job is selected

**Severity: Low**

Shows `#0`, `1 entries`, disabled play button and slider. The purpose isn't obvious. An empty state message like "Select a job above to browse the audit trail" would help. Chrome DevTools' Performance panel (since Chrome 135) uses this pattern: centered illustration + clear call-to-action in empty panel states.

### 7.3 Empty panels should guide the user

**Severity: Low**

The "Agent Activity" panel shows "Select a job from the timeline bar" and the "Request Viewer" shows "Enter a document ID to view the request." These are adequate but could include more context — the Request Viewer could explain that doc IDs come from clicking entries in Agent Activity (it says this in small text, but it's easy to miss).

---

## 8. Inconsistencies

### 8.1 Mixed iconography

**Severity: Low**

| Location | Icon System |
|----------|-------------|
| Sidebar nav links | Material Symbols (`dashboard`, `folder_shared`, `bug_report`) |
| Bottom tab bar | Material Symbols (`construction`, `work`, `add_circle`) |
| Sidebar debug section | Emoji (`🔵`, `🐘`, `🍃`, `📐`, `🔄`) |
| Review tab refresh | Raw Unicode `↻` |
| Jobs tab refresh | Text button "Refresh" |

Three icon systems in one app. The sidebar debug section should use Material Symbols like everything else.

### 8.2 Job ID format varies across the app

**Severity: Low**

| Location | Format |
|----------|--------|
| Job list table | `6118debd...` (8 chars + `...`) |
| Mobile header dropdown | `6118debd… · processing` (8 chars + `…` + status) |
| Debug timeline dropdown | `6118debd... \| processing (46 steps)` (8 chars + `...` + status + step count) |
| Review tab detail | `ID: 2af0c28e...` (8 chars + `...`) |

Standardize to one format. Since descriptions are more useful, prefer: `"Determine next steps" · processing` with the UUID available on hover or copy.

---

## 9. Create Job Form

### 9.1 Advanced options push buttons below the fold

**Severity: Low**

When expanded, the form becomes very long (autonomy, model presets, strategic/tactical model configs, tool categories, instructions textarea, datasources). The Reset/Create buttons scroll off-screen.

**Suggestion:** Make the action buttons sticky at the bottom of the form viewport. Or restructure advanced options into collapsible sub-sections (Model Config, Tools, Instructions, Datasources) so users can expand only what they need.

---

## 10. Minor Polish

| # | Issue | Severity |
|---|-------|----------|
| 10.1 | Logout button fires immediately — no confirmation | Low |
| 10.2 | Login page is bare — just "SRW / Cockpit" + email input. No hint about how accounts are created or managed. | Low |
| 10.3 | Layout picker overlay doesn't close on outside click | Low |
| 10.4 | Filter chips with zero counts (`Created (0)`, `Failed (0)`) add visual noise — could be dimmed or hidden | Low |
| 10.5 | `outline: none` on form inputs without `:focus-visible` alternative removes the browser's default focus indicator | Medium |

---

## What Works Well

- **Catppuccin Mocha theme** — consistently applied, cohesive, and well-suited for a developer tool. Primary and secondary text colors pass WCAG AA comfortably (13.8:1 and 8.2:1 respectively). Status badge colors use desaturated Catppuccin palette stops rather than raw hues — correct for a dark theme.
- **Tiling panel system** — the split/close/swap controls per panel, layout presets, and localStorage persistence are a powerful feature. The recursive `SplitPanelComponent` with `angular-split` is well-architected.
- **Create Job form** — expert selection cards with icons and tag chips are informative. Progressive disclosure via "Show Advanced Options" keeps the basic flow simple. Model preset quick-select buttons are a nice touch.
- **Status badge semantic colors** — green (completed), yellow (processing), orange (pending_review), red (failed), grey (cancelled) form a clear, consistent system.
- **Filter chips** in the Jobs list — fast, single-click status filtering with count badges.
- **CSS custom properties** — the entire theme is driven by CSS variables in `:root`, making it trivial to adjust or add theme variants.
- **IndexedDB caching** (via Dexie) for audit trail data — avoids re-fetching large datasets on tab switches.
- **Review UI** (when a job is loaded) — the summary, confidence bar, deliverables list, Gitea workspace link, and approve/feedback flow are well-designed and convey the right information for a review decision.
- **Responsive `@media` queries** are consistent at 768px wherever used, and the instruction builder correctly bumps textarea font to 16px to prevent iOS auto-zoom.

---

## Priority Summary

| Priority | Items |
|----------|-------|
| **High** | 1.1 (dual nav), 2.1 (no ARIA), 2.2 (no keyboard nav), 3.1 (silent API errors), 4.3 (no delete confirmation) |
| **Medium** | 1.2 (duplicate nav), 2.3 (contrast failures), 4.2 (dead progress column), 4.4 (UUID dropdowns), 5.1 (review dead end), 5.2 (no approve confirmation), 7.1 (illegible DB table), 10.5 (missing focus rings) |
| **Low** | 4.1 (status underscore), 4.5 (no search), 6.1 (builder empty state), 6.2 (no session reset), 7.2 (cryptic timeline), 7.3 (empty panel guidance), 8.1 (mixed icons), 8.2 (ID format variance), 9.1 (form scroll), 10.1-10.4 |
