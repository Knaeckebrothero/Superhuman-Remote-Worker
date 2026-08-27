-- migration:     0194_notifications_cutover.sql
-- description:   Slice 3 of the unified notification system (knowledge-base/
--                knowledge/features/unified_notification_system.md §6): the
--                cockpit now reads ONLY the feed, so the items the legacy
--                joins used to derive on read are backfilled as feed rows —
--                jobs awaiting review, pending sudo / VM-upgrade requests, and
--                recent unanswered agent messages. The two retired stores are
--                commented as such; they are NOT dropped here (a later
--                migration, once the rollout has proven out).
-- depends-on:    0193_notification_steps.sql
-- expected:      < 5s. Three bounded INSERT … SELECT statements (open items
--                only); no existing table is altered.
-- locks:         none beyond row inserts into notifications.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

-- Ids are minted exactly as the orchestrator mints them
-- (services/notification_catalog.py::notification_id):
--   uuid5(NAMESPACE_URL, 'srw-notification-v1:{recipient_kind}:{recipient_id}:{dedup_key}')
-- so a later record() for the same item lands on the same row. The backfill
-- rows carry no deferred steps (they are old; nothing should escalate) and
-- use their own 'backfill:' dedup prefix so they never collide with a live
-- producer's key.

-- 1. Jobs waiting on a human decision → review_queue.
INSERT INTO public.notifications (
    id, recipient_kind, recipient_id, category, severity, subject, body,
    source_kind, source_id, dedup_key, actions, payload, created_at
)
SELECT
    uuid_generate_v5(
        uuid_ns_url(),
        'srw-notification-v1:user:' || j.user_id::text || ':backfill:job:' || j.id::text
    ),
    'user',
    j.user_id,
    'review_queue',
    'normal',
    'Job ' || left(j.id::text, 8) || ' completed — review required',
    COALESCE(left(j.description, 500), ''),
    'job',
    j.id::text,
    'backfill:job:' || j.id::text,
    jsonb_build_array(
        jsonb_build_object('type', 'approve', 'label_key', 'notifications.actions.approve',
                           'style', 'primary', 'input', NULL, 'input_name', NULL,
                           'params', jsonb_build_object('job_id', j.id::text)),
        jsonb_build_object('type', 'resume', 'label_key', 'notifications.actions.resumeWithFeedback',
                           'style', 'default', 'input', 'textarea', 'input_name', 'feedback',
                           'params', jsonb_build_object('job_id', j.id::text)),
        jsonb_build_object('type', 'open', 'label_key', 'notifications.actions.openJob',
                           'style', 'default', 'input', NULL, 'input_name', NULL,
                           'params', jsonb_build_object('job_id', j.id::text))
    ),
    jsonb_build_object(
        'job_id', j.id::text,
        'config_name', j.config_name,
        'job_description', COALESCE(left(j.description, 100), ''),
        'backfill', true
    ),
    COALESCE(j.updated_at, j.created_at, now())
FROM public.jobs j
WHERE j.status IN ('pending_review', 'reviewing')
  AND j.user_id IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM public.notifications n
      WHERE n.source_kind = 'job' AND n.source_id = j.id::text AND n.resolved_at IS NULL
  )
ON CONFLICT (id) DO NOTHING;

-- 2. Pending sudo / VM-upgrade requests → sudo_request / vm_upgrade. The
--    recipient is the job owner, else the thread owner.
INSERT INTO public.notifications (
    id, recipient_kind, recipient_id, category, severity, subject, body,
    source_kind, source_id, dedup_key, actions, payload, created_at
)
SELECT
    uuid_generate_v5(
        uuid_ns_url(),
        'srw-notification-v1:user:' || owner.user_id::text || ':backfill:sudo:' || r.id::text
    ),
    'user',
    owner.user_id,
    CASE WHEN r.request_type = 'vm_upgrade' THEN 'vm_upgrade' ELSE 'sudo_request' END,
    CASE WHEN r.request_type = 'vm_upgrade' THEN 'high' ELSE 'critical' END,
    CASE WHEN r.request_type = 'vm_upgrade'
         THEN 'Job ' || left(COALESCE(r.job_id::text, ''), 8) || ' needs a VM'
         ELSE 'Sudo approval needed: ' || left(r.command, 60) END,
    COALESCE(r.command, ''),
    'sudo_request',
    r.id::text,
    'backfill:sudo:' || r.id::text,
    CASE WHEN r.request_type = 'vm_upgrade' THEN jsonb_build_array(
        jsonb_build_object('type', 'approve_upgrade', 'label_key', 'notifications.actions.upgradeToVm',
                           'style', 'primary', 'input', NULL, 'input_name', NULL,
                           'params', jsonb_build_object('request_id', r.id::text, 'job_id', r.job_id::text)),
        jsonb_build_object('type', 'resume_without_vm', 'label_key', 'notifications.actions.resumeWithoutVm',
                           'style', 'default', 'input', NULL, 'input_name', NULL,
                           'params', jsonb_build_object('request_id', r.id::text, 'job_id', r.job_id::text)),
        jsonb_build_object('type', 'deny', 'label_key', 'notifications.actions.deny',
                           'style', 'danger', 'input', 'text', 'input_name', 'reason',
                           'params', jsonb_build_object('request_id', r.id::text, 'job_id', r.job_id::text))
    ) ELSE jsonb_build_array(
        jsonb_build_object('type', 'approve', 'label_key', 'notifications.actions.approve',
                           'style', 'primary', 'input', NULL, 'input_name', NULL,
                           'params', jsonb_build_object('request_id', r.id::text, 'job_id', r.job_id::text, 'thread_id', r.thread_id::text)),
        jsonb_build_object('type', 'deny', 'label_key', 'notifications.actions.deny',
                           'style', 'danger', 'input', 'text', 'input_name', 'reason',
                           'params', jsonb_build_object('request_id', r.id::text, 'job_id', r.job_id::text, 'thread_id', r.thread_id::text)),
        jsonb_build_object('type', 'open', 'label_key', 'notifications.actions.open',
                           'style', 'default', 'input', NULL, 'input_name', NULL,
                           'params', jsonb_build_object('request_id', r.id::text, 'job_id', r.job_id::text, 'thread_id', r.thread_id::text))
    ) END,
    jsonb_build_object(
        'request_id', r.id::text,
        'job_id', r.job_id::text,
        'thread_id', r.thread_id::text,
        'command', r.command,
        'vm_name', r.vm_name,
        'request_type', r.request_type,
        'backfill', true
    ),
    COALESCE(r.requested_at, now())
