# `reviewing` Parent Un-stick Watchdog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Guarantee a parent job never stays wedged in `reviewing` forever when its critic dies by any path other than a clean `/complete` — flip it to `pending_review` and notify the owner.

**Architecture:** A second, ordered step inside the existing `stale_verification_sweeper` loop. Step 1 (unchanged) cancels dead critic subjobs; a new Step 2 calls a new DB method `unstick_reviewing_parents(grace)` that CAS-flips wedged parents to `pending_review`, then dispatches a best-effort notification via a new `NotificationService.notify_review_returned_to_manual` method. No pod/workspace-lifecycle changes (P0 already reclaims the pod); no new background loop.

**Tech Stack:** Python 3.12 (CI floor), asyncpg (Postgres), FastAPI orchestrator, pytest + `unittest.mock`.

**Spec:** `docs/superpowers/specs/2026-07-05-reviewing-parent-unstick-watchdog-design.md`

## Global Constraints

- **Predicate semantics (verbatim from spec):** un-stick a `reviewing` parent only when every critic child is terminal-`failed`/`cancelled` **or none exists** — i.e. `NOT EXISTS` a critic whose `status NOT IN ('failed','cancelled')`. This deliberately excludes both **live** critics (`processing`/`created`/`paused`/`waiting…`) and **`completed`** critics (the verdict handler's job — anti-race).
- **Critic discriminator:** a verification subjob is identified by `context->>'verification_target' IS NOT NULL` (same discriminator `cancel_stale_verification_subjobs` uses).
- **CAS + idempotency:** the flip UPDATE must carry `WHERE status='reviewing'` so a second sweeper / the verdict handler cannot double-flip. Mirrors `claim_delegation_resume`.
- **Grace floor:** new env `REVIEWING_STUCK_GRACE_MINUTES`, default **30**. Read via `os.getenv` at module load, exactly like the existing `STALE_VERIFICATION_HOURS` (default 6, **unchanged**) and `STALE_VERIFICATION_SWEEP_SECONDS` (default 300). **No Helm change** — these sweep env vars are not surfaced in Helm today; the default applies and it stays overridable via env.
- **Notification:** a dedicated `NotificationService` method mirroring `notify_automation_auto_disabled` — SSE broadcast + email if the user has it enabled, **NOT** quiet-hours-gated (an unattended job needs prompt attention), best-effort and non-fatal.
- **Test convention (verbatim from the sibling file):** these DB helpers have **no test DB** — mock the connection and assert the SQL's wire-level contract + return parsing (see the header of `tests/test_stale_verification_sweeper.py`). The behavioral predicate matrix is verified on the dev/k3d cluster, not in pytest.
- **In-orchestrator import style:** sibling services import as `from services.notification_service import notification_service` (see `orchestrator/services/cron_dispatcher.py:39`). To avoid adding an import-chain dependency to the sweeper's test import, the default notifier is imported **lazily inside `_sweep_tick`** only when rows exist and no notifier was injected.
- **Git:** work on `develop`. Commit after each task. **Do NOT push** (user's standing rule). Every commit message ends with the trailer `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

---

## File Structure

- `orchestrator/database/postgres.py` — **Modify.** Add `unstick_reviewing_parents(grace_minutes)` next to `cancel_stale_verification_subjobs` (~line 2742).
- `orchestrator/services/notification_service.py` — **Modify.** Add `notify_review_returned_to_manual(...)` next to `notify_automation_auto_disabled` (~line 213).
- `orchestrator/services/stale_verification_sweeper.py` — **Modify.** Add `REVIEWING_STUCK_GRACE_MINUTES`; extend `_sweep_tick` with Step 2 + notification; update the loop's call + log line.
- `tests/test_stale_verification_sweeper.py` — **Modify.** New `TestUnstickReviewingParents` class; update `TestSweepTick` for the new signature + notify wiring.
- `tests/test_notify_review_returned.py` — **Create.** Unit test for the new NotificationService method.
- `docs/issues/critic_failure_leaves_parent_job_stuck_reviewing.md` — **Modify.** Mark fix item #4 implemented, link the spec.
- `docs/superpowers/specs/2026-07-05-reviewing-parent-unstick-watchdog-design.md` — **Modify.** Flip Status to Implemented.

---

## Task 1: `unstick_reviewing_parents` DB method

**Files:**
- Modify: `orchestrator/database/postgres.py` (add after `cancel_stale_verification_subjobs`, ~line 2786)
- Test: `tests/test_stale_verification_sweeper.py` (new class `TestUnstickReviewingParents`)

**Interfaces:**
- Consumes: `self.acquire()` async context manager yielding an asyncpg connection (existing pattern).
- Produces: `async def unstick_reviewing_parents(self, grace_minutes: int = 30) -> list[dict[str, Any]]` — returns one dict per un-stuck parent with keys `id`, `user_id`, `config_name` (asyncpg `Record` → `dict`). Empty list when nothing was un-stuck.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_stale_verification_sweeper.py`:

```python
class TestUnstickReviewingParents:
    @pytest.mark.asyncio
    async def test_executes_update_with_grace_and_returns_rows(self):
        conn = AsyncMock()
        conn.fetch = AsyncMock(
            return_value=[
                {"id": "p1", "user_id": "u1", "config_name": "scholar"},
            ]
        )
        db = _make_db(conn)

        rows = await db.unstick_reviewing_parents(grace_minutes=30)

        assert rows == [{"id": "p1", "user_id": "u1", "config_name": "scholar"}]
        conn.fetch.assert_awaited_once()
        args = conn.fetch.await_args.args
        sql = args[0]
        # Flips reviewing → pending_review, gated by the grace floor and the
        # "no non-failed/cancelled critic" clause; grace binds as $1.
        assert "status = 'pending_review'" in sql
        assert "p.status = 'reviewing'" in sql
        assert "make_interval" in sql
        assert "verification_target" in sql
        assert "NOT IN ('failed', 'cancelled')" in sql
        assert "RETURNING" in sql
        assert args[1] == 30

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_rows(self):
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])
        db = _make_db(conn)

        assert await db.unstick_reviewing_parents() == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_stale_verification_sweeper.py::TestUnstickReviewingParents -v`
Expected: FAIL with `AttributeError: ... has no attribute 'unstick_reviewing_parents'`

- [ ] **Step 3: Implement the DB method**

In `orchestrator/database/postgres.py`, immediately after `cancel_stale_verification_subjobs` returns (after line 2786):

```python
    async def unstick_reviewing_parents(
        self, grace_minutes: int = 30
    ) -> List[Dict[str, Any]]:
        """Un-stick parents wedged in 'reviewing' whose critic pipeline is dead.

        A parent goes 'reviewing' and a critic verification subjob checks its
        work; the critic un-sticks the parent only by reaching its own
        /complete (``_handle_critic_verdict_on_complete``). A critic that ends
        'failed', or is orphaned → cancelled, never reaches that handler, so the
        parent sits in 'reviewing' forever. This flips such a parent back to
        'pending_review' (human review takes over) + lets the caller notify.

        Fires only when EVERY critic child is terminal-failed/cancelled (or none
        exists) and the parent has been reviewing past ``grace_minutes``:

        - A live critic ('processing'/'created'/'paused'/'waiting…') keeps the
          parent untouched — a long review or a recovering (paused) critic is
          never pre-empted.
        - A 'completed' critic also keeps the parent untouched — that is the
          verdict handler's job; excluding it avoids racing that handler.
        - The grace floor on ``updated_at`` dodges the critic-spawn and
          in-flight-verdict windows.

        The CAS (``WHERE status='reviewing'``) makes it idempotent and safe
        against a concurrent sweeper / the verdict handler.

        See docs/superpowers/specs/2026-07-05-reviewing-parent-unstick-watchdog-design.md
        and critic_failure_leaves_parent_job_stuck_reviewing.md (#4).

        Args:
            grace_minutes: minimum time a parent must have been in 'reviewing'.

        Returns:
            One dict ``{id, user_id, config_name}`` per parent un-stuck.
        """
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                UPDATE jobs AS p
                   SET status = 'pending_review',
                       error_message = 'Automated verification did not complete (critic pipeline died); returned to manual review.',
                       updated_at = CURRENT_TIMESTAMP
                 WHERE p.status = 'reviewing'
                   AND p.updated_at
                       < CURRENT_TIMESTAMP - make_interval(mins => $1::int)
                   AND NOT EXISTS (
                         SELECT 1 FROM jobs c
                          WHERE c.parent_job_id = p.id
                            AND c.context->>'verification_target' IS NOT NULL
                            AND c.status NOT IN ('failed', 'cancelled')
                       )
                RETURNING p.id, p.user_id, p.config_name
                """,
                grace_minutes,
            )
        return [dict(r) for r in rows]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_stale_verification_sweeper.py::TestUnstickReviewingParents -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add orchestrator/database/postgres.py tests/test_stale_verification_sweeper.py
git commit -m "feat(orchestrator): add unstick_reviewing_parents DB predicate

Flips a parent wedged in 'reviewing' back to 'pending_review' when every
critic child is terminal-failed/cancelled (or none exists) and it has been
reviewing past a grace floor. CAS on status='reviewing'; excludes live and
completed critics. P2 of the critic-failure incident." \
-m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: `notify_review_returned_to_manual` NotificationService method

**Files:**
- Modify: `orchestrator/services/notification_service.py` (add after `notify_automation_auto_disabled`, ~line 288)
- Create: `tests/test_notify_review_returned.py`

**Interfaces:**
- Consumes: the module singleton `notification_service` (an instance of `NotificationService`) with `connect(db, email_service, notification_feed)` already called; internal helpers `self._get_user_channels(user_id)`, `self._notification_feed.broadcast(...)`, `self._email_service.send_system_notification(...)`, `self._db.get_user(user_id)`, `self._cockpit_url`.
- Produces: `async def notify_review_returned_to_manual(self, user_id: str, job_id: str, config_name: str) -> dict[str, Any]` — returns a per-channel results dict (e.g. `{"sse": True, "email": True}`); returns `{"error": ...}` when the service is not connected. Never raises.

- [ ] **Step 1: Write the failing test**

Create `tests/test_notify_review_returned.py`:

```python
"""Unit test for NotificationService.notify_review_returned_to_manual.

