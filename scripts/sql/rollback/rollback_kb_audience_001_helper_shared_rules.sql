-- Rollback of kb_audience_001: the eight rows back to contact_type 'employer'.
-- Never run without an explicit go-word.

begin;

update cb_knowledge_base_updated set contact_type = 'employer'
 where id = any(array[
    'b1cd3903-99aa-453d-ae01-9e304802262f',
    '605a179f-59aa-4b19-9df7-54dba8af3fa6',
    '9a49c956-5d75-4b16-83e3-9dbf1f643ea0',
    '14195b15-8130-4d84-8f18-dbc0b0666b1e',
    '064d3eb9-8954-44a6-b180-11430bf7c244',
    '538905ee-5500-4899-84a4-95fdfe702038',
    'f65f728e-1902-4a0f-9095-85cda4857fe1',
    'e4006c4e-513f-4f03-8911-7a809c3cc14f'
 ]::uuid[])
   and contact_type = 'all';

commit;