FROM public.sudo_approval_requests r
JOIN LATERAL (
    SELECT j.user_id FROM public.jobs j WHERE j.id = r.job_id
    UNION ALL
    SELECT t.user_id FROM public.threads t WHERE t.id = r.thread_id
    LIMIT 1
) owner ON owner.user_id IS NOT NULL
WHERE r.status = 'pending'
  AND NOT EXISTS (
      SELECT 1 FROM public.notifications n
      WHERE n.source_kind = 'sudo_request' AND n.source_id = r.id::text AND n.resolved_at IS NULL
  )
ON CONFLICT (id) DO NOTHING;

-- 3. Recent outbound agent messages whose thread has no later human reply →
--    agent_message (30-day window; the legacy inbox listed these on read).
INSERT INTO public.notifications (
    id, recipient_kind, recipient_id, category, severity, subject, body,
    source_kind, source_id, dedup_key, actions, payload, created_at
)
SELECT
    uuid_generate_v5(
        uuid_ns_url(),
        'srw-notification-v1:user:' || m.user_id::text || ':backfill:message:' || m.id::text
    ),
    'user',
    m.user_id,
    'agent_message',
    'normal',
    COALESCE(m.subject, 'Message from your agent'),
    COALESCE(left(m.message, 2000), ''),
    'message_thread',
    m.thread_id,
    'backfill:message:' || m.id::text,
    jsonb_build_array(
        jsonb_build_object('type', 'reply', 'label_key', 'notifications.actions.reply',
                           'style', 'primary', 'input', 'textarea', 'input_name', 'message',
                           'params', jsonb_build_object('job_id', m.job_id::text, 'thread_id', m.thread_id)),
        jsonb_build_object('type', 'open', 'label_key', 'notifications.actions.openJob',
                           'style', 'default', 'input', NULL, 'input_name', NULL,
                           'params', jsonb_build_object('job_id', m.job_id::text, 'thread_id', m.thread_id))
    ),
    jsonb_build_object(
        'job_id', m.job_id::text,
        'thread_id', m.thread_id,
        'message_log_id', m.id::text,
        'backfill', true
    ),
    m.created_at
FROM public.message_log m
WHERE m.direction = 'outbound'
  AND m.user_id IS NOT NULL
  AND m.job_id IS NOT NULL        -- a job-less thread (officer/session notices) has no reply path
  AND m.thread_id IS NOT NULL
  AND m.created_at > now() - interval '30 days'
  AND m.created_at = (
      SELECT max(x.created_at) FROM public.message_log x WHERE x.thread_id = m.thread_id
  )
  AND NOT EXISTS (
      SELECT 1 FROM public.notifications n
      WHERE n.source_kind = 'message_thread' AND n.source_id = m.thread_id
  )
ON CONFLICT (id) DO NOTHING;

COMMENT ON TABLE public.notification_queue IS
    'RETIRED (0193, unified notification system slice 3): the quiet-hours digest queue. '
    'Deferred delivery is a notification_steps row now. Kept until a later DROP.';
COMMENT ON TABLE public.thread_notifications IS
    'RETIRED (0193, unified notification system slice 3): the headless permission-email '
    'audit table. Deliveries are notification_deliveries rows now. Kept until a later DROP.';

COMMIT;
