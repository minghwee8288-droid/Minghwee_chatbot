-- Rollback of cb_complaint_cases_001. Never run without an explicit go-word.
-- Set COMPLAINT_CASE_TABLE=false first. Nothing is lost that the bot needs: the same
-- fields are on each ticket's captured_info. The transcript copy goes with the table;
-- the messages themselves stay on wp_chat_messages.

begin;
drop table if exists public.cb_complaint_cases;
commit;