Mirrors the notify_automation_auto_disabled setup in
test_headless_notifications_phase4.py — mocked feed + email service, no real DB.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.services.notification_service import NotificationService


def _connected_service(*, email_enabled=True, has_email=True):
    svc = NotificationService()
    feed = MagicMock()
    feed.broadcast = MagicMock()
    email = MagicMock()
    email.send_system_notification = AsyncMock(return_value=True)
    db = MagicMock()
    db.get_user = AsyncMock(
        return_value={"email": "owner@example.com", "display_name": "Owner"}
        if has_email
        else {"email": None}
    )
    svc.connect(db=db, email_service=email, notification_feed=feed)
    svc._get_user_channels = AsyncMock(return_value={"email": email_enabled})
    return svc, feed, email


class TestNotifyReviewReturnedToManual:
    @pytest.mark.asyncio
    async def test_broadcasts_sse_and_sends_email(self):
        svc, feed, email = _connected_service()

        result = await svc.notify_review_returned_to_manual(
            user_id="u1", job_id="job-123", config_name="scholar"
        )

        assert result["sse"] is True
        assert result["email"] is True
        # SSE carries the job id + a review deep-link.
        feed.broadcast.assert_called_once()
        sse_kwargs = feed.broadcast.call_args.kwargs
        assert sse_kwargs["event_type"] == "review_returned_to_manual"
        assert sse_kwargs["data"]["job_id"] == "job-123"
        # Email addressed to the owner, mentions the config label.
        email.send_system_notification.assert_awaited_once()
        mail_kwargs = email.send_system_notification.await_args.kwargs
        assert mail_kwargs["to"] == "owner@example.com"
        assert "scholar" in mail_kwargs["body_md"]

    @pytest.mark.asyncio
    async def test_skips_email_when_channel_disabled(self):
        svc, feed, email = _connected_service(email_enabled=False)

        result = await svc.notify_review_returned_to_manual(
            user_id="u1", job_id="job-123", config_name="scholar"
        )

        assert result.get("sse") is True
        email.send_system_notification.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_returns_error_when_not_connected(self):
        svc = NotificationService()  # not connected → _available False

        result = await svc.notify_review_returned_to_manual(
            user_id="u1", job_id="job-123", config_name="scholar"
        )

        assert "error" in result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_notify_review_returned.py -v`
Expected: FAIL with `AttributeError: 'NotificationService' object has no attribute 'notify_review_returned_to_manual'`

- [ ] **Step 3: Implement the method**

In `orchestrator/services/notification_service.py`, after `notify_automation_auto_disabled` ends (before `notify_admins_user_registered`, ~line 288):

```python
    async def notify_review_returned_to_manual(
        self,
        user_id: str,
        job_id: str,
        config_name: str,
    ) -> dict[str, Any]:
        """Notify the owner that automated review died and the job is back to manual review.

        Called from ``stale_verification_sweeper`` after
        ``unstick_reviewing_parents`` flips a parent 'reviewing' →
        'pending_review' because its critic pipeline died. Mirrors
        ``notify_automation_auto_disabled``: SSE for real-time cockpit plus an
        email if the user has that channel enabled. Does NOT respect quiet
        hours — an unattended job needing manual review should reach the owner
        promptly. Best-effort; failures are logged, never raised.
        """
        if not self._available:
            return {"error": "NotificationService not initialized"}

        results: dict[str, Any] = {}
        user_channels = await self._get_user_channels(user_id)

        label = config_name or "job"
        review_path = f"/inbox?job={job_id}&review=1"
        subject = "Job needs manual review — automated verification failed"
        body_md = (
            f"Automated verification for your **{label}** job did not complete "
            f"(the review pipeline died), so it has been returned to **manual "
            f"review**. Open it in the cockpit to approve it or send it back "
            f"with feedback."
        )

        # SSE broadcast — real-time cockpit notification.
        if self._notification_feed:
            try:
                self._notification_feed.broadcast(
                    user_id=user_id,
                    event_type="review_returned_to_manual",
                    data={
                        "job_id": job_id,
                        "config_name": config_name,
                        "cockpit_url": f"{self._cockpit_url}{review_path}",
                    },
                )
                results["sse"] = True
            except Exception as e:
                logger.warning(
                    "SSE broadcast failed for review-returned (job=%s): %s",
                    job_id,
                    e,
                )
                results["sse"] = False

        # Email — only if the user has the email channel enabled and SMTP is
        # configured. The SSE event is the primary in-cockpit signal.
        if user_channels.get("email", True) and self._email_service and self._db:
            try:
                user = await self._db.get_user(user_id)
            except Exception:
                user = None
            if user and user.get("email"):
                try:
                    results[
                        "email"
                    ] = await self._email_service.send_system_notification(
                        to=user["email"],
                        to_name=user.get("display_name") or "User",
                        subject=subject,
                        body_md=body_md,
                        cockpit_path=review_path,
                    )
                except Exception as e:
                    logger.warning(
                        "Email send failed for review-returned (job=%s): %s",
                        job_id,
                        e,
                    )
                    results["email"] = False
        return results
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_notify_review_returned.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add orchestrator/services/notification_service.py tests/test_notify_review_returned.py
git commit -m "feat(orchestrator): add notify_review_returned_to_manual

