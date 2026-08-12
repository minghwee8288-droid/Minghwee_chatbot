-- cb_tickets: description + multi-service tracking
--
-- The chatbot files one ticket per topic and folds later messages on the same
-- topic into it (app/services/ticket.py: merge_into / append_description /
-- add_service_type). That needs two things the original portal schema does not
-- have:
--
--   description   a running, dated log of why the ticket exists and what has
--                 been added to it since. Appended to, never overwritten.
--   service_type  an ARRAY, so a client who asks about fees while a new_hiring
--                 ticket is open lands on that ticket as a second service
--                 instead of spawning a duplicate.
--
-- cb_tickets is chatbot-only today (no portal ticket UI exists yet), so
-- changing its shape here is safe; the portal build can read the array.
--
-- Idempotent: safe to re-run. Apply with
--   .venv/Scripts/python.exe scripts/apply_ticket_multiservice.py
-- or paste into the Supabase SQL editor.

begin;

-- 1. description ------------------------------------------------------------
alter table public.cb_tickets
    add column if not exists description text not null default '';

-- 2. service_type -> text[] -------------------------------------------------
-- The old CHECK is a scalar = ANY(...) test and cannot survive the type
-- change, so it goes first and is rebuilt as a containment test below.
alter table public.cb_tickets drop constraint if exists cb_tkt_service_check;

do $$
begin
    if (select data_type
          from information_schema.columns
         where table_schema = 'public'
           and table_name = 'cb_tickets'
           and column_name = 'service_type') <> 'ARRAY' then
        alter table public.cb_tickets
            alter column service_type type text[]
            using case
                when service_type is null then array[]::text[]
                else array[service_type]
            end;
    end if;
end $$;

-- cardinality(), not array_length(): array_length('{}', 1) is NULL, and a
-- CHECK that evaluates to NULL passes — an empty array would sail straight
-- through `array_length(service_type, 1) > 0`.
alter table public.cb_tickets
    add constraint cb_tkt_service_check check (
        service_type <@ array[
            'new_hiring', 'direct_hiring', 'replacement', 'transfer', 'renewal',
            'home_leave', 'passport_renewal', 'dispute_salary', 'dispute_assault',
            'fee_enquiry', 'salary_enquiry'
        ]::text[]
        and cardinality(service_type) > 0
    );

commit;
