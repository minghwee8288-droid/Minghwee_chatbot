-- Enable RLS on the tables the ticket-system portal reads/writes.
-- service_role (used by the chatbot) bypasses RLS regardless of these
-- policies, so this only restricts the anon key used by the new portal.
--
-- Run this in the Supabase dashboard for the MING HWEE project:
-- https://supabase.com/dashboard/project/<your-project-ref>/sql/new

alter table public.cb_tickets enable row level security;
alter table public.wp_chat_conversations enable row level security;
alter table public.wp_chat_messages enable row level security;
alter table public.cb_handovers enable row level security;

-- Read access for any logged-in user.
create policy "authenticated_read_cb_tickets"
  on public.cb_tickets for select
  to authenticated
  using (true);

create policy "authenticated_read_wp_chat_conversations"
  on public.wp_chat_conversations for select
  to authenticated
  using (true);

create policy "authenticated_read_wp_chat_messages"
  on public.wp_chat_messages for select
  to authenticated
  using (true);

create policy "authenticated_read_cb_handovers"
  on public.cb_handovers for select
  to authenticated
  using (true);

-- The portal's only write path: status/priority/assignment updates on tickets.
create policy "authenticated_update_cb_tickets"
  on public.cb_tickets for update
  to authenticated
  using (true)
  with check (true);