SSE + email (no quiet-hours gate) telling a job owner their automated review
died and the job is back to manual review. Mirrors notify_automation_auto_disabled." \
-m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Wire Step 2 into the sweeper tick + loop

**Files:**
- Modify: `orchestrator/services/stale_verification_sweeper.py`
- Test: `tests/test_stale_verification_sweeper.py` (update `TestSweepTick`)

**Interfaces:**
- Consumes: `db.cancel_stale_verification_subjobs(stale_hours) -> int` (existing); `db.unstick_reviewing_parents(grace_minutes) -> list[dict]` (Task 1); `notifier.notify_review_returned_to_manual(user_id, job_id, config_name)` (Task 2, defaults to the `notification_service` singleton).
- Produces: `async def _sweep_tick(db, stale_hours, grace_minutes, notifier=None) -> tuple[int, int]` returning `(cancelled, unstuck)`; module constant `REVIEWING_STUCK_GRACE_MINUTES: int`.

- [ ] **Step 1: Update the existing tick test + add the new wiring tests**

Replace the `TestSweepTick` class in `tests/test_stale_verification_sweeper.py` with:

```python
class TestSweepTick:
    @pytest.mark.asyncio
    async def test_runs_both_steps_and_returns_counts(self):
        db = AsyncMock()
        db.cancel_stale_verification_subjobs = AsyncMock(return_value=3)
        db.unstick_reviewing_parents = AsyncMock(return_value=[])
        notifier = AsyncMock()

        cancelled, unstuck = await _sweep_tick(
            db, stale_hours=6, grace_minutes=30, notifier=notifier
        )

        assert (cancelled, unstuck) == (3, 0)
        db.cancel_stale_verification_subjobs.assert_awaited_once_with(6)
        db.unstick_reviewing_parents.assert_awaited_once_with(30)
        notifier.notify_review_returned_to_manual.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_notifies_each_unstuck_parent(self):
        db = AsyncMock()
        db.cancel_stale_verification_subjobs = AsyncMock(return_value=0)
        db.unstick_reviewing_parents = AsyncMock(
            return_value=[
                {"id": "p1", "user_id": "u1", "config_name": "scholar"},
                {"id": "p2", "user_id": "u2", "config_name": "developer"},
            ]
        )
        notifier = AsyncMock()

        cancelled, unstuck = await _sweep_tick(
            db, stale_hours=6, grace_minutes=30, notifier=notifier
        )

        assert (cancelled, unstuck) == (0, 2)
        assert notifier.notify_review_returned_to_manual.await_count == 2
        first = notifier.notify_review_returned_to_manual.await_args_list[0].kwargs
        assert first == {
            "user_id": "u1",
            "job_id": "p1",
            "config_name": "scholar",
        }

    @pytest.mark.asyncio
    async def test_notify_failure_does_not_abort_tick(self):
        db = AsyncMock()
        db.cancel_stale_verification_subjobs = AsyncMock(return_value=0)
        db.unstick_reviewing_parents = AsyncMock(
            return_value=[{"id": "p1", "user_id": "u1", "config_name": "scholar"}]
        )
        notifier = AsyncMock()
        notifier.notify_review_returned_to_manual = AsyncMock(
            side_effect=RuntimeError("smtp down")
        )

        # Must swallow the notify error and still report the un-stuck count.
        cancelled, unstuck = await _sweep_tick(
            db, stale_hours=6, grace_minutes=30, notifier=notifier
        )
        assert (cancelled, unstuck) == (0, 1)
```

