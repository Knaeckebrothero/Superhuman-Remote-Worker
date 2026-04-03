C# Persistent Agent Sessions - UI Assessment

Reviewed: 2026-03-31
Updated: 2026-04-02

## Bugs

### ~~1. Grammar: "1 turns"~~ — Fixed (2026-04-01)

Pluralization now conditional: `{{ (thread.total_turns || 0) === 1 ? 'turn' : 'turns' }}`.

### ~~2. Raw citation markup exposed in chat~~ — Fixed (2026-04-01)

Added custom `marked` inline extension (`citation-extension.ts`) that parses `【cite_web(Title, URL)】` and `【cite_document(Title, Path)】` into rendered links/spans. Registered via `MARKED_EXTENSIONS` provider in `app.config.ts`.

### ~~3. Sidebar "Sessions" link hijacked by active session~~ — Fixed (2026-04-02)

`sessionsLink` computed signal now always returns `'/sessions'`.

### ~~4. Silent error on session creation~~ — Fixed (2026-04-01)

Now shows toast error: `this.toast.error(e?.error?.detail || 'Failed to create session')`.

### ~~5. No confirmation on "End Session"~~ — Fixed (2026-04-02)

Now shows `confirm('End this session? Work will be saved but the agent will stop.')` before proceeding.

## UX Issues

### ~~6. All sessions indistinguishable~~ — Fixed (2026-04-02)

Auto-generates a 5-8 word title via `_generate_title()` after the first assistant turn. Pushes `title.updated` WS event to update the chat header live. Also fixed end/idle title checks to catch "Local Session" variants.

### ~~7. Chat header always says "Persistent Agent"~~ — Fixed (2026-04-01)

Now shows `chat.sessionTitle() || 'Persistent Agent'`. Title loaded from REST endpoint on connect via `loadThreadMeta()`. Also added status bar with model name, turn count, and permission mode.

### ~~8. Duplicate project names in New Session dialog~~ — Fixed (2026-04-02)

Sessions page now passes `user_id` to `/api/projects` so only the user's own projects are returned (via `project_members` join). Eliminates duplicates from other users' identically-named projects.

### ~~9. Filter-specific empty states missing~~ — Fixed (2026-04-02)

Shows "No active sessions." or "No ended sessions." with icon when filter yields no results.

### ~~10. IDE polling never stops for 'unavailable'~~ — Fixed (2026-04-02)

Polling now stops after 30 attempts (5 minutes) regardless of status.

## Polish / Minor

### ~~11. Config badge shows raw name~~ — Fixed (2026-04-02)

Now uses `titlecase` pipe: "interactive" → "Interactive".

### ~~12. No session counts on filter tabs~~ — Fixed (2026-04-01)

Tabs now show `All (N) | Active (N) | Ended (N)` via computed signals.

### 13. Model list is hardcoded

**File:** `cockpit/src/app/simple/pages/sessions/sessions-page.component.ts:96-117`

Will need code changes every time a model is added/removed.

### ~~14. "Active session" banner shows even on "Ended" filter~~ — Fixed (2026-04-02)

Banner now hidden when `statusFilter() === 'ended'`.

## Summary

Updated: 2026-04-02

All issues resolved except #13 (model list hardcoded — needs API endpoint to serve available models dynamically).
