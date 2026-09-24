-- cb_kb_rules: the pricing and contact rules that used to live as Python
-- constants, so the KB Admin UI can change them without a deploy.
--
-- One table, not four: ~17 rows, one reader (app/services/kb_rules.py), and the
-- per-type differences are CHECK constraints rather than separate schemas.
-- Deliberately NOT inside cb_knowledge_base_updated: a rule row there would be
-- retrievable if it were ever embedded, and `is_active` would mean two things.
--
-- Nothing reads this table until RULES_FROM_DB=true. With it off the bot uses
-- the code defaults in kb_rules.py, which this seed mirrors exactly - and
-- selfcheck_kb_prep.py fails if the two ever drift apart.
--
-- Run with:  python scripts/apply_sql.py --expect-ref <project ref> <this file>

begin;

create table if not exists public.cb_kb_rules (
    id          bigint generated always as identity primary key,
    rule_type   text        not null,
    -- For the two contact rows this names WHICH contact detail it is
    -- ('whatsapp_number' / 'office_address'), keeping the column list fixed.
    service_type text       not null default 'all',
    nationality text        not null default 'all',
    value       jsonb       not null,
    -- Developer-only. The UI shows a locked rule and does not let it be edited.
    locked      boolean     not null default false,
    updated_by  text        not null default 'seed',
    updated_at  timestamptz not null default now(),

    constraint cb_kb_rules_key unique (rule_type, service_type, nationality),

    constraint cb_kb_rules_type_check check (
        rule_type in ('price_policy', 'price_nationality', 'salary_floor', 'contact')
    ),

    -- May this service quote its fee ('stated') or must it defer to an agent
    -- ('withheld')? insurance and candidate_new_hiring are in neither list
    -- today and must stay out: a row for either would put them in one.
    constraint cb_kb_rules_price_policy_check check (
        rule_type <> 'price_policy' or (
            nationality = 'all'
            and value in ('"stated"'::jsonb, '"withheld"'::jsonb)
            and service_type in (
                'renewal', 'passport_renewal', 'home_leave', 'new_hiring',
                'transfer', 'transfer_employer', 'direct_hiring', 'fee_enquiry',
                'replacement'
            )
        )
    ),

    -- fee_enquiry is what a bare "how much do you charge?" resolves to, and
    -- the agency's rule 12 is that it is answered only once the service and
    -- nationality are known. Withheld, always, and not editable.
    constraint cb_kb_rules_fee_enquiry_check check (
        not (rule_type = 'price_policy' and service_type = 'fee_enquiry')
        or (value = '"withheld"'::jsonb and locked)
    ),

    -- Which nationalities we HOLD a fee for, on a service priced per
    -- nationality. A list, possibly empty; only the three we place.
    constraint cb_kb_rules_price_nationality_check check (
        rule_type <> 'price_nationality' or (
            nationality = 'all'
            and jsonb_typeof(value) = 'array'
            and value <@ '["PH", "ID", "MM"]'::jsonb
        )
    ),

    -- Minimum monthly salary (SGD) a helper of this nationality can be placed
    -- at. Bounded so a typo cannot offer a S$6,500 or a S$65 floor.
    constraint cb_kb_rules_salary_floor_check check (
        rule_type <> 'salary_floor' or (
            service_type = 'all'
            and nationality in ('PH', 'ID', 'MM')
            and jsonb_typeof(value) = 'number'
            and (value #>> '{}')::numeric between 300 and 2000
        )
    ),

    constraint cb_kb_rules_contact_check check (
        rule_type <> 'contact' or (
            nationality = 'all'
            and service_type in ('whatsapp_number', 'office_address')
            and jsonb_typeof(value) = 'string'
            and length(value #>> '{}') between 5 and 300
        )
    )
);

comment on table public.cb_kb_rules is
    'Chatbot pricing/contact rules editable from the KB Admin UI. Read by '
    'app/services/kb_rules.py only when RULES_FROM_DB=true.';

-- The service-role key bypasses RLS; the portal's anon/authenticated key must
-- not be able to read or change pricing rules at all, so: RLS on, no policies.
alter table public.cb_kb_rules enable row level security;

-- transfer and transfer_employer are ONE switch. The employer's key is what
-- _subject_service returns and the candidate's is what _aliased() rewrites it
-- to, so if they disagree a transfer fee question is stated on one path and
-- withheld on the other. Checked at COMMIT (deferred), so an update that sets
-- both in one transaction passes and one that sets only one of them fails.
create or replace function public.cb_kb_rules_transfer_pair_check()
returns trigger
language plpgsql
as $$
declare
    rule text;
    a jsonb;
    b jsonb;
begin
    foreach rule in array array['price_policy', 'price_nationality'] loop
        select value into a from public.cb_kb_rules
         where rule_type = rule and service_type = 'transfer' and nationality = 'all';
        select value into b from public.cb_kb_rules
         where rule_type = rule and service_type = 'transfer_employer' and nationality = 'all';
        if a is distinct from b then
            raise exception
                'cb_kb_rules: % for transfer (%) and transfer_employer (%) must be equal',
                rule, a, b;
        end if;
    end loop;
    return null;
end;
$$;

drop trigger if exists cb_kb_rules_transfer_pair on public.cb_kb_rules;
create constraint trigger cb_kb_rules_transfer_pair
    after insert or update or delete on public.cb_kb_rules
    deferrable initially deferred
    for each row execute function public.cb_kb_rules_transfer_pair_check();

commit;