Also update the two `TestSweeperLoop` tests so the mocked `db` has the new method (add `db.unstick_reviewing_parents = AsyncMock(return_value=[])` right after each `db.cancel_stale_verification_subjobs = AsyncMock(...)` line in `test_no_tick_when_shutdown_preset`, `test_runs_one_tick_then_exits_on_shutdown`, and `test_tick_exception_does_not_kill_loop`).

- [ ] **Step 2: Run the tick tests to verify they fail**

Run: `python -m pytest tests/test_stale_verification_sweeper.py::TestSweepTick -v`
Expected: FAIL — `_sweep_tick()` currently takes 2 args / returns an int (`TypeError` or assertion failure on the tuple).

- [ ] **Step 3: Implement Step 2 in the sweeper**

Edit `orchestrator/services/stale_verification_sweeper.py`.

Add the grace constant after `STALE_HOURS` (after line 39):

```python
# Grace floor (minutes) a parent must have sat in 'reviewing' before the
# watchdog un-sticks it. Long enough to clear the critic-spawn window and any
# in-flight verdict; the real gate is "no non-failed/cancelled critic exists".
REVIEWING_STUCK_MINUTES = int(os.getenv("REVIEWING_STUCK_GRACE_MINUTES", "30"))
```

Replace the loop's tick call + logging (lines 51–62) with:

