-- Seed cb_kb_rules with EXACTLY the values the code uses today (2026-09-24),
-- so switching RULES_FROM_DB on changes nothing. The source of each block is
-- named; kb_rules.DEFAULTS mirrors this file and selfcheck_kb_prep.py asserts
-- the two are identical before the switch may go on.
--
-- Idempotent: ON CONFLICT DO NOTHING, so a re-run never overwrites a value
-- somebody has since changed from the UI.
--
-- Run with:  python scripts/apply_sql.py --expect-ref <project ref> <this file>

begin;

insert into public.cb_kb_rules (rule_type, service_type, nationality, value, locked)
values
    -- guards.FEE_STATED_SERVICES
    ('price_policy', 'renewal',           'all', '"stated"',   false),
    ('price_policy', 'passport_renewal',  'all', '"stated"',   false),
    ('price_policy', 'home_leave',        'all', '"stated"',   false),
    ('price_policy', 'new_hiring',        'all', '"stated"',   false),
    ('price_policy', 'transfer',          'all', '"stated"',   false),
    ('price_policy', 'transfer_employer', 'all', '"stated"',   false),
    -- guards.COST_WITHHELD_SERVICES (fee_enquiry is locked: see 001)
    ('price_policy', 'direct_hiring',     'all', '"withheld"', false),
    ('price_policy', 'fee_enquiry',       'all', '"withheld"', true),
    ('price_policy', 'replacement',       'all', '"withheld"', false),

    -- ticket.FEE_BY_NATIONALITY
    ('price_nationality', 'passport_renewal',  'all', '["PH", "ID"]',       false),
    ('price_nationality', 'home_leave',        'all', '["PH", "ID"]',       false),
    ('price_nationality', 'new_hiring',        'all', '["PH", "ID", "MM"]', false),
    ('price_nationality', 'transfer',          'all', '["PH", "ID", "MM"]', false),
    ('price_nationality', 'transfer_employer', 'all', '["PH", "ID", "MM"]', false),

    -- info_collector._SALARY_FLOOR_BY_NATIONALITY
    ('salary_floor', 'all', 'PH', '650', false),

    -- The agency's WhatsApp number (2026-09-11) and its one office (2026-09-16).
    -- No bot code reads these; the knowledge-base rows carry them, and
    -- selfcheck_kb_prep.py checks the rows agree with these values.
    ('contact', 'whatsapp_number', 'all', '"+65 6534 2277"', false),
    ('contact', 'office_address',  'all',
        '"101 Upper Cross Street, #03-54, People''s Park Centre, Singapore 058357"', false)
on conflict (rule_type, service_type, nationality) do nothing;

commit;
