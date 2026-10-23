-- kb_audience_001: eight MOM rule entries a helper may ask about, opened to
-- both audiences (contact_type 'employer' -> 'all').                2026-10-23
--
-- WHY: a helper's search reads contact_type 'candidate' + 'all' only
-- (rag.search), so these employer-tagged rows - her rest days, her room, her
-- food, when her salary is paid, the insurance the employer must buy, the levy
-- that is never taken from her salary, the 6-monthly medical - were invisible
-- to the person they protect. Each states a rule that is the same from either
-- side of the desk; none carries an agency fee or an employer-only price.
--
-- Rows deliberately LEFT as employer: agency fees and packages, the security
-- bond, EOP, hiring cost and budget rows, contract-clause chunks, and the
-- "Related guides" chunks (they mix employer advice into the rule).
--
-- NOT APPLIED. Apply only on the go-word:
--   python scripts/apply_sql.py --expect-ref <project ref> scripts/sql/kb_audience_001_helper_shared_rules.sql
-- Rollback: scripts/sql/rollback/rollback_kb_audience_001_helper_shared_rules.sql
--
-- Side effects, by design: the updated_at trigger stamps these 8 rows, and the
-- external-write trigger (kb_admin_006) records each update as external_* in
-- cb_kb_audit. Four of the rows are kb-admin-managed (605a179f, 538905ee,
-- f65f728e, e4006c4e). No text, embedding or other column changes, and the
-- loader never sets contact_type on these rows. Re-running it changes nothing.

begin;

do $$
declare
  ids uuid[] := array[
    'b1cd3903-99aa-453d-ae01-9e304802262f',  -- How many rest days does my maid get?
    '605a179f-59aa-4b19-9df7-54dba8af3fa6',  -- Can my maid work on her rest day? How much do I pay?
    '9a49c956-5d75-4b16-83e3-9dbf1f643ea0',  -- Do I need to provide a separate room for my maid?
    '14195b15-8130-4d84-8f18-dbc0b0666b1e',  -- Do I need to feed my maid or give a food allowance?
    '064d3eb9-8954-44a6-b180-11430bf7c244',  -- How do I pay my maid's salary?
    '538905ee-5500-4899-84a4-95fdfe702038',  -- What insurance am I legally required to buy?
    'f65f728e-1902-4a0f-9095-85cda4857fe1',  -- What is the FDW levy?
    'e4006c4e-513f-4f03-8911-7a809c3cc14f'   -- What is the six-monthly medical examination (6ME)?
  ]::uuid[];
  found int;
  changed int;
begin
  select count(*) into found from cb_knowledge_base_updated
   where id = any(ids) and is_active and contact_type in ('employer', 'all');
  if found <> 8 then
    raise exception 'kb_audience_001: expected 8 active rows tagged employer or all, found %', found;
  end if;

  update cb_knowledge_base_updated set contact_type = 'all'
   where id = any(ids) and contact_type = 'employer';
  get diagnostics changed = row_count;

  select count(*) into found from cb_knowledge_base_updated
   where id = any(ids) and contact_type = 'all';
  if found <> 8 then
    raise exception 'kb_audience_001: after the update % of 8 rows are tagged all', found;
  end if;
  raise notice 'kb_audience_001: % row(s) opened to helpers, 8 of 8 now tagged all - PASS', changed;
end $$;

commit;