```python
    while not shutdown_event.is_set():
        try:
            cancelled, unstuck = await _sweep_tick(
                db, STALE_HOURS, REVIEWING_STUCK_MINUTES
            )
            if cancelled:
                logger.info(
                    "Stale verification sweeper cancelled %d orphaned subjob(s)",
                    cancelled,
                )
            if unstuck:
                logger.info(
                    "Stale verification sweeper un-stuck %d reviewing parent(s) "
                    "→ pending_review",
                    unstuck,
                )
        except Exception:
            logger.exception(
                "Stale verification sweeper tick raised; will retry next tick"
            )
```

Also update the startup log line (lines 46–50) to include the grace:

```python
    logger.info(
        "Stale verification sweeper started (tick=%ds, stale_hours=%d, "
        "reviewing_grace_min=%d)",
        TICK_SECONDS,
        STALE_HOURS,
        REVIEWING_STUCK_MINUTES,
    )
```

Replace the `_sweep_tick` function (lines 73–75) with:

```python
async def _sweep_tick(
    db: Any,
    stale_hours: int,
    grace_minutes: int,
    notifier: Any = None,
) -> tuple[int, int]:
    """Run one sweep. Returns ``(cancelled_subjobs, unstuck_parents)``.

    Step 1 cancels dead/orphaned critic subjobs (also what turns a lingering
    'paused' orphan terminal at the stale horizon). Step 2 then un-sticks any
    parent whose critic pipeline is now dead and notifies its owner. Ordering
    matters: a critic cancelled in Step 1 makes its parent eligible in Step 2
    on this same tick.
    """
    cancelled = await db.cancel_stale_verification_subjobs(stale_hours)

    unstuck_rows = await db.unstick_reviewing_parents(grace_minutes)
    if unstuck_rows and notifier is None:
        # Lazy import keeps the sweeper's test import free of the
        # notification_service dependency (tests always inject a notifier).
        from services.notification_service import notification_service as notifier

    for row in unstuck_rows:
        try:
            await notifier.notify_review_returned_to_manual(
                user_id=str(row["user_id"]),
                job_id=str(row["id"]),
                config_name=row.get("config_name") or "",
            )
        except Exception:
            logger.exception(
                "Failed to notify owner of un-stuck reviewing parent %s "
                "(non-fatal)",
                row.get("id"),
            )

    return cancelled, len(unstuck_rows)
```

