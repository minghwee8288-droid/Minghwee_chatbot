-- Rollback of kb_rules_003_salary_floors.sql: removes the ID and MM floors it
-- added (only rows it wrote, by updated_by). Never run without an explicit
-- go-word. Also set kb_rules.DEFAULTS salary_floor back to {"PH": 650}.
begin;
delete from public.cb_kb_rules
where rule_type = 'salary_floor' and service_type = 'all'
  and nationality in ('ID', 'MM') and updated_by = 'fees_doc_2026-10-15';
commit;
