-- kb_admin_018_fee_lock_sentences.sql (2026-10-16) - the KB watcher's salary/fee lock, per CHANGED sentence.
--
-- 015's kb_admin__watch_fee_lock locked a draft as "touches a helper salary figure" when a money
-- figure and the word "salary" appeared ANYWHERE in the old or new entry - so a rest-day line added
-- to an entry that already said "Her salary is S$650." could never be approved. Now it locks only
-- when a sentence the change ADDS, REPLACES or REMOVES itself carries a money figure and salary or
-- fee wording (a government fee named as such - MOM / work permit / levy fee - is not fee wording).
-- An unchanged sentence never triggers it.
--
-- Everything else is unchanged and still decided by 015's function, renamed (not copied, so the
-- generated fees-document arrays stay in one place): a fees-document row (by question or by a
-- correction in its text), any agency-fee wording anywhere, and a money figure from a page that is
-- not a government rule page. The kb-admin twin is lib/watcher/feeLock.ts (SENTENCE_SCOPED_SALARY_LOCK).
--
-- Every caller (propose, propose_checked, approve) calls kb_admin__watch_fee_lock by name, so they
-- all get the new rule. Re-runnable. Rollback: rollback/rollback_kb_admin_018_fee_lock_sentences.sql.
-- Apply ONLY via: python scripts/apply_sql.py --expect-ref <ref> scripts/sql/kb_admin_018_fee_lock_sentences.sql

begin;

do $$
begin
    if to_regprocedure('public.kb_admin__watch_fee_lock(text,text,text,text,text)') is null then
        raise exception 'FAIL: apply kb_admin_015_watcher before 018';
    end if;
    -- First run only: keep 015's function under its own name.
    if to_regprocedure('public.kb_admin__watch_fee_lock_015(text,text,text,text,text)') is null then
        grant create on schema public to kb_admin_fn_owner;
        alter function public.kb_admin__watch_fee_lock(text, text, text, text, text) rename to kb_admin__watch_fee_lock_015;
        revoke create on schema public from kb_admin_fn_owner;
    end if;
end
$$;

-- The sentences on one side and not the other (whitespace collapsed); a new entry is all of its sentences.
create or replace function public.kb_admin__watch_changed_sentences(
    p_target_question text, p_target_answer text, p_new_question text, p_new_answer text)
returns setof text
language sql immutable
set search_path = pg_catalog, public, pg_temp
as $$
    with b as (
        select btrim(regexp_replace(x, '\s+', ' ', 'g')) as s
          from regexp_split_to_table(concat_ws(E'\n', p_target_question, p_target_answer), '(?<=[.!?])\s+|\n+') x),
    a as (
        select btrim(regexp_replace(x, '\s+', ' ', 'g')) as s
          from regexp_split_to_table(concat_ws(E'\n', p_new_question, p_new_answer), '(?<=[.!?])\s+|\n+') x)
    select s from a where s <> '' and s not in (select s from b)
    union all
    select s from b where s <> '' and s not in (select s from a);
$$;

create or replace function public.kb_admin__watch_fee_lock(
    p_target_question text, p_target_answer text, p_new_question text, p_new_answer text, p_category text)
returns text
language plpgsql immutable
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_base text := public.kb_admin__watch_fee_lock_015(p_target_question, p_target_answer, p_new_question, p_new_answer, p_category);
    v_hit  text;
begin
    -- Only 015's whole-text salary verdict is re-decided; every other verdict (and null) stands.
    if v_base is distinct from 'the change touches a helper salary figure (an agency pricing rule)' then
        return v_base;
    end if;
    select s into v_hit
      from public.kb_admin__watch_changed_sentences(p_target_question, p_target_answer, p_new_question, p_new_answer) s
     where public.kb_admin__watch_money(null, null, null, s)
       and (s ~* '\msalar(y|ies)\M'
            or regexp_replace(s, '\m(mom|ministry of manpower|government|work permit|application|issuance|levy)\s+(processing\s+)?fees?\M', ' ', 'gi') ~* '\mfees?\M')
     limit 1;
    if v_hit is not null then
        return case when v_hit ~* '\msalar(y|ies)\M'
                    then 'the change touches a helper salary figure (an agency pricing rule)'
                    else 'the change states a fee figure (an agency pricing rule)' end;
    end if;
    -- 015 stopped at the salary check; the rule after it still applies.
    if coalesce(p_category, '') <> 'government_rule' then
        return 'the change touches a money figure and the page is not a government rule page';
    end if;
    return null;
