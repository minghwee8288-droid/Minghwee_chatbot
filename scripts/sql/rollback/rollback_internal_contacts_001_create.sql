-- Rollback of internal_contacts_001. Refuses while the table holds any row, so
-- nobody's staff list is dropped by accident. Never run without a go-word.

begin;

do $$
begin
  if to_regclass('public.cb_internal_contacts') is not null
     and exists (select 1 from public.cb_internal_contacts) then
    raise exception 'rollback_internal_contacts_001: the table holds rows - remove them first';
  end if;
end $$;

drop table if exists public.cb_internal_contacts;

commit;
