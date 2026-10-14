-- ticket_number_001_counter.sql - a ticket reference is never issued twice.
--
-- Phase 3 Step 5 Batch B2, item 1 (2026-10-14). CB-2026-0033 was issued three
-- times in one day - a work permit renewal, a home leave and a passport
-- renewal - on test threads cleared with reset-ui between them.
-- ticket.next_ticket_number read the highest number still ON cb_tickets and
-- added one, so deleting a ticket (reset-ui, reset_conversation.py) handed its
-- number out again. A reference a client has been told to quote must never
-- come back as somebody else's.
--
-- The counter below only ever goes up. Deleting tickets does not move it, and
-- two tickets created at the same moment take two numbers: the UPSERT locks the
-- year's row, so the second caller waits and gets the next one.
--
-- Additive and cb_-only: one new table, one new function. cb_tickets is not
-- altered (its unique index cb_tkt_tenant_number_uidx stays as the backstop).
-- The bot calls cb_next_ticket_number when ticket.TICKET_NUMBER_COUNTER is on
-- and falls back to the old read-max-and-add-one if the function is missing,
-- so the code can be deployed before or after this file is applied.
--
-- Apply: python scripts/apply_sql.py --expect-ref <project ref> scripts/sql/ticket_number_001_counter.sql
-- Rollback: scripts/sql/rollback/rollback_ticket_number_001_counter.sql (never without a go-word).

begin;

create table if not exists public.cb_ticket_counters (
    year        integer primary key check (year between 2000 and 2999),
    last_number integer not null check (last_number >= 0),
    updated_at  timestamptz not null default now()
);

comment on table public.cb_ticket_counters is
    'Last CB-YYYY-NNNN issued per year. Only ever increases; deleting tickets never moves it (Step 5 B2 item 1).';

-- Nothing but the service role (which bypasses RLS) and the function below
-- may touch it. RLS on with no policies, the same as cb_kb_rules.
alter table public.cb_ticket_counters enable row level security;
revoke all on public.cb_ticket_counters from public;
revoke all on public.cb_ticket_counters from anon, authenticated;

-- Seed 2026 above everything that may already have been issued. The highest
-- surviving number is readable; numbers issued and then deleted are not, and
-- CB-2026-0033 had been issued and deleted three times when this was written.
-- So 2026 starts after 0099 at the earliest: a skipped block of numbers costs
-- nothing, a reused one is the defect. Re-running never lowers it.
insert into public.cb_ticket_counters (year, last_number)
select 2026, greatest(coalesce(max(split_part(ticket_number, '-', 3)::integer), 0), 99)
from public.cb_tickets
where ticket_number ~ '^CB-2026-[0-9]+$'
on conflict (year) do update
    set last_number = greatest(public.cb_ticket_counters.last_number, excluded.last_number),
        updated_at  = now();

-- The next reference for p_year. A year with no row yet starts after the
-- highest ticket already on file for it (a fallback number issued before this
-- function existed is never re-issued), else at 1.
create or replace function public.cb_next_ticket_number(p_year integer)
returns text
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    n integer;
begin
    if p_year is null or p_year < 2000 or p_year > 2999 then
        raise exception 'cb_next_ticket_number: bad year %', p_year;
    end if;
    insert into public.cb_ticket_counters as c (year, last_number)
    values (
        p_year,
        coalesce((
            select max(split_part(t.ticket_number, '-', 3)::integer)
            from public.cb_tickets t
            where t.ticket_number ~ ('^CB-' || p_year || '-[0-9]+$')
        ), 0) + 1
    )
    on conflict (year) do update
        set last_number = c.last_number + 1,
            updated_at  = now()
    returning c.last_number into n;
    -- lpad would TRUNCATE a fifth digit ('10000' -> '1000'), which is a reuse.
    return 'CB-' || p_year || '-' || case when n < 10000 then lpad(n::text, 4, '0') else n::text end;
end
$$;

revoke all on function public.cb_next_ticket_number(integer) from public;
revoke all on function public.cb_next_ticket_number(integer) from anon, authenticated;
grant execute on function public.cb_next_ticket_number(integer) to service_role;

-- Checks: the function exists, the seed is in place and above every
-- surviving 2026 ticket.
do $$
declare
    seeded integer;
    surviving integer;
begin
    select last_number into seeded from public.cb_ticket_counters where year = 2026;
    select coalesce(max(split_part(ticket_number, '-', 3)::integer), 0) into surviving
    from public.cb_tickets where ticket_number ~ '^CB-2026-[0-9]+$';
    if seeded is null or seeded < surviving or seeded < 99 then
        raise exception 'ticket counter seed check failed (seeded %, surviving %)', seeded, surviving;
    end if;
    if not exists (select 1 from pg_proc where proname = 'cb_next_ticket_number') then
        raise exception 'cb_next_ticket_number missing';
    end if;
    raise notice 'ticket_number_001: OK - 2026 counter at %, next is %', seeded, seeded + 1;
end
$$;

commit;
