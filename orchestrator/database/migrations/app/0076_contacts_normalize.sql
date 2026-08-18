-- migration:     0076_contacts_normalize.sql
-- description:   Cross-channel contacts registry, Phase 1 of
--                docs/features/contacts_registry.md. Creates the normalized
--                contacts / contact_addresses / project_contacts tables and
--                backfills them from external_contacts. The legacy table is
--                deliberately left in place and untouched; a later migration
--                (next release) re-runs the idempotent backfill sweep
--                and drops it. Uniqueness is per OWNER, not global: two users
--                may each register anna@acme.de. Idempotency anchor is the
--                (owner_user_id, channel, address) key — contacts itself has
--                no natural unique key by design (duplicate names are legal).

CREATE TABLE IF NOT EXISTS contacts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    display_name TEXT NOT NULL,
    notes TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_contacts_owner ON contacts(owner_user_id);

CREATE TABLE IF NOT EXISTS contact_addresses (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    contact_id UUID NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    owner_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    channel TEXT NOT NULL CHECK (channel IN ('email', 'whatsapp')),
    address TEXT NOT NULL,
    is_primary BOOLEAN NOT NULL DEFAULT false,
    opt_in_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (opt_in_status IN ('pending', 'opted_in', 'opted_out')),
    last_inbound_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (owner_user_id, channel, address)
);
CREATE INDEX IF NOT EXISTS idx_contact_addresses_contact
    ON contact_addresses(contact_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_contact_primary_per_channel
    ON contact_addresses(contact_id, channel) WHERE is_primary;

CREATE TABLE IF NOT EXISTS project_contacts (
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    contact_id UUID NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    added_by UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (project_id, contact_id)
);
CREATE INDEX IF NOT EXISTS idx_project_contacts_contact
    ON project_contacts(contact_id);

-- Backfill. Row-by-row plpgsql (not set-based): the join-back from inserted
-- contacts to source rows is ambiguous when two same-named contacts have
-- different emails, and we want RAISE WARNING for unresolvable-owner skips.
-- Iterating created_at ASC + overwriting display_name each hit implements
-- "most recently created source row wins" for name conflicts.
DO $$
DECLARE
    r RECORD;
    v_owner UUID;
    v_contact UUID;
BEGIN
    FOR r IN SELECT * FROM external_contacts ORDER BY created_at ASC LOOP
        SELECT COALESCE(
            r.added_by,
            (SELECT pm.user_id FROM project_members pm
              WHERE pm.project_id = r.project_id AND pm.role = 'owner'
              ORDER BY pm.added_at ASC LIMIT 1)
        ) INTO v_owner;
        IF v_owner IS NULL THEN
            RAISE WARNING 'contacts backfill: skipping external_contact % (no resolvable owner)', r.id;
            CONTINUE;
        END IF;
        SELECT ca.contact_id INTO v_contact FROM contact_addresses ca
         WHERE ca.owner_user_id = v_owner
           AND ca.channel = 'email'
           AND ca.address = LOWER(r.email);
        IF v_contact IS NULL THEN
            INSERT INTO contacts (owner_user_id, display_name, created_at)
                 VALUES (v_owner, r.display_name, r.created_at)
              RETURNING id INTO v_contact;
            INSERT INTO contact_addresses
                   (contact_id, owner_user_id, channel, address, is_primary, opt_in_status)
            VALUES (v_contact, v_owner, 'email', LOWER(r.email), true, 'opted_in');
        ELSE
            UPDATE contacts SET display_name = r.display_name, updated_at = NOW()
             WHERE id = v_contact;
        END IF;
        INSERT INTO project_contacts (project_id, contact_id, added_by, created_at)
             VALUES (r.project_id, v_contact, r.added_by, r.created_at)
        ON CONFLICT DO NOTHING;
    END LOOP;
END $$;
