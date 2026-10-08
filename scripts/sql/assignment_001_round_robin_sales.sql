-- ============================================================================
-- Round robin = every active SALES profile of Ming Hwee's tenant.
--
-- The agency's rule (2026-10-08): every salesperson in `profiles` takes leads
-- and complaints in turn, and one salesperson handles one client's whole
-- conversation. The second half is code, not data - assignment._conversation_agent
-- gives every later ticket on a conversation to whoever has its first one; round
-- robin only picks for a conversation nobody owns yet.
--
-- A SYNC, not a one-off seed, and safe to re-run whenever the sales team
-- changes:
--   * an active sales profile with no row gets one (sorted after the others);
--   * a row whose profile is no longer an active sales profile is switched OFF,
--     never deleted - its total_assigned history stays;
--   * a row whose profile is an active salesperson again is switched back on.
-- An assault report still goes to the admin (ESCALATION_PROFILE_ID), and an
-- employer with a salesperson_profile_id still goes to that salesperson.
--
-- The tenant is read from the agency's own branch (code CT) rather than written
-- here, because this repository is public.
--
-- Before 2026-10-08 cb_round_robin_state was EMPTY, so cb_get_next_agent()
-- returned nothing and 7 of 8 chatbot tickets were created unassigned.
--
-- Apply:  python scripts/apply_sql.py --expect-ref <ref> scripts/sql/assignment_001_round_robin_sales.sql
-- ============================================================================

BEGIN;

WITH t AS (SELECT tenant_id FROM branches WHERE code = 'CT'),
sales AS (
    SELECT p.id, p.tenant_id, p.display_name
    FROM profiles p, t
    WHERE p.tenant_id = t.tenant_id
      AND p.archetype_key = 'sales'
      AND p.status = 'active'
)
INSERT INTO cb_round_robin_state (tenant_id, agent_profile_id, is_active, sort_order)
SELECT s.tenant_id, s.id, TRUE,
       COALESCE((SELECT max(sort_order) FROM cb_round_robin_state r, t
                 WHERE r.tenant_id = t.tenant_id), 0)
       + row_number() OVER (ORDER BY s.display_name, s.id)
FROM sales s
WHERE NOT EXISTS (
    SELECT 1 FROM cb_round_robin_state r
    WHERE r.tenant_id = s.tenant_id AND r.agent_profile_id = s.id
);

UPDATE cb_round_robin_state r
SET is_active = (p.archetype_key = 'sales' AND p.status = 'active'),
    updated_at = now()
FROM profiles p
WHERE p.id = r.agent_profile_id
  AND r.tenant_id = (SELECT tenant_id FROM branches WHERE code = 'CT')
  AND r.is_active IS DISTINCT FROM (p.archetype_key = 'sales' AND p.status = 'active');

-- Refuse to commit an empty rotation: that is the state this file exists to end.
DO $$
DECLARE n int;
BEGIN
    SELECT count(*) INTO n FROM cb_round_robin_state
    WHERE is_active AND tenant_id = (SELECT tenant_id FROM branches WHERE code = 'CT');
    IF n = 0 THEN
        RAISE EXCEPTION 'round robin would be empty - no active sales profile in the tenant';
    END IF;
    RAISE NOTICE 'round robin: % active salesperson(s)', n;
END $$;

COMMIT;

-- Check (read-only):
--   SELECT p.display_name, r.is_active, r.sort_order, r.total_assigned, r.last_assigned_at
--   FROM cb_round_robin_state r JOIN profiles p ON p.id = r.agent_profile_id
--   ORDER BY r.sort_order;
