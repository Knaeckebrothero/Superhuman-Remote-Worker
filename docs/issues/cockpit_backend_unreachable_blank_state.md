# Cockpit Silently Hides Sidebar When Backend Is Unreachable

**Date:** 2026-05-03
**Status:** Open
**Component:** `cockpit/src/app/`

## Summary

When the orchestrator API is unreachable (down, wrong port, network error, CORS rejection), the cockpit logs in via Keycloak successfully but then renders a partially-functional shell with **no sidebar, no error message, and no indication that anything is wrong**. The user sees the default route content (currently the Instruction Builder) but cannot navigate anywhere. There is no fallback page, no toast, no banner — the failure is completely silent.

This is a poor first-run experience and was confusing enough during local development that it required Playwright + DOM inspection + reading three service files to diagnose.

## Reproduction

1. Stop the orchestrator (or run cockpit pointing at a host where `/api/users/me` 502s/refuses).
2. `cd cockpit && npm start`.
3. Log into Keycloak.
4. Land back on `localhost:4200`.
5. Observe: router-outlet renders, banners render, **no sidebar**, no error UI.

## Root Cause

The shell gates the sidebar on user state that never loads:

```typescript
// cockpit/src/app/app.ts:203-205
readonly showSidebar = computed(
  () => this.userService.isAuthenticated() && this.userService.isApproved(),
);
```

```typescript
// cockpit/src/app/core/services/user.service.ts
readonly isAuthenticated = computed(
  () => this.keycloak.authenticated && this.currentUser() !== null,
);
```

Flow when the backend is down:

1. Keycloak completes login → `keycloak.authenticated = true`.
2. `UserService` calls `/api/users/me` to populate `currentUser`.
3. The request fails (network error / 5xx / CORS).
4. `currentUser()` stays `null` → `isAuthenticated()` stays `false`.
5. `showSidebar()` is `false` → sidebar is omitted.
6. `pendingApproval()` is also `false` (it requires `isAuthenticated && !isApproved`) → the pending-approval card is not shown either.
7. The `@else` branch in `app.ts:68-72` renders the router-outlet anyway, so the user sees default route content but no shell chrome.

The failure is indistinguishable from "not logged in yet" from the template's perspective, but Keycloak has already redirected back, so the user thinks they *are* logged in.

## Desired Behaviour

When `keycloak.authenticated === true` but the user-info fetch has failed, render a clear fallback page that:

- States that the backend API is unreachable.
- Shows the configured API base URL (so the user can sanity-check `environment.ts` / Helm values).
- Offers a **Retry** button that re-runs the user-info fetch.
- Offers a **Logout** button (mirror the pending-approval card).
- Optionally surfaces the underlying error (status code, error message) behind a "Details" disclosure.

## Suggested Implementation

1. Extend `UserService` with a tri-state load signal: `'loading' | 'ready' | 'error'` plus a `lastError` signal.
2. In `app.ts`, add a `backendUnreachable` computed: `keycloak.authenticated && userLoadState() === 'error'`.
3. Branch the template:
   - `pendingApproval()` → existing card.
   - `backendUnreachable()` → new fallback card.
   - else → router-outlet.
4. Reuse the visual language of the existing pending-approval card (`app.ts:58-67`) for consistency.
5. Add Transloco keys under `errors.backend.*`.

## Out of Scope

- Cockpit-side health-check polling. The retry button is enough; we do not need a background poller for this case.
- Distinguishing "user row missing in DB" from "API completely unreachable" — both are `currentUser === null` from the cockpit's perspective. The fallback page can offer the same retry path for both.

## Notes

This bug was masked on the dev cluster because the orchestrator is always running there and the operator's Keycloak account had been provisioned long ago. It only surfaced locally where the orchestrator wasn't started.
