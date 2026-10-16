-- Rollback of kb_admin_018_fee_lock_sentences.sql: put 015's fee lock back under its own name.
-- Writes no data. NEVER run without an explicit go-word from the user.
begin;
do $$
begin
    if to_regprocedure('public.kb_admin__watch_fee_lock_015(text,text,text,text,text)') is null then
        raise notice 'nothing to roll back: 018 is not installed';
        return;
    end if;
    drop function public.kb_admin__watch_fee_lock(text, text, text, text, text);
    drop function if exists public.kb_admin__watch_changed_sentences(text, text, text, text);
    grant create on schema public to kb_admin_fn_owner;
    alter function public.kb_admin__watch_fee_lock_015(text, text, text, text, text) rename to kb_admin__watch_fee_lock;
    revoke create on schema public from kb_admin_fn_owner;
    raise notice 'ROLLED BACK 018: the 015 fee lock is back';
end
$$;
commit;