- [ ] **Step 4: Run the tick tests to verify they pass**

Run: `python -m pytest tests/test_stale_verification_sweeper.py -v`
Expected: PASS (all classes — `TestSweepTick`, `TestSweeperLoop`, `TestCancelStaleVerificationSubjobs`, `TestUnstickReviewingParents`)

- [ ] **Step 5: Lint + full sweeper-adjacent test run**

Run: `ruff check orchestrator/services/stale_verification_sweeper.py orchestrator/database/postgres.py orchestrator/services/notification_service.py`
Expected: no errors (CI auto-runs ruff on push; catch it here).

Run: `python -m pytest tests/test_stale_verification_sweeper.py tests/test_notify_review_returned.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add orchestrator/services/stale_verification_sweeper.py tests/test_stale_verification_sweeper.py
git commit -m "feat(orchestrator): un-stick reviewing parents in the verification sweeper

Step 2 of the tick calls unstick_reviewing_parents and notifies each owner via
notify_review_returned_to_manual. Adds REVIEWING_STUCK_GRACE_MINUTES (default 30).
Closes the 'critic dies, parent wedged in reviewing forever' gap (P2)." \
-m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Documentation

**Files:**
- Modify: `docs/issues/critic_failure_leaves_parent_job_stuck_reviewing.md`
- Modify: `docs/superpowers/specs/2026-07-05-reviewing-parent-unstick-watchdog-design.md`

- [ ] **Step 1: Mark the issue's fix item #4 implemented**

In `docs/issues/critic_failure_leaves_parent_job_stuck_reviewing.md`, under "## Expanded fix (priority order)", change item **4** to record it as done, appending:

```markdown
   **[2026-07-05 — IMPLEMENTED]** The `reviewing` watchdog is now Step 2 of
   `stale_verification_sweeper`: `unstick_reviewing_parents(grace)` CAS-flips a
   parent to `pending_review` once every critic child is terminal-failed/
   cancelled (or none exists) and it has been reviewing past
   `REVIEWING_STUCK_GRACE_MINUTES` (default 30). Owner is notified via
   `NotificationService.notify_review_returned_to_manual`. The `paused`-orphan
   case rides the existing 6h critic-cancel (a `paused` critic may be
   recovering, so it is deliberately treated as live). Design + rejected
   options (b)/(c): `docs/superpowers/specs/2026-07-05-reviewing-parent-unstick-watchdog-design.md`.
```

- [ ] **Step 2: Flip the spec status**

In `docs/superpowers/specs/2026-07-05-reviewing-parent-unstick-watchdog-design.md`, change the first status line to:

```markdown
**Status:** Implemented (2026-07-05) on `develop`; unit-tested (mock-connection contract + tick wiring). Behavioral predicate matrix pending dev-cluster verification.
```

- [ ] **Step 3: Commit**

```bash
git add docs/issues/critic_failure_leaves_parent_job_stuck_reviewing.md docs/superpowers/specs/2026-07-05-reviewing-parent-unstick-watchdog-design.md
git commit -m "docs: record reviewing-parent un-stick watchdog (fix #4) as implemented" \
-m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Post-plan verification (dev cluster — not pytest)

The SQL predicate's runtime behavior has **no test DB** (per the sweeper's test convention). After merge/deploy to dev, verify on k3d/dev by seeding a `reviewing` parent with each critic end-state and confirming:

- critic `failed` / `cancelled` / none → parent flips to `pending_review` after grace; owner notified.
- critic `processing` / `paused` / `completed` → parent untouched.
- P0 frees the pod once the (now terminal) critic no longer counts as a live shared child.

Use the in-pod python + `X-Internal-Key` path from the "Run/verify a job on local k3d" memory note; the un-stuck rows surface as `pending_review` in the cockpit.
```
