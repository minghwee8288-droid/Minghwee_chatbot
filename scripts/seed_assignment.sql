-- ============================================================================
-- Seeding the chatbot needs before it can hand a conversation to a human.
-- Run in the Supabase SQL editor AFTER confirming the names marked CONFIRM.
-- Re-run safe: every statement is idempotent.
--
-- Verify the result with:  python scripts/preflight.py
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1. Round-robin rotation  (cb_round_robin_state is currently EMPTY, so
--    cb_get_next_agent() returns nothing and no lead can be assigned)
--
-- CONFIRM: which consultants should receive new leads?
-- The tenant has 20 profiles with archetype_key = 'sales', but 18 of them are
-- @growwstacks.com development accounts. Seeding those would route real client
-- leads to the dev team, so only the two @minghwee.sg consultants are listed
-- below. Add or remove rows before running.
--
--   dddddd02-0000-0000-0000-000000000002  Grace Lim    grace@minghwee.sg
--   dddddd03-0000-0000-0000-000000000003  Winston Teo  winston@minghwee.sg
-- ----------------------------------------------------------------------------

INSERT INTO cb_round_robin_state (tenant_id, agent_profile_id, is_active, sort_order)
VALUES
    ('11111111-1111-1111-1111-111111111111', 'dddddd02-0000-0000-0000-000000000002', TRUE, 1),
    ('11111111-1111-1111-1111-111111111111', 'dddddd03-0000-0000-0000-000000000003', TRUE, 2)
ON CONFLICT DO NOTHING;

-- Sanity check — should return one of the agents above:
-- SELECT cb_get_next_agent('11111111-1111-1111-1111-111111111111');


-- ----------------------------------------------------------------------------
-- 2. Portal bridge  (all 6 wp_chat_users rows have profile_id IS NULL, so the
--    portal cannot show who a conversation was assigned to)
--
-- CONFIRM: the two sides do not line up by email — the portal uses
-- @minghwee.com and the platform uses @minghwee.sg — and only "Shirley"
-- appears on both. Fill in the mapping below.
--
--   portal wp_chat_users            platform profiles
--   -------------------------------  --------------------------------------
--   1  Admin        (admin)          ?
--   2  Thomas Phua  (admin)          ? no profile with this name exists
--   3  Geraldine    (agent, sales)   ? no profile with this name exists
--   5  Shirley      (agent, sales)   Shirley Ong  dddddd04-...004  (archetype 'admin')
--   6  gurdeep      (agent, ops)     ?
--   7  Raghav       (agent)          ?
--
-- Note on row 5: the portal lists Shirley in the sales department but her
-- platform profile is archetype_key = 'admin'. Admin is the assault-escalation
-- target, so if she should also take normal sales leads she needs to be in the
-- rotation above as well.
-- ----------------------------------------------------------------------------

-- UPDATE wp_chat_users SET profile_id = 'dddddd04-0000-0000-0000-000000000004' WHERE id = 5;  -- Shirley
-- UPDATE wp_chat_users SET profile_id = '<profile uuid>' WHERE id = 2;  -- Thomas Phua
-- UPDATE wp_chat_users SET profile_id = '<profile uuid>' WHERE id = 3;  -- Geraldine
-- UPDATE wp_chat_users SET profile_id = '<profile uuid>' WHERE id = 6;  -- gurdeep
-- UPDATE wp_chat_users SET profile_id = '<profile uuid>' WHERE id = 7;  -- Raghav

-- Anyone in the round robin MUST have a wp_chat_users row, or the assignment
-- succeeds in cb_tickets but never appears in the portal. Check with:
--   SELECT r.agent_profile_id, p.display_name, u.id AS portal_user_id
--   FROM cb_round_robin_state r
--   JOIN profiles p ON p.id = r.agent_profile_id
--   LEFT JOIN wp_chat_users u ON u.profile_id = r.agent_profile_id
--   WHERE r.is_active;


-- ----------------------------------------------------------------------------
-- 3. Style config — NOTHING TO SEED
--
-- cb_style_config is no longer read. The voice guide (Part B of the system
-- prompt) lives in app/graph/prompts/style.py, because its sections and their
-- ordering are part of the prompt and do not survive being stored as a table of
-- one-line settings. Edit that file to change how the bot writes.
--
-- The table can be left as it is; the application never queries it.
-- ----------------------------------------------------------------------------