end;
$$;

grant create on schema public to kb_admin_fn_owner;
alter function public.kb_admin__watch_changed_sentences(text, text, text, text) owner to kb_admin_fn_owner;
alter function public.kb_admin__watch_fee_lock(text, text, text, text, text) owner to kb_admin_fn_owner;
revoke create on schema public from kb_admin_fn_owner;
revoke all on function public.kb_admin__watch_changed_sentences(text, text, text, text) from public, anon, authenticated, service_role;
revoke all on function public.kb_admin__watch_fee_lock(text, text, text, text, text) from public, anon, authenticated, service_role;
revoke all on function public.kb_admin__watch_fee_lock_015(text, text, text, text, text) from public, anon, authenticated, service_role;

do $$
declare
    salary constant text := 'the change touches a helper salary figure (an agency pricing rule)';
begin
    -- Unchanged salary sentence + a new rest-day line: approvable now.
    if public.kb_admin__watch_fee_lock('Rest days?', 'Her salary is S$650. One rest day a week.', 'Rest days?',
            'Her salary is S$650. One rest day a week. From 2027 it goes in the contract.', 'government_rule') is not null then
        raise exception 'FAIL: an unchanged salary sentence still locks the change';
    end if;
    -- A changed sentence with a salary figure, or a fee figure, still locks; so does removing one.
    if public.kb_admin__watch_fee_lock('Pay?', 'Her salary is S$650.', 'Pay?', 'Her salary is S$700.', 'government_rule') is distinct from salary
       or public.kb_admin__watch_fee_lock(null, null, 'Salary?', 'The minimum salary is S$700.', 'government_rule') is distinct from salary
       or public.kb_admin__watch_fee_lock('Pay?', 'Her salary is S$650. Rest days apply.', 'Pay?', 'Rest days apply.', 'government_rule') is distinct from salary
       or public.kb_admin__watch_fee_lock('Cost?', 'Salary is set by contract.', 'Cost?', 'Salary is set by contract. The medical fee is $90.', 'government_rule') is null then
        raise exception 'FAIL: a changed salary or fee sentence no longer locks';
    end if;
    -- Unchanged: agency fees, fees-document rows, non-government money, government figures.
    if public.kb_admin__watch_fee_lock(null, null, 'Cost?', 'The agency fee is S$1,428.', 'government_rule') is null
       or public.kb_admin__watch_fee_lock('How much does a replacement cost?', 'x', 'y', 'z', 'government_rule') is null
       or public.kb_admin__watch_fee_lock('Pay?', 'Her salary is S$650.', 'Pay?', 'Her salary is S$650. Rest one day.', 'process') is null
       or public.kb_admin__watch_fee_lock(null, null, 'MOM fee?', 'The MOM processing fee is $35.', 'government_rule') is not null
       or public.kb_admin__watch_fee_lock(null, null, 'Levy?', 'The monthly levy is $300.', 'government_rule') is not null then
        raise exception 'FAIL: 018 changed a verdict it must not change';
    end if;
    if has_function_privilege('kb_admin_editor', 'public.kb_admin__watch_fee_lock(text, text, text, text, text)', 'EXECUTE')
       or has_function_privilege('kb_admin_editor', 'public.kb_admin__watch_fee_lock_015(text, text, text, text, text)', 'EXECUTE') then
        raise exception 'FAIL: kb_admin_editor can call the fee lock directly';
    end if;
    raise notice 'PASS: 018 - the salary/fee lock fires only on a changed sentence; every other verdict unchanged';
end
$$;

commit;
