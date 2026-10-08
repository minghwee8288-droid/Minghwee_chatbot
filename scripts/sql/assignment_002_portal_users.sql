-- ============================================================================
-- cb_agent_portal_users: which WhatsApp-portal login (wp_chat_users.id) each
-- salesperson's platform profile is.
--
-- The bot assigns tickets to a PROFILE; the portal inbox shows an owner by
-- PORTAL USER (wp_chat_conversations.assigned_user_id). wp_chat_users.profile_id
-- was built for this link and is empty on every row, and that table is the
-- portal's - so the chatbot keeps its own copy of the link here instead of
-- writing to theirs (agreed 2026-10-08). assignment.portal_user_of() reads this
-- table first and falls back to wp_chat_users.profile_id, so if the portal team
-- ever fills that column themselves nothing here conflicts.
--
-- Only references their table; never alters it. ON DELETE CASCADE means a
-- portal user or profile being deleted removes OUR row and never blocks theirs.
-- RLS on with no policies: invisible to the portal's anon/authenticated keys;
-- the bot uses the service role.
--
-- A SYNC, safe to re-run (run it after assignment_001 whenever the sales team
-- changes): every active sales profile of the agency's tenant is linked to the
-- portal user with the SAME email in the same tenant. A salesperson with no
-- matching portal login is reported and left unlinked - the ticket still gets
-- them, the inbox just cannot show it. Admins and managers are deliberately not
-- linked, so an assault ticket never takes a chat over in the inbox.
--
-- Apply:  python scripts/apply_sql.py --expect-ref <ref> scripts/sql/assignment_002_portal_users.sql
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS cb_agent_portal_users (
    profile_id      uuid PRIMARY KEY REFERENCES profiles(id) ON DELETE CASCADE,
    portal_user_id  bigint NOT NULL UNIQUE REFERENCES wp_chat_users(id) ON DELETE CASCADE,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE cb_agent_portal_users ENABLE ROW LEVEL SECURITY;

WITH t AS (SELECT tenant_id FROM branches WHERE code = 'CT'),
pairs AS (
    SELECT p.id AS profile_id, u.id AS portal_user_id
    FROM profiles p
    JOIN t ON p.tenant_id = t.tenant_id
    JOIN wp_chat_users u ON u.tenant_id = t.tenant_id
                        AND lower(u.email) = lower(p.email)
                        AND u.is_active
    WHERE p.archetype_key = 'sales' AND p.status = 'active'
)
INSERT INTO cb_agent_portal_users (profile_id, portal_user_id)
SELECT profile_id, portal_user_id FROM pairs
ON CONFLICT (profile_id) DO UPDATE
    SET portal_user_id = EXCLUDED.portal_user_id, updated_at = now()
    WHERE cb_agent_portal_users.portal_user_id IS DISTINCT FROM EXCLUDED.portal_user_id;

DO $$
DECLARE r record; n int := 0;
BEGIN
    FOR r IN
        SELECT p.display_name, m.portal_user_id
        FROM profiles p
        LEFT JOIN cb_agent_portal_users m ON m.profile_id = p.id
        WHERE p.tenant_id = (SELECT tenant_id FROM branches WHERE code = 'CT')
          AND p.archetype_key = 'sales' AND p.status = 'active'
        ORDER BY p.display_name
    LOOP
        IF r.portal_user_id IS NULL THEN
            RAISE NOTICE 'NOT linked: % has no active portal login with the same email', r.display_name;
        ELSE
            n := n + 1;
            RAISE NOTICE 'linked: % -> portal user %', r.display_name, r.portal_user_id;
        END IF;
    END LOOP;
    RAISE NOTICE '% salesperson(s) linked', n;
END $$;

COMMIT;
