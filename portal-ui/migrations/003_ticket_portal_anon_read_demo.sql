-- Temporary: allow the anon key to READ ticket data for the no-login demo
-- link. Writes (ticket status/priority/assignment updates) stay restricted
-- to logged-in users via the existing authenticated_update_cb_tickets
-- policy from 002_ticket_portal_rls.sql -- the anon key gets no write path.
--
-- Anyone with the demo URL can view live client tickets and WhatsApp
-- conversations. Drop these policies (see bottom of this file) once the
-- module is integrated into the main portal with real login.
--
-- Run this in the Supabase dashboard for the MING HWEE project.

create policy "anon_read_cb_tickets"
  on public.cb_tickets for select
  to anon
  using (true);

create policy "anon_read_wp_chat_conversations"
  on public.wp_chat_conversations for select
  to anon
  using (true);

create policy "anon_read_wp_chat_messages"
  on public.wp_chat_messages for select
  to anon
  using (true);

create policy "anon_read_cb_handovers"
  on public.cb_handovers for select
  to anon
  using (true);

-- To revoke later:
-- drop policy "anon_read_cb_tickets" on public.cb_tickets;
-- drop policy "anon_read_wp_chat_conversations" on public.wp_chat_conversations;
-- drop policy "anon_read_wp_chat_messages" on public.wp_chat_messages;
-- drop policy "anon_read_cb_handovers" on public.cb_handovers;
