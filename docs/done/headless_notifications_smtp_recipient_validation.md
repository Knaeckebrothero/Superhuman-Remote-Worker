# Headless notifications — SMTP bridge accepts undeliverable recipients

**Status:** Resolved 2026-05-13. Shipped Option A as recommended: `orchestrator/services/email.py` now has a module-level `_is_undeliverable_recipient` helper that rejects empty addresses, malformed `@`-placement, and the four RFC 6761 reserved TLDs (`invalid`/`test`/`example`/`localhost`). `_send` filters the recipient list before composing the message and returns False with a warning log if any are undeliverable, so the caller records `delivery_status='failed'` rather than `'sent'`. Option B (`email-validator` with DNS-MX) was deliberately not adopted — the DNS lookup adds a network failure mode that could block legitimate mail, and `.invalid` already fails the cheap syntactic check. Tests in `tests/test_email_service.py` cover the predicate (parametrized over reserved TLDs + malformed shapes) and the `_send` integration path (list-with-one-bad aborts the whole envelope).

## Symptom (observed 2026-05-12 in Phase 2-4 smoke)

During smoke setup a test user was created with email `smoke-tester@example.invalid`. The notify sweeper picked the user up, generated magic-link tokens, called `email_service.send_email(...)`, and the dev SMTP bridge returned success. `thread_notifications.delivery_status` was written as `'sent'`.

`.invalid` is reserved by **RFC 6761 §6.4** as a TLD that **must never resolve in the DNS** and which exists specifically so it cannot accidentally be delivered to. A `'sent'` status row for a `.invalid` recipient is a contradiction: the bridge cannot have actually delivered the message, but our DB now claims it did.

The same flaw would let `user@nonexistent.example` or `garbage@@@formatting` through, depending on what the SMTP relay tolerates.

## Root cause

`orchestrator/services/email.py:send_email` has no recipient validation. The full pre-send guard is:

```python
if not self.is_configured:
    logger.warning("SMTP not configured — cannot send email")
    return False
```

After that, the address is passed verbatim to `aiosmtplib.send(...)`. The dev SMTP bridge (whatever's behind `SMTP_HOST` in the dev compose) accepts the envelope at the protocol level — `RCPT TO:` parses, server returns 250 — without verifying that the domain has an MX record or that the TLD is real. So `send_email` returns `True`, the sweeper writes `'sent'`, everyone's happy until somebody tries to debug "why didn't my user get the email."

## Impact

- **False-positive observability**: `delivery_status='sent'` is unreliable as a signal that the message reached an inbox. Anyone debugging "user X didn't get notified" has to know to also check whether their email is a real address.
- **Test-data leak risk (mild)**: a developer who fat-fingers a real address in a dev seed (e.g. drops the `.invalid` suffix accidentally) gets no protection from the layer that *could* have caught it.
- **No prod-load impact**: real users have real emails; the bridge doesn't see `.invalid` in prod traffic.

This is a hygiene/correctness bug, not a security bug. Magic-link tokens are bound to specific approval_ids, so even if the SMTP relay open-relayed `.invalid` mail somewhere, the recipient couldn't do anything useful with the link.

## Fix

Two layers; pick one or both.

### A — Block reserved TLDs at the bridge (cheap, 100% local)

In `send_email`, before composing the `MIMEMultipart`, validate each recipient against the small set of RFC 6761 reserved TLDs:

```python
_RFC6761_RESERVED_TLDS = frozenset({"invalid", "test", "example", "localhost"})

def _is_undeliverable_recipient(addr: str) -> bool:
    if "@" not in addr:
        return True
    _, _, domain = addr.rpartition("@")
    if not domain or domain.startswith(".") or ".." in domain:
        return True
    tld = domain.rsplit(".", 1)[-1].lower()
    return tld in _RFC6761_RESERVED_TLDS

# In send_email():
bad = [r for r in recipients if _is_undeliverable_recipient(r)]
if bad:
    logger.warning(f"Refusing to send to undeliverable recipient(s): {bad}")
    return False
```

Caller already returns `False` → caller path in `send_permission_pending_email` already maps that to `delivery_status='failed'`, which is the truthful outcome.

Costs ~15 lines, no new dependency, exactly handles the observed case.

### B — Real RFC 5321 / 5322 validation

Use `email-validator` (which does syntax + DNS MX lookup). Heavier:

- Adds a dependency
- DNS lookup at send time → latency + an external-network failure mode that can keep us from sending **valid** mail
- Doesn't help with `.invalid` specifically (since `.invalid` *will* fail the MX check, but we already know it's bad from the TLD alone)

Recommend skipping B for now. A handles the symptom and is impossible to get wrong.

## Related code

- `orchestrator/services/email.py:84` — `send_email`, where the validation should live.
- `orchestrator/services/headless_notifications.py:411-416` — `skipped_smtp` outcome that we'd produce for a recipient blocked by A. (Strictly, this is a `failed` not a `skipped_smtp` — but the existing log line is close enough.)
- See also `headless_notifications_skipped_status_dedup.md` — once that's fixed, `delivery_status='failed'` will correctly suppress re-dispatch.

## Resolution (2026-05-13)

Shipped Option A in the same PR as the sweeper dedup fix ([[headless_notifications_skipped_status_dedup]]). The two changes compose: an `.invalid` recipient now writes `delivery_status='failed'`, which the sweeper's widened `IN (...)` set permanently suppresses on subsequent ticks. One small extension to the proposed predicate: empty local-part addresses (`@host.com`) also reject as malformed, since RFC 5321 requires a non-empty local part.
