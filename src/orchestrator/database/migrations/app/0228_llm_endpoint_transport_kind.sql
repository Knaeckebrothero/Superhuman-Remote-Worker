-- Subscription proxy: an explicit transport marker on llm_endpoints, plus a
-- behaviour-preserving routing backfill for the models already attached to the
-- seeded codex-proxy row.
--
-- Design: knowledge-base/knowledge/features/subscription_proxy.md §6, §8.
--
-- Before this, "is this row the CLIProxyAPI subscription proxy?" was answered
-- by the literal label `codex-proxy` or a `codex-proxy` substring in the
-- base_url. Both are cosmetic: renaming the label (which §8.3 asks us to do)
-- would have silently moved every attached model off the Responses factory and
-- stripped its reasoning summaries. `transport_kind` is the stable answer.
--
-- Transactional on purpose: the marker, the rename and the per-model routing
-- backfill are one change. Landing the rename without the marker is exactly the
-- regression described above, so they must not be separable.

ALTER TABLE public.llm_endpoints
    ADD COLUMN IF NOT EXISTS transport_kind text;

COMMENT ON COLUMN public.llm_endpoints.transport_kind IS
    'Stable transport marker, independent of label/base_url. '
    '''subscription-proxy'' = the shared CLIProxyAPI deployment fronting '
    'connected subscription accounts. NULL = an ordinary OpenAI-compatible '
    'endpoint.';

-- Mark the existing seeded row in place: same id, same attached catalog rows,
-- same credentials. Matched on either label so a stack that already carries the
-- new name (a fresh install seeded by a newer orchestrator) converges too.
UPDATE public.llm_endpoints
   SET transport_kind = 'subscription-proxy',
       updated_at = CURRENT_TIMESTAMP
 WHERE user_id IS NULL
   AND label IN ('codex-proxy', 'subscription-proxy')
   AND transport_kind IS DISTINCT FROM 'subscription-proxy';

-- Rename to the canonical label, but only when it cannot collide: an admin may
-- have hand-created their own row named `subscription-proxy`, and
-- uq_llm_endpoint_label_system would abort the whole migration. Leaving the
-- legacy label in place is safe now that transport_kind carries the identity.
UPDATE public.llm_endpoints AS e
   SET label = 'subscription-proxy',
       updated_at = CURRENT_TIMESTAMP
 WHERE e.user_id IS NULL
   AND e.label = 'codex-proxy'
   AND NOT EXISTS (
       SELECT 1
         FROM public.llm_endpoints other
        WHERE other.user_id IS NULL
          AND other.label = 'subscription-proxy'
   );

-- Behaviour-preserving routing backfill. Every catalog row attached to that
-- endpoint resolved onto the codex (OpenAI Responses) factory before this
-- change, because the endpoint identity alone decided it. Record that as the
-- row's explicit client protocol so the new resolver reaches the same answer.
--
-- Deliberately NOT backfilled: params_json.routing.subscription_sources. The
-- upstream account that serves each of these rows is not knowable from the
-- database, and stamping them all as `codex` would be the blanket tagging §8.2
-- forbids -- it would also hand a mixed-provider row the Codex-only context
-- clamp. Sources stay unknown until a rediscovery attributes them; an unknown
-- source on the codex factory keeps today's clamp, which is the status quo.
UPDATE public.models AS m
   SET params_json = jsonb_set(
           COALESCE(m.params_json, '{}'::jsonb),
           '{routing,client_protocol}',
           '"openai-responses"'::jsonb,
           true
       ),
       updated_at = CURRENT_TIMESTAMP
  FROM public.llm_endpoints AS e
 WHERE m.provider_kind = 'endpoint'
   AND m.provider_ref = e.id::text
   AND e.user_id IS NULL
   AND e.transport_kind = 'subscription-proxy'
   AND COALESCE(m.params_json, '{}'::jsonb) #> '{routing,client_protocol}'
       IS NULL;
