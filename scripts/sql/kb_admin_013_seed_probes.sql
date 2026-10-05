-- kb_admin_013_seed_probes: the questions the impact check runs.
--
-- Seeds cb_kb_probe_questions (009). Idempotent: ON CONFLICT (question_text)
-- DO NOTHING, so a re-run adds only questions that are new, and never
-- changes or re-activates one an editor has since edited or switched off.
-- The bot never reads this table.
--
-- Sources:
--   * scripts/check_retrieval.py DEFAULT_QUERIES (the ten calibration questions);
--   * the agency's six worked fee examples of 2026-09-22 (CLAUDE.md change
--     log; EX5 and EX6 are their exact wording, the other four are written
--     from the description there). The expected figure is in notes - it is
--     what a reviewer looks for in the result, not something checked here;
--   * questions each source document answers (contract, MOM guide, helper
--     rights, behaviour code, expectation checklist, FAQs), so replacing any
--     one of them shows what moved. Filters are the routing the bot would use
--     for that asker; NULL = no filter, as in cb_match_knowledge_base_updated.
--
-- Run with:  python scripts/apply_sql.py --expect-ref <project ref> <this file>
-- Rollback:  dropped with the table by rollback_kb_admin_009 (it is only a seed).

begin;

insert into public.cb_kb_probe_questions (question_text, service, audience, nationality, notes) values
    -- check_retrieval.DEFAULT_QUERIES
    ('how much do you charge for a filipino helper',            null,               'employer',  'PH',  'check_retrieval default'),
    ('how long does the whole hiring process take',             'new_hiring',       'employer',  null,  'check_retrieval default; expects about 4 to 6 weeks'),
    ('what documents do i need to hire a maid',                 'new_hiring',       'employer',  null,  'check_retrieval default'),
    ('can i transfer a helper from another employer',           'transfer',         'employer',  null,  'check_retrieval default'),
    ('what is the salary for an indonesian helper',             'salary_enquiry',   'employer',  'ID',  'check_retrieval default'),
    ('my helper''s work permit is expiring, how do i renew',    'renewal',          'employer',  null,  'check_retrieval default'),
    ('she wants to go home for leave, what do i need to do',    'home_leave',       'employer',  null,  'check_retrieval default'),
    ('what happens if the helper doesn''t suit us',             'replacement',      'employer',  null,  'check_retrieval default'),
    ('do i need to buy insurance for the helper',               null,               'employer',  null,  'check_retrieval default'),
    ('how much is the levy for a domestic helper',              null,               'employer',  null,  'check_retrieval default'),

    -- The agency's six fee examples (2026-09-22)
    ('How much does an Indonesian transfer helper cost?',       'transfer',         'employer',  'ID',  'agency fee example 1: expects $1,588'),
    ('How much is passport renewal for a Filipino helper?',     'passport_renewal', 'employer',  'PH',  'agency fee example 2: expects $450'),
    ('What are the third-party fees for a Myanmar new hire?',   'new_hiring',       'employer',  'MM',  'agency fee example 3: expects $328, subject to change'),
    ('How much is work permit renewal?',                        'renewal',          'employer',  null,  'agency fee example 4: expects $695'),
    ('How much does Philippines transfer helper cost?',         'transfer',         'employer',  'PH',  'agency fee example 5 (their wording): expects $1,688'),
    ('What are the fees for Indonesian new hire?',              'new_hiring',       'employer',  'ID',  'agency fee example 6 (their wording): expects $1,188'),

    -- Employer: contract, MOM rules, process
    ('what is the replacement guarantee period',                'replacement',      'employer',  null,  'Client Service Agreement'),
    ('can i get a refund if the helper leaves early',           'replacement',      'employer',  null,  'Client Service Agreement'),
    ('what is the minimum income to hire a helper',             'new_hiring',       'employer',  null,  'MOM guide; expects S$2,500'),
    ('what is the levy for a PR household',                     null,               'employer',  null,  'expects $300, no $60 concession for a PR'),
    ('how much is the security bond',                           'new_hiring',       'employer',  null,  'expects $5,000'),
    ('what medical insurance does my helper need',              null,               'employer',  null,  'three minimums in the KB (CLAUDE.md 9.14) - watch which one wins'),
    ('what is the Settling-In Programme',                       'new_hiring',       'employer',  null,  'MOM guide'),
    ('do first-time employers need to attend a course',         'new_hiring',       'employer',  null,  'MOM guide'),
    ('how long does a transfer take',                           'transfer',         'employer',  null,  'expects 1 to 2 weeks from the interview'),
    ('what does a direct hire cost',                            'direct_hiring',    'employer',  null,  'no fee held: expects a deferral, not a figure'),
    ('what if my helper runs away',                             null,               'employer',  null,  'missing-helper steps: police and MOM within 24 hours'),
    ('what should i do if my helper is pregnant',               null,               'employer',  null,  'MOM report steps'),
    ('how long does passport renewal take for an Indonesian helper', 'passport_renewal', 'employer', 'ID', 'expects 2 weeks'),
    ('what documents are needed for home leave for a Filipino helper', 'home_leave', 'employer', 'PH', 'original passport + ticket itinerary'),
    ('how much is home leave for an Indonesian helper',         'home_leave',       'employer',  'ID',  'expects $250'),

    -- Helper (candidate): rights, behaviour code, checklist
    ('can my employer keep my passport',                        null,               'candidate', null,  'helper rights'),
    ('my employer has not paid my salary, who do i call',       null,               'candidate', null,  'helper rights; expects the agency WhatsApp number'),
    ('someone in the house is touching me, what do i do',       null,               'candidate', null,  'emergency row: police first'),
    ('how many rest days must i get',                           null,               'candidate', null,  'helper rights'),
    ('do i have to pay any fee to Ming Hwee',                   null,               'candidate', null,  'expects: no fee to Ming Hwee'),
    ('what happens after i register as a helper',               null,               'candidate', null,  'her journey rows'),
    ('what documents do i need to apply for a job',             null,               'candidate', null,  'her own documents, never an employer NRIC'),
    ('can i use my phone during work',                          null,               'candidate', null,  'Behaviour Code'),
    ('what should i bring when i come to Singapore',            null,               'candidate', null,  'Helper Expectation Checklist'),

    -- Anyone
    ('where is your office',                                    'general',          'all',       null,  'expects the Chinatown address'),
    ('what are your opening hours',                             'general',          'all',       null,  'office hours row')
on conflict (question_text) do nothing;

do $$
declare
    n integer;
begin
    select count(*) into n from public.cb_kb_probe_questions;
    if n < 42 then
        raise exception 'FAIL: expected at least 42 probe questions, found %', n;
    end if;
    -- Every service named is one the live table actually uses (else the
    -- probe's filter narrows to 'general' and tests nothing).
    if exists (select 1 from public.cb_kb_probe_questions p
                where p.service is not null
                  and not exists (select 1 from public.cb_knowledge_base_updated k
                                   where k.is_active and k.service_type = p.service)) then
        raise exception 'FAIL: a probe names a service no active row uses';
    end if;
    raise notice 'PASS: 013 - % probe questions present', n;
end
$$;

commit;
